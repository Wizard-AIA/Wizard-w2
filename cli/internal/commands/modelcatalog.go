package commands

// Model discovery for the interactive `wizard init` flow. Asking a person to
// type "Manager model" with no idea what their provider offers produces typos
// and models that do not exist, so once credentials are known init asks the
// provider itself which models are usable and offers only those.
//
// This runs only for an interactive terminal session and only against the
// provider the user just chose, with the key they just typed -- the same
// request the backend makes at runtime. Scripts (`--non-interactive`, piped
// stdin, any explicit configuration flag) never make these calls.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

const (
	maxModelListBytes  = 4 << 20
	modelLookupTimeout = 8 * time.Second
)

// modelHTTPClient honors HTTPS_PROXY/NO_PROXY like every other CLI request.
// Tests replace it.
var modelHTTPClient = &http.Client{}

// Discovery failure classes, so init can say something useful for each.
var (
	// errKeyRejected: the provider answered but refused the credentials.
	errKeyRejected = errors.New("the provider rejected the API key")
	// errProviderUnreachable: no answer at all (not running, offline, proxy).
	errProviderUnreachable = errors.New("could not reach the provider")
)

// modelList is what a provider offers, split by what each model is for.
type modelList struct {
	Chat      []string
	Embedding []string
}

func (m modelList) empty() bool { return len(m.Chat) == 0 && len(m.Embedding) == 0 }

// modelSource says where to look and how to authenticate.
type modelSource struct {
	Provider string
	BaseURL  string // empty = the provider's default endpoint
	APIKey   string
}

// defaultModelBaseURL is each provider's default API root.
func defaultModelBaseURL(provider string) string {
	switch provider {
	case "ollama":
		return "http://127.0.0.1:11434"
	case "lmstudio":
		return "http://127.0.0.1:1234/v1"
	case "openai":
		return "https://api.openai.com/v1"
	case "anthropic":
		return "https://api.anthropic.com/v1"
	case "gemini":
		return "https://generativelanguage.googleapis.com/v1beta"
	}
	return ""
}

// discoverModels lists the models src's provider offers. It returns
// errKeyRejected or errProviderUnreachable (wrapped) so callers can branch.
func discoverModels(ctx context.Context, src modelSource) (modelList, error) {
	base := strings.TrimRight(firstNonEmpty(src.BaseURL, defaultModelBaseURL(src.Provider)), "/")
	if base == "" {
		return modelList{}, fmt.Errorf("no endpoint is known for provider %q", src.Provider)
	}
	if _, err := url.ParseRequestURI(base); err != nil {
		return modelList{}, fmt.Errorf("invalid endpoint %q: %w", base, err)
	}

	var endpoint string
	header := http.Header{"Accept": {"application/json"}, "User-Agent": {"wizard-cli-init"}}
	switch src.Provider {
	case "ollama":
		endpoint = base + "/api/tags"
	case "gemini":
		endpoint = base + "/models?pageSize=1000"
		// The key goes in a header, never the URL, so it cannot end up in a
		// proxy log or an error message.
		header.Set("x-goog-api-key", src.APIKey)
	case "anthropic":
		endpoint = base + "/models?limit=1000"
		header.Set("x-api-key", src.APIKey)
		header.Set("anthropic-version", "2023-06-01")
	default: // lmstudio, openai, custom_gateway: OpenAI-compatible
		endpoint = base + "/models"
		if src.APIKey != "" {
			header.Set("Authorization", "Bearer "+src.APIKey)
		}
	}

	ctx, cancel := context.WithTimeout(ctx, modelLookupTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return modelList{}, err
	}
	req.Header = header
	resp, err := modelHTTPClient.Do(req)
	if err != nil {
		return modelList{}, fmt.Errorf("%w: %v", errProviderUnreachable, redactURLError(err))
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden ||
		(resp.StatusCode == http.StatusBadRequest && src.Provider == "gemini"):
		// Gemini answers 400 "API key not valid" for a bad key.
		return modelList{}, fmt.Errorf("%w (HTTP %d)", errKeyRejected, resp.StatusCode)
	case resp.StatusCode != http.StatusOK:
		return modelList{}, fmt.Errorf("%w: HTTP %d", errProviderUnreachable, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxModelListBytes))
	if err != nil {
		return modelList{}, fmt.Errorf("%w: %v", errProviderUnreachable, err)
	}
	return parseModelList(src.Provider, body)
}

// redactURLError strips the request URL from a *url.Error so a failure message
// can never echo a credential that was placed in a URL by a custom endpoint.
func redactURLError(err error) error {
	var ue *url.Error
	if errors.As(err, &ue) {
		return ue.Err
	}
	return err
}

