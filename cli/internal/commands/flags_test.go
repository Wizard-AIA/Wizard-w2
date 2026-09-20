package commands

import (
	"bytes"
	"flag"
	"testing"

	"wizard/internal/exitcode"
)

func testEnv() (*Env, *bytes.Buffer, *bytes.Buffer) {
	var out, errb bytes.Buffer
	return &Env{Out: &out, Err: &errb}, &out, &errb
}

func TestParseFlagsHelpExitsZeroOnStdout(t *testing.T) {
	env, out, errb := testEnv()
	fs := flag.NewFlagSet("start", flag.ContinueOnError)
	fs.Bool("no-browser", false, "skip the browser")
	code, done := parseFlags(env, fs, []string{"--help"})
	if !done || code != exitcode.OK {
		t.Fatalf("--help: got (%d, %v), want (0, true)", code, done)
	}
	if out.Len() == 0 || errb.Len() != 0 {
		t.Fatalf("usage must go to stdout for an explicit --help; out=%q err=%q", out, errb)
	}
}

func TestParseFlagsBadFlagIsUsageError(t *testing.T) {
	env, _, errb := testEnv()
	fs := flag.NewFlagSet("start", flag.ContinueOnError)
	code, done := parseFlags(env, fs, []string{"--nope"})
	if !done || code != exitcode.Usage || errb.Len() == 0 {
		t.Fatalf("got (%d, %v) err=%q, want usage error", code, done, errb)
	}
}

func TestRejectArgs(t *testing.T) {
	env, _, errb := testEnv()
	if _, done := rejectArgs(env, "stop", nil); done {
		t.Fatal("no args must be accepted")
	}
	if code, done := rejectArgs(env, "stop", []string{"extra"}); !done || code != exitcode.Usage || errb.Len() == 0 {
		t.Fatalf("got (%d, %v), want usage error", code, done)
	}
}

func TestValidPort(t *testing.T) {
	for _, ok := range []string{"1", "8000", "65535"} {
		if !validPort(ok) {
			t.Errorf("%q should be valid", ok)
		}
	}
	for _, bad := range []string{"", "0", "-1", "65536", "80a", "http"} {
		if validPort(bad) {
			t.Errorf("%q should be invalid", bad)
		}
	}
}
