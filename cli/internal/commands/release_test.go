package commands

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"wizard/internal/compat"
)

func TestReleaseUpdateAvailable(t *testing.T) {
	for _, test := range []struct {
		current string
		latest  string
		want    bool
	}{
		{"v1.2.3", "v1.2.4", true},
		{"v1.2.3", "v1.2.3", false},
		{"v2.0.0", "v1.9.9", false},
		{"dev", "v1.2.3", true},
	} {
		got, err := releaseUpdateAvailable(test.current, test.latest)
		if err != nil || got != test.want {
			t.Fatalf("releaseUpdateAvailable(%q, %q) = (%v, %v), want (%v, nil)", test.current, test.latest, got, err, test.want)
		}
	}
	if _, err := releaseUpdateAvailable("v1.2", "v1.2.3"); err == nil {
		t.Fatal("malformed installed version was accepted")
	}
}

func TestChecksumForAssetRejectsAmbiguousAndMalformedEntries(t *testing.T) {
	asset := "Wizard-v1.2.3-linux-amd64.zip"
	digest := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	if _, err := checksumForAsset([]byte(digest+"  "+asset+"\n"+digest+"  "+asset+"\n"), asset); err == nil {
		t.Fatal("duplicate checksum entries were accepted")
	}
	if _, err := checksumForAsset([]byte("not-a-hash  "+asset+"\n"), asset); err == nil {
		t.Fatal("malformed checksum entry was accepted")
	}
}

func TestRunUpdateCheckReportsAvailableRelease(t *testing.T) {
	originalURL, originalClient := releaseAPIURL, releaseHTTPClient
	originalVersion := compat.BuildVersion
	defer func() {
		releaseAPIURL, releaseHTTPClient = originalURL, originalClient
		compat.BuildVersion = originalVersion
	}()
	compat.BuildVersion = "v1.0.0"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/latest" {
			t.Fatalf("unexpected request path %s", r.URL.Path)
		}
		_, _ = fmt.Fprint(w, `{"tag_name":"v1.1.0","assets":[]}`)
	}))
	defer server.Close()
	releaseAPIURL, releaseHTTPClient = server.URL+"/latest", server.Client()
	out, errOut := &bytes.Buffer{}, &bytes.Buffer{}
	env := &Env{Out: out, Err: errOut}
	if code := RunUpdate(env, []string{"--check"}); code != 0 {
		t.Fatalf("RunUpdate(--check) = %d, stderr=%s", code, errOut.String())
	}
	if got := out.String(); !bytes.Contains([]byte(got), []byte("Latest Wizard release: v1.1.0")) || !bytes.Contains([]byte(got), []byte("A new version is available")) {
		t.Fatalf("unexpected check output: %s", got)
	}
}

func TestStageReleaseArchiveVerifiesChecksumAndPackage(t *testing.T) {
	archiveName := releaseArchiveName("v1.2.3")
	packageName := archiveName[:len(archiveName)-len(".zip")]
	archive := releaseZip(t, packageName)
	digest := sha256.Sum256(archive)
	checksums := []byte(hex.EncodeToString(digest[:]) + "  " + archiveName + "\n")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/archive":
			_, _ = w.Write(archive)
		case "/checksums":
			_, _ = w.Write(checksums)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	originalURL, originalClient := releaseAPIURL, releaseHTTPClient
	defer func() { releaseAPIURL, releaseHTTPClient = originalURL, originalClient }()
	releaseAPIURL, releaseHTTPClient = server.URL+"/latest", server.Client()
	release := latestRelease{TagName: "v1.2.3", Assets: []releaseAsset{
		{Name: archiveName, URL: server.URL + "/archive", Size: int64(len(archive))},
		{Name: checksumAssetName, URL: server.URL + "/checksums", Size: int64(len(checksums))},
	}}
	stage, packageDir, err := stageReleaseArchive(context.Background(), t.TempDir(), release)
	if err != nil {
		t.Fatalf("stageReleaseArchive: %v", err)
	}
	defer os.RemoveAll(stage)
	if filepath.Base(packageDir) != packageName {
		t.Fatalf("package directory = %q, want archive root %q", packageDir, packageName)
	}
	if _, err := os.Stat(filepath.Join(packageDir, "cli", "wizard"+executableSuffix())); err != nil {
		t.Fatalf("verified binary missing: %v", err)
	}
}

func releaseZip(t *testing.T, root string) []byte {
	t.Helper()
	var data bytes.Buffer
	writer := zip.NewWriter(&data)
	for name, contents := range map[string]string{
		root + "/backend/main.py":                 "print('wizard')",
		root + "/frontend/package.json":           "{}",
		root + "/cli/wizard" + executableSuffix(): "binary",
	} {
		header := &zip.FileHeader{Name: name, Method: zip.Deflate}
		if name == root+"/cli/wizard"+executableSuffix() {
			header.SetMode(0o755)
		}
		file, err := writer.CreateHeader(header)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := file.Write([]byte(contents)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return data.Bytes()
}
