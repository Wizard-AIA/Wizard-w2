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
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

const (
	defaultReleaseAPIURL = "https://api.github.com/repos/Wizard-AIA/Wizard-w2/releases/latest"
	checksumAssetName    = "SHA256SUMS"
	maxReleaseMetadata   = 1 << 20
	maxChecksumFile      = 4 << 20
	maxReleaseArchive    = int64(2 << 30)
	maxArchiveFiles      = 20000
	maxArchiveExtracted  = int64(4 << 30)
)

var (
	releaseAPIURL     = defaultReleaseAPIURL
	releaseHTTPClient = &http.Client{Timeout: 20 * time.Second}
)

type releaseAsset struct {
	Name string `json:"name"`
	URL  string `json:"browser_download_url"`
	Size int64  `json:"size"`
}

type latestRelease struct {
	TagName string         `json:"tag_name"`
	HTMLURL string         `json:"html_url"`
	Assets  []releaseAsset `json:"assets"`
}

func fetchLatestRelease(ctx context.Context) (latestRelease, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, releaseAPIURL, nil)
	if err != nil {
		return latestRelease{}, fmt.Errorf("creating release request: %w", err)
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "wizard-cli-update")
	resp, err := releaseHTTPClient.Do(req)
	if err != nil {
		return latestRelease{}, fmt.Errorf("checking for the latest Wizard release: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return latestRelease{}, fmt.Errorf("release service returned %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	var release latestRelease
	decoder := json.NewDecoder(io.LimitReader(resp.Body, maxReleaseMetadata+1))
	if err := decoder.Decode(&release); err != nil {
		return latestRelease{}, fmt.Errorf("reading release metadata: %w", err)
	}
	if _, err := parseReleaseVersion(release.TagName); err != nil {
		return latestRelease{}, fmt.Errorf("release service returned an invalid tag %q: %w", release.TagName, err)
	}
	return release, nil
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
	return fmt.Sprintf("Wizard-%s-%s-%s.zip", tag, runtime.GOOS, runtime.GOARCH)
}

// versionParts intentionally accepts only stable numeric release tags. A
// preview should be installed deliberately rather than silently selected by
// "latest", and a malformed tag must never be treated as newer.
func parseReleaseVersion(raw string) ([]int, error) {
	v := strings.TrimPrefix(strings.TrimSpace(raw), "v")
	if v == "" || strings.ContainsAny(v, "+-") {
		return nil, fmt.Errorf("expected a stable vMAJOR.MINOR.PATCH tag")
	}
	parts := strings.Split(v, ".")
	if len(parts) != 3 {
		return nil, fmt.Errorf("expected three numeric components")
	}
	out := make([]int, len(parts))
	for i, part := range parts {
		if part == "" || (len(part) > 1 && part[0] == '0') {
			return nil, fmt.Errorf("invalid component %q", part)
		}
		n, err := strconv.Atoi(part)
		if err != nil || n < 0 {
			return nil, fmt.Errorf("invalid component %q", part)
		}
		out[i] = n
	}
	return out, nil
}

// releaseUpdateAvailable reports whether the published tag is newer than the
// current binary. Development builds are deliberately considered updateable:
// they have no immutable release identity to compare.
func releaseUpdateAvailable(current, latest string) (bool, error) {
	if strings.TrimSpace(current) == "dev" {
		return true, nil
	}
	currentParts, err := parseReleaseVersion(current)
	if err != nil {
		return false, fmt.Errorf("current CLI build version %q is not a stable release: %w", current, err)
	}
	latestParts, err := parseReleaseVersion(latest)
	if err != nil {
		return false, err
	}
	for i := range currentParts {
		if currentParts[i] != latestParts[i] {
			return currentParts[i] < latestParts[i], nil
		}
	}
	return false, nil
}

func releaseCheck(ctx context.Context) (latestRelease, bool, error) {
	release, err := fetchLatestRelease(ctx)
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
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, asset.URL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "wizard-cli-update")
	resp, err := releaseHTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("downloading %s: %w", asset.Name, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("downloading %s: server returned %s", asset.Name, resp.Status)
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
		name := strings.TrimPrefix(fields[len(fields)-1], "*")
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
