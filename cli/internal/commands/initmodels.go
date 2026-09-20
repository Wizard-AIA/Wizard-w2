package commands

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"strings"

	"wizard/internal/hostinfo"
	"wizard/internal/ui"
)

// The interactive init flow asks the provider what it actually offers, so a
// person picks from real choices instead of typing a model name blind.
//
// Both hooks are variables so tests can drive the flow without a terminal or a
// network: discovery only runs when stdin is a real terminal, never for
// scripts, piped input or explicit configuration flags.
var (
	initDiscoveryEnabled = readerIsTerminal
	initModelDiscovery   = func(ctx context.Context, src modelSource) (modelList, error) { return discoverModels(ctx, src) }
)

const autoModelLabel = "auto-select (recommended)"

// initCatalog is what init learned about the chosen provider.
type initCatalog struct {
	provider string
	models   modelList
	known    bool // false: nothing was looked up (or the lookup failed)
}

var providerLabels = map[string]string{
	"ollama": "Ollama", "lmstudio": "LM Studio", "anthropic": "Anthropic",
	"openai": "OpenAI", "gemini": "Gemini", "custom_gateway": "your gateway",
}

// providerBaseURL is where init should look for provider's models: the URL the
// user just typed, else the one saved in backend/.env, else the default.
func providerBaseURL(env *Env, settings *initSettings, provider string) string {
	if provider == "custom_gateway" {
		return firstNonEmpty(settings.gatewayURL, envValue(env, "GATEWAY_API_URL"))
	}
	if settings.baseURL != "" {
		return settings.baseURL
	}
	if key, ok := baseURLKeyFor(provider); ok {
		return envValue(env, key)
	}
	return ""
}

// providerAPIKey is the key init has for provider, from this run or the saved
// backend/.env.
func providerAPIKey(env *Env, settings *initSettings, provider string) string {
	switch provider {
	case "anthropic":
		return firstNonEmpty(settings.anthropicKey, envValue(env, "ANTHROPIC_API_KEY"))
	case "openai":
		return firstNonEmpty(settings.openaiKey, envValue(env, "OPENAI_API_KEY"))
	case "gemini":
		return firstNonEmpty(settings.geminiKey, envValue(env, "GEMINI_API_KEY"))
	case "lmstudio":
		return firstNonEmpty(settings.lmstudioKey, envValue(env, "LMSTUDIO_API_KEY"))
	case "custom_gateway":
		return firstNonEmpty(settings.gatewayKey, envValue(env, "GATEWAY_API_KEY"))
	}
	return ""
}

// setProviderAPIKey stores key into the settings field for provider.
func setProviderAPIKey(settings *initSettings, provider, key string) {
	switch provider {
	case "anthropic":
		settings.anthropicKey = key
	case "openai":
		settings.openaiKey = key
	case "gemini":
		settings.geminiKey = key
	case "lmstudio":
		settings.lmstudioKey = key
	case "custom_gateway":
		settings.gatewayKey = key
	}
}

// discoverForInit checks the provider (and, for cloud providers, proves the key
// works) and returns what it offers. On a rejected key it offers to re-enter
// it. It never fails init: any problem degrades to typing a model name.
func discoverForInit(env *Env, input io.Reader, reader *bufio.Reader, settings *initSettings, provider string) initCatalog {
	cat := initCatalog{provider: provider}
	if !initDiscoveryEnabled(input) {
		return cat
	}
	p := ui.New(env.Out)
	label := providerLabels[provider]
	if label == "" {
		label = provider
	}

	for attempt := 0; attempt < 3; attempt++ {
		src := modelSource{Provider: provider, BaseURL: providerBaseURL(env, settings, provider), APIKey: providerAPIKey(env, settings, provider)}
		if cloudProviders[provider] && src.APIKey == "" {
			p.Check(ui.Warn, label, "no API key entered; model names will have to be typed")
			return cat
		}
		p.Printf("\nChecking %s...\n", label)
		models, err := initModelDiscovery(context.Background(), src)
		switch {
		case err == nil:
			cat.models, cat.known = models, true
			p.Check(ui.OK, label, describeDiscovery(provider, models))
			return cat
		case errors.Is(err, errKeyRejected):
			p.Check(ui.Fail, label, "the provider rejected this API key ("+strings.TrimPrefix(err.Error(), errKeyRejected.Error()+" ")+")")
			choice, cerr := promptChoiceForInput(env.Out, input, reader, "What now?", []string{"re-enter the key", "continue anyway"}, "re-enter the key")
			if cerr != nil || choice != "re-enter the key" {
				return cat
			}
			value, set, serr := promptSecretOptional(env.Out, reader, input, label+" API key", "")
			if serr != nil {
				return cat
			}
			if set {
				setProviderAPIKey(settings, provider, value)
			}
		default:
			p.Check(ui.Warn, label, providerProblem(provider, src, err))
			// A local server may live on another machine. Ask where only now,
			// after the default address failed, and retry once with the answer;
			// Enter gives up and leaves the models on auto-select.
			if !cloudProviders[provider] && attempt == 0 {
				current := firstNonEmpty(src.BaseURL, defaultModelBaseURL(provider))
				address, set, aerr := promptOptional(env.Out, reader, label+" address (Enter to skip)", current, false)
				if aerr == nil && set && address != current {
					settings.baseURL = address
					continue
				}
			}
			return cat
		}
	}
	return cat
}

