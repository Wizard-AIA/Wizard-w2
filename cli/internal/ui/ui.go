// Package ui renders every human-facing `wizard` message the same way:
// consistent headings, aligned status lines, tables and summaries.
//
// Output adapts to where it is going. A capable terminal gets colour and
// Unicode symbols; a pipe, a CI log, a dumb terminal or a legacy Windows
// console gets plain ASCII that is stable to grep. NO_COLOR, FORCE_COLOR,
// WIZARD_NO_COLOR and WIZARD_ASCII are honoured, as is `--no-color`.
package ui

import (
	"fmt"
	"io"
	"os"
	"runtime"
	"strings"
	"unicode/utf8"

	"golang.org/x/term"
)

// Status is the outcome shown at the start of a check line.
type Status int

const (
	OK Status = iota
	Warn
	Fail
	Info
	Skip
)

// Theme is what the destination can render.
type Theme struct {
	Color   bool
	Unicode bool
	Width   int
}

// Detect decides the theme for w from the environment. getenv is injected so
// tests need not touch the process environment.
func Detect(w io.Writer, getenv func(string) string, goos string) Theme {
	t := Theme{Width: 100}
	f, isFile := w.(*os.File)
	tty := isFile && term.IsTerminal(int(f.Fd()))
	if tty {
		if width, _, err := term.GetSize(int(f.Fd())); err == nil && width > 0 {
			t.Width = width
		}
	}
	dumb := getenv("TERM") == "dumb"

	t.Color = tty && !dumb && getenv("NO_COLOR") == "" && getenv("WIZARD_NO_COLOR") == ""
	if getenv("FORCE_COLOR") != "" && getenv("NO_COLOR") == "" && getenv("WIZARD_NO_COLOR") == "" {
		t.Color = true
	}

	t.Unicode = tty && !dumb && getenv("WIZARD_ASCII") == ""
	if goos == "windows" && getenv("WT_SESSION") == "" && getenv("TERM_PROGRAM") == "" && getenv("ConEmuANSI") == "" {
		// Legacy conhost fonts lack many of these glyphs.
		t.Unicode = false
	}
	if t.Color && !enableVT(w) {
		t.Color = false
		t.Unicode = false
	}
	if t.Width > 120 {
		t.Width = 120
	}
	if t.Width < 60 {
		t.Width = 60
	}
	return t
}

// Printer writes themed output to W.
type Printer struct {
	W io.Writer
	T Theme
}

// New builds a Printer for w using the process environment.
func New(w io.Writer) *Printer {
	return &Printer{W: w, T: Detect(w, os.Getenv, runtime.GOOS)}
}

func (p *Printer) sgr(code, s string) string {
	if !p.T.Color || s == "" {
		return s
	}
	return "\x1b[" + code + "m" + s + "\x1b[0m"
}

// Bold, Dim, Accent, Green, Yellow and Red style s when colour is on.
func (p *Printer) Bold(s string) string   { return p.sgr("1", s) }
func (p *Printer) Dim(s string) string    { return p.sgr("2", s) }
func (p *Printer) Accent(s string) string { return p.sgr("38;5;176", s) }
func (p *Printer) Green(s string) string  { return p.sgr("32", s) }
func (p *Printer) Yellow(s string) string { return p.sgr("33", s) }
func (p *Printer) Red(s string) string    { return p.sgr("31", s) }

func (p *Printer) glyph(s Status) string {
	if p.T.Unicode {
		return [...]string{p.Green("✓"), p.Yellow("!"), p.Red("✗"), p.Accent("•"), p.Dim("–")}[s]
	}
	return [...]string{p.Green("[ OK ]"), p.Yellow("[WARN]"), p.Red("[FAIL]"), p.Accent("[INFO]"), p.Dim("[SKIP]")}[s]
}

// Word is the plain-text name of a status, for JSON and summaries.
func (s Status) Word() string { return [...]string{"pass", "warn", "fail", "info", "skip"}[s] }

// Printf writes formatted text unchanged.
func (p *Printer) Printf(format string, a ...any) { fmt.Fprintf(p.W, format, a...) }

// Println writes a line unchanged.
func (p *Printer) Println(a ...any) { fmt.Fprintln(p.W, a...) }

