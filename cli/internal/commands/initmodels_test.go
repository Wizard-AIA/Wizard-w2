package commands

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"path/filepath"
	"strings"
	"testing"
)

// withDiscovery turns on model discovery for a test and stubs the network. The
// stub receives every lookup so tests can also assert what was sent.
func withDiscovery(t *testing.T, stub func(src modelSource) (modelList, error)) *[]modelSource {
	t.Helper()
	oldEnabled, oldDiscover := initDiscoveryEnabled, initModelDiscovery
	var calls []modelSource
	initDiscoveryEnabled = func(io.Reader) bool { return true }
	initModelDiscovery = func(_ context.Context, src modelSource) (modelList, error) {
		calls = append(calls, src)
		return stub(src)
	}
	t.Cleanup(func() { initDiscoveryEnabled, initModelDiscovery = oldEnabled, oldDiscover })
	return &calls
}

func initEnv(t *testing.T, saved, input string) (*Env, *bytes.Buffer) {
	t.Helper()
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, saved)
	env := newTestEnv(t, backendDir)
	out := &bytes.Buffer{}
	env.In = strings.NewReader(input)
	env.Out = out
	return env, out
}

// The point of the feature: the user picks from what the provider offers.
func TestInitOffersOnlyModelsTheProviderReports(t *testing.T) {
	calls := withDiscovery(t, func(src modelSource) (modelList, error) {
		return modelList{
			Chat:      []string{"gemini-2.5-flash", "gemini-2.5-pro"},
			Embedding: []string{"gemini-embedding-001"},
		}, nil
	})
	// provider=gemini(5); key; mode; schema; manager: option 3 (auto=1,
	// flash=2, pro=3); worker: option 2; embedding provider; embedding: option 2
	// (auto=1, gemini-embedding-001=2).
	env, out := initEnv(t, "", "5\nfake-gemini-key-1234567890\n\n\n3\n2\n\n2\n")
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatal(err)
	}
	if settings.managerModel != "gemini-2.5-pro" || settings.workerModel != "gemini-2.5-flash" {
		t.Fatalf("manager/worker = %q/%q, want the chosen listed models", settings.managerModel, settings.workerModel)
	}
	if settings.embeddingModel != "gemini-embedding-001" {
		t.Fatalf("embedding model = %q", settings.embeddingModel)
	}
	if len(*calls) == 0 || (*calls)[0].APIKey != "fake-gemini-key-1234567890" || (*calls)[0].Provider != "gemini" {
		t.Fatalf("discovery must be asked with the key just entered: %+v", *calls)
	}
	got := out.String()
	for _, want := range []string{"key accepted", "2 chat model(s)", autoModelLabel, "gemini-2.5-pro"} {
		if !strings.Contains(got, want) {
			t.Errorf("output missing %q:\n%s", want, got)
		}
	}
}

// Nobody should be asked to type an endpoint or a model name for a cloud
// provider: both come from the provider.
func TestInitNeverAsksACloudUserForAnEndpointOrAModelName(t *testing.T) {
	for _, provider := range []struct{ choice, name string }{{"3", "anthropic"}, {"4", "openai"}, {"5", "gemini"}} {
		withDiscovery(t, func(modelSource) (modelList, error) {
			return modelList{Chat: []string{"model-a", "model-b"}, Embedding: []string{"embed-a"}}, nil
		})
		env, out := initEnv(t, "", provider.choice+"\nfake-key-1234567890\n\n\n1\n1\n\n1\n")
		settings := initSettings{}
		if err := promptInitSettings(env, &settings); err != nil {
			t.Fatalf("%s: %v", provider.name, err)
		}
		got := strings.ToLower(out.String())
		for _, forbidden := range []string{"base url", "provider url", "endpoint", "enter a model name", "type a model"} {
			if strings.Contains(got, forbidden) {
				t.Errorf("%s: init must not ask for %q:\n%s", provider.name, forbidden, out.String())
			}
		}
		if settings.baseURL != "" {
			t.Errorf("%s: no endpoint should have been collected, got %q", provider.name, settings.baseURL)
		}
	}
}

func TestInitNeverPrintsTheKeyOnlyAReceipt(t *testing.T) {
	withDiscovery(t, func(modelSource) (modelList, error) { return modelList{Chat: []string{"m"}}, nil })
	const key = "fake-openai-secret-key-abcdefghij"
	env, out := initEnv(t, "", "4\n"+key+"\n\n\n1\n1\n\n")
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(out.String(), key) {
		t.Fatalf("the API key must never be printed:\n%s", out.String())
	}
	if !strings.Contains(out.String(), "…ghij") {
		t.Fatalf("the summary should prove the key arrived (length and last characters):\n%s", out.String())
	}
}

