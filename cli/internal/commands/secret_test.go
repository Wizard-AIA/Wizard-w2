package commands

import (
	"bytes"
	"fmt"
	"strings"
	"testing"
)

func TestReadMaskedFromEchoesOneGlyphPerCharacter(t *testing.T) {
	var out bytes.Buffer
	got, err := readMaskedFrom(strings.NewReader("sk-abc123\r"), &out)
	if err != nil || got != "sk-abc123" {
		t.Fatalf("got (%q, %v)", got, err)
	}
	if n := strings.Count(out.String(), maskGlyph); n != 9 {
		t.Fatalf("echoed %d glyphs, want 9 (one per character): %q", n, out.String())
	}
	if strings.Contains(out.String(), "sk-abc123") {
		t.Fatal("the secret itself must never be echoed")
	}
}

func TestReadMaskedFromBackspaceAndClear(t *testing.T) {
	got, err := readMaskedFrom(strings.NewReader("abcX\x7f\x7fd\r"), &bytes.Buffer{})
	if err != nil || got != "abd" {
		t.Fatalf("backspace: got (%q, %v), want abd", got, err)
	}
	got, err = readMaskedFrom(strings.NewReader("wrong\x15right\n"), &bytes.Buffer{})
	if err != nil || got != "right" {
		t.Fatalf("Ctrl-U: got (%q, %v), want right", got, err)
	}
}

func TestReadMaskedFromIgnoresEscapeSequencesAndPasteMarkers(t *testing.T) {
	// up-arrow, bracketed-paste start/end around the key, an SS3 arrow.
	in := "\x1b[Aab\x1b[200~cd\x1b[201~\x1bOAef\r"
	got, err := readMaskedFrom(strings.NewReader(in), &bytes.Buffer{})
	if err != nil || got != "abcdef" {
		t.Fatalf("got (%q, %v), want abcdef", got, err)
	}
}

func TestReadMaskedFromHandlesMultibyteAndTrims(t *testing.T) {
	got, err := readMaskedFrom(strings.NewReader("  kéy™  \r"), &bytes.Buffer{})
	if err != nil || got != "kéy™" {
		t.Fatalf("got (%q, %v)", got, err)
	}
}

func TestReadMaskedFromCancel(t *testing.T) {
	for _, in := range []string{"ab\x03", "\x04"} {
		if _, err := readMaskedFrom(strings.NewReader(in), &bytes.Buffer{}); err != errInputCancelled {
			t.Errorf("%q: err = %v, want errInputCancelled", in, err)
		}
	}
}

func TestDescribeSecretProvesArrivalWithoutRevealingIt(t *testing.T) {
	key := "TEST.FakeKeyNotReal_0123456789abcdefghijklmnopqrstuvWXYZ"
	d := describeSecret(key)
	if !strings.Contains(d, fmt.Sprintf("%d characters", len(key))) || !strings.Contains(d, "…WXYZ") {
		t.Fatalf("description %q should give length and the last characters", d)
	}
	if strings.Contains(d, key[:20]) {
		t.Fatal("must not reveal the start of the key")
	}
	if short := describeSecret("hunter2"); strings.Contains(short, "ter2") || !strings.Contains(short, "7 characters") {
		t.Fatalf("a short secret must reveal only its length, got %q", short)
	}
}
