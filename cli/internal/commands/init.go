package commands

import (
	"flag"
	"fmt"
	"io"
	"os"

	"wizard/internal/hostinfo"
)

const (
	minPythonMajor = 3
	minPythonMinor = 12
	minNodeMajor   = 20
)

// RunInit implements `wizard init`: optional interactive setup, prerequisite
// installation, environment check, .env setup, dependency install, and
// optional model pulls. Bare interactive runs offer to install missing tools;
// scripted runs require the explicit --install-prerequisites flag.
func RunInit(env *Env, args []string) int {
	fs := flag.NewFlagSet("init", flag.ContinueOnError)
	pullModels := fs.Bool("pull-models", false, "Also `ollama pull` a small default manager/worker pair if Ollama is present and no model is pinned.")
	installPrerequisites := fs.Bool("install-prerequisites", false, "Install missing Python, Node.js, uv, pnpm, and required model-server tools using the host package manager.")
	noInstallPrerequisites := fs.Bool("no-install-prerequisites", false, "Only check prerequisites; never install missing tools.")
	managerModel := fs.String("manager-model", "qwen3:8b", "Model to pull for the manager role with --pull-models.")
	workerModel := fs.String("worker-model", "qwen2.5-coder:7b", "Model to pull for the worker role with --pull-models.")
	embeddingProvider := fs.String("embedding-provider", "", "Pin EMBEDDING_PROVIDER: ollama | lmstudio | openai | gemini | custom_gateway. Empty follows --provider; Anthropic does not support embeddings.")
	embeddingModel := fs.String("embedding-model", "", "Model to use for embeddings (e.g. nomic-embed-text, bge-m3, text-embedding-3-small).")
	provider := fs.String("provider", "", "Pin API_PROVIDER: ollama | lmstudio | anthropic | openai | gemini | custom_gateway. Empty leaves backend/.env's existing/default value.")
	dataMode := fs.String("data-mode", "", "Pin DATA_MODE: local-only | hybrid | cloud-only. Empty leaves it to derive -- see backend/.env.example.")
	interactive := fs.Bool("interactive", false, "Ask setup questions even when configuration flags are also supplied.")
	nonInteractive := fs.Bool("non-interactive", false, "Never ask setup questions; use flags and backend/.env defaults.")
	baseURL := fs.String("base-url", "", "Point --provider at a proxy: writes ANTHROPIC_BASE_URL/OPENAI_BASE_URL/GEMINI_BASE_URL/LMSTUDIO_BASE_URL/OLLAMA_BASE_URL depending on --provider.")
	lmstudioKey := fs.String("lmstudio-key", "", "Write LMSTUDIO_API_KEY into backend/.env.")
	anthropicKey := fs.String("anthropic-key", "", "Write ANTHROPIC_API_KEY into backend/.env.")
	openaiKey := fs.String("openai-key", "", "Write OPENAI_API_KEY into backend/.env.")
	geminiKey := fs.String("gemini-key", "", "Write GEMINI_API_KEY into backend/.env.")
	gatewayURL := fs.String("gateway-url", "", "Write GATEWAY_API_URL into backend/.env (custom_gateway provider).")
	gatewayKey := fs.String("gateway-key", "", "Write GATEWAY_API_KEY into backend/.env (custom_gateway provider).")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	explicit := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { explicit[f.Name] = true })
	if *interactive && *nonInteractive {
		fmt.Fprintln(env.Err, "--interactive and --non-interactive cannot be used together")
		return 2
	}
	if *installPrerequisites && *noInstallPrerequisites {
		fmt.Fprintln(env.Err, "--install-prerequisites and --no-install-prerequisites cannot be used together")
		return 2
	}
	hasConfigFlags := false
	for _, name := range []string{
		"provider", "data-mode", "manager-model", "worker-model", "embedding-provider",
		"embedding-model", "base-url", "lmstudio-key", "anthropic-key", "openai-key",
		"gemini-key", "gateway-url", "gateway-key",
	} {
		hasConfigFlags = hasConfigFlags || explicit[name]
	}
	settings := initSettings{providerConfig: providerConfig{
		provider: *provider, dataMode: *dataMode, baseURL: *baseURL,
		embeddingProvider: *embeddingProvider, embeddingModel: *embeddingModel,
		lmstudioKey: *lmstudioKey, anthropicKey: *anthropicKey,
		openaiKey: *openaiKey, geminiKey: *geminiKey,
		gatewayURL: *gatewayURL, gatewayKey: *gatewayKey,
	}, managerModel: *managerModel, workerModel: *workerModel,
		managerModelFlagSet: explicit["manager-model"], workerModelFlagSet: explicit["worker-model"]}
	if *provider != "" && !validProviders[*provider] {
		fmt.Fprintf(env.Err, "invalid --provider %q; must be one of: ollama, lmstudio, anthropic, openai, gemini, custom_gateway\n", *provider)
		return 2
	}
	if *dataMode != "" && !validDataModes[*dataMode] {
		fmt.Fprintf(env.Err, "invalid --data-mode %q; must be one of: local-only, hybrid, cloud-only\n", *dataMode)
		return 2
	}
	if *embeddingProvider != "" && !embeddingProviders[*embeddingProvider] {
		fmt.Fprintf(env.Err, "invalid --embedding-provider %q; must be one of: ollama, lmstudio, openai, gemini, custom_gateway (Anthropic does not provide embeddings)\n", *embeddingProvider)
		return 2
	}

	// --pull-models is an operational request, not a request to re-open the
	// setup questionnaire. Use --interactive when both behaviors are wanted.
	if shouldPromptInit(env.In, *interactive, *nonInteractive, hasConfigFlags || *pullModels) {
		fmt.Fprintln(env.Out, "\nWizard setup (press Enter to keep the shown default; use --non-interactive for automation)")
		if err := promptInitSettings(env, &settings); err != nil {
			fmt.Fprintf(env.Err, "Interactive setup cancelled: %v\n", err)
			return 2
		}
	}
	*provider = settings.provider
	*dataMode = settings.dataMode
	*baseURL = settings.baseURL
	*embeddingProvider = settings.embeddingProvider
	*embeddingModel = settings.embeddingModel
	*lmstudioKey = settings.lmstudioKey
	*anthropicKey = settings.anthropicKey
	*openaiKey = settings.openaiKey
	*geminiKey = settings.geminiKey
	*gatewayURL = settings.gatewayURL
	*gatewayKey = settings.gatewayKey
	modelsExplicit := explicit["manager-model"] || explicit["worker-model"] || settings.managerModelSet || settings.workerModelSet
	if settings.managerModelSet {
		*managerModel = settings.managerModel
	}
	if settings.workerModelSet {
		*workerModel = settings.workerModel
	}

	configuredProvider := *provider
	if configuredProvider == "" {
		configuredProvider, _, _ = readEnvValue(env.BackendEnvPath(), "API_PROVIDER")
	}
	configuredDataMode := *dataMode
	if configuredDataMode == "" {
		configuredDataMode, _, _ = readEnvValue(env.BackendEnvPath(), "DATA_MODE")
	}
	configuredEmbeddingProvider := *embeddingProvider
	if configuredEmbeddingProvider == "" {
		configuredEmbeddingProvider, _, _ = readEnvValue(env.BackendEnvPath(), "EMBEDDING_PROVIDER")
	}

	// A pure-cloud setup (a cloud provider and not also hybrid) has no local
	// weights to size -- RAM-based manager/worker fitting below is Ollama-tag
	// arithmetic (modelfit.go) that means nothing for a model named on
	// Anthropic/OpenAI/a gateway, so it and the default Ollama model pull are
	// skipped in favor of MODEL_NAME/WORKER_MODEL_NAME staying empty
	// (auto-select on that provider), same as .env.example's own default.
	pureCloud := configuredProvider != "" && cloudProviders[configuredProvider] && configuredDataMode != "hybrid"

	fmt.Fprintln(env.Out, "Checking prerequisites...")
	python := CheckPython(minPythonMajor, minPythonMinor)
	node := CheckNode(minNodeMajor)
	uv := CheckUV()
	pnpm := CheckPnpm()
	ollama := CheckOllama()

	printCheck(env.Out, python)
	printCheck(env.Out, node)
	printCheck(env.Out, uv)
	printCheck(env.Out, pnpm)
	ollamaRequired := requiresOllama(configuredProvider, configuredDataMode, configuredEmbeddingProvider)
	if ollamaRequired {
		printCheck(env.Out, ollama)
	} else if !ollama.Found {
		fmt.Fprintln(env.Out, "  [OPTIONAL] Ollama     not found on PATH; skipped for this provider/data-mode configuration.")
	} else {
		printCheck(env.Out, ollama)
	}

	missing := requiredPrerequisites(python, node, uv, pnpm)
	if ollamaRequired && !ollama.OK {
		missing = append(missing, ollama)
	}
	if len(missing) > 0 {
		shouldInstall := *installPrerequisites
		if !shouldInstall && !*noInstallPrerequisites && shouldPromptInit(env.In, *interactive, *nonInteractive, hasConfigFlags || *pullModels) {
			var err error
			shouldInstall, err = askInstallPrerequisites(env, missing)
			if err != nil {
				fmt.Fprintf(env.Err, "Prerequisite installation prompt failed: %v\n", err)
				return 1
			}
		}
		if shouldInstall {
			if err := installMissingPrerequisites(env, missing); err != nil {
				fmt.Fprintf(env.Err, "\n%s\n", err)
				return 1
			}
			refreshToolPath()
			python = CheckPython(minPythonMajor, minPythonMinor)
			node = CheckNode(minNodeMajor)
			uv = CheckUV()
			pnpm = CheckPnpm()
			ollama = CheckOllama()
			fmt.Fprintln(env.Out, "\nRechecking prerequisites after installation...")
			printCheck(env.Out, python)
			printCheck(env.Out, node)
			printCheck(env.Out, uv)
			printCheck(env.Out, pnpm)
			if ollamaRequired {
				printCheck(env.Out, ollama)
			}
			missing = requiredPrerequisites(python, node, uv, pnpm)
			if ollamaRequired && !ollama.OK {
				missing = append(missing, ollama)
			}
		}
		if len(missing) > 0 {
			fmt.Fprintln(env.Err, "\nOne or more required prerequisites are still missing or too old. Install them and re-run `wizard init`.")
			return 1
		}
	}

	// Decide the manager/worker pair before touching backend/.env, so a
	// freshly created file can be pre-filled with whatever was decided.
	// modelsExplicit means the user named at least one model themselves --
	// their choice is respected either way, just accompanied by a fit note
	// rather than silently swapped, matching how a Docker-unreachable
	// fallback is announced elsewhere in this codebase rather than silent.
	resolvedManager, resolvedWorker := *managerModel, *workerModel
	applied := false
	if pureCloud {
		fmt.Fprintf(env.Out, "\n[CLOUD] provider=%s -- skipping local Ollama model sizing; MODEL_NAME/WORKER_MODEL_NAME stay empty (auto-select on %s) unless you pin one yourself.\n", *provider, *provider)
	} else {
		ramBytes, ramErr := hostinfo.TotalRAMBytes()
		recManager, recWorker, overridden, reason := recommendModels(ramBytes, ramErr == nil, *managerModel, *workerModel)
		applied = overridden && !modelsExplicit
		switch {
		case overridden && modelsExplicit:
			fmt.Fprintf(env.Out, "\n[HOST] %s (kept: --manager-model/--worker-model given explicitly)\n", reason)
		case applied:
			resolvedManager, resolvedWorker = recManager, recWorker
			fmt.Fprintf(env.Out, "\n[HOST] %s\n", reason)
		default:
			fmt.Fprintf(env.Out, "\n[HOST] %s\n", reason)
		}
	}

	if err := ensureEnvFile(env, applied, resolvedManager, resolvedWorker); err != nil {
		fmt.Fprintf(env.Err, "Could not set up backend/.env: %v\n", err)
		return 1
	}

	if err := applyProviderConfig(env, settings.providerConfig); err != nil {
		fmt.Fprintf(env.Err, "Could not write provider settings to backend/.env: %v\n", err)
		return 1
	}
	if settings.managerModelSet {
		if err := setEnvValue(env.BackendEnvPath(), "MODEL_NAME", settings.managerModel); err != nil {
			fmt.Fprintf(env.Err, "Could not write model settings to backend/.env: %v\n", err)
			return 1
		}
	}
	if settings.workerModelSet {
		if err := setEnvValue(env.BackendEnvPath(), "WORKER_MODEL_NAME", settings.workerModel); err != nil {
			fmt.Fprintf(env.Err, "Could not write model settings to backend/.env: %v\n", err)
			return 1
		}
	}
	if settings.dataModeClear {
		if err := setEnvValue(env.BackendEnvPath(), "DATA_MODE", ""); err != nil {
			fmt.Fprintf(env.Err, "Could not clear data mode in backend/.env: %v\n", err)
			return 1
		}
	}
	if settings.embeddingProviderClear {
		if err := setEnvValue(env.BackendEnvPath(), "EMBEDDING_PROVIDER", ""); err != nil {
			fmt.Fprintf(env.Err, "Could not clear embedding provider in backend/.env: %v\n", err)
			return 1
		}
	}
	if settings.embeddingModelClear {
		if err := setEnvValue(env.BackendEnvPath(), "EMBEDDING_REMOTE_MODEL", ""); err != nil {
			fmt.Fprintf(env.Err, "Could not clear embedding model in backend/.env: %v\n", err)
			return 1
		}
	}
	finalProvider, _, _ := readEnvValue(env.BackendEnvPath(), "API_PROVIDER")
	finalEmbeddingProvider, _, _ := readEnvValue(env.BackendEnvPath(), "EMBEDDING_PROVIDER")
	warnMissingCloudConfig(env, finalProvider)

	if err := installDependencies(env, python); err != nil {
		fmt.Fprintf(env.Err, "%v\n", err)
		return 1
	}

	switch {
	case pureCloud && *pullModels:
		fmt.Fprintln(env.Out, "\n--pull-models ignored: provider is a pure-cloud setup, so no local Ollama models are used by default. Pass --data-mode hybrid if you also want a local fallback model.")
	case *pullModels:
		if !ollama.Found {
			fmt.Fprintln(env.Err, "\n--pull-models given but Ollama was not found on PATH; nothing was pulled.")
			return 1
		}
		fmt.Fprintln(env.Out, "\nPulling default models via Ollama...")
		if !pullDefaultModels(env, resolvedManager, resolvedWorker, *embeddingModel, finalProvider, finalEmbeddingProvider) {
			return 1
		}
	case ollama.Found && !pureCloud:
		fmt.Fprintln(env.Out, "\nOllama detected. Run `wizard init --pull-models` to also fetch default manager, worker, and embedding models.")
	}

	fmt.Fprintln(env.Out, "\nDone. Run `wizard start` to launch the backend and frontend.")
	return 0
}

