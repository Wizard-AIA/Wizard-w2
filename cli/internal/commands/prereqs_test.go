package commands

import (
	"fmt"
	"regexp"
	"runtime"
	"strings"
	"testing"
)

// The rule these tables exist to keep: the minimum is a floor, not a target.
// What init installs for a missing tool is the current release; the
// minimum-version package is only a fallback, and it is derived from the same
// constants the checks use so raising a minimum in one place updates both.
func TestInstallTablesPreferTheCurrentReleaseAndFallBackToTheMinimum(t *testing.T) {
	versioned := regexp.MustCompile(`(@\d|\.\d+(\.\d+)?$)`)
	for _, p := range brewFormulae {
		if versioned.MatchString(p.formulae[0]) {
			t.Errorf("%s: the first Homebrew candidate %q pins a version; the current release must come first", p.name, p.formulae[0])
		}
	}
	for _, p := range wingetPackages {
		if versioned.MatchString(p.id) {
			t.Errorf("winget id %q pins a version", p.id)
		}
	}
	find := func(name string) []string {
		for _, p := range brewFormulae {
			if p.name == name {
				return p.formulae
			}
		}
		t.Fatalf("no Homebrew entry for %s", name)
		return nil
	}
	if got := find("Python"); len(got) != 2 || got[1] != "python@"+fmt.Sprintf("%d.%d", minPythonMajor, minPythonMinor) {
		t.Errorf("Python fallback %v is not derived from the minimum", got)
	}
	if got := find("Node.js"); len(got) != 2 || got[1] != fmt.Sprintf("node@%d", minNodeMajor) {
		t.Errorf("Node fallback %v is not derived from the minimum", got)
	}
	if wingetPythonFallback != fmt.Sprintf("Python.Python.%d.%d", minPythonMajor, minPythonMinor) {
		t.Errorf("winget Python fallback %q is not derived from the minimum", wingetPythonFallback)
	}
}

// A keg-only fallback (node@<min>) is invisible to PATH unless its opt/ bin
// directory is searched; this is the exact failure that broke `wizard init` on
// a clean Mac.
func TestToolPathsCoverKegOnlyNodeFallback(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Homebrew locations are not searched on Windows")
	}
	want := fmt.Sprintf("/opt/homebrew/opt/node@%d/bin", minNodeMajor)
	for _, p := range platformToolPaths("/home/u") {
		if p == want {
			return
		}
	}
	t.Fatalf("platformToolPaths does not include %s", want)
}

func TestAnyVersionAtOrAboveTheMinimumPasses(t *testing.T) {
	for _, v := range [][2]int{{3, 12}, {3, 13}, {3, 14}, {3, 20}, {4, 0}} {
		if c := finishCheck(ToolCheck{}, v, "x", minPythonMajor, minPythonMinor); !c.OK {
			t.Errorf("Python %d.%d should satisfy the %d.%d minimum", v[0], v[1], minPythonMajor, minPythonMinor)
		}
	}
	if c := finishCheck(ToolCheck{}, [2]int{3, 11}, "3.11", minPythonMajor, minPythonMinor); c.OK {
		t.Error("Python 3.11 is below the minimum")
	}
	for _, major := range []int{20, 22, 24, 26} {
		if c := finishCheck(ToolCheck{}, [2]int{major, 0}, "x", minNodeMajor, 0); !c.OK {
			t.Errorf("Node %d should satisfy the %d minimum", major, minNodeMajor)
		}
	}
	if c := finishCheck(ToolCheck{}, [2]int{18, 19}, "18.19", minNodeMajor, 0); c.OK {
		t.Error("Node 18 is below the minimum")
	}
}

// Hints tell a person what to run themselves, so they must not steer anyone
// onto a specific old release: they name the package and state the floor.
func TestInstallHintsNameNoPinnedVersion(t *testing.T) {
	hints := map[string]string{"python": pythonInstallHint(), "node": nodeInstallHint(), "uv": uvInstallHint(), "pnpm": pnpmInstallHint()}
	for name, hint := range hints {
		for _, pinned := range []string{"python@3", "node@", "Python.Python.3", "python3.12"} {
			if strings.Contains(hint, pinned) {
				t.Errorf("%s hint %q names a pinned version (%s)", name, hint, pinned)
			}
		}
	}
	if !strings.Contains(pythonInstallHint(), fmt.Sprintf("%d.%d", minPythonMajor, minPythonMinor)) {
		t.Error("the Python hint should state the minimum")
	}
}

func TestRequiresOllamaOnlyWhenConfigurationNeedsIt(t *testing.T) {
	tests := []struct {
		name              string
		provider          string
		dataMode          string
		embeddingProvider string
		want              bool
	}{
		{name: "cloud gemini skips ollama", provider: "gemini", dataMode: "cloud-only", embeddingProvider: "gemini", want: false},
		{name: "cloud anthropic skips ollama for auto embeddings", provider: "anthropic", dataMode: "cloud-only", embeddingProvider: "auto", want: false},
		{name: "local ollama requires ollama", provider: "ollama", dataMode: "local-only", embeddingProvider: "auto", want: true},
		{name: "local lm studio does not require ollama", provider: "lmstudio", dataMode: "local-only", embeddingProvider: "auto", want: false},
		{name: "ollama embeddings require ollama", provider: "gemini", dataMode: "cloud-only", embeddingProvider: "ollama", want: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := requiresOllama(tt.provider, tt.dataMode, tt.embeddingProvider); got != tt.want {
				t.Fatalf("requiresOllama(%q, %q, %q) = %v, want %v", tt.provider, tt.dataMode, tt.embeddingProvider, got, tt.want)
			}
		})
	}
}

func TestRequiredPrerequisitesReturnsOnlyFailedChecks(t *testing.T) {
	python := ToolCheck{Name: "Python", OK: true}
	node := ToolCheck{Name: "Node.js", OK: false}
	uv := ToolCheck{Name: "uv", OK: true}
	pnpm := ToolCheck{Name: "pnpm", OK: false}

	missing := requiredPrerequisites(python, node, uv, pnpm)
	if len(missing) != 2 || missing[0].Name != "Node.js" || missing[1].Name != "pnpm" {
		t.Fatalf("requiredPrerequisites returned %#v, want Node.js and pnpm", missing)
	}
}

func TestEmbeddingProviderOptionsExcludeUnsupportedCloudProviders(t *testing.T) {
	options := embeddingProviderOptions()
	for _, option := range options {
		if option == "anthropic" {
			t.Fatal("Anthropic must not be offered as an embedding provider")
		}
	}
	for _, option := range []string{"auto", "ollama", "lmstudio", "openai", "gemini", "custom_gateway"} {
		found := false
		for _, candidate := range options {
			if candidate == option {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("embedding provider option %q is missing from %v", option, options)
		}
	}
}
