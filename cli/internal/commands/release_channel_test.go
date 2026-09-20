package commands

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"wizard/internal/exitcode"
)

func TestReleaseUpdateAvailableWithPreReleases(t *testing.T) {
	for _, test := range []struct {
		current, latest string
		want            bool
	}{
		{"v1.0.13", "v1.0.14-beta.1", true},         // stable -> a newer beta (pre-release channel)
		{"v1.0.14-beta.1", "v1.0.14-beta.2", true},  // beta -> next beta
		{"v1.0.14-beta.2", "v1.0.14-beta.10", true}, // numeric, not lexical
		{"v1.0.14-beta.2", "v1.0.14-beta.1", false},
		{"v1.0.14-beta.9", "v1.0.14-rc.1", true},
		{"v1.0.14-rc.1", "v1.0.14", true}, // the stable release passes its own candidates
		{"v1.0.14", "v1.0.14-rc.5", false},
		{"v1.0.14-beta.1", "v1.0.13", false}, // never downgrade
		{"v1.0.14-beta.1", "v1.0.14-beta.1", false},
		{"v1.0.13", "v1.0.14-alpha.1", true},
	} {
		got, err := releaseUpdateAvailable(test.current, test.latest)
		if err != nil || got != test.want {
			t.Errorf("releaseUpdateAvailable(%q, %q) = (%v, %v), want (%v, nil)", test.current, test.latest, got, err, test.want)
		}
	}
	for _, bad := range [][2]string{{"v1.0.13", "v1.0.14-preview.1"}, {"v1.0.13", "nightly"}, {"v1.0.14-beta", "v1.0.14"}, {"", "v1.0.14"}} {
		if _, err := releaseUpdateAvailable(bad[0], bad[1]); err == nil {
			t.Errorf("releaseUpdateAvailable(%q, %q) accepted a malformed version", bad[0], bad[1])
		}
	}
}

// releaseServer serves both endpoints and records which were hit.
func releaseServer(t *testing.T, latestBody, listBody string) (hits *[]string) {
	t.Helper()
	originalLatest, originalList, originalClient := releaseAPIURL, releaseListURL, releaseHTTPClient
	t.Cleanup(func() {
		releaseAPIURL, releaseListURL, releaseHTTPClient = originalLatest, originalList, originalClient
	})
	seen := []string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.URL.Path)
		switch r.URL.Path {
		case "/latest":
			_, _ = fmt.Fprint(w, latestBody)
		case "/list":
			_, _ = fmt.Fprint(w, listBody)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	releaseAPIURL, releaseListURL, releaseHTTPClient = server.URL+"/latest", server.URL+"/list", server.Client()
	return &seen
}

func TestNewestReleaseIsTheHighestFlaggedPreReleaseAheadOfStable(t *testing.T) {
	// The list arrives newest-created first, not highest first. It also holds
	// the repository's real history: an earlier v2.x line that is published and
	// unflagged (numerically "newer" than v1.0.x), a draft, an odd tag, and a beta
	// nobody flagged as a pre-release. None of those may win.
	releaseServer(t, `{"tag_name":"v1.0.13","assets":[]}`, `[
		{"tag_name":"v1.0.13","assets":[]},
		{"tag_name":"v1.0.14-beta.2","prerelease":true,"assets":[]},
		{"tag_name":"v1.0.14-beta.10","prerelease":true,"assets":[]},
		{"tag_name":"v1.0.14-beta.1","prerelease":true,"assets":[]},
		{"tag_name":"v1.0.14-rc.3","assets":[]},
		{"tag_name":"v9.9.9-beta.1","draft":true,"prerelease":true,"assets":[]},
		{"tag_name":"nightly","prerelease":true,"assets":[]},
		{"tag_name":"v2.2.1","assets":[]},
		{"tag_name":"v2.0.0-w2-planning","assets":[]},
		{"tag_name":"v1.0.12","assets":[]}
	]`)
	got, err := fetchNewestRelease(context.Background())
	if err != nil || got.TagName != "v1.0.14-beta.10" {
		t.Fatalf("fetchNewestRelease = %q, %v; want v1.0.14-beta.10", got.TagName, err)
	}
}

