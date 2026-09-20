package commands

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"unicode/utf8"

	"golang.org/x/term"
)

// errInputCancelled is returned when the user presses Ctrl-C or Ctrl-D on an
// empty line while typing a secret.
var errInputCancelled = errors.New("interactive setup cancelled")

// maskGlyph is echoed once per typed or pasted character. The old prompt used
// term.ReadPassword, which echoes nothing at all, so a person could not tell a
// successful paste from a paste that never arrived.
const maskGlyph = "•"

// readMaskedSecret reads a secret from a terminal, echoing one mask glyph per
// character so a paste is visibly registered, and returns it trimmed.
func readMaskedSecret(file *os.File, out io.Writer) (string, error) {
	state, err := term.MakeRaw(int(file.Fd()))
	if err != nil {
		// Raw mode unavailable: fall back to silent input rather than echo.
		value, rerr := term.ReadPassword(int(file.Fd()))
		fmt.Fprintln(out)
		return strings.TrimSpace(string(value)), rerr
	}
	defer func() { _ = term.Restore(int(file.Fd()), state) }()
	return readMaskedFrom(file, out)
}

// readMaskedFrom is the terminal-independent core of readMaskedSecret: it
// consumes bytes from r until Enter, handling backspace, Ctrl-U (clear line),
// Ctrl-C/Ctrl-D (cancel) and multi-byte characters, and drops terminal escape
// sequences (arrow keys, bracketed-paste markers) instead of storing them.
func readMaskedFrom(r io.Reader, out io.Writer) (string, error) {
	var value []rune
	buf := make([]byte, 1)
	var pending []byte // an incomplete UTF-8 sequence
	skipEscape := 0    // bytes of an escape sequence still to swallow
	inCSI := false

	erase := func(n int) {
		for i := 0; i < n; i++ {
			fmt.Fprint(out, "\b \b")
		}
	}
	for {
		n, err := r.Read(buf)
		if n == 0 {
			if err == nil {
				continue
			}
			if err == io.EOF && len(value) > 0 {
				fmt.Fprint(out, "\r\n")
				return strings.TrimSpace(string(value)), nil
			}
			return "", err
		}
		b := buf[0]

		switch {
		case inCSI:
			// ESC [ <params> <final byte 0x40-0x7e>
			if b >= 0x40 && b <= 0x7e {
				inCSI = false
			}
			continue
		case skipEscape > 0:
			skipEscape--
			switch b {
			case '[':
				inCSI = true
			case 'O': // SS3 (ESC O A for an arrow key): one more byte follows
				skipEscape = 1
			}
			continue
		}

		switch b {
		case 0x1b: // ESC: swallow the introducer plus the CSI that follows
			skipEscape = 1
			continue
		case '\r', '\n':
			fmt.Fprint(out, "\r\n")
			return strings.TrimSpace(string(value)), nil
		case 0x03: // Ctrl-C
			fmt.Fprint(out, "\r\n")
			return "", errInputCancelled
		case 0x04: // Ctrl-D
			if len(value) == 0 {
				fmt.Fprint(out, "\r\n")
				return "", errInputCancelled
			}
			continue
		case 0x7f, 0x08: // backspace
			if len(value) > 0 {
				value = value[:len(value)-1]
				erase(1)
			}
			continue
		case 0x15: // Ctrl-U
			erase(len(value))
			value = value[:0]
			continue
		}
		if b < 0x20 {
			continue // other control characters are not part of a secret
		}

		pending = append(pending, b)
		if !utf8.FullRune(pending) {
			continue
		}
		r, size := utf8.DecodeRune(pending)
		pending = pending[size:]
		if r == utf8.RuneError {
			continue
		}
		value = append(value, r)
		fmt.Fprint(out, maskGlyph)
	}
}

// describeSecret is a safe, human summary of a secret that proves it arrived
// without printing it: its length and, for keys long enough that four
// characters reveal nothing useful, its last four.
func describeSecret(secret string) string {
	n := utf8.RuneCountInString(secret)
	if n < 16 {
		return fmt.Sprintf("%d characters", n)
	}
	runes := []rune(secret)
	return fmt.Sprintf("%d characters, ends with …%s", n, string(runes[n-4:]))
}
