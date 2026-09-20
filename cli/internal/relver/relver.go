// Package relver parses and orders Wizard release versions.
//
// Two shapes exist, and nothing else is accepted:
//
//	stable       vMAJOR.MINOR.PATCH               v1.0.14
//	pre-release  vMAJOR.MINOR.PATCH-KIND.N        v1.0.14-beta.1
//
// where KIND is alpha, beta or rc and N is a positive number without leading
// zeros. The grammar is deliberately narrower than SemVer so that a malformed
// or unexpected tag can never be mistaken for a newer release, and it is the
// same grammar scripts/release.py, install.sh and install.ps1 enforce.
//
// Ordering follows SemVer: 1.0.14-alpha.2 < 1.0.14-beta.1 < 1.0.14-beta.2 <
// 1.0.14-rc.1 < 1.0.14, and any 1.0.14-x is older than 1.0.15-x.
package relver

import (
	"fmt"
	"strconv"
	"strings"
)

// kindRank orders the pre-release kinds. A kind not listed is not a release.
var kindRank = map[string]int{"alpha": 1, "beta": 2, "rc": 3}

// Version is a parsed release version.
type Version struct {
	Major, Minor, Patch int
	// Kind is "" for a stable release, otherwise alpha, beta or rc.
	Kind string
	// N numbers the pre-release within its kind (beta.1, beta.2); 0 when stable.
	N int
}

// Parse reads a release version or tag. A leading "v" is optional.
func Parse(raw string) (Version, error) {
	s := strings.TrimPrefix(strings.TrimSpace(raw), "v")
	if s == "" {
		return Version{}, fmt.Errorf("empty version")
	}
	if strings.Contains(s, "+") {
		return Version{}, fmt.Errorf("build metadata (+) is not allowed in a release version")
	}
	core, pre, hasPre := strings.Cut(s, "-")
	parts := strings.Split(core, ".")
	if len(parts) != 3 {
		return Version{}, fmt.Errorf("expected MAJOR.MINOR.PATCH")
	}
	var nums [3]int
	for i, part := range parts {
		n, err := number(part)
		if err != nil {
			return Version{}, err
		}
		nums[i] = n
	}
	v := Version{Major: nums[0], Minor: nums[1], Patch: nums[2]}
	if !hasPre {
		return v, nil
	}
	kind, numText, ok := strings.Cut(pre, ".")
	if !ok || strings.Contains(numText, ".") {
		return Version{}, fmt.Errorf("a pre-release must look like -beta.1, got -%s", pre)
	}
	if _, known := kindRank[kind]; !known {
		return Version{}, fmt.Errorf("unknown pre-release kind %q (use alpha, beta or rc)", kind)
	}
	n, err := number(numText)
	if err != nil {
		return Version{}, err
	}
	if n < 1 {
		return Version{}, fmt.Errorf("a pre-release number starts at 1, got %d", n)
	}
	v.Kind, v.N = kind, n
	return v, nil
}

// number parses one numeric component: digits only, no leading zeros.
func number(s string) (int, error) {
	if s == "" || (len(s) > 1 && s[0] == '0') {
		return 0, fmt.Errorf("invalid component %q", s)
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return 0, fmt.Errorf("invalid component %q", s)
		}
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return 0, fmt.Errorf("invalid component %q", s)
	}
	return n, nil
}

// Stable reports whether v is a full release rather than a pre-release.
func (v Version) Stable() bool { return v.Kind == "" }

// String is the version without the "v": 1.0.14 or 1.0.14-beta.1.
func (v Version) String() string {
	s := fmt.Sprintf("%d.%d.%d", v.Major, v.Minor, v.Patch)
	if !v.Stable() {
		s += fmt.Sprintf("-%s.%d", v.Kind, v.N)
	}
	return s
}

// Tag is the git tag for v: v1.0.14 or v1.0.14-beta.1.
func (v Version) Tag() string { return "v" + v.String() }

// Compare returns -1, 0 or 1 as a is older than, equal to or newer than b.
func Compare(a, b Version) int {
	for _, pair := range [][2]int{{a.Major, b.Major}, {a.Minor, b.Minor}, {a.Patch, b.Patch}} {
		if pair[0] != pair[1] {
			return sign(pair[0] - pair[1])
		}
	}
	switch {
	case a.Stable() && b.Stable():
		return 0
	case a.Stable():
		return 1 // 1.0.14 is newer than any 1.0.14-x
	case b.Stable():
		return -1
	}
	if a.Kind != b.Kind {
		return sign(kindRank[a.Kind] - kindRank[b.Kind])
	}
	return sign(a.N - b.N)
}

func sign(n int) int {
	switch {
	case n < 0:
		return -1
	case n > 0:
		return 1
	}
	return 0
}