// parseModelList turns a provider's list response into sorted, de-duplicated
// chat and embedding lists.
func parseModelList(provider string, body []byte) (modelList, error) {
	var ids []string
	switch provider {
	case "ollama":
		var r struct {
			Models []struct {
				Name string `json:"name"`
			} `json:"models"`
		}
		if err := json.Unmarshal(body, &r); err != nil {
			return modelList{}, fmt.Errorf("unexpected Ollama response: %w", err)
		}
		for _, m := range r.Models {
			ids = append(ids, m.Name)
		}
	case "gemini":
		var r struct {
			Models []struct {
				Name    string   `json:"name"`
				Methods []string `json:"supportedGenerationMethods"`
			} `json:"models"`
		}
		if err := json.Unmarshal(body, &r); err != nil {
			return modelList{}, fmt.Errorf("unexpected Gemini response: %w", err)
		}
		var out modelList
		for _, m := range r.Models {
			id := strings.TrimPrefix(m.Name, "models/")
			switch {
			case hasString(m.Methods, "embedContent"):
				out.Embedding = append(out.Embedding, id)
			case hasString(m.Methods, "generateContent") && isChatModel("gemini", id):
				out.Chat = append(out.Chat, id)
			}
		}
		return tidyModelList(out), nil
	default: // openai-compatible and anthropic share {"data":[{"id":...}]}
		var r struct {
			Data []struct {
				ID string `json:"id"`
			} `json:"data"`
		}
		if err := json.Unmarshal(body, &r); err != nil {
			return modelList{}, fmt.Errorf("unexpected model list response: %w", err)
		}
		for _, m := range r.Data {
			ids = append(ids, m.ID)
		}
	}

	var out modelList
	for _, id := range ids {
		switch {
		case isEmbeddingModel(id):
			out.Embedding = append(out.Embedding, id)
		case isChatModel(provider, id):
			out.Chat = append(out.Chat, id)
		}
	}
	return tidyModelList(out), nil
}

func tidyModelList(m modelList) modelList {
	m.Chat = sortedUnique(m.Chat)
	m.Embedding = sortedUnique(m.Embedding)
	return m
}

// sortedUnique de-duplicates and orders models: stable releases first, then
// previews, each alphabetical, so the dependable choices are at the top.
func sortedUnique(in []string) []string {
	seen := make(map[string]bool, len(in))
	out := make([]string, 0, len(in))
	for _, s := range in {
		if s != "" && !seen[s] {
			seen[s] = true
			out = append(out, s)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		pi, pj := isPreviewModel(out[i]), isPreviewModel(out[j])
		if pi != pj {
			return !pi
		}
		return out[i] < out[j]
	})
	return out
}

func hasString(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

// isEmbeddingModel recognises embedding models by the naming every provider
// and the common open models use.
func isEmbeddingModel(id string) bool {
	id = strings.ToLower(id)
	return strings.Contains(id, "embed") || strings.HasPrefix(id, "bge-") || strings.HasPrefix(id, "all-minilm")
}

// nonChatFamilies are model families that a cloud provider lists next to its
// chat models (Gemini reports image, speech and music models as able to
// "generateContent" too) but that cannot serve as a manager or worker.
// Showing them would offer choices that fail the first time they are used.
var nonChatFamilies = []string{
	"whisper", "tts", "dall-e", "moderation", "davinci", "babbage", "audio",
	"realtime", "transcribe", "image", "sora", "search-preview", "computer-use", "codex-mini",
	"lyria", "nano-banana", "robotics", "deep-research", "antigravity", "veo", "imagen", "live", "learnlm", "aqa",
}

// isChatModel reports whether id can be picked as a manager/worker model.
// Local servers (Ollama, LM Studio) list only what the user installed on
// purpose, so those are never filtered by name.
func isChatModel(provider, id string) bool {
	if provider == "ollama" || provider == "lmstudio" {
		return true
	}
	lower := strings.ToLower(id)
	for _, family := range nonChatFamilies {
		if strings.Contains(lower, family) {
			return false
		}
	}
	if provider == "openai" {
		return strings.HasPrefix(lower, "gpt-") || strings.HasPrefix(lower, "chatgpt-") ||
			strings.HasPrefix(lower, "o1") || strings.HasPrefix(lower, "o3") || strings.HasPrefix(lower, "o4") ||
			strings.HasPrefix(lower, "o5")
	}
	return true
}

// isPreviewModel marks experimental releases, which are listed after stable ones.
func isPreviewModel(id string) bool {
	lower := strings.ToLower(id)
	return strings.Contains(lower, "preview") || strings.Contains(lower, "-exp") || strings.Contains(lower, "experimental")
}
