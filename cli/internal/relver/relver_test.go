package relver

import "testing"

func TestParseAcceptsOnlyTheTwoShapes(t *testing.T) {
	good := map[string]string{
		"1.0.14":          "1.0.14",
		"v1.0.14":         "1.0.14",
		" v1.0.14\n":      "1.0.14",
		"v1.0.14-beta.1":  "1.0.14-beta.1",
		"1.0.14-alpha.3":  "1.0.14-alpha.3",
		"v10.20.30-rc.12": "10.20.30-rc.12",
		"v0.0.1":          "0.0.1",
	}
	for in, want := range good {
		v, err := Parse(in)
		if err != nil {
			t.Errorf("Parse(%q): %v", in, err)
			continue
		}
		if v.String() != want {
			t.Errorf("Parse(%q).String() = %q, want %q", in, v.String(), want)
		}
	}

	bad := []string{
		"", "v", "1", "1.0", "1.0.14.1", "v01.0.14", "1.00.14", "1.0.014", "1.0.x", "-1.0.14", "1.0.-4",
		"1.0.14-", "1.0.14-beta", "1.0.14-beta.", "1.0.14-beta.0", "1.0.14-beta.01", "1.0.14-beta.1.2",
		"1.0.14-preview.1", "1.0.14-Beta.1", "1.0.14-beta.1+build5", "1.0.14+build5", "1.0.14-beta.x",
		"latest", "nightly", "1.0.14-beta.99999999999999999999",
	}
	for _, in := range bad {
		if v, err := Parse(in); err == nil {
			t.Errorf("Parse(%q) = %v, want an error", in, v)
		}
	}
}

func TestCompareFollowsSemverPrecedence(t *testing.T) {
	// Each entry is strictly older than the next.
	order := []string{
		"1.0.13",
		"1.0.14-alpha.1", "1.0.14-alpha.2",
		"1.0.14-beta.1", "1.0.14-beta.2", "1.0.14-beta.10",
		"1.0.14-rc.1", "1.0.14-rc.2",
		"1.0.14",
		"1.0.15-beta.1",
		"1.0.15",
		"1.1.0-beta.1",
		"1.1.0",
		"2.0.0-alpha.1",
	}
	for i := range order {
		for j := range order {
			a, _ := Parse(order[i])
			b, _ := Parse(order[j])
			want := sign(i - j)
			if got := Compare(a, b); got != want {
				t.Errorf("Compare(%s, %s) = %d, want %d", order[i], order[j], got, want)
			}
		}
	}
}

func TestBetaTenIsNewerThanBetaTwo(t *testing.T) {
	// A string sort gets this wrong; the numeric compare must not.
	a, _ := Parse("1.0.14-beta.2")
	b, _ := Parse("1.0.14-beta.10")
	if Compare(a, b) != -1 {
		t.Fatal("beta.2 must be older than beta.10")
	}
}

func TestTagRoundTrips(t *testing.T) {
	for _, in := range []string{"v1.0.14", "v1.0.14-beta.1", "v1.0.14-rc.7"} {
		v, err := Parse(in)
		if err != nil || v.Tag() != in {
			t.Errorf("Parse(%q).Tag() = %q, %v", in, v.Tag(), err)
		}
	}
}

func TestStable(t *testing.T) {
	stable, _ := Parse("1.0.14")
	pre, _ := Parse("1.0.14-rc.1")
	if !stable.Stable() || pre.Stable() {
		t.Fatalf("Stable() wrong: stable=%v pre=%v", stable.Stable(), pre.Stable())
	}
}
