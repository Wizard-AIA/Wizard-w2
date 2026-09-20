package commands

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

// RunAttach implements `wizard attach`: prints current status, then tails
// backend.log and frontend.log together, source-prefixed, until Ctrl+C.
// Read-only -- it does not touch the daemon, only watches its logs.
func RunAttach(env *Env, args []string) int {
	if code, done := noFlags(env, "attach", args); done {
		return code
	}
	RunStatus(env, nil)
	_, _ = fmt.Fprintln(env.Out, "\n--- following backend.log and frontend.log (Ctrl+C to stop) ---")

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	lines := make(chan string, 256)
	go followFile(ctx, "backend", env.BackendLogPath(), lines)
	go followFile(ctx, "frontend", env.FrontendLogPath(), lines)

	for {
		select {
		case <-ctx.Done():
			_, _ = fmt.Fprintln(env.Out, "\nDetached.")
			return 0
		case line := <-lines:
			fmt.Fprintln(env.Out, line)
		}
	}
}

// followFile polls path for growth and emits any newly-appended, complete
// lines, prefixed with label. Polling rather than a filesystem watcher: the
// three target platforms have three different notification APIs, and a log
// file appended to a few times a second needs nothing fancier than this.
//
// Two things a naive "scan to EOF, remember the size" version gets wrong: a
// final unterminated line (the writer is mid-write) must not be treated as
// complete just because the poll caught it there, and rotate.go replaces the
// active log file outright -- a same-or-larger size after replacement must
// not read as "nothing new" or "already seen".
func followFile(ctx context.Context, label, path string, out chan<- string) {
	var offset int64
	var lastFile os.FileInfo
	ticker := time.NewTicker(300 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}

		f, err := os.Open(path)
		if err != nil {
			continue // not created yet, or rotated away mid-read; try again next tick
		}
		info, err := f.Stat()
		if err != nil {
			f.Close()
			continue
		}

		switch {
		case lastFile != nil && !os.SameFile(lastFile, info):
			offset = 0 // rotate.go swapped in a different file at this path
		case info.Size() < offset:
			offset = 0 // truncated out from under us
		}
		lastFile = info

		if info.Size() == offset {
			f.Close()
			continue
		}
		if _, err := f.Seek(offset, 0); err != nil {
			f.Close()
			continue
		}

		reader := bufio.NewReader(f)
		for {
			line, err := reader.ReadString('\n')
			if err != nil || line == "" {
				// Either clean EOF or an incomplete trailing line -- leave
				// offset before whichever bytes weren't consumed here, so a
				// line finished by the next write is read whole rather than
				// as just its suffix.
				break
			}
			select {
			case out <- fmt.Sprintf("[%s] %s", label, strings.TrimRight(line, "\n")):
				offset += int64(len(line))
			case <-ctx.Done():
				f.Close()
				return
			}
		}
		f.Close()
	}
}
