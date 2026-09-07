package commands

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

// initSettings is the complete set of setup choices that the interactive
// `wizard init` flow owns. The flag flow remains separate so init is still
// safe to use from scripts and package-manager post-install hooks.
type initSettings struct {
	providerConfig
	managerModel           string
	workerModel            string
	managerModelSet        bool
	workerModelSet         bool
	managerModelFlagSet    bool
	workerModelFlagSet     bool
	dataModeClear          bool
	embeddingProviderClear bool
}

// shouldPromptInit enables the setup flow for a bare init from a real
// terminal. Explicit configuration flags keep their old non-interactive
// behavior; --interactive is available when a user wants prompts alongside
// flags or when stdin is not a TTY.
func shouldPromptInit(in io.Reader, interactive, nonInteractive, hasConfigFlags bool) bool {
	if nonInteractive {
		return false
	}
	if interactive {
		return true
	}
	return !hasConfigFlags && readerIsTerminal(in)
}

func readerIsTerminal(in io.Reader) bool {
	file, ok := in.(*os.File)
	if !ok {
		return false
	}
	info, err := file.Stat()
	return err == nil && info.Mode()&os.ModeCharDevice != 0
}

func promptInitSettings(env *Env, settings *initSettings) error {
	input := env.In
	if input == nil {
		input = os.Stdin
	}
	reader := bufio.NewReader(input)

	providerDefault := settings.provider
	if providerDefault == "" {
		providerDefault = envValueOr(env, "API_PROVIDER", "ollama")
	}
	provider, err := promptChoice(env.Out, reader, "Default provider", []string{
		"ollama", "lmstudio", "anthropic", "openai", "gemini", "custom_gateway",
	}, providerDefault)
	if err != nil {
		return err
	}
	settings.provider = provider

	modeDefault := settings.dataMode
	if modeDefault == "" {
		modeDefault = envValueOr(env, "DATA_MODE", "auto")
	}
	if modeDefault == "" {
		modeDefault = "auto"
	}
	if modeDefault == "auto" && cloudProviders[provider] {
		modeDefault = "cloud-only"
	}
	mode, err := promptChoice(env.Out, reader, "Data mode", []string{
		"auto", "local-only", "hybrid", "cloud-only",
	}, modeDefault)
	if err != nil {
		return err
	}
	if mode == "auto" {
		settings.dataModeClear = settings.dataMode != "" || envValue(env, "DATA_MODE") != ""
		settings.dataMode = ""
	} else {
		settings.dataMode = mode
	}

	schemaDefault := strings.ToLower(envValueOr(env, "DATA_SCHEMA_ONLY", "true"))
	if schemaDefault != "true" && schemaDefault != "false" {
		schemaDefault = "true"
	}
	schemaOnly, err := promptChoice(env.Out, reader, "Send only schema to cloud models?", []string{
		"true", "false",
	}, schemaDefault)
	if err != nil {
		return err
	}
	settings.dataSchemaOnly = schemaOnly

	managerCurrent := envValue(env, "MODEL_NAME")
	if settings.managerModelSet || settings.managerModelFlagSet {
		managerCurrent = settings.managerModel
	}
	settings.managerModel, settings.managerModelSet, err = promptModel(env.Out, reader,
		"Manager model", managerCurrent)
	if err != nil {
		return err
	}
	workerCurrent := envValue(env, "WORKER_MODEL_NAME")
	if settings.workerModelSet || settings.workerModelFlagSet {
		workerCurrent = settings.workerModel
	}
	settings.workerModel, settings.workerModelSet, err = promptModel(env.Out, reader,
		"Worker model", workerCurrent)
	if err != nil {
		return err
	}

	embeddingDefault := settings.embeddingProvider
	if embeddingDefault == "" {
		embeddingDefault = envValueOr(env, "EMBEDDING_PROVIDER", "auto")
	}
	if embeddingDefault == "" {
		embeddingDefault = "auto"
	}
	embeddingProvider, err := promptChoice(env.Out, reader, "Embedding provider", []string{
		"auto", "ollama", "lmstudio", "anthropic", "openai", "gemini", "custom_gateway",
	}, embeddingDefault)
	if err != nil {
		return err
	}
	if embeddingProvider == "auto" {
		settings.embeddingProviderClear = settings.embeddingProvider != "" || envValue(env, "EMBEDDING_PROVIDER") != ""
		settings.embeddingProvider = ""
	} else {
		settings.embeddingProvider = embeddingProvider
	}
	embeddingCurrent := envValue(env, "EMBEDDING_REMOTE_MODEL")
	if settings.embeddingModel != "" {
		embeddingCurrent = settings.embeddingModel
	}
	embeddingModel, set, err := promptModel(env.Out, reader, "Embedding model", embeddingCurrent)
	if err != nil {
		return err
	}
	if set {
		settings.embeddingModel = embeddingModel
	}

	if err := promptProviderCredentials(env.Out, reader, env, settings, provider); err != nil {
		return err
	}

	fmt.Fprintln(env.Out, "Configuration captured. Continuing with dependency setup...")
	return nil
}

