//go:build !windows

package commands

import (
	"os"
	"testing"
	"time"
)

// A bare Escape press used to block readTerminalKey until two more keys were
// typed, freezing every prompt that reads the raw terminal.
func TestReadTerminalKeyReturnsALoneEscape(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer w.Close()
	if _, err := w.Write([]byte{0x1b}); err != nil {
		t.Fatal(err)
	}
	got := make(chan []byte, 1)
	go func() {
		key, _ := readTerminalKey(r)
		got <- key
	}()
	select {
	case key := <-got:
		if string(key) != "\x1b" {
			t.Fatalf("key = %q, want a lone Escape", key)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("readTerminalKey is still blocked after a lone Escape")
	}
}

func TestReadTerminalKeyStillDecodesArrowSequences(t *testing.T) {
	for _, seq := range []string{"\x1b[A", "\x1b[B", "\x1bOH", "\x1b[5~"} {
		r, w, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(seq)); err != nil {
			t.Fatal(err)
		}
		key, err := readTerminalKey(r)
		r.Close()
		w.Close()
		if err != nil || string(key) != seq {
			t.Fatalf("readTerminalKey(%q) = %q, %v", seq, key, err)
		}
	}
}