func describeDiscovery(provider string, m modelList) string {
	switch provider {
	case "ollama":
		if len(m.Chat) == 0 {
			return "running, but no chat models are installed yet"
		}
		return fmt.Sprintf("running; %d chat model(s) installed, %d embedding model(s)", len(m.Chat), len(m.Embedding))
	case "lmstudio":
		return fmt.Sprintf("running; %d chat model(s), %d embedding model(s) available", len(m.Chat), len(m.Embedding))
	}
	return fmt.Sprintf("key accepted; %d chat model(s), %d embedding model(s) available", len(m.Chat), len(m.Embedding))
}

func providerProblem(provider string, src modelSource, err error) string {
	switch provider {
	case "ollama":
		base := firstNonEmpty(src.BaseURL, defaultModelBaseURL("ollama"))
		return fmt.Sprintf("not reachable at %s. Start it with `ollama serve` (or install it from https://ollama.com/download); you can still type model names", base)
	case "lmstudio":
		return "not reachable. Start the LM Studio local server; you can still type model names"
	}
	return fmt.Sprintf("could not list models (%v); you can still type model names", err)
}

// promptModelSelect asks for a model with a dropdown of what is actually
// available. suggestions are shown after the installed models and marked as
// not installed (used to recommend a starter pair for a fresh Ollama).
func promptModelSelect(env *Env, input io.Reader, reader *bufio.Reader, label, current string, cat initCatalog, kind string, suggestions []string) (string, bool, error) {
	available := cat.models.Chat
	if kind == "embedding" {
		available = cat.models.Embedding
	}
	// Scripted or piped input has no terminal to show a list on: keep the
	// line-oriented, typed behaviour so existing automation still works.
	if !initDiscoveryEnabled(input) {
		return promptModel(env.Out, reader, label, current)
	}
	p := ui.New(env.Out)
	// On a terminal nobody is asked to type a model name. If the provider's
	// list could not be fetched there is nothing valid to offer, so the choice
	// is left to auto-select rather than asked blind.
	if !cat.known || (len(available) == 0 && len(suggestions) == 0) {
		reason := "chosen automatically; pick a specific one later on the Models page"
		if cat.known && kind == "embedding" {
			reason = fmt.Sprintf("%s offers no embedding models; retrieval falls back to word overlap or another provider", providerLabels[cat.provider])
		}
		p.Check(ui.Info, label, reason)
		return current, false, nil
	}

	values := map[string]string{} // display label -> model name
	options := []string{autoModelLabel}
	seen := map[string]bool{}
	add := func(name, suffix string) {
		if seen[name] {
			return
		}
		seen[name] = true
		display := name + suffix
		if name == current {
			display += "  (current)"
		}
		values[display] = name
		options = append(options, display)
	}
	for _, name := range available {
		add(name, "")
	}
	for _, name := range suggestions {
		if !hasString(available, name) {
			add(name, "  (not installed yet)")
		}
	}

	if current != "" && !hasString(available, current) {
		p.Check(ui.Warn, "Saved model", fmt.Sprintf("%q is not offered by %s right now", current, providerLabels[cat.provider]))
	}
	defaultOption := autoModelLabel
	for display, name := range values {
		if name == current {
			defaultOption = display
		}
	}

	choice, err := promptChoiceForInput(env.Out, input, reader, label, options, defaultOption)
	if err != nil {
		return "", false, err
	}
	switch choice {
	case autoModelLabel:
		if current == "" {
			return "", false, nil
		}
		return "", true, nil // clear the saved model
	}
	if name := values[choice]; name == current {
		return current, false, nil // keeping the saved model is not a change
	}
	return values[choice], true, nil
}

// promptHybridCloud is the cloud half of a hybrid setup: local models stay the
// default, and one cloud provider is made available for roles that are moved to
// it later. It asks for the provider from a list, then its key, then proves the
// key works. No endpoint is asked for, and no model is chosen here: per-role
// cloud models are picked from the provider's live list on the Models page.
func promptHybridCloud(env *Env, input io.Reader, reader *bufio.Reader, settings *initSettings) error {
	const skip = "skip (add one later on the Models page)"
	choice, err := promptChoiceForInput(env.Out, input, reader, "Cloud provider to pair with your local models",
		[]string{skip, "anthropic", "openai", "gemini", "custom_gateway"}, skip)
	if err != nil {
		return err
	}
	if choice == skip {
		return nil
	}
	if err := promptProviderCredentials(env.Out, reader, input, env, settings, choice); err != nil {
		return err
	}
	// Verify the key. discoverForInit uses (and updates) the settings, so a
	// re-entered key lands in the right field; its model list is not used here.
	saved := settings.baseURL
	discoverForInit(env, input, reader, settings, choice)
	settings.baseURL = saved
	return nil
}

// starterModels suggests a manager/worker pair for a fresh Ollama, sized to
// this machine's memory the same way the non-interactive path does.
func starterModels(manager, worker string) []string {
	ram, err := hostinfo.TotalRAMBytes()
	m, w, _, _ := recommendModels(ram, err == nil, manager, worker)
	if m == w {
		return []string{m}
	}
	return []string{m, w}
}
