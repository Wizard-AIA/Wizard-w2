package commands

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"
)

func TestShouldPromptInitOnlyForBareInteractiveRuns(t *testing.T) {
	if shouldPromptInit(strings.NewReader(""), false, false, false) {
		t.Fatal("a non-terminal reader must not trigger prompts")
	}
	if shouldPromptInit(strings.NewReader(""), false, false, true) {
		t.Fatal("configuration flags must preserve non-interactive behavior")
	}
	if !shouldPromptInit(strings.NewReader(""), true, false, true) {
		t.Fatal("--interactive must force prompts")
	}
	if shouldPromptInit(strings.NewReader(""), true, true, false) {
		t.Fatal("--non-interactive must win over --interactive")
	}
}

func TestPromptInitSettingsCapturesProviderModeModelsAndCredentials(t *testing.T) {
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, "API_PROVIDER=ollama\nDATA_SCHEMA_ONLY=true\n")
	out := &bytes.Buffer{}
	env := newTestEnv(t, backendDir)
	env.In = strings.NewReader("4\n3\n\nqwen3:8b\nqwen2.5-coder:7b\n1\ntext-embedding-3-small\nsk-openai-test\nhttps://proxy.example/v1\n")
	env.Out = out
	settings := initSettings{}

	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if settings.provider != "openai" || settings.dataMode != "hybrid" || settings.dataSchemaOnly != "true" {
		t.Fatalf("unexpected provider settings: %#v", settings.providerConfig)
	}
	if !settings.managerModelSet || settings.managerModel != "qwen3:8b" || !settings.workerModelSet || settings.workerModel != "qwen2.5-coder:7b" {
		t.Fatalf("unexpected model settings: %#v", settings)
	}
	if settings.embeddingProvider != "" || settings.embeddingModel != "text-embedding-3-small" {
		t.Fatalf("unexpected embedding settings: %#v", settings.providerConfig)
	}
	if settings.openaiKey != "sk-openai-test" || settings.baseURL != "https://proxy.example/v1" {
		t.Fatalf("unexpected credentials/base URL: %#v", settings.providerConfig)
	}
	if !strings.Contains(out.String(), "Default provider") || !strings.Contains(out.String(), "OpenAI API key") {
		t.Fatalf("expected interactive prompts, got: %s", out.String())
	}
}

func TestPromptInitSettingsKeepsAutoDefaults(t *testing.T) {
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, "")
	env := newTestEnv(t, backendDir)
	env.In = strings.NewReader("\n\n\n\n\n\n\n\n")
	env.Out = &bytes.Buffer{}
	settings := initSettings{}

	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if settings.provider != "ollama" || settings.dataMode != "" || settings.dataSchemaOnly != "true" {
		t.Fatalf("unexpected defaults: %#v", settings.providerConfig)
	}
	if settings.managerModelSet || settings.workerModelSet || settings.embeddingModel != "" {
		t.Fatalf("blank model answers must retain auto-select defaults: %#v", settings)
	}
}

func TestApplyTerminalKeyMovesAndAcceptsChoices(t *testing.T) {
	tests := []struct {
		name     string
		key      []byte
		selected int
		want     int
		accept   bool
	}{
		{"down", []byte("\x1b[B"), 0, 1, false},
		{"up wraps", []byte("\x1b[A"), 0, 2, false},
		{"enter", []byte("\r"), 1, 1, true},
		{"numeric fallback", []byte("2"), 0, 1, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, accepted := applyTerminalKey(tt.key, tt.selected, 3)
			if got != tt.want || accepted != tt.accept {
				t.Fatalf("applyTerminalKey(%q, %d, 3) = (%d, %v), want (%d, %v)", tt.key, tt.selected, got, accepted, tt.want, tt.accept)
			}
		})
	}
}
