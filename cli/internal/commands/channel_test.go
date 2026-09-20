package commands

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"wizard/internal/compat"
	"wizard/internal/exitcode"
)

// withBuild pins the running build's version for one test.
func withBuild(t *testing.T, version string) {
	t.Helper()
	original := compat.BuildVersion
	compat.BuildVersion = version
	t.Cleanup(func() { compat.BuildVersion = original })
}

func TestParseChannel(t *testing.T) {
	for in, want := range map[string]Channel{
		"stable": ChannelStable, " Stable\n": ChannelStable,
		"pre-release": ChannelPreRelease, "prerelease": ChannelPreRelease, "PRE-RELEASE": ChannelPreRelease,
	} {
		got, err := ParseChannel(in)
		if err != nil || got != want {
			t.Errorf("ParseChannel(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, in := range []string{"", "beta", "nightly", "latest", "stable,pre-release"} {
		if got, err := ParseChannel(in); err == nil {
			t.Errorf("ParseChannel(%q) = %q, want an error", in, got)
		}
	}
}

func TestResolveChannelPrecedence(t *testing.T) {
	cases := []struct {
		name       string
		build      string
		file       string // "" means no file
		wantChan   Channel
		wantSource string
		wantProbl  bool
	}{
		{"a fresh stable install follows stable", "v1.0.13", "", ChannelStable, sourceDefault, false},
		{"a dev build follows stable", "dev", "", ChannelStable, sourceDefault, false},
		{"the choice is honoured", "v1.0.13", "pre-release\n", ChannelPreRelease, sourceChosen, false},
		{"a pre-release build with no choice stays on pre-release", "v1.0.14-beta.1", "", ChannelPreRelease, sourceImplied, false},
		{"an explicit stable choice beats the implied channel", "v1.0.14-beta.1", "stable\n", ChannelStable, sourceChosen, false},
		{"an unreadable choice is reported and ignored", "v1.0.13", "beta\n", ChannelStable, sourceDefault, true},
		{"an unreadable choice on a pre-release build keeps the implied channel", "v1.0.14-rc.1", "???", ChannelPreRelease, sourceImplied, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			withBuild(t, tc.build)
			env := &Env{ConfigDir: t.TempDir()}
			if tc.file != "" {
				if err := os.WriteFile(env.channelPath(), []byte(tc.file), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			ch, source, problem := resolveChannel(env)
			if ch != tc.wantChan || source != tc.wantSource || (problem != nil) != tc.wantProbl {
				t.Fatalf("resolveChannel = (%q, %q, %v); want (%q, %q, problem=%v)", ch, source, problem, tc.wantChan, tc.wantSource, tc.wantProbl)
			}
		})
	}
}

func TestResolveChannelWithoutAConfigDirDoesNotTouchTheWorkingDirectory(t *testing.T) {
	withBuild(t, "v1.0.13")
	// An Env with no ConfigDir (tests, --help) must not read "update-channel"
	// from whatever directory the process happens to be in.
	dir := t.TempDir()
	t.Chdir(dir)
	if err := os.WriteFile(filepath.Join(dir, channelFileName), []byte("pre-release\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ch, source, problem := resolveChannel(&Env{})
	if ch != ChannelStable || source != sourceDefault || problem != nil {
		t.Fatalf("resolveChannel(&Env{}) = (%q, %q, %v)", ch, source, problem)
	}
}

func TestSaveChannelRoundTripsAndCreatesTheDirectory(t *testing.T) {
	withBuild(t, "v1.0.13")
	env := &Env{ConfigDir: filepath.Join(t.TempDir(), "nested", "wizard")}
	if err := saveChannel(env, ChannelPreRelease); err != nil {
		t.Fatal(err)
	}
	if ch, source, _ := resolveChannel(env); ch != ChannelPreRelease || source != sourceChosen {
		t.Fatalf("after save: (%q, %q)", ch, source)
	}
	if err := saveChannel(&Env{}, ChannelStable); err == nil {
		t.Fatal("saving with no config directory must fail, not write into the working directory")
	}
}

func TestSaveChannelReplacesTheFileWholeAndLeavesNothingBehind(t *testing.T) {
	withBuild(t, "v1.0.13")
	dir := filepath.Join(t.TempDir(), "wizard")
	env := &Env{ConfigDir: dir}
	for _, ch := range []Channel{ChannelPreRelease, ChannelStable, ChannelPreRelease} {
		if err := saveChannel(env, ch); err != nil {
			t.Fatal(err)
		}
		data, err := os.ReadFile(env.channelPath())
		if err != nil || string(data) != string(ch)+"\n" {
			t.Fatalf("file after saving %q = %q, %v", ch, data, err)
		}
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Fatalf("the config directory should hold only the channel file, got %d entries", len(entries))
	}
	info, err := entries[0].Info()
	if err != nil {
		t.Fatal(err)
	}
	if os.PathSeparator == '/' && info.Mode().Perm()&0o077 != 0 {
		t.Fatalf("the channel file must not be readable by others: %v", info.Mode())
	}
}

func TestSaveChannelFailureLeavesNoTempFile(t *testing.T) {
	withBuild(t, "v1.0.13")
	dir := filepath.Join(t.TempDir(), "wizard")
	env := &Env{ConfigDir: dir}
	// A non-empty directory where the file should go: the rename cannot succeed.
	if err := os.MkdirAll(env.channelPath(), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(env.channelPath(), "keep"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := saveChannel(env, ChannelPreRelease); err == nil {
		t.Fatal("expected an error when the channel path is a non-empty directory")
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 {
		names := []string{}
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("a failed save left extra files behind: %v", names)
	}
}

func TestRememberChannelSavesOnceAndSaysSo(t *testing.T) {
	withBuild(t, "v1.0.13")
	out := &bytes.Buffer{}
	env := &Env{ConfigDir: t.TempDir(), Out: out, Err: &bytes.Buffer{}}
	rememberChannel(env, ChannelPreRelease)
	if !strings.Contains(out.String(), "Update channel set to pre-release") {
		t.Fatalf("no confirmation: %q", out.String())
	}
	out.Reset()
	rememberChannel(env, ChannelPreRelease)
	if out.Len() != 0 {
		t.Fatalf("repeating the same choice must be silent, got %q", out.String())
	}
}

func TestUpToDateMessageAdmitsBeingAhead(t *testing.T) {
	if got := upToDateMessage("v1.0.13", "v1.0.13"); got != "Wizard is up to date." {
		t.Errorf("equal versions: %q", got)
	}
	got := upToDateMessage("v1.0.14-beta.2", "v1.0.13")
	if !strings.Contains(got, "newer than the latest release on this channel") || !strings.Contains(got, "v1.0.13") {
		t.Errorf("a pre-release ahead of stable must say so, got %q", got)
	}
}

func TestRunChannelShowsAndSets(t *testing.T) {
	withBuild(t, "v1.0.13")
	t.Setenv("WIZARD_CONFIG_DIR", t.TempDir())

	out, errOut := &bytes.Buffer{}, &bytes.Buffer{}
	if code := RunChannel(out, errOut, nil); code != exitcode.OK || !strings.Contains(out.String(), "Update channel: stable (default)") {
		t.Fatalf("show: code=%d out=%q err=%q", code, out.String(), errOut.String())
	}

	out.Reset()
	if code := RunChannel(out, errOut, []string{"pre-release"}); code != exitcode.OK || !strings.Contains(out.String(), "set to pre-release") {
		t.Fatalf("set: code=%d out=%q err=%q", code, out.String(), errOut.String())
	}

	out.Reset()
	if code := RunChannel(out, errOut, nil); code != exitcode.OK || !strings.Contains(out.String(), "Update channel: pre-release (chosen)") {
		t.Fatalf("show after set: code=%d out=%q", code, out.String())
	}

	out.Reset()
	if code := RunChannel(out, errOut, []string{"stable"}); code != exitcode.OK {
		t.Fatalf("switch back: %d", code)
	}
	if ch, _, _ := resolveChannel(&Env{ConfigDir: os.Getenv("WIZARD_CONFIG_DIR")}); ch != ChannelStable {
		t.Fatalf("switch back did not stick: %q", ch)
	}
}

func TestRunChannelRejectsBadInput(t *testing.T) {
	t.Setenv("WIZARD_CONFIG_DIR", t.TempDir())
	for _, args := range [][]string{{"beta"}, {"stable", "extra"}, {"--bogus"}} {
		out, errOut := &bytes.Buffer{}, &bytes.Buffer{}
		if code := RunChannel(out, errOut, args); code != exitcode.Usage {
			t.Errorf("RunChannel(%v) = %d, want %d (stderr %q)", args, code, exitcode.Usage, errOut.String())
		}
	}
	// A bad request must not have written a channel file.
	if _, err := os.Stat(filepath.Join(os.Getenv("WIZARD_CONFIG_DIR"), channelFileName)); !os.IsNotExist(err) {
		t.Fatalf("a rejected request left a channel file behind: %v", err)
	}
}

func TestRunChannelHelpExitsZero(t *testing.T) {
	out, errOut := &bytes.Buffer{}, &bytes.Buffer{}
	if code := RunChannel(out, errOut, []string{"--help"}); code != exitcode.OK || !strings.Contains(out.String(), "wizard channel [stable|pre-release]") {
		t.Fatalf("--help: code=%d out=%q err=%q", code, out.String(), errOut.String())
	}
}

func TestDoctorReportsTheChannel(t *testing.T) {
	withBuild(t, "v1.0.14-beta.1")
	t.Setenv("WIZARD_CONFIG_DIR", t.TempDir())
	report := collectDoctor(false, false)
	for _, check := range report.Checks {
		if check.ID == "channel" {
			if check.State != "pass" || !strings.Contains(check.Detail, "pre-release") || !strings.Contains(check.Detail, sourceImplied) {
				t.Fatalf("channel check = %+v", check)
			}
			return
		}
	}
	t.Fatal("doctor has no channel check")
}
