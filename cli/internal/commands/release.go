package commands

// Release metadata and archive staging for `wizard update`. This stays in the
// CLI rather than the shell installers so an installed Wizard can update
// without trusting a shell parser or overwriting its live package.

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"wizard/internal/platform"
	"wizard/internal/relver"
)

const (
	// defaultReleaseAPIURL is GitHub's "latest release", which never returns a
	// pre-release or a draft. Every stable install, and every CLI built before
	// pre-releases existed, follows it, so publishing a pre-release cannot
	// reach anyone who did not ask for one.
	defaultReleaseAPIURL = "https://api.github.com/repos/Wizard-AIA/Wizard-w2/releases/latest"
	// defaultReleaseListURL lists recent releases, pre-releases included, for
	// the pre-release channel. Newest-created first, but not necessarily the
	// highest version, so the caller picks the maximum itself.
	defaultReleaseListURL = "https://api.github.com/repos/Wizard-AIA/Wizard-w2/releases?per_page=30"
	checksumAssetName     = "SHA256SUMS"
	maxReleaseMetadata    = 1 << 20
	maxReleaseList        = 4 << 20
	maxChecksumFile       = 4 << 20
	maxReleaseArchive     = int64(2 << 30)
	maxArchiveFiles       = 20000
	maxArchiveExtracted   = int64(4 << 30)
)

const (
	// metadataTimeout bounds a release-API request. downloadTimeout bounds a
	// whole archive transfer; it is deliberately generous because it also has
	// to cover slow proxied links, unlike the old client-wide 20s cap that
	// aborted any download slower than a few hundred KB/s.
	metadataTimeout = 20 * time.Second
	downloadTimeout = 30 * time.Minute
)

// errNetwork marks a failure to reach or read from the release service, so
// callers can exit with exitcode.Network instead of a generic failure.
var errNetwork = errors.New("network error")

var (
	releaseAPIURL  = defaultReleaseAPIURL
	releaseListURL = defaultReleaseListURL
	// The default transport honors HTTPS_PROXY/HTTP_PROXY/NO_PROXY and, on
	// Unix, SSL_CERT_FILE, which is what corporate proxies need. No overall
	// Client.Timeout: it would include the response body read.
	releaseHTTPClient = &http.Client{}
)

type releaseAsset struct {
	Name string `json:"name"`
	URL  string `json:"browser_download_url"`
	Size int64  `json:"size"`
}

type latestRelease struct {
	TagName    string         `json:"tag_name"`
	HTMLURL    string         `json:"html_url"`
	Draft      bool           `json:"draft"`
	Prerelease bool           `json:"prerelease"`
	Assets     []releaseAsset `json:"assets"`
}

// getReleaseJSON GETs a GitHub release endpoint and decodes at most limit bytes
// of JSON into out. Failures that mean "the service was unreachable or said no"
// wrap errNetwork so callers exit with the network code.
func getReleaseJSON(ctx context.Context, url string, limit int64, out any) error {
	ctx, cancel := context.WithTimeout(ctx, metadataTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("creating release request: %w", err)
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "wizard-cli-update")
	setGitHubAuth(req)
	resp, err := releaseHTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("checking for the latest Wizard release: %w: %v", errNetwork, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		if resp.Header.Get("X-RateLimit-Remaining") == "0" {
			return fmt.Errorf("GitHub's anonymous rate limit for this network is used up: %w. Try again later, or set GITHUB_TOKEN (any token, no scopes) to raise it", errNetwork)
		}
		return fmt.Errorf("release service returned %s: %w: %s", resp.Status, errNetwork, strings.TrimSpace(string(body)))
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, limit+1)).Decode(out); err != nil {
		return fmt.Errorf("reading release metadata: %w", err)
	}
	return nil
}

