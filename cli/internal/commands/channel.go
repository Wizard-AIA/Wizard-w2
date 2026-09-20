package commands

import (
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"wizard/internal/appdir"
	"wizard/internal/compat"
	"wizard/internal/exitcode"
	"wizard/internal/relver"
)

// Channel is which releases `wizard update` follows.
type Channel string

const (
	// ChannelStable follows full releases only: GitHub's "latest release".
	ChannelStable Channel = "stable"
	// ChannelPreRelease follows the newest release of any kind, so a beta or
	// release candidate is offered until a stable release passes it.
	ChannelPreRelease Channel = "pre-release"
)

const channelFileName = "update-channel"

// ParseChannel reads a channel name as a person types it.
func ParseChannel(s string) (Channel, error) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "stable":
		return ChannelStable, nil
	case "pre-release", "prerelease":
		return ChannelPreRelease, nil
	}
	return "", fmt.Errorf("unknown channel %q: use stable or pre-release", s)
}

// channelPath is where the chosen channel is remembered, or "" when the
// environment has no config directory (tests, --help).
func (e *Env) channelPath() string {
	if e.ConfigDir == "" {
		return ""
	}
	return filepath.Join(e.ConfigDir, channelFileName)
}

// Where the effective channel came from, for `wizard channel` and `doctor`.
const (
	sourceChosen  = "chosen"
	sourceImplied = "implied by this pre-release build"
	sourceDefault = "default"
)

// isPreReleaseBuild reports whether the running binary is a pre-release.
func isPreReleaseBuild() bool {
	v, err := relver.Parse(compat.BuildVersion)
	return err == nil && !v.Stable()
}

// resolveChannel decides which channel applies. An explicit choice wins. With
// none, a pre-release build stays on the pre-release channel, because falling
// back to stable would leave it "ahead" of every stable release and never
// update. Otherwise stable, the default for everyone.
//
// A setting file that cannot be understood is reported and ignored rather than
// trusted: guessing at it could move someone onto a channel they did not pick.
func resolveChannel(env *Env) (ch Channel, source string, problem error) {
	if path := env.channelPath(); path != "" {
		data, err := os.ReadFile(path)
		switch {
		case err == nil:
			chosen, perr := ParseChannel(string(data))
			if perr == nil {
				return chosen, sourceChosen, nil
			}
			problem = fmt.Errorf("%s is not a channel name (%q); ignoring it", path, strings.TrimSpace(string(data)))
		case !os.IsNotExist(err):
			problem = fmt.Errorf("could not read %s: %v; ignoring it", path, err)
		}
	}
	if isPreReleaseBuild() {
		return ChannelPreRelease, sourceImplied, problem
	}
	return ChannelStable, sourceDefault, problem
}

// saveChannel remembers ch as the explicit choice.
func saveChannel(env *Env, ch Channel) error {
	path := env.channelPath()
	if path == "" {
		return fmt.Errorf("no Wizard config directory to save the channel in")
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	// Write beside the file and rename over it. WriteFile truncates first, so a
	// `wizard update` or `wizard doctor` in another terminal could read the file
	// empty, call it "not a channel name" and fall back to stable for that run.
	tmp, err := os.CreateTemp(dir, ".update-channel-*") // created 0600
	if err != nil {
		return err
	}
	_, werr := tmp.WriteString(string(ch) + "\n")
	cerr := tmp.Close()
	if werr == nil {
		werr = cerr
	}
	if werr == nil {
		werr = os.Rename(tmp.Name(), path)
	}
	if werr != nil {
		_ = os.Remove(tmp.Name())
	}
	return werr
}

func describeChannel(ch Channel) string {
	if ch == ChannelPreRelease {
		return "every release, including pre-releases (beta, rc); a newer stable release still wins"
	}
	return "full releases only"
}

// RunChannel implements `wizard channel [stable|pre-release]`. It needs no
// checkout, so the installers can call it right after installing.
func RunChannel(out, errw io.Writer, args []string) int {
	env := &Env{Out: out, Err: errw}
	if dir, err := appdir.ConfigDir(); err == nil {
		env.ConfigDir = dir
	}
	fs := flag.NewFlagSet("channel", flag.ContinueOnError)
	fs.Usage = func() {
		fmt.Fprintln(fs.Output(), "Usage: wizard channel [stable|pre-release]")
		fmt.Fprintln(fs.Output(), "\nShow or change which releases `wizard update` follows.")
		fmt.Fprintln(fs.Output(), "  stable        full releases only (the default)")
		fmt.Fprintln(fs.Output(), "  pre-release   every release, including betas and release candidates")
	}
	if code, done := parseFlags(env, fs, args); done {
		return code
	}
	rest := fs.Args()
	if len(rest) > 1 {
		fmt.Fprintf(errw, "wizard channel takes at most one argument (got %d). See `wizard channel --help`.\n", len(rest))
		return exitcode.Usage
	}

	if len(rest) == 1 {
		want, err := ParseChannel(rest[0])
		if err != nil {
			fmt.Fprintln(errw, err)
			return exitcode.Usage
		}
		if err := saveChannel(env, want); err != nil {
			fmt.Fprintf(errw, "Could not save the update channel: %v\n", err)
			return exitcode.Failure
		}
		fmt.Fprintf(out, "Update channel set to %s: %s.\n", want, describeChannel(want))
		fmt.Fprintln(out, "Run `wizard update` to update on it now.")
		return exitcode.OK
	}

	ch, source, problem := resolveChannel(env)
	if problem != nil {
		fmt.Fprintf(errw, "warning: %v\n", problem)
	}
	fmt.Fprintf(out, "Update channel: %s (%s)\n", ch, source)
	fmt.Fprintf(out, "Follows: %s.\n", describeChannel(ch))
	if ch == ChannelStable {
		fmt.Fprintln(out, "Switch with `wizard channel pre-release`.")
	} else {
		fmt.Fprintln(out, "Switch back with `wizard channel stable`.")
	}
	return exitcode.OK
}
