package commands

import "testing"

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