// fetchLatestRelease is the stable channel: GitHub's latest release, which must
// carry a full-release tag. A pre-release here would mean the service is
// misconfigured, and installing it silently is the one thing this must not do.
func fetchLatestRelease(ctx context.Context) (latestRelease, error) {
	var release latestRelease
	if err := getReleaseJSON(ctx, releaseAPIURL, maxReleaseMetadata, &release); err != nil {
		return latestRelease{}, err
	}
	v, err := relver.Parse(release.TagName)
	if err != nil {
		return latestRelease{}, fmt.Errorf("release service returned an invalid tag %q: %w", release.TagName, err)
	}
	if !v.Stable() {
		return latestRelease{}, fmt.Errorf("release service returned the pre-release %q as the latest release; refusing it on the stable channel", release.TagName)
	}
	return release, nil
}

// fetchNewestRelease is the pre-release channel: the stable release, unless a
// published pre-release is newer than it.
//
// It deliberately does not take the highest version among everything listed.
// This repository still carries releases from an earlier v2.x line, published
// and unflagged, that outrank v1.0.x numerically; "highest wins" would send
// every pre-release user to v2.2.1. So the stable release comes from GitHub's
// own "latest" answer, and from the list only releases GitHub itself flags as
// pre-releases, carrying a tag in the release grammar, count. Drafts, odd tags
// and unflagged tags are ignored, so none of them can break updates.
func fetchNewestRelease(ctx context.Context) (latestRelease, error) {
	best, err := fetchLatestRelease(ctx)
	if err != nil {
		return latestRelease{}, err
	}
	bestVersion, err := relver.Parse(best.TagName)
	if err != nil {
		return latestRelease{}, err // unreachable: fetchLatestRelease validated it
	}
	var releases []latestRelease
	if err := getReleaseJSON(ctx, releaseListURL, maxReleaseList, &releases); err != nil {
		return latestRelease{}, err
	}
	for _, release := range releases {
		if release.Draft || !release.Prerelease {
			continue
		}
		v, err := relver.Parse(release.TagName)
		if err != nil || v.Stable() {
			continue
		}
		if relver.Compare(v, bestVersion) > 0 {
			best, bestVersion = release, v
		}
	}
	return best, nil
}

// fetchRelease returns the release a channel points at.
func fetchRelease(ctx context.Context, ch Channel) (latestRelease, error) {
	if ch == ChannelPreRelease {
		return fetchNewestRelease(ctx)
	}
	return fetchLatestRelease(ctx)
}

// setGitHubAuth attaches GITHUB_TOKEN / GH_TOKEN to a request for GitHub's API,
// lifting the 60-requests-an-hour anonymous limit that shared office and CI
// addresses hit. The token goes to api.github.com and nowhere else: not to a
// mirror set through releaseAPIURL, and never to the archive downloads.
func setGitHubAuth(req *http.Request) {
	if req.URL.Scheme != "https" || req.URL.Hostname() != "api.github.com" {
		return
	}
	token := os.Getenv("GITHUB_TOKEN")
	if token == "" {
		token = os.Getenv("GH_TOKEN")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
}

func (r latestRelease) asset(name string) (releaseAsset, bool) {
	for _, asset := range r.Assets {
		if asset.Name == name && asset.URL != "" {
			return asset, true
		}
	}
	return releaseAsset{}, false
}

func releaseArchiveName(tag string) string {
	return platform.ArtifactName(tag, platform.Current())
}

// releaseUpdateAvailable reports whether the published tag is newer than the
// current binary, by SemVer precedence (a pre-release is older than its own
// stable release). A malformed tag on either side is an error, never "newer".
// Development builds are deliberately considered updateable: they have no
// immutable release identity to compare.
func releaseUpdateAvailable(current, latest string) (bool, error) {
	if strings.TrimSpace(current) == "dev" {
		return true, nil
	}
	installed, err := relver.Parse(current)
	if err != nil {
		return false, fmt.Errorf("current CLI build version %q is not a release version: %w", current, err)
	}
	published, err := relver.Parse(latest)
	if err != nil {
		return false, err
	}
	return relver.Compare(installed, published) < 0, nil
}

func releaseCheck(ctx context.Context, ch Channel) (latestRelease, bool, error) {
	release, err := fetchRelease(ctx, ch)
	if err != nil {
		return latestRelease{}, false, err
	}
	available, err := releaseUpdateAvailable(currentBuildVersion(), release.TagName)
	if err != nil {
		return latestRelease{}, false, err
	}
	return release, available, nil
}

func validReleaseURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return fmt.Errorf("invalid download URL %q", raw)
	}
	// The endpoint is a package variable only tests replace. Production always
	// uses GitHub HTTPS; permitting httptest's local HTTP endpoint keeps the
	// verification path fully testable without opening a policy escape hatch.
	if u.Scheme != "https" && !(strings.HasPrefix(releaseAPIURL, "http://") && u.Scheme == "http") {
		return fmt.Errorf("refusing non-HTTPS download URL %q", raw)
	}
	return nil
}

