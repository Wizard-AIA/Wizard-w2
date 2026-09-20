package commands

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"wizard/internal/ui"
)

func opts(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = fmt.Sprintf("model-%02d", i+1)
	}
	return out
}

func keys(d *dropdownState, seq ...string) dropdownAction {
	var last dropdownAction
	for _, k := range seq {
		last = d.key([]byte(k))
	}
	return last
}

const (
	up, down = "\x1b[A", "\x1b[B"
	pgUp     = "\x1b[5~"
	pgDown   = "\x1b[6~"
	home     = "\x1b[H"
	end      = "\x1b[F"
)

func TestDropdownStartsOnTheCurrentOption(t *testing.T) {
	d := newDropdown([]string{"ollama", "lmstudio", "gemini"}, "gemini", 10)
	if d.selected != 2 {
		t.Fatalf("selected = %d, want the current option", d.selected)
	}
	if d := newDropdown([]string{"a", "b"}, "not-there", 10); d.selected != 0 {
		t.Fatal("an unknown current value falls back to the first option")
	}
}

func TestDropdownArrowsWrapAroundAndAcceptWithEnter(t *testing.T) {
	d := newDropdown(opts(3), "", 10)
	keys(d, up)
	if d.selected != 2 {
		t.Fatalf("up from the top must wrap to the last option, got %d", d.selected)
	}
	keys(d, down)
	if d.selected != 0 {
		t.Fatalf("down from the bottom must wrap to the first option, got %d", d.selected)
	}
	if got := keys(d, "\r"); got != dropdownAccept {
		t.Fatal("Enter accepts")
	}
	if got := keys(newDropdown(opts(3), "", 10), "\x03"); got != dropdownCancel {
		t.Fatal("Ctrl-C cancels")
	}
}

// A long model list must scroll: the highlighted row is always inside the
// window, and only the window is drawn.
func TestDropdownWindowFollowsTheSelectionThroughALongList(t *testing.T) {
	d := newDropdown(opts(42), "", 10)
	for i := 0; i < 25; i++ {
		keys(d, down)
		if d.selected < d.top || d.selected >= d.top+d.window {
			t.Fatalf("after %d downs the selection %d is outside the window [%d,%d)", i+1, d.selected, d.top, d.top+d.window)
		}
	}
	keys(d, home)
	if d.selected != 0 || d.top != 0 {
		t.Fatalf("Home: selected=%d top=%d", d.selected, d.top)
	}
	keys(d, end)
	if d.selected != 41 || d.top != 32 {
		t.Fatalf("End: selected=%d top=%d, want 41 and a window ending at the last row", d.selected, d.top)
	}
	keys(d, pgUp)
	if d.selected != 31 {
		t.Fatalf("PgUp moves by a window, got %d", d.selected)
	}
	keys(d, pgDown, pgDown, pgDown)
	if d.selected != 41 {
		t.Fatalf("PgDn clamps at the last option (no wrap), got %d", d.selected)
	}
}

func TestDropdownTypeAheadJumpsToAModelByPrefix(t *testing.T) {
	names := []string{"auto-select", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash", "gemma-4-31b-it"}
	clock := time.Unix(1000, 0)
	d := newDropdown(names, "", 3)
	d.now = func() time.Time { return clock }
	keys(d, "g", "e", "m", "i", "n", "i", "-", "3")
	if names[d.selected] != "gemini-3-flash" {
		t.Fatalf("typing 'gemini-3' selected %q", names[d.selected])
	}
	// After a pause the buffer restarts, so a new word is not appended to the old.
	clock = clock.Add(2 * time.Second)
	keys(d, "g", "e", "m", "m")
	if names[d.selected] != "gemma-4-31b-it" {
		t.Fatalf("after a pause, 'gemm' selected %q", names[d.selected])
	}
	// No prefix match: falls back to "contains".
	clock = clock.Add(2 * time.Second)
	keys(d, "p", "r", "o")
	if names[d.selected] != "gemini-2.5-pro" {
		t.Fatalf("'pro' should find gemini-2.5-pro, got %q", names[d.selected])
	}
}

// Short lists keep the old numbered shortcut; long lists must not, or a model
// named "3-..." could never be typed.
func TestDropdownDigitShortcutOnlyForShortLists(t *testing.T) {
	d := newDropdown(opts(4), "", 10)
	if got := keys(d, "3"); got != dropdownAccept || d.selected != 2 {
		t.Fatalf("digit 3 on a short list: action=%v selected=%d", got, d.selected)
	}
	long := newDropdown([]string{"a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "3-turbo"}, "", 10)
	if got := keys(long, "3"); got == dropdownAccept {
		t.Fatal("a digit on a long list is type-ahead, not a shortcut")
	}
	if long.options[long.selected] != "3-turbo" {
		t.Fatalf("typing '3' should jump to 3-turbo, got %q", long.options[long.selected])
	}
}

func TestDropdownIgnoresUnknownKeys(t *testing.T) {
	d := newDropdown(opts(3), "", 10)
	for _, k := range []string{"\x1b[Z", "\t", "\x00", "\x1b[2~"} {
		if d.key([]byte(k)) != dropdownNone || d.selected != 0 {
			t.Errorf("%q must be ignored", k)
		}
	}
}

func TestDropdownLinesMarkTheSelectedRowAndShowOnlyTheWindow(t *testing.T) {
	d := newDropdown(opts(42), "model-15", 10)
	ascii := &ui.Printer{T: ui.Theme{}}
	lines := dropdownLines(ascii, d)
	if len(lines) != 11 { // 10 options + the hint
		t.Fatalf("got %d lines, want the 10-row window plus a hint", len(lines))
	}
	marked := 0
	for _, l := range lines {
		if strings.Contains(l, "> ") && strings.Contains(l, "model-15") {
			marked++
		}
	}
	if marked != 1 {
		t.Fatalf("exactly the selected row must carry the pointer:\n%s", strings.Join(lines, "\n"))
	}
	if !strings.Contains(lines[len(lines)-1], "15/42") {
		t.Fatalf("a scrolling list shows its position: %q", lines[len(lines)-1])
	}
	for _, l := range lines {
		for _, r := range l {
			if r > 127 {
				t.Fatalf("ASCII theme produced a non-ASCII rune %q in %q", r, l)
			}
		}
	}
	uni := &ui.Printer{T: ui.Theme{Unicode: true}}
	if !strings.Contains(strings.Join(dropdownLines(uni, d), "\n"), "❯") {
		t.Fatal("the Unicode theme uses the ❯ pointer")
	}
}

func TestDropdownShortListShowsEveryOptionAndNoScrollHint(t *testing.T) {
	d := newDropdown([]string{"ollama", "gemini"}, "", 10)
	lines := dropdownLines(&ui.Printer{T: ui.Theme{}}, d)
	if len(lines) != 3 {
		t.Fatalf("got %d lines: %v", len(lines), lines)
	}
	if strings.Contains(lines[2], "/2") {
		t.Fatalf("no position counter when nothing scrolls: %q", lines[2])
	}
}

func TestDropdownEscapeClearsTypeAheadWithoutCancelling(t *testing.T) {
	d := newDropdown([]string{"alpha", "beta", "gamma"}, "", 10)
	keys(d, "g")
	if d.selected != 2 {
		t.Fatalf("type-ahead did not jump to gamma (selected %d)", d.selected)
	}
	if action := keys(d, "\x1b"); action != dropdownNone {
		t.Fatalf("a lone Escape returned action %v, want none", action)
	}
	if d.typed != "" {
		t.Fatalf("Escape left %q in the search buffer", d.typed)
	}
}