func promptProviderCredentials(out io.Writer, reader *bufio.Reader, env *Env, settings *initSettings, provider string) error {
	if provider == "custom_gateway" {
		gatewayURLCurrent := envValue(env, "GATEWAY_API_URL")
		if settings.gatewayURL != "" {
			gatewayURLCurrent = settings.gatewayURL
		}
		value, set, err := promptOptional(out, reader, "Gateway URL", gatewayURLCurrent, false)
		if err != nil {
			return err
		}
		if set {
			settings.gatewayURL = value
		}
		gatewayKeyCurrent := envValue(env, "GATEWAY_API_KEY")
		if settings.gatewayKey != "" {
			gatewayKeyCurrent = settings.gatewayKey
		}
		value, set, err = promptOptional(out, reader, "Gateway API key", gatewayKeyCurrent, false)
		if err != nil {
			return err
		}
		if set {
			settings.gatewayKey = value
		}
		return nil
	}

	keyName := map[string]struct {
		label string
		value *string
	}{
		"lmstudio":  {"LM Studio API key (optional)", &settings.lmstudioKey},
		"anthropic": {"Anthropic API key", &settings.anthropicKey},
		"openai":    {"OpenAI API key", &settings.openaiKey},
		"gemini":    {"Gemini API key", &settings.geminiKey},
	}[provider]
	if keyName.value != nil {
		key := map[string]string{
			"lmstudio":  "LMSTUDIO_API_KEY",
			"anthropic": "ANTHROPIC_API_KEY",
			"openai":    "OPENAI_API_KEY",
			"gemini":    "GEMINI_API_KEY",
		}[provider]
		current := envValue(env, key)
		if *keyName.value != "" {
			current = *keyName.value
		}
		value, set, err := promptOptional(out, reader, keyName.label, current, false)
		if err != nil {
			return err
		}
		if set {
			*keyName.value = value
		}
	}

	value, set, err := promptOptional(out, reader, "Custom provider base URL (optional)", "", false)
	if err != nil {
		return err
	}
	if set {
		settings.baseURL = value
	}
	return nil
}

func promptModel(out io.Writer, reader *bufio.Reader, label, current string) (string, bool, error) {
	value, set, err := promptOptional(out, reader,
		label+" (blank = auto-select; type 'auto' to clear a saved model)", current, true)
	if err != nil {
		return "", false, err
	}
	if set && strings.EqualFold(value, "auto") {
		return "", true, nil
	}
	return value, set, nil
}

func promptOptional(out io.Writer, reader *bufio.Reader, label, current string, allowClear bool) (string, bool, error) {
	defaultText := current
	if defaultText == "" {
		defaultText = "auto-select"
	}
	fmt.Fprintf(out, "%s [%s]: ", label, defaultText)
	line, err := reader.ReadString('\n')
	if err != nil && err != io.EOF {
		return "", false, err
	}
	value := strings.TrimSpace(line)
	if value == "" {
		return current, false, nil
	}
	if !allowClear && strings.EqualFold(value, "auto") {
		return "", false, nil
	}
	return value, true, nil
}

func promptChoice(out io.Writer, reader *bufio.Reader, label string, options []string, current string) (string, error) {
	defaultIndex := 0
	for i, option := range options {
		if option == current {
			defaultIndex = i
		}
	}
	fmt.Fprintf(out, "\n%s:\n", label)
	for i, option := range options {
		marker := " "
		if i == defaultIndex {
			marker = "*"
		}
		fmt.Fprintf(out, "  %s%d) %s\n", marker, i+1, option)
	}
	for {
		fmt.Fprintf(out, "Select [%d]: ", defaultIndex+1)
		line, err := reader.ReadString('\n')
		if err != nil && err != io.EOF {
			return "", err
		}
		value := strings.TrimSpace(line)
		if value == "" {
			return options[defaultIndex], nil
		}
		if index, parseErr := strconv.Atoi(value); parseErr == nil && index >= 1 && index <= len(options) {
			return options[index-1], nil
		}
		for _, option := range options {
			if strings.EqualFold(value, option) {
				return option, nil
			}
		}
		fmt.Fprintf(out, "Invalid choice %q; enter a number or one of the listed values.\n", value)
		if err == io.EOF {
			return "", io.ErrUnexpectedEOF
		}
	}
}

func envValue(env *Env, key string) string {
	value, _, _ := readEnvValue(env.BackendEnvPath(), key)
	return value
}

func envValueOr(env *Env, key, fallback string) string {
	if value := envValue(env, key); value != "" {
		return value
	}
	return fallback
}
