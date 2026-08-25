package commands

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeBackendEnv(t *testing.T, backendDir, content string) string {
	t.Helper()
	if err := os.MkdirAll(backendDir, 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(backendDir, ".env")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestApplyProviderConfigWritesOnlyGivenFields(t *testing.T) {
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, "APP_NAME=Wizard\nAPI_PROVIDER=ollama\n")
	env := newTestEnv(t, backendDir)

	err := applyProviderConfig(env, providerConfig{provider: "anthropic", anthropicKey: "sk-ant-test"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if v, _, _ := readEnvValue(env.BackendEnvPath(), "API_PROVIDER"); v != "anthropic" {
		t.Fatalf("API_PROVIDER = %q, want %q", v, "anthropic")
	}
	if v, _, _ := readEnvValue(env.BackendEnvPath(), "ANTHROPIC_API_KEY"); v != "sk-ant-test" {
		t.Fatalf("ANTHROPIC_API_KEY = %q, want %q", v, "sk-ant-test")
	}
	// DATA_MODE was never set on the config -- must not appear at all.
	if _, found, _ := readEnvValue(env.BackendEnvPath(), "DATA_MODE"); found {
		t.Fatal("DATA_MODE should not have been written when the flag was empty")
	}
	// An unrelated existing key must survive untouched.
	if v, found, _ := readEnvValue(env.BackendEnvPath(), "APP_NAME"); !found || v != "Wizard" {
		t.Fatalf("APP_NAME got (%q, %v), want (\"Wizard\", true)", v, found)
	}
}

func TestApplyProviderConfigAppliesAgainstAnExistingFile(t *testing.T) {
	// Unlike ensureEnvFile, applyProviderConfig must take effect even when
	// backend/.env already exists and was not just created by this run --
	// an explicit --provider/--data-mode flag is a deliberate instruction to
	// reconfigure an already-set-up install, e.g. switching it to cloud.
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, "API_PROVIDER=ollama\nDATA_MODE=local-only\n")
	env := newTestEnv(t, backendDir)

	if err := applyProviderConfig(env, providerConfig{provider: "openai", dataMode: "cloud-only", openaiKey: "sk-test"}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if v, _, _ := readEnvValue(env.BackendEnvPath(), "API_PROVIDER"); v != "openai" {
		t.Fatalf("API_PROVIDER = %q, want %q", v, "openai")
	}
	if v, _, _ := readEnvValue(env.BackendEnvPath(), "DATA_MODE"); v != "cloud-only" {
		t.Fatalf("DATA_MODE = %q, want %q", v, "cloud-only")
	}
	if v, _, _ := readEnvValue(env.BackendEnvPath(), "OPENAI_API_KEY"); v != "sk-test" {
		t.Fatalf("OPENAI_API_KEY = %q, want %q", v, "sk-test")
	}
}

func TestApplyProviderConfigNeverBlanksAnExistingKey(t *testing.T) {
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, `ANTHROPIC_API_KEY="sk-ant-already-set"`+"\n")
	env := newTestEnv(t, backendDir)

	if err := applyProviderConfig(env, providerConfig{provider: "anthropic"}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if v, _, _ := readEnvValue(env.BackendEnvPath(), "ANTHROPIC_API_KEY"); v != "sk-ant-already-set" {
		t.Fatalf("ANTHROPIC_API_KEY = %q, want the pre-existing key left untouched", v)
	}
}

func TestApplyProviderConfigBaseURLGoesToTheRightKey(t *testing.T) {
	cases := []struct {
		provider string
		wantKey  string
	}{
		{"ollama", "OLLAMA_BASE_URL"},
		{"", "OLLAMA_BASE_URL"},
		{"lmstudio", "LMSTUDIO_BASE_URL"},
		{"anthropic", "ANTHROPIC_BASE_URL"},
		{"openai", "OPENAI_BASE_URL"},
		{"gemini", "GEMINI_BASE_URL"},
	}
	for _, c := range cases {
		backendDir := filepath.Join(t.TempDir(), "backend")
		writeBackendEnv(t, backendDir, "APP_NAME=Wizard\n")
		env := newTestEnv(t, backendDir)

		if err := applyProviderConfig(env, providerConfig{provider: c.provider, baseURL: "https://proxy.example/v1"}); err != nil {
			t.Fatalf("provider=%q: unexpected error: %v", c.provider, err)
		}
		if v, found, _ := readEnvValue(env.BackendEnvPath(), c.wantKey); !found || v != "https://proxy.example/v1" {
			t.Fatalf("provider=%q: %s got (%q, %v), want (\"https://proxy.example/v1\", true)", c.provider, c.wantKey, v, found)
		}
	}
}

func TestApplyProviderConfigBaseURLRejectsCustomGateway(t *testing.T) {
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, "APP_NAME=Wizard\n")
	env := newTestEnv(t, backendDir)

	err := applyProviderConfig(env, providerConfig{provider: "custom_gateway", baseURL: "https://gateway.example/v1"})
	if err == nil {
		t.Fatal("expected an error: custom_gateway's URL is --gateway-url, not --base-url")
	}
}

func TestNeedsOptionalRequirements(t *testing.T) {
	cases := []struct {
		name     string
		content  string
		expected bool
	}{
		{"no file at all", "", false},
		{"default ollama provider", "API_PROVIDER=ollama\n", false},
		{"empty provider, local-only mode", "DATA_MODE=local-only\n", false},
		{"anthropic provider", "API_PROVIDER=anthropic\n", true},
		{"openai provider", "API_PROVIDER=openai\n", true},
		{"gemini provider", "API_PROVIDER=gemini\n", true},
		{"custom_gateway provider", "API_PROVIDER=custom_gateway\n", true},
		{"lmstudio provider", "API_PROVIDER=lmstudio\n", true},
		{"hybrid data mode, default provider", "DATA_MODE=hybrid\n", true},
		{"cloud-only data mode, default provider", "DATA_MODE=cloud-only\n", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			backendDir := filepath.Join(t.TempDir(), "backend")
			env := newTestEnv(t, backendDir)
			if c.content != "" {
				writeBackendEnv(t, backendDir, c.content)
			}
			if got := needsOptionalRequirements(env); got != c.expected {
				t.Fatalf("needsOptionalRequirements() = %v, want %v", got, c.expected)
			}
		})
	}
}

func TestWarnMissingCloudConfigNamesTheMissingKey(t *testing.T) {
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, "API_PROVIDER=anthropic\n")
	env := newTestEnv(t, backendDir)

	warnMissingCloudConfig(env, "anthropic")

	out := env.Out.(interface{ String() string }).String()
	if !strings.Contains(out, "ANTHROPIC_API_KEY") || !strings.Contains(out, "--anthropic-key") {
		t.Fatalf("expected a notice naming ANTHROPIC_API_KEY/--anthropic-key, got: %s", out)
	}
}

func TestWarnMissingCloudConfigSilentWhenKeyPresent(t *testing.T) {
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, `API_PROVIDER=anthropic
ANTHROPIC_API_KEY="sk-ant-already-set"
`)
	env := newTestEnv(t, backendDir)

	warnMissingCloudConfig(env, "anthropic")

	out := env.Out.(interface{ String() string }).String()
	if out != "" {
		t.Fatalf("expected no warning once the key is set, got: %s", out)
	}
}

func TestWarnMissingCloudConfigSilentForLocalProviders(t *testing.T) {
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, "API_PROVIDER=ollama\n")
	env := newTestEnv(t, backendDir)

	warnMissingCloudConfig(env, "ollama")

	out := env.Out.(interface{ String() string }).String()
	if out != "" {
		t.Fatalf("ollama needs no key; expected no warning, got: %s", out)
	}
}