// Blank writes an empty line.
func (p *Printer) Blank() { fmt.Fprintln(p.W) }

// Banner is the product line shown by help and setup flows.
func (p *Printer) Banner(version, tagline string) {
	title := "Wizard"
	if version != "" {
		title += " " + version
	}
	sep := "  ·  "
	if !p.T.Unicode {
		sep = "  -  "
	}
	fmt.Fprintf(p.W, "%s%s%s\n", p.Bold(p.Accent(title)), sep, p.Dim(tagline))
}

// Section starts a titled group, with a rule under it.
func (p *Printer) Section(title string) {
	rule := "─"
	if !p.T.Unicode {
		rule = "-"
	}
	// One rule width for every section keeps the page aligned.
	fmt.Fprintf(p.W, "\n%s\n%s\n", p.Bold(title), p.Dim(strings.Repeat(rule, min(64, p.T.Width))))
}

// Step prints "[n/total] message" for a multi-step operation.
func (p *Printer) Step(n, total int, message string) {
	fmt.Fprintf(p.W, "%s %s\n", p.Accent(fmt.Sprintf("[%d/%d]", n, total)), message)
}

// Check prints an aligned status line: glyph, label column, detail.
func (p *Printer) Check(s Status, label, detail string) {
	fmt.Fprintf(p.W, "  %s  %s  %s\n", p.glyph(s), pad(label, 18), detail)
}

// Hint prints an indented remediation line under a check.
func (p *Printer) Hint(text string) {
	arrow := "→"
	if !p.T.Unicode {
		arrow = "->"
	}
	for i, line := range strings.Split(strings.TrimRight(text, "\n"), "\n") {
		prefix := "     " + p.Dim(arrow) + " "
		if i > 0 {
			prefix = "       "
		}
		fmt.Fprintf(p.W, "%s%s\n", prefix, p.Dim(line))
	}
}

// KV prints "  key      value" with the key column aligned to keyWidth.
func (p *Printer) KV(keyWidth int, key, value string) {
	fmt.Fprintf(p.W, "  %s  %s\n", p.Dim(pad(key, keyWidth)), value)
}

// Table prints rows under headers with aligned columns.
func (p *Printer) Table(headers []string, rows [][]string) {
	widths := make([]int, len(headers))
	for i, h := range headers {
		widths[i] = utf8.RuneCountInString(h)
	}
	for _, r := range rows {
		for i := 0; i < len(headers) && i < len(r); i++ {
			if n := utf8.RuneCountInString(r[i]); n > widths[i] {
				widths[i] = n
			}
		}
	}
	line := func(cells []string, style func(string) string) {
		var b strings.Builder
		b.WriteString(" ")
		for i := range headers {
			cell := ""
			if i < len(cells) {
				cell = cells[i]
			}
			if i == len(headers)-1 {
				b.WriteString(" " + style(cell))
			} else {
				b.WriteString(" " + style(pad(cell, widths[i])) + " ")
			}
		}
		fmt.Fprintln(p.W, strings.TrimRight(b.String(), " "))
	}
	line(headers, p.Bold)
	for _, r := range rows {
		line(r, func(s string) string { return s })
	}
}

// Summary prints the closing tally of a diagnostic run.
func (p *Printer) Summary(pass, warn, fail int) {
	parts := []string{p.Green(fmt.Sprintf("%d passed", pass))}
	if warn > 0 {
		parts = append(parts, p.Yellow(fmt.Sprintf("%d warning%s", warn, plural(warn))))
	} else {
		parts = append(parts, p.Dim("0 warnings"))
	}
	if fail > 0 {
		parts = append(parts, p.Red(fmt.Sprintf("%d failed", fail)))
	} else {
		parts = append(parts, p.Dim("0 failed"))
	}
	sep := "  ·  "
	if !p.T.Unicode {
		sep = "  |  "
	}
	fmt.Fprintf(p.W, "\n%s\n", strings.Join(parts, sep))
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// pad left-aligns s in a column of width runes (never truncating).
func pad(s string, width int) string {
	if n := utf8.RuneCountInString(s); n < width {
		return s + strings.Repeat(" ", width-n)
	}
	return s
}