// pullDefaultModels pulls manager/worker/embedding into whichever role does not
// already have a model pinned in backend/.env (MODEL_NAME/WORKER_MODEL_NAME/EMBEDDING_REMOTE_MODEL
// -- see backend/src/config.py), so a configured checkout does not
// re-download a default it will not use. It reports whether every requested
// pull succeeded; the caller turns that into `wizard init`'s exit code, since
// a silently skipped or failed pull under --pull-models must not look like a
// completed one.
func pullDefaultModels(env *Env, managerModel, workerModel, embeddingModel, provider, embeddingProvider string) bool {
	type modelPull struct {
		role   string
		envKey string
		model  string
	}
	pulls := []modelPull{
		{"manager", "MODEL_NAME", managerModel},
		{"worker", "WORKER_MODEL_NAME", workerModel},
	}
	// Also pull embedding model if using Ollama
	if (embeddingProvider == "" || embeddingProvider == "ollama") && (provider == "" || provider == "ollama") {
		emb := embeddingModel
		if emb == "" {
			emb = "nomic-embed-text"
		}
		pulls = append(pulls, modelPull{"embedding", "EMBEDDING_REMOTE_MODEL", emb})
	}

	ok := true
	for _, p := range pulls {
		pinned, found, err := readEnvValue(env.BackendEnvPath(), p.envKey)
		switch {
		case err != nil:
			fmt.Fprintf(env.Err, "  could not check whether %s is already pinned (%v); pulling the default anyway.\n", p.envKey, err)
		case found && pinned != "":
			fmt.Fprintf(env.Out, "  %s already has %s=%s pinned in backend/.env; skipping.\n", p.role, p.envKey, pinned)
			continue
		}
		if err := runStreamed(env, env.RepoRoot, "ollama", []string{"pull", p.model}); err != nil {
			fmt.Fprintf(env.Err, "ollama pull %s failed: %v\n", p.model, err)
			ok = false
		}
	}
	return ok
}