func TestLegacyHigherNumberedReleasesNeverHijackThePreReleaseChannel(t *testing.T) {
	// v2.2.1 is published, unflagged and numerically above v1.0.13; with no
	// pre-release out, the channel is simply the stable release.
	releaseServer(t, `{"tag_name":"v1.0.13","assets":[]}`, `[
		{"tag_name":"v1.0.13","assets":[]},
		{"tag_name":"v2.2.1","assets":[]},
		{"tag_name":"v2.2.0","assets":[]}
	]`)
	got, err := fetchRelease(context.Background(), ChannelPreRelease)
	if err != nil || got.TagName != "v1.0.13" {
		t.Fatalf("got %q, %v; want the stable v1.0.13", got.TagName, err)
	}
}

func TestPreReleaseChannelPrefersTheStableReleaseThatPassesTheCandidate(t *testing.T) {
	releaseServer(t, `{"tag_name":"v1.0.14","assets":[]}`, `[
		{"tag_name":"v1.0.14-rc.2","prerelease":true,"assets":[]},
		{"tag_name":"v1.0.14","assets":[]},
		{"tag_name":"v1.0.14-rc.1","prerelease":true,"assets":[]}
	]`)
	got, err := fetchRelease(context.Background(), ChannelPreRelease)
	if err != nil || got.TagName != "v1.0.14" {
		t.Fatalf("got %q, %v; want the stable v1.0.14", got.TagName, err)
	}
}

func TestAStalePreReleaseBehindStableIsIgnored(t *testing.T) {
	releaseServer(t, `{"tag_name":"v1.0.13","assets":[]}`, `[{"tag_name":"v1.0.12-beta.4","prerelease":true,"assets":[]}]`)
	got, err := fetchNewestRelease(context.Background())
	if err != nil || got.TagName != "v1.0.13" {
		t.Fatalf("got %q, %v; want v1.0.13", got.TagName, err)
	}
}

func TestNewestReleaseFailsWhenEitherEndpointFails(t *testing.T) {
	releaseServer(t, `{"tag_name":"v1.0.13","assets":[]}`, `not json`)
	if _, err := fetchNewestRelease(context.Background()); err == nil {
		t.Fatal("malformed list JSON accepted")
	}
	releaseServer(t, `not json`, `[]`)
	if _, err := fetchNewestRelease(context.Background()); err == nil {
		t.Fatal("malformed latest JSON accepted")
	}
	// A pre-release served as "latest" is refused on this channel too, not passed through.
	releaseServer(t, `{"tag_name":"v1.0.14-beta.1","assets":[]}`, `[]`)
	if _, err := fetchNewestRelease(context.Background()); err == nil {
		t.Fatal("a pre-release served as latest was accepted")
	}
}

func TestStableChannelRefusesAPreReleaseFromTheLatestEndpoint(t *testing.T) {
	releaseServer(t, `{"tag_name":"v1.0.14-beta.1","assets":[]}`, `[]`)
	if _, err := fetchLatestRelease(context.Background()); err == nil || !strings.Contains(err.Error(), "refusing it on the stable channel") {
		t.Fatalf("a pre-release served as latest must be refused, got %v", err)
	}
}

func TestStableChannelNeverAsksForTheReleaseList(t *testing.T) {
	withBuild(t, "v1.0.12")
	hits := releaseServer(t, `{"tag_name":"v1.0.13","assets":[]}`, `[{"tag_name":"v1.0.14-beta.1","assets":[]}]`)
	out, errOut := &bytes.Buffer{}, &bytes.Buffer{}
	env := &Env{Out: out, Err: errOut, ConfigDir: t.TempDir()}
	if code := RunUpdate(env, []string{"--check"}); code != exitcode.OK {
		t.Fatalf("RunUpdate(--check) = %d: %s", code, errOut.String())
	}
	if len(*hits) != 1 || (*hits)[0] != "/latest" {
		t.Fatalf("the stable channel must use only /latest, hit %v", *hits)
	}
	if got := out.String(); !strings.Contains(got, "Update channel: stable") || !strings.Contains(got, "Latest Wizard release: v1.0.13") {
		t.Fatalf("output: %s", got)
	}
}

