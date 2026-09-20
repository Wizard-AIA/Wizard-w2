package commands

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"golang.org/x/term"

	"wizard/internal/ui"
)

// The interactive select ("dropdown") used by `wizard init`.
//
// It is split so nearly all of it can be tested without a terminal:
//
//	dropdownState  the pure state machine: keys in, selection out
//	dropdownLines  the pure renderer: state in, lines out
//	runDropdown    the thin loop that reads a raw terminal and repaints
//
// Where ANSI cursor movement works the list is redrawn in place with the
// highlighted row marked, scrolls when it is longer than the window, and
// collapses to a single "label  value" line when a choice is made. Elsewhere
// (dumb terminals, legacy consoles) promptArrowChoice's one-line fallback runs.

// errRawUnavailable means the terminal refused raw mode; the caller falls back
// to a plain numbered prompt.
var errRawUnavailable = errors.New("terminal raw mode unavailable")

type dropdownAction int

const (
	dropdownNone dropdownAction = iota
	dropdownAccept
	dropdownCancel
)

// dropdownState is the selection state of one menu.
type dropdownState struct {
	options  []string
	selected int
	top      int // index of the first visible option
	window   int // number of visible options

	typed   string // type-ahead buffer
	typedAt time.Time
	now     func() time.Time
}

const typeAheadReset = 1200 * time.Millisecond

func newDropdown(options []string, current string, window int) *dropdownState {
	d := &dropdownState{options: options, window: max(1, min(window, len(options))), now: time.Now}
	for i, o := range options {
		if o == current {
			d.selected = i
			break
		}
	}
	d.reveal()
	return d
}

// reveal scrolls the window so the selected row is visible.
func (d *dropdownState) reveal() {
	if d.selected < d.top {
		d.top = d.selected
	}
	if d.selected >= d.top+d.window {
		d.top = d.selected - d.window + 1
	}
	d.top = max(0, min(d.top, len(d.options)-d.window))
}

func (d *dropdownState) move(delta int) {
	n := len(d.options)
	if n == 0 {
		return
	}
	d.selected = ((d.selected+delta)%n + n) % n // wraps, as the old menu did
	d.reveal()
}

func (d *dropdownState) jump(to int) {
	d.selected = max(0, min(to, len(d.options)-1))
	d.reveal()
}

// key applies one decoded key press.
func (d *dropdownState) key(k []byte) dropdownAction {
	switch string(k) {
	case "\r", "\n":
		return dropdownAccept
	case "\x03": // Ctrl-C
		return dropdownCancel
	case "\x1b[A":
		d.typed = ""
		d.move(-1)
	case "\x1b[B":
		d.typed = ""
		d.move(1)
	case "\x1b[H", "\x1b[1~", "\x1bOH":
		d.jump(0)
	case "\x1b[F", "\x1b[4~", "\x1bOF":
		d.jump(len(d.options) - 1)
	case "\x1b[5~":
		d.jump(d.selected - d.window)
	case "\x1b[6~":
		d.jump(d.selected + d.window)
	default:
		if len(k) != 1 || k[0] < 0x20 || k[0] > 0x7e {
			return dropdownNone
		}
		// A digit picks that row outright when the list is short enough to
		// number on one screen (the behaviour of the old numbered menu) -- but
		// only when the person has not started typing a name: in "gemini-3" the
		// 3 is part of the word.
		if d.now().Sub(d.typedAt) > typeAheadReset {
			d.typed = ""
		}
		if d.typed == "" && len(d.options) <= 9 && k[0] >= '1' && int(k[0]-'0') <= len(d.options) {
			d.jump(int(k[0] - '1'))
			return dropdownAccept
		}
		d.typeAhead(k[0])
	}
	return dropdownNone
}

// typeAhead jumps to the first option starting with the characters typed
// within the last second, falling back to one that merely contains them: with a
// long model list, typing "gemini-2.5-p" is faster than scrolling.
func (d *dropdownState) typeAhead(ch byte) {
	if d.now().Sub(d.typedAt) > typeAheadReset {
		d.typed = ""
	}
	d.typedAt = d.now()
	d.typed += strings.ToLower(string(ch))
	for i, o := range d.options {
		if strings.HasPrefix(strings.ToLower(o), d.typed) {
			d.jump(i)
			return
		}
	}
	for i, o := range d.options {
		if strings.Contains(strings.ToLower(o), d.typed) {
			d.jump(i)
			return
		}
	}
}

// dropdownLines renders the menu body: the visible options with the selected
// row marked, and a hint line. It never contains a trailing newline; the caller
// joins with the terminal's own line ending (raw mode needs "\r\n").
func dropdownLines(p *ui.Printer, d *dropdownState) []string {
	pointer, blank := "❯", " "
	if !p.T.Unicode {
		pointer = ">"
	}
	lines := make([]string, 0, d.window+1)
	for i := d.top; i < d.top+d.window && i < len(d.options); i++ {
		if i == d.selected {
			lines = append(lines, "  "+p.Accent(pointer)+" "+p.Bold(d.options[i]))
		} else {
			lines = append(lines, "  "+blank+" "+d.options[i])
		}
	}
	hint := "↑/↓ move · Enter select"
	if !p.T.Unicode {
		hint = "up/down to move, Enter to select"
	}
	if len(d.options) > d.window {
		if p.T.Unicode {
			hint = fmt.Sprintf("↑/↓ move · type to jump · Enter select · %d/%d", d.selected+1, len(d.options))
		} else {
			hint = fmt.Sprintf("up/down to move, type to jump, Enter to select (%d/%d)", d.selected+1, len(d.options))
		}
	}
	return append(lines, "  "+p.Dim(hint))
}

// runDropdown paints the menu on a raw terminal and returns the chosen option.
func runDropdown(out io.Writer, in *os.File, p *ui.Printer, label string, options []string, current string) (string, error) {
	state, err := term.MakeRaw(int(in.Fd()))
	if err != nil {
		return "", errRawUnavailable
	}
	restoreInput := ui.EnableVTInput(in)
	defer func() { restoreInput(); _ = term.Restore(int(in.Fd()), state) }()

	window := 10
	if h := p.T.Height; h > 0 && h-5 < window {
		window = max(4, h-5) // leave room for the header and hint on short terminals
	}
	d := newDropdown(options, current, window)

	fmt.Fprint(out, "\r\n")
	painted := 0
	paint := func() {
		if painted > 0 {
			fmt.Fprintf(out, "\x1b[%dA", painted) // back to the top of the menu
		}
		// The label is part of the repainted block so collapsing removes it too.
		lines := append([]string{p.Bold(label)}, dropdownLines(p, d)...)
		for _, l := range lines {
			fmt.Fprintf(out, "\r\x1b[2K%s\r\n", l)
		}
		painted = len(lines)
	}
	paint()
	for {
		key, err := readTerminalKey(in)
		if err != nil {
			return "", err
		}
		switch d.key(key) {
		case dropdownCancel:
			collapse(out, painted)
			return "", errors.New("interactive setup cancelled")
		case dropdownAccept:
			collapse(out, painted)
			glyph := "✓"
			if !p.T.Unicode {
				glyph = "*"
			}
			fmt.Fprintf(out, "  %s %s  %s\r\n", p.Green(glyph), label, p.Bold(options[d.selected]))
			return options[d.selected], nil
		}
		paint()
	}
}

// collapse erases the painted menu (n lines) so a finished choice leaves one
// line behind instead of the whole list.
func collapse(out io.Writer, n int) {
	if n > 0 {
		fmt.Fprintf(out, "\x1b[%dA\r\x1b[J", n)
	}
}
