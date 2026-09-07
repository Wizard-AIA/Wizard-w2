// Package compat guards against running this binary against a backend
// checkout it was not built for.
//
// There is no new backend field for this: backend/src/api/routes/meta.py
// already reports API_VERSION (currently "4.0.0", the w2 generation bump)
// from both GET /health and GET /api/config. CompatAPIVersion below is this
// binary's own build-time opinion of that number, set via
// `-ldflags "-X wizard/internal/compat.CompatAPIVersion=4.0.0"`
// (see cli/README.md) with the literal here as the fallback for a plain
// `go build`. Only the major component is compared, so a routine backend
// patch/minor bump does not force a CLI rebuild -- only a change large enough
// that the backend's own maintainers bumped the major version does (as w1 ->
// w2 did: a w1-era binary compiled with CompatAPIVersion="3.1.0" correctly
// refuses to pair with a w2 backend).
package compat

import (
	"fmt"
	"strconv"
	"strings"
)

// CompatAPIVersion is overridden at build time via -ldflags -X. Keep this
// literal in sync with API_VERSION in backend/src/api/routes/meta.py when
// building without ldflags (e.g. `go build` during local development).
var CompatAPIVersion = "4.0.0"

// BuildVersion identifies this CLI release. The release build stamps it with
// the immutable Git tag; a plain local build remains explicitly "dev" so it
// is never mistaken for a published artifact.
var BuildVersion = "dev"

// Major returns the leading numeric component of a dotted version string,
// e.g. "3.1.0" -> 3. An unparsable string yields an error rather than a
// silent 0, since a 0 would compare as "older than everything" and mask the
// real problem (a backend that changed its version format).
func Major(version string) (int, error) {
	first := strings.SplitN(strings.TrimSpace(version), ".", 2)[0]
	n, err := strconv.Atoi(first)
	if err != nil {
		return 0, fmt.Errorf("could not parse a major version from %q: %w", version, err)
	}
	return n, nil
}

// Mismatch reports whether the backend's reported version and this binary's
// compiled-in compat version disagree at the major-version level.
func Mismatch(backendVersion string) (mismatched bool, err error) {
	backendMajor, err := Major(backendVersion)
	if err != nil {
		return false, err
	}
	compiledMajor, err := Major(CompatAPIVersion)
	if err != nil {
		return false, err
	}
	return backendMajor != compiledMajor, nil
}