func printCheck(out io.Writer, c ToolCheck) {
	switch {
	case !c.Found:
		fmt.Fprintf(out, "  [MISSING] %-10s not found on PATH.", c.Name)
		if c.InstallHint != "" {
			fmt.Fprintf(out, " Install: %s", c.InstallHint)
		}
		fmt.Fprintln(out)
	case !c.OK:
		fmt.Fprintf(out, "  [TOO OLD] %-10s %s at %s (need >= %d.%d). Install: %s\n", c.Name, c.Version, c.Path, c.MinMajor, c.MinMinor, c.InstallHint)
	case c.Version != "":
		fmt.Fprintf(out, "  [OK]      %-10s %s at %s\n", c.Name, c.Version, c.Path)
	default:
		fmt.Fprintf(out, "  [OK]      %-10s at %s\n", c.Name, c.Path)
	}
}

// ensureEnvFile copies backend/.env.example to backend/.env if the latter
// does not exist yet. The app already runs with none of those values set
// (see backend/.env.example's own header), so this is a convenience starting
// point to edit, not a required step.
//
// applied means the RAM-aware smart default (see modelfit.go) chose manager
// and worker for the caller -- in that case the fresh file is pre-filled
// with MODEL_NAME/WORKER_MODEL_NAME rather than left at .env.example's empty
// "auto-select" defaults. An existing .env is never touched either way,
// matching the "leaving it as is" guarantee below.
func ensureEnvFile(env *Env, applied bool, manager, worker string) error {
	if _, err := os.Stat(env.BackendEnvPath()); err == nil {
		fmt.Fprintln(env.Out, "\nbackend/.env already exists, leaving it as is.")
		return nil
	}
	src, err := os.Open(env.BackendEnvExamplePath())
	if err != nil {
		return err
	}
	defer src.Close()

	dst, err := os.OpenFile(env.BackendEnvPath(), os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(dst, src); err != nil {
		_ = dst.Close() // already returning the copy error; a close error here adds nothing
		return err
	}
	if err := dst.Close(); err != nil {
		return err
	}

	if !applied {
		fmt.Fprintln(env.Out, "\nCreated backend/.env from backend/.env.example. Edit it to pin a provider/model if you want one.")
		return nil
	}
	if err := setEnvValue(env.BackendEnvPath(), "MODEL_NAME", manager); err != nil {
		return err
	}
	if err := setEnvValue(env.BackendEnvPath(), "WORKER_MODEL_NAME", worker); err != nil {
		return err
	}
	fmt.Fprintf(env.Out, "\nCreated backend/.env from backend/.env.example, with MODEL_NAME/WORKER_MODEL_NAME pinned to %s.\n", manager)
	return nil
}

// ensureVenv creates the wizard-managed Python venv if it does not already
// have a usable interpreter in it. Kept under the platform config directory
// (see internal/appdir) rather than inside the checkout, so it survives a
// `git clean` and does not collide with a developer's own venv there.
//
// Built with `uv venv` rather than `python -m venv`: uv is already a required
// prerequisite (installDependencies uses it to install requirements), and its
// venv creation is materially faster. The env this produces has no pip binary
// in it -- uv installs packages without needing one -- which is why
// Env.VenvPip was removed rather than kept for a tool nothing calls any more.
func ensureVenv(env *Env, python ToolCheck) error {
	if env.VenvExists() {
		fmt.Fprintln(env.Out, "\nUsing existing venv at", env.VenvDir)
		return nil
	}
	fmt.Fprintln(env.Out, "\nCreating a Python environment at", env.VenvDir)
	return runStreamed(env, env.RepoRoot, "uv", []string{"venv", "--python", python.Path, env.VenvDir})
}
