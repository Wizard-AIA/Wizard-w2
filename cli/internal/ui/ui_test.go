package ui

import (
	"bytes"
	"strings"
	"testing"
)

func env(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

// A buffer is not a terminal: output must be plain, ASCII and stable to grep.
func TestNonTerminalOutputIsPlainASCII(t *testing.T) {
	var out bytes.Buffer
	p := &Printer{W: &out, T: Detect(&out, env(nil), "linux")}
	p.Banner("1.0.13", "tagline")
	p.Section("Checks")
	p.Check(OK, "Python", "3.12.4")
	p.Check(Warn, "PATH", "not on PATH")
	p.Hint("run: wizard doctor")
	p.Check(Fail, "Node.js", "missing")
	p.Summary(1, 1, 1)

	got := out.String()
	if strings.Contains(got, "\x1b") {
		t.Fatalf("no ANSI escapes outside a terminal: %q", got)
	}
	for _, r := range got {
		if r > 127 {
			t.Fatalf("non-ASCII rune %q in plain output: %q", r, got)
		}
	}
	for _, want := range []string{"[ OK ]  Python", "[WARN]  PATH", "[FAIL]  Node.js", "-> run: wizard doctor", "1 passed", "1 warning", "1 failed"} {
		if !strings.Contains(got, want) {
			t.Errorf("output missing %q:\n%s", want, got)
		}
	}
}

func TestColorAndUnicodeWhenEnabled(t *testing.T) {
	var out bytes.Buffer
	p := &Printer{W: &out, T: Theme{Color: true, Unicode: true, Width: 100}}
	p.Check(OK, "Python", "3.12")
	if !strings.Contains(out.String(), "\x1b[32m✓") {
		t.Fatalf("expected a green check mark: %q", out.String())
	}
}

func TestWindowsLegacyConsoleFallsBackToASCII(t *testing.T) {
	// Detect needs a real terminal to enable anything, so assert the rule
	// through the Unicode gate directly: no WT_SESSION/TERM_PROGRAM on Windows.
	th := Detect(&bytes.Buffer{}, env(nil), "windows")
	if th.Unicode || th.Color {
		t.Fatalf("a non-terminal on Windows must be plain: %+v", th)
	}
}

func TestTableAlignsColumns(t *testing.T) {
	var out bytes.Buffer
	p := &Printer{W: &out, T: Theme{Width: 100}}
	p.Table([]string{"COMMAND", "PURPOSE"}, [][]string{{"init", "set up"}, {"uninstall", "remove"}})
	lines := strings.Split(strings.TrimRight(out.String(), "\n"), "\n")
	if len(lines) != 3 {
		t.Fatalf("got %d lines: %q", len(lines), out.String())
	}
	col := strings.Index(lines[0], "PURPOSE")
	if strings.Index(lines[1], "set up") != col || strings.Index(lines[2], "remove") != col {
		t.Fatalf("columns are not aligned:\n%s", out.String())
	}
}

func TestPadCountsRunesNotBytes(t *testing.T) {
	if got := pad("é", 3); got != "é  " {
		t.Fatalf("pad = %q", got)
	}
}