func TestInitRejectedKeyOffersReEntryAndUsesTheNewKey(t *testing.T) {
	withDiscovery(t, func(src modelSource) (modelList, error) {
		if src.APIKey == "bad-key-0000000000" {
			return modelList{}, fmt.Errorf("%w (HTTP 401)", errKeyRejected)
		}
		return modelList{Chat: []string{"gpt-4.1"}}, nil
	})
	// provider=openai(4); bad key; discovery rejects it, so "What now?" -> 1
	// (re-enter) and the new key; then mode, schema, manager 2 (gpt-4.1),
	// worker 1, embedding provider (no embedding models: the step is skipped).
	env, out := initEnv(t, "", "4\nbad-key-0000000000\n1\ngood-key-1111111111\n\n\n2\n1\n\n")
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatal(err)
	}
	if settings.openaiKey != "good-key-1111111111" {
		t.Fatalf("openaiKey = %q, want the re-entered key", settings.openaiKey)
	}
	if !strings.Contains(out.String(), "rejected") {
		t.Fatalf("the user must be told the key was rejected:\n%s", out.String())
	}
	if settings.managerModel != "gpt-4.1" {
		t.Fatalf("after re-entry the models must come from the working key, manager = %q", settings.managerModel)
	}
}

// With no list to offer, init does not fall back to asking someone to type a
// model name blind: it leaves the choice on auto-select and says where to change it.
func TestInitContinueAnywayLeavesModelsOnAutoInsteadOfAskingToType(t *testing.T) {
	withDiscovery(t, func(modelSource) (modelList, error) { return modelList{}, fmt.Errorf("%w (HTTP 403)", errKeyRejected) })
	// key, "What now?" -> 2 (continue anyway); then mode, schema, embedding provider.
	env, out := initEnv(t, "", "4\nkey-key-key-key-key\n2\n\n\n\n")
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatal(err)
	}
	if settings.managerModelSet || settings.workerModelSet || settings.managerModel != "" {
		t.Fatalf("no model may be set from an unverified guess: %+v", settings)
	}
	if !strings.Contains(out.String(), "Models page") {
		t.Fatalf("should say where to choose models later:\n%s", out.String())
	}
}

func TestInitUnreachableOllamaAsksForItsAddressAndRetries(t *testing.T) {
	calls := withDiscovery(t, func(src modelSource) (modelList, error) {
		if src.BaseURL == "" {
			return modelList{}, fmt.Errorf("%w: connection refused", errProviderUnreachable)
		}
		return modelList{Chat: []string{"qwen3:8b"}}, nil
	})
	// provider default (ollama); the default address fails, so the address is
	// asked and answered; mode; schema; manager 2 (qwen3:8b); worker 1;
	// embedding provider; embedding model 1 (auto; only a starter is offered).
	env, out := initEnv(t, "", "\nhttp://gpu-box:11434\n\n\n2\n1\n\n1\n")
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatal(err)
	}
	if settings.baseURL != "http://gpu-box:11434" {
		t.Fatalf("the address the user gave must be kept for backend/.env, got %q", settings.baseURL)
	}
	if settings.managerModel != "qwen3:8b" {
		t.Fatalf("models must come from the retried address, manager = %q", settings.managerModel)
	}
	if len(*calls) != 2 || (*calls)[1].BaseURL != "http://gpu-box:11434" {
		t.Fatalf("expected a retry against the new address: %+v", *calls)
	}
	if !strings.Contains(out.String(), "ollama serve") {
		t.Fatalf("should tell the user how to start Ollama:\n%s", out.String())
	}
}

func TestInitUnreachableOllamaEnterSkipsWithoutBlockingSetup(t *testing.T) {
	calls := withDiscovery(t, func(modelSource) (modelList, error) {
		return modelList{}, fmt.Errorf("%w: connection refused", errProviderUnreachable)
	})
	env, _ := initEnv(t, "", "\n\n\n\n\n\n\n")
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatalf("an unreachable local server must not fail init: %v", err)
	}
	if len(*calls) != 1 {
		t.Fatalf("pressing Enter must not retry: %+v", *calls)
	}
}