func downloadReleaseAsset(ctx context.Context, asset releaseAsset, destination string, limit int64) (err error) {
	if err := validReleaseURL(asset.URL); err != nil {
		return err
	}
	if asset.Size < 0 || asset.Size > limit {
		return fmt.Errorf("release asset %s has unsafe declared size %d", asset.Name, asset.Size)
	}
	ctx, cancel := context.WithTimeout(ctx, downloadTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, asset.URL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "wizard-cli-update")
	resp, err := releaseHTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("downloading %s: %w: %v", asset.Name, errNetwork, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("downloading %s: %w: server returned %s", asset.Name, errNetwork, resp.Status)
	}
	if resp.ContentLength > limit {
		return fmt.Errorf("release asset %s exceeds the %d byte limit", asset.Name, limit)
	}
	out, err := os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer func() {
		if closeErr := out.Close(); err == nil && closeErr != nil {
			err = fmt.Errorf("closing downloaded %s: %w", asset.Name, closeErr)
		}
	}()
	n, err := io.Copy(out, io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return err
	}
	if n > limit {
		return fmt.Errorf("release asset %s exceeds the %d byte limit", asset.Name, limit)
	}
	return out.Sync()
}

func checksumForAsset(contents []byte, assetName string) (string, error) {
	var found string
	for _, line := range strings.Split(string(contents), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		// sha256sum writes "digest  name", or "digest *name" in binary mode, and
		// "digest  ./name" when it was run over ./*.zip -- which is how every
		// release up to v1.0.12 published its SHA256SUMS, so ./ must be accepted.
		name := strings.TrimPrefix(strings.TrimPrefix(fields[len(fields)-1], "*"), "./")
		if name != assetName {
			continue
		}
		digest := strings.ToLower(fields[0])
		if len(digest) != sha256.Size*2 {
			return "", fmt.Errorf("checksum for %s is not SHA-256", assetName)
		}
		if _, err := hex.DecodeString(digest); err != nil {
			return "", fmt.Errorf("checksum for %s is malformed", assetName)
		}
		if found != "" {
			return "", fmt.Errorf("checksum list has duplicate entries for %s", assetName)
		}
		found = digest
	}
	if found == "" {
		return "", fmt.Errorf("checksum list does not contain %s", assetName)
	}
	return found, nil
}

