package commands

import "fmt"

// validProviders/validDataModes mirror backend/.env.example's own comments
// for API_PROVIDER/DATA_MODE -- kept here rather than asked from the backend
// (which is not running yet when `wizard init` validates a flag).
var validProviders = map[string]bool{
	"ollama": true, "lmstudio": true, "anthropic": true, "openai": true, "gemini": true, "custom_gateway": true,
}

// cloudProviders are the API_PROVIDER values that speak to a remote service
// rather than a local model server.
var cloudProviders = map[string]bool{
	"anthropic": true, "openai": true, "gemini": true, "custom_gateway": true,
}

var validDataModes = map[string]bool{
	"local-only": true, "hybrid": true, "cloud-only": true,
}

// providerConfig carries `wizard init`'s provider/data-mode/key flags through
// to backend/.env. Every field is optional -- an empty one means "leave
// whatever backend/.env already has," never "clear it," matching
// .env.example's own "the app runs with none of them set" guarantee.
type providerConfig struct {
	provider          string
	dataMode          string
	baseURL           string
	embeddingProvider string
	embeddingModel    string
	dataSchemaOnly    string
	lmstudioKey       string
	anthropicKey      string
	openaiKey         string
	geminiKey         string
	gatewayURL        string
	gatewayKey        string
}

// applyProviderConfig writes only the fields the caller actually set, using
// the same setEnvValue ensureEnvFile itself relies on. Unlike
// ensureEnvFile's fresh-file-only guarantee, this always applies: passing
// --provider/--anthropic-key etc. is an explicit instruction, not a passive
// default, so it takes effect against an already-configured backend/.env too
// -- e.g. switching an existing local install to hybrid or cloud-only.
func applyProviderConfig(env *Env, cfg providerConfig) error {
	sets := [][2]string{
		{"API_PROVIDER", cfg.provider},
		{"DATA_MODE", cfg.dataMode},
		{"EMBEDDING_PROVIDER", cfg.embeddingProvider},
		{"EMBEDDING_REMOTE_MODEL", cfg.embeddingModel},
		{"DATA_SCHEMA_ONLY", cfg.dataSchemaOnly},
		{"LMSTUDIO_API_KEY", cfg.lmstudioKey},
		{"ANTHROPIC_API_KEY", cfg.anthropicKey},
		{"OPENAI_API_KEY", cfg.openaiKey},
		{"GEMINI_API_KEY", cfg.geminiKey},
		{"GATEWAY_API_URL", cfg.gatewayURL},
		{"GATEWAY_API_KEY", cfg.gatewayKey},
	}
	if cfg.baseURL != "" {
		key, ok := baseURLKeyFor(cfg.provider)
		if !ok {
			return fmt.Errorf("--base-url needs --provider ollama|lmstudio|anthropic|openai|gemini to know which setting to write (custom_gateway is already a URL: use --gateway-url)")
		}
		sets = append(sets, [2]string{key, cfg.baseURL})
	}
	for _, kv := range sets {
		if kv[1] == "" {
			continue
		}
		if err := setEnvValue(env.BackendEnvPath(), kv[0], kv[1]); err != nil {
			return fmt.Errorf("writing %s: %w", kv[0], err)
		}
	}
	return nil
}

func baseURLKeyFor(provider string) (string, bool) {
	switch provider {
	case "", "ollama":
		return "OLLAMA_BASE_URL", true
	case "lmstudio":
		return "LMSTUDIO_BASE_URL", true
	case "anthropic":
		return "ANTHROPIC_BASE_URL", true
	case "openai":
		return "OPENAI_BASE_URL", true
	case "gemini":
		return "GEMINI_BASE_URL", true
	default:
		return "", false
	}
}

// warnMissingCloudConfig reads back whatever backend/.env ended up with
// (not just this run's flags, since API_PROVIDER may have been pinned by an
// earlier `wizard init` and only DATA_MODE changed this time) and names the
// one thing a cloud/gateway provider cannot run without. Detect-and-instruct
// rather than blocking: the same key can be added later from the Models
// page, so an empty one here is not an error `wizard init` should fail on.
func warnMissingCloudConfig(env *Env, provider string) {
	notice := func(key, flag string) {
		if v, found, _ := readEnvValue(env.BackendEnvPath(), key); !found || v == "" {
			fmt.Fprintf(env.Out, "\n[CLOUD] %s is still empty. Set it with `wizard init %s ...`, edit backend/.env, or add it from the Models page once the app is running.\n", key, flag)
		}
	}
	switch provider {
	case "anthropic":
		notice("ANTHROPIC_API_KEY", "--anthropic-key")
	case "openai":
		notice("OPENAI_API_KEY", "--openai-key")
	case "gemini":
		notice("GEMINI_API_KEY", "--gemini-key")
	case "custom_gateway":
		notice("GATEWAY_API_URL", "--gateway-url")
		notice("GATEWAY_API_KEY", "--gateway-key")
	}
}

// needsOptionalRequirements decides whether requirements-optional.txt's
// langchain-openai/langchain-anthropic clients are needed, by reading
// whatever backend/.env currently says rather than being told -- so `wizard
// update` reinstalling after a `git pull` stays correct without remembering
// what `wizard init` was originally run with.
//
// Any provider other than plain `ollama` needs langchain-openai:
// LLMProvider's OpenAI-compatible branch (backend/src/core/llm/provider.py)
// is what serves lmstudio/openai/custom_gateway, and only langchain-ollama
// ships in requirements.txt. `anthropic` needs langchain-anthropic on top,
// which that same file raises a clear ImportError instructing the user to
// install without. hybrid/cloud-only data modes install it unconditionally
// too, since a role can be assigned to a cloud provider from the UI after
// init runs, with no further `wizard init`/`wizard update` in between to
// catch the switch.
func needsOptionalRequirements(env *Env) bool {
	provider, _, _ := readEnvValue(env.BackendEnvPath(), "API_PROVIDER")
	if provider != "" && provider != "ollama" {
		return true
	}
	dataMode, _, _ := readEnvValue(env.BackendEnvPath(), "DATA_MODE")
	return dataMode == "hybrid" || dataMode == "cloud-only"
}