func TestPreReleaseCheckFollowsTheListAndDoesNotSaveTheChannel(t *testing.T) {
	withBuild(t, "v1.0.13")
	hits := releaseServer(t, `{"tag_name":"v1.0.13","assets":[]}`, `[{"tag_name":"v1.0.14-beta.1","prerelease":true,"assets":[]},{"tag_name":"v1.0.13","assets":[]},{"tag_name":"v2.2.1","assets":[]}]`)
	out, errOut := &bytes.Buffer{}, &bytes.Buffer{}
	env := &Env{Out: out, Err: errOut, ConfigDir: t.TempDir()}
	if code := RunUpdate(env, []string{"--check", "--pre-release"}); code != exitcode.OK {
		t.Fatalf("code %d: %s", code, errOut.String())
	}
	if len(*hits) != 2 || (*hits)[0] != "/latest" || (*hits)[1] != "/list" {
		t.Fatalf("the pre-release channel reads the stable release, then the list; hit %v", *hits)
	}
	got := out.String()
	for _, want := range []string{"Update channel: pre-release", "Latest Wizard release: v1.0.14-beta.1", "A new version is available", "--check does not save the channel"} {
		if !strings.Contains(got, want) {
			t.Errorf("output lacks %q:\n%s", want, got)
		}
	}
	if _, err := os.Stat(env.channelPath()); !os.IsNotExist(err) {
		t.Fatalf("--check must not save the channel (stat err=%v)", err)
	}
}

func TestSavedChannelIsUsedByAPlainCheck(t *testing.T) {
	withBuild(t, "v1.0.13")
	hits := releaseServer(t, `{"tag_name":"v1.0.13","assets":[]}`, `[{"tag_name":"v1.0.14-rc.1","prerelease":true,"assets":[]}]`)
	env := &Env{Out: &bytes.Buffer{}, Err: &bytes.Buffer{}, ConfigDir: t.TempDir()}
	if err := saveChannel(env, ChannelPreRelease); err != nil {
		t.Fatal(err)
	}
	if code := RunUpdate(env, []string{"--check"}); code != exitcode.OK {
		t.Fatalf("code %d", code)
	}
	if len(*hits) != 2 || (*hits)[1] != "/list" {
		t.Fatalf("a saved pre-release channel must be followed, hit %v", *hits)
	}
}

func TestExplicitStableOverridesASavedPreReleaseChannelForOneCheck(t *testing.T) {
	withBuild(t, "v1.0.14-beta.2")
	hits := releaseServer(t, `{"tag_name":"v1.0.13","assets":[]}`, `[]`)
	out := &bytes.Buffer{}
	env := &Env{Out: out, Err: &bytes.Buffer{}, ConfigDir: t.TempDir()}
	if err := saveChannel(env, ChannelPreRelease); err != nil {
		t.Fatal(err)
	}
	if code := RunUpdate(env, []string{"--check", "--channel", "stable"}); code != exitcode.OK {
		t.Fatalf("code %d", code)
	}
	if len(*hits) != 1 || (*hits)[0] != "/latest" {
		t.Fatalf("hit %v", *hits)
	}
	// On a beta newer than stable the tool must not say "up to date" or offer a downgrade.
	if got := out.String(); strings.Contains(got, "A new version is available") || !strings.Contains(got, "newer than the latest release on this channel") {
		t.Fatalf("output: %s", got)
	}
}

func TestUpdateRejectsBadChannelFlags(t *testing.T) {
	for _, args := range [][]string{
		{"--check", "--channel", "beta"},
		{"--check", "--pre-release", "--channel", "stable"},
		{"--channel"},
	} {
		errOut := &bytes.Buffer{}
		env := &Env{Out: &bytes.Buffer{}, Err: errOut, ConfigDir: t.TempDir()}
		if code := RunUpdate(env, args); code != exitcode.Usage {
			t.Errorf("RunUpdate(%v) = %d, want %d (stderr %q)", args, code, exitcode.Usage, errOut.String())
		}
	}
	// --pre-release together with --channel pre-release is redundant, not contradictory.
	releaseServer(t, `{"tag_name":"v1.0.13","assets":[]}`, `[{"tag_name":"v1.0.13","assets":[]}]`)
	withBuild(t, "v1.0.13")
	if code := RunUpdate(&Env{Out: &bytes.Buffer{}, Err: &bytes.Buffer{}, ConfigDir: t.TempDir()}, []string{"--check", "--pre-release", "--channel", "pre-release"}); code != exitcode.OK {
		t.Errorf("redundant flags rejected: %d", code)
	}
}