func TestInitHybridPairsACloudProviderAndVerifiesItsKey(t *testing.T) {
	calls := withDiscovery(t, func(src modelSource) (modelList, error) {
		if src.Provider == "gemini" && src.APIKey != "fake-gemini-key-1234" {
			t.Errorf("the hybrid cloud lookup must use the key just entered, got %q", src.APIKey)
		}
		return modelList{Chat: []string{"m1"}}, nil
	})
	// provider default (ollama); mode 3 (hybrid); cloud provider 4 (gemini:
	// skip=1, anthropic=2, openai=3, gemini=4); its key; schema; manager 1;
	// worker 1; embedding provider; embedding model 1.
	env, out := initEnv(t, "", "\n3\n4\nfake-gemini-key-1234\n\n1\n1\n\n1\n")
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatal(err)
	}
	if settings.provider != "ollama" || settings.dataMode != "hybrid" {
		t.Fatalf("the local provider stays the default in hybrid: %+v", settings.providerConfig)
	}
	if settings.geminiKey != "fake-gemini-key-1234" {
		t.Fatalf("the hybrid cloud key was not captured: %+v", settings.providerConfig)
	}
	sawGemini := false
	for _, c := range *calls {
		sawGemini = sawGemini || c.Provider == "gemini"
	}
	if !sawGemini {
		t.Fatal("the hybrid cloud key must be verified against its provider")
	}
	if strings.Contains(strings.ToLower(out.String()), "base url") {
		t.Fatalf("no endpoint question:\n%s", out.String())
	}
}

func TestInitHybridCloudCanBeSkipped(t *testing.T) {
	withDiscovery(t, func(modelSource) (modelList, error) { return modelList{Chat: []string{"m1"}}, nil })
	env, _ := initEnv(t, "", "\n3\n\n\n1\n1\n\n1\n") // hybrid, then accept "skip"
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatal(err)
	}
	if settings.geminiKey != "" || settings.anthropicKey != "" || settings.openaiKey != "" {
		t.Fatalf("skipping must not collect a key: %+v", settings.providerConfig)
	}
}

func TestInitFreshOllamaSuggestsStarterModelsMarkedNotInstalled(t *testing.T) {
	withDiscovery(t, func(modelSource) (modelList, error) { return modelList{}, nil })
	env, out := initEnv(t, "", "\n\n\n\n\n\n\n\n\n") // accept every default
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatal(err)
	}
	got := out.String()
	if !strings.Contains(got, "no chat models are installed yet") {
		t.Fatalf("an empty Ollama must be called out:\n%s", got)
	}
	if !strings.Contains(got, "(not installed yet)") {
		t.Fatalf("starter suggestions must be marked as not installed:\n%s", got)
	}
	if settings.managerModelSet || settings.workerModelSet {
		t.Fatal("accepting the default (auto-select) must not pin a model")
	}
}

func TestInitInstalledOllamaModelsAreOfferedAndCurrentIsMarked(t *testing.T) {
	withDiscovery(t, func(modelSource) (modelList, error) {
		return modelList{Chat: []string{"llama3.2:3b", "qwen3:8b"}, Embedding: []string{"nomic-embed-text"}}, nil
	})
	env, out := initEnv(t, "MODEL_NAME=qwen3:8b\n", "\n\n\n\n\n\n\n\n\n")
	settings := initSettings{}
	if err := promptInitSettings(env, &settings); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "qwen3:8b  (current)") {
		t.Fatalf("the saved model must be marked current in the list:\n%s", out.String())
	}
	if settings.managerModelSet {
		t.Fatal("pressing Enter on the current model must not rewrite it")
	}
}

func TestInitWarnsWhenSavedModelIsNoLongerOffered(t *testing.T) {
	withDiscovery(t, func(modelSource) (modelList, error) { return modelList{Chat: []string{"llama3.2:3b"}}, nil })
	env, out := initEnv(t, "MODEL_NAME=gone-model:1b\n", "\n\n\n\n\n\n\n\n\n")
	if err := promptInitSettings(env, &initSettings{}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), `"gone-model:1b" is not offered`) {
		t.Fatalf("a saved model that no longer exists must be flagged:\n%s", out.String())
	}
}

// Scripts keep the old, typed behaviour and never touch the network.
func TestInitDiscoveryIsSkippedForScriptedInput(t *testing.T) {
	called := false
	oldDiscover := initModelDiscovery
	initModelDiscovery = func(context.Context, modelSource) (modelList, error) { called = true; return modelList{}, nil }
	t.Cleanup(func() { initModelDiscovery = oldDiscover })
	env, _ := initEnv(t, "", "\n\n\n\n\n\n\n\n") // strings.Reader is not a terminal
	if err := promptInitSettings(env, &initSettings{}); err != nil {
		t.Fatal(err)
	}
	if called {
		t.Fatal("piped/scripted input must never trigger a network lookup")
	}
}

func TestInitCloudWithoutAKeyDoesNotLookUpModels(t *testing.T) {
	calls := withDiscovery(t, func(modelSource) (modelList, error) { return modelList{Chat: []string{"x"}}, nil })
	env, out := initEnv(t, "", "4\n\n\n\n\n\n\n\n\n")
	if err := promptInitSettings(env, &initSettings{}); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 0 {
		t.Fatalf("no key means no lookup, got %+v", *calls)
	}
	if !strings.Contains(out.String(), "no API key entered") {
		t.Fatalf("should explain why no models are listed:\n%s", out.String())
	}
}