// stageReleaseArchive downloads, checks, and safely extracts a release into a
// private sibling directory. It never alters current until its caller has
// also prepared dependencies in the staged package.
func stageReleaseArchive(ctx context.Context, installRoot string, release latestRelease) (stageDir, packageDir string, err error) {
	archiveName := releaseArchiveName(release.TagName)
	archive, ok := release.asset(archiveName)
	if !ok {
		return "", "", fmt.Errorf("release %s has no %s artifact", release.TagName, archiveName)
	}
	checksums, ok := release.asset(checksumAssetName)
	if !ok {
		return "", "", fmt.Errorf("release %s has no %s integrity file", release.TagName, checksumAssetName)
	}
	stageDir, err = os.MkdirTemp(installRoot, ".wizard-update-")
	if err != nil {
		return "", "", fmt.Errorf("creating update staging directory: %w", err)
	}
	fail := func(cause error) (string, string, error) {
		_ = os.RemoveAll(stageDir)
		return "", "", cause
	}
	checksumPath := filepath.Join(stageDir, checksumAssetName)
	if err := downloadReleaseAsset(ctx, checksums, checksumPath, maxChecksumFile); err != nil {
		return fail(err)
	}
	checksumContents, err := os.ReadFile(checksumPath)
	if err != nil {
		return fail(fmt.Errorf("reading downloaded integrity file: %w", err))
	}
	expected, err := checksumForAsset(checksumContents, archiveName)
	if err != nil {
		return fail(err)
	}
	archivePath := filepath.Join(stageDir, archiveName)
	if err := downloadReleaseAsset(ctx, archive, archivePath, maxReleaseArchive); err != nil {
		return fail(err)
	}
	file, err := os.Open(archivePath)
	if err != nil {
		return fail(err)
	}
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		file.Close()
		return fail(err)
	}
	file.Close()
	if actual := hex.EncodeToString(hash.Sum(nil)); actual != expected {
		return fail(fmt.Errorf("checksum mismatch for %s", archiveName))
	}
	zipReader, err := zip.OpenReader(archivePath)
	if err != nil {
		return fail(fmt.Errorf("opening verified release archive: %w", err))
	}
	defer zipReader.Close()
	packageName := strings.TrimSuffix(archiveName, ".zip")
	if err := extractReleaseArchive(zipReader, stageDir, packageName); err != nil {
		return fail(err)
	}
	packageDir = filepath.Join(stageDir, packageName)
	if err := validateReleasePackage(packageDir); err != nil {
		return fail(err)
	}
	return stageDir, packageDir, nil
}

func extractReleaseArchive(reader *zip.ReadCloser, stageDir, packageName string) error {
	if len(reader.File) == 0 || len(reader.File) > maxArchiveFiles {
		return fmt.Errorf("release archive has an unsafe file count")
	}
	var total int64
	prefix := packageName + "/"
	for _, file := range reader.File {
		if !strings.HasPrefix(file.Name, prefix) || strings.Contains(file.Name, "\\") {
			return fmt.Errorf("release archive contains unsafe path %q", file.Name)
		}
		rel := strings.TrimPrefix(file.Name, prefix)
		if rel == "" {
			continue
		}
		clean := filepath.Clean(filepath.FromSlash(rel))
		if clean == "." || filepath.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
			return fmt.Errorf("release archive contains unsafe path %q", file.Name)
		}
		if file.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("release archive contains a symlink %q", file.Name)
		}
		total += int64(file.UncompressedSize64)
		if total < 0 || total > maxArchiveExtracted {
			return fmt.Errorf("release archive expands beyond the safe limit")
		}
		path := filepath.Join(stageDir, packageName, clean)
		if file.FileInfo().IsDir() {
			if err := os.MkdirAll(path, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return err
		}
		in, err := file.Open()
		if err != nil {
			return err
		}
		out, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, file.Mode().Perm())
		if err != nil {
			in.Close()
			return err
		}
		_, copyErr := io.Copy(out, in)
		closeErr := out.Close()
		in.Close()
		if copyErr != nil {
			return copyErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
	return nil
}

func validateReleasePackage(root string) error {
	for _, path := range []string{
		filepath.Join(root, "backend", "main.py"),
		filepath.Join(root, "frontend", "package.json"),
		filepath.Join(root, "cli", "wizard"+executableSuffix()),
	} {
		info, err := os.Stat(path)
		if err != nil || info.IsDir() {
			return fmt.Errorf("verified archive is missing required file %s", path)
		}
	}
	if runtime.GOOS != "windows" {
		binary := filepath.Join(root, "cli", "wizard")
		info, err := os.Stat(binary)
		if err != nil || info.Mode()&0o111 == 0 {
			return fmt.Errorf("verified archive has a non-executable wizard binary")
		}
	}
	return nil
}

func executableSuffix() string {
	if runtime.GOOS == "windows" {
		return ".exe"
	}
	return ""
}
