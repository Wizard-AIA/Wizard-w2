package commands

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func serve(t *testing.T, handler http.HandlerFunc) string {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return srv.URL
}

func TestDiscoverModelsOllamaSplitsChatAndEmbedding(t *testing.T) {
	base := serve(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tags" {
			t.Errorf("path = %s, want /api/tags", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"models":[{"name":"qwen3:8b"},{"name":"nomic-embed-text:latest"},{"name":"qwen2.5-coder:7b"},{"name":"qwen3:8b"}]}`))
	})
	got, err := discoverModels(context.Background(), modelSource{Provider: "ollama", BaseURL: base})
	if err != nil {
		t.Fatal(err)
	}
	want := modelList{Chat: []string{"qwen2.5-coder:7b", "qwen3:8b"}, Embedding: []string{"nomic-embed-text:latest"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

func TestDiscoverModelsGeminiUsesHeaderKeyAndMethods(t *testing.T) {
	const key = "fake-gemini-key-for-test"
	base := serve(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.String(), key) {
			t.Errorf("the API key must never appear in the URL: %s", r.URL)
		}
		if r.Header.Get("x-goog-api-key") != key {
			t.Errorf("x-goog-api-key = %q", r.Header.Get("x-goog-api-key"))
		}
		_, _ = w.Write([]byte(`{"models":[
			{"name":"models/gemini-2.5-flash","supportedGenerationMethods":["generateContent","countTokens"]},
			{"name":"models/gemini-embedding-001","supportedGenerationMethods":["embedContent"]},
			{"name":"models/aqa","supportedGenerationMethods":["generateAnswer"]}]}`))
	})
	got, err := discoverModels(context.Background(), modelSource{Provider: "gemini", BaseURL: base, APIKey: key})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got.Chat, []string{"gemini-2.5-flash"}) || !reflect.DeepEqual(got.Embedding, []string{"gemini-embedding-001"}) {
		t.Fatalf("got %+v; models that cannot generate or embed (aqa) must be hidden", got)
	}
}

func TestDiscoverModelsOpenAIHidesNonChatFamilies(t *testing.T) {
	base := serve(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fake-openai-key" {
			t.Errorf("Authorization = %q", r.Header.Get("Authorization"))
		}
		_, _ = w.Write([]byte(`{"data":[{"id":"gpt-4.1"},{"id":"gpt-4o-mini"},{"id":"o3-mini"},{"id":"whisper-1"},
			{"id":"dall-e-3"},{"id":"tts-1"},{"id":"text-embedding-3-small"},{"id":"gpt-4o-realtime-preview"},{"id":"davinci-002"}]}`))
	})
	got, err := discoverModels(context.Background(), modelSource{Provider: "openai", BaseURL: base, APIKey: "fake-openai-key"})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got.Chat, []string{"gpt-4.1", "gpt-4o-mini", "o3-mini"}) {
		t.Fatalf("chat = %v", got.Chat)
	}
	if !reflect.DeepEqual(got.Embedding, []string{"text-embedding-3-small"}) {
		t.Fatalf("embedding = %v", got.Embedding)
	}
}

func TestDiscoverModelsAnthropicHeaders(t *testing.T) {
	base := serve(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-api-key") != "fake-anthropic-key" || r.Header.Get("anthropic-version") == "" {
			t.Errorf("missing Anthropic auth headers: %v", r.Header)
		}
		_, _ = w.Write([]byte(`{"data":[{"id":"claude-sonnet-4-5"},{"id":"claude-haiku-4-5"}]}`))
	})
	got, err := discoverModels(context.Background(), modelSource{Provider: "anthropic", BaseURL: base, APIKey: "fake-anthropic-key"})
	if err != nil || len(got.Chat) != 2 || len(got.Embedding) != 0 {
		t.Fatalf("got (%+v, %v)", got, err)
	}
}

func TestDiscoverModelsClassifiesFailures(t *testing.T) {
	for name, status := range map[string]int{"unauthorized": 401, "forbidden": 403} {
		base := serve(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status) })
		_, err := discoverModels(context.Background(), modelSource{Provider: "openai", BaseURL: base, APIKey: "bad"})
		if !errors.Is(err, errKeyRejected) {
			t.Errorf("%s: err = %v, want errKeyRejected", name, err)
		}
	}
	// Gemini reports an invalid key as HTTP 400.
	base := serve(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(400) })
	if _, err := discoverModels(context.Background(), modelSource{Provider: "gemini", BaseURL: base, APIKey: "bad"}); !errors.Is(err, errKeyRejected) {
		t.Errorf("gemini 400: err = %v, want errKeyRejected", err)
	}
	server5xx := serve(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) })
	if _, err := discoverModels(context.Background(), modelSource{Provider: "openai", BaseURL: server5xx}); !errors.Is(err, errProviderUnreachable) {
		t.Errorf("503: err = %v, want errProviderUnreachable", err)
	}
	// Nothing listening: the address of a just-closed server.
	closed := httptest.NewServer(http.NotFoundHandler())
	closedURL := closed.URL
	closed.Close()
	if _, err := discoverModels(context.Background(), modelSource{Provider: "ollama", BaseURL: closedURL}); !errors.Is(err, errProviderUnreachable) {
		t.Errorf("closed port: err = %v, want errProviderUnreachable", err)
	}
}

func TestDiscoverModelsErrorNeverEchoesCredentialsInURL(t *testing.T) {
	closed := httptest.NewServer(http.NotFoundHandler())
	closedURL := closed.URL + "/?token=super-secret-token"
	closed.Close()
	_, err := discoverModels(context.Background(), modelSource{Provider: "custom_gateway", BaseURL: closedURL})
	if err == nil {
		t.Fatal("expected an error")
	}
	if strings.Contains(err.Error(), "super-secret-token") {
		t.Fatalf("error leaks a credential: %v", err)
	}
}

func TestDiscoverModelsRejectsProviderWithoutEndpoint(t *testing.T) {
	if _, err := discoverModels(context.Background(), modelSource{Provider: "custom_gateway"}); err == nil {
		t.Fatal("custom_gateway without a URL must fail cleanly, not dial an empty address")
	}
}

func TestIsEmbeddingModel(t *testing.T) {
	for _, id := range []string{"nomic-embed-text", "mxbai-embed-large", "text-embedding-3-small", "bge-m3", "all-minilm:l6-v2"} {
		if !isEmbeddingModel(id) {
			t.Errorf("%q should be an embedding model", id)
		}
	}
	for _, id := range []string{"qwen3:8b", "gpt-4o", "gemini-2.5-flash"} {
		if isEmbeddingModel(id) {
			t.Errorf("%q should not be an embedding model", id)
		}
	}
}
