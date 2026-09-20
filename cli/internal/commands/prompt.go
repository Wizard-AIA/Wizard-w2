package commands

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	"golang.org/x/term"

	"wizard/internal/ui"
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
	embeddingModelClear    bool
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
	provider, err := promptChoiceForInput(env.Out, input, reader, "Default provider", []string{
		"ollama", "lmstudio", "anthropic", "openai", "gemini", "custom_gateway",
	}, providerDefault)
	if err != nil {
		return err
	}
	settings.provider = provider

	// Credentials come right after the provider, before any model question:
	// with a working key init can ask the provider which models exist and offer
	// only those, instead of asking for a model name the user cannot know.
	if err := promptProviderCredentials(env.Out, reader, input, env, settings, provider); err != nil {
		return err
	}
	catalog := discoverForInit(env, input, reader, settings, provider)

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
	mode, err := promptChoiceForInput(env.Out, input, reader, "Data mode", []string{
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
	if mode == "hybrid" && !cloudProviders[provider] && initDiscoveryEnabled(input) {
		if err := promptHybridCloud(env, input, reader, settings); err != nil {
			return err
		}
	}

	schemaDefault := strings.ToLower(envValueOr(env, "DATA_SCHEMA_ONLY", "true"))
	if schemaDefault != "true" && schemaDefault != "false" {
		schemaDefault = "true"
	}
	schemaOnly, err := promptChoiceForInput(env.Out, input, reader, "Send only schema to cloud models?", []string{
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
	// A fresh Ollama has no models yet; suggest a starter pair sized to this
	// machine so the person is not left guessing names.
	var starters []string
	if provider == "ollama" {
		starters = starterModels("qwen3:8b", "qwen2.5-coder:7b") // the --manager-model/--worker-model defaults
	}
	settings.managerModel, settings.managerModelSet, err = promptModelSelect(env, input, reader,
		"Manager model", managerCurrent, catalog, "chat", starters)
	if err != nil {
		return err
	}
	workerCurrent := envValue(env, "WORKER_MODEL_NAME")
	if settings.workerModelSet || settings.workerModelFlagSet {
		workerCurrent = settings.workerModel
	}
	settings.workerModel, settings.workerModelSet, err = promptModelSelect(env, input, reader,
		"Worker model", workerCurrent, catalog, "chat", starters)
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
	if embeddingDefault != "auto" && !embeddingProviders[embeddingDefault] {
		fmt.Fprintf(env.Out, "\nSaved embedding provider %q does not support embeddings; defaulting to auto.\n", embeddingDefault)
		embeddingDefault = "auto"
	}
	embeddingProvider, err := promptChoiceForInput(env.Out, input, reader, "Embedding provider", embeddingProviderOptions(), embeddingDefault)
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
	embeddingCat := embeddingCatalog(env, input, settings, catalog, embeddingProvider)
	var embeddingStarters []string
	if embeddingProviderOrDefault(embeddingProvider, provider) == "ollama" {
		embeddingStarters = []string{"nomic-embed-text"}
	}
	embeddingModel, set, err := promptModelSelect(env, input, reader, "Embedding model", embeddingCurrent, embeddingCat, "embedding", embeddingStarters)
	if err != nil {
		return err
	}
	if set {
		settings.embeddingModel = embeddingModel
		settings.embeddingModelClear = embeddingModel == ""
	}

	summarizeInitChoices(env, settings, provider)
	return nil
}

// embeddingProviderOrDefault resolves "auto" to the chat provider.
func embeddingProviderOrDefault(embeddingProvider, provider string) string {
	if embeddingProvider == "" || embeddingProvider == "auto" {
		return provider
	}
	return embeddingProvider
}

// embeddingCatalog returns the models to offer for the embedding step: the chat
// provider's own list when embeddings follow it, otherwise a fresh lookup of the
// chosen embedding provider (skipped silently without a terminal, or without a
// key for a cloud provider).
func embeddingCatalog(env *Env, input io.Reader, settings *initSettings, chat initCatalog, embeddingProvider string) initCatalog {
	target := embeddingProviderOrDefault(embeddingProvider, chat.provider)
	if target == chat.provider {
		return chat
	}
	cat := initCatalog{provider: target}
	if !initDiscoveryEnabled(input) {
		return cat
	}
	key := providerAPIKey(env, settings, target)
	if cloudProviders[target] && key == "" {
		return cat
	}
	models, err := initModelDiscovery(context.Background(), modelSource{Provider: target, BaseURL: providerBaseURL(env, settings, target), APIKey: key})
	if err != nil {
		return cat
	}
	cat.models, cat.known = models, true
	return cat
}

// summarizeInitChoices prints what init captured. Keys are described, never
// printed, so a screenshot of the terminal is safe to share.
func summarizeInitChoices(env *Env, settings *initSettings, provider string) {
	p := ui.New(env.Out)
	p.Section("Your setup")
	show := func(key, value string) {
		if value == "" {
			value = p.Dim("auto")
		}
		p.KV(16, key, value)
	}
	show("Provider", provider)
	show("Data mode", firstNonEmpty(settings.dataMode, "auto"))
	show("Manager model", settings.managerModel)
	show("Worker model", settings.workerModel)
	show("Embedding model", settings.embeddingModel)
	if key := providerAPIKey(env, settings, provider); key != "" {
		p.KV(16, "API key", describeSecret(key))
	}
	fmt.Fprintln(env.Out, "\nContinuing with dependency setup...")
}

func promptProviderCredentials(out io.Writer, reader *bufio.Reader, input io.Reader, env *Env, settings *initSettings, provider string) error {
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
		value, set, err = promptSecretOptional(out, reader, input, "Gateway API key", gatewayKeyCurrent)
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
		value, set, err := promptSecretOptional(out, reader, input, keyName.label, current)
		if err != nil {
			return err
		}
		if set {
			*keyName.value = value
		}
	}
	// No endpoint question: every provider's API root is known, and asking a
	// person to type one is how typos and wrong URLs get in. A proxy is still
	// available as `--base-url`; a local server on another machine is asked for
	// only if the default address turns out to be unreachable (see
	// discoverForInit).
	return nil
}

// promptSecretOptional reads an API key without ever printing it, but with
// visible feedback: each typed or pasted character shows as a mask glyph, and
// once entered a receipt line proves the key arrived (its length and last four
// characters). The old prompt echoed nothing at all, so a person could not tell
// a successful paste from a paste that never happened.
//
// Piped/non-terminal input deliberately retains the line-oriented fallback so
// `wizard init --interactive` remains testable and usable from a wrapper.
func promptSecretOptional(out io.Writer, reader *bufio.Reader, input io.Reader, label, current string) (string, bool, error) {
	defaultText := "not set"
	if current != "" {
		defaultText = "saved, " + describeSecret(current)
	}

	if file, ok := input.(*os.File); ok && readerIsTerminal(file) && reader.Buffered() == 0 {
		fmt.Fprintf(out, "%s [%s]\n  paste or type it; each • is one character, Enter to confirm, Enter alone to keep it: ", label, defaultText)
		value, err := readMaskedSecret(file, out)
		if err != nil {
			return "", false, err
		}
		if value == "" {
			if current != "" {
				fmt.Fprintf(out, "  Keeping the saved key (%s).\n", describeSecret(current))
			}
			return current, false, nil
		}
		fmt.Fprintf(out, "  Key received: %s.\n", describeSecret(value))
		return value, true, nil
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
	return value, true, nil
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

func promptChoiceForInput(out io.Writer, input io.Reader, reader *bufio.Reader, label string, options []string, current string) (string, error) {
	if file, ok := input.(*os.File); ok && readerIsTerminal(file) {
		return promptArrowChoice(out, file, label, options, current)
	}
	return promptChoice(out, reader, label, options, current)
}

// promptArrowChoice is the interactive select. On a terminal that supports
// ANSI cursor movement it is a real dropdown (see dropdown.go); on one that does
// not -- a dumb terminal, an old console -- it falls back to a numbered list
// with a single redrawn "Selected:" line, which needs only a carriage return.
func promptArrowChoice(out io.Writer, file *os.File, label string, options []string, current string) (string, error) {
	if p := ui.New(out); p.T.Cursor {
		value, err := runDropdown(out, file, p, label, options, current)
		if !errors.Is(err, errRawUnavailable) {
			return value, err
		}
	}

	defaultIndex := 0
	for i, option := range options {
		if option == current {
			defaultIndex = i
			break
		}
	}

	state, err := term.MakeRaw(int(file.Fd()))
	if err != nil {
		return promptChoice(out, bufio.NewReader(file), label, options, current)
	}
	defer func() { _ = term.Restore(int(file.Fd()), state) }()

	// Raw mode turns off "\n" -> "\r\n" translation, so lines end in "\r\n"
	// here; a bare "\n" staircases the text across the screen.
	fmt.Fprintf(out, "\r\n%s (use ↑/↓, Enter):\r\n", label)
	for i, option := range options {
		fmt.Fprintf(out, "  %d) %s\r\n", i+1, option)
	}
	selected := defaultIndex
	renderArrowSelection(out, options, selected, false)
	for {
		key, err := readTerminalKey(file)
		if err != nil {
			return "", err
		}
		if len(key) == 1 && key[0] == 3 {
			return "", fmt.Errorf("interactive setup cancelled")
		}
		next, accepted := applyTerminalKey(key, selected, len(options))
		if accepted {
			fmt.Fprint(out, "\r\n")
			return options[next], nil
		}
		if next == selected {
			continue
		}
		selected = next
		renderArrowSelection(out, options, selected, true)
	}
}

func renderArrowSelection(out io.Writer, options []string, selected int, redraw bool) {
	if redraw {
		// PowerShell may pass arrow bytes through raw mode but not enable ANSI
		// cursor-up/erase sequences. Keep redraws on one line so the menu never
		// grows a duplicate option list on terminals without VT processing.
		fmt.Fprint(out, "\r")
	}
	selection := fmt.Sprintf("Selected: %s", options[selected])
	// Padding clears leftovers when the previous option has a longer name.
	width := 0
	for _, option := range options {
		if len(option) > width {
			width = len(option)
		}
	}
	fmt.Fprintf(out, "%-*s", len(selection)+width+10, selection)
}

// escapeSequenceWait is how long readTerminalKey waits for the bytes that follow
// an Escape before deciding it was the Escape key itself. Terminals send an
// arrow key's three bytes together, so this only has to outlast a slow link.
const escapeSequenceWait = 50 * time.Millisecond

func readTerminalKey(file *os.File) ([]byte, error) {
	first := []byte{0}
	if _, err := io.ReadFull(file, first); err != nil {
		return nil, err
	}
	if first[0] != 0x1b {
		return first, nil
	}
	// A bare Escape press has nothing after it. Reading two more bytes here
	// would hang the prompt until the person pressed two other keys.
	if !inputPending(file, escapeSequenceWait) {
		return first, nil
	}
	sequence := make([]byte, 3)
	sequence[0] = first[0]
	if _, err := io.ReadFull(file, sequence[1:]); err != nil {
		return sequence[:1], nil
	}
	// PgUp, PgDn, Home and End arrive as ESC [ <digit> ~ : consume the "~" so
	// it is not read as a typed character on the next call.
	if sequence[1] == '[' && sequence[2] >= '0' && sequence[2] <= '9' {
		tail := []byte{0}
		if _, err := io.ReadFull(file, tail); err == nil {
			sequence = append(sequence, tail[0])
		}
	}
	return sequence, nil
}

func applyTerminalKey(key []byte, selected, optionCount int) (int, bool) {
	if optionCount == 0 {
		return selected, true
	}
	switch string(key) {
	case "\r", "\n":
		return selected, true
	case "\x1b[A", "k":
		return (selected + optionCount - 1) % optionCount, false
	case "\x1b[B", "j":
		return (selected + 1) % optionCount, false
	}
	if len(key) == 1 && key[0] >= '1' && int(key[0]-'0') <= optionCount {
		return int(key[0] - '1'), true
	}
	return selected, false
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
