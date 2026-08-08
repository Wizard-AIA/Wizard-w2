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
	minPythonMinor = 11
	minNodeMajor   = 20
)

// RunInit implements `wizard init`: environment check, .env setup, dependency
// install, optional model pulls. Detect-and-instruct only -- it never
// invokes a package manager on the user's behalf for Python/Node/Ollama
// themselves, only for this project's own dependencies once the
// prerequisites are confirmed present.
func RunInit(env *Env, args []string) int {
	fs := flag.NewFlagSet("init", flag.ContinueOnError)
	pullModels := fs.Bool("pull-models", false, "Also `ollama pull` a small default manager/worker pair if Ollama is present and no model is pinned.")
	managerModel := fs.String("manager-model", "qwen3:8b", "Model to pull for the manager role with --pull-models.")
	workerModel := fs.String("worker-model", "qwen2.5-coder:7b", "Model to pull for the worker role with --pull-models.")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	explicit := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { explicit[f.Name] = true })
	modelsExplicit := explicit["manager-model"] || explicit["worker-model"]

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
	printCheck(env.Out, ollama)

	if !python.OK || !node.OK || !uv.OK || !pnpm.OK {
		fmt.Fprintln(env.Err, "\nOne or more required prerequisites are missing or too old. Install them and re-run `wizard init`.")
		return 1
	}

	// Decide the manager/worker pair before touching backend/.env, so a
	// freshly created file can be pre-filled with whatever was decided.
	// modelsExplicit means the user named at least one model themselves --
	// their choice is respected either way, just accompanied by a fit note
	// rather than silently swapped, matching how a Docker-unreachable
	// fallback is announced elsewhere in this codebase rather than silent.
	ramBytes, ramErr := hostinfo.TotalRAMBytes()
	recManager, recWorker, overridden, reason := recommendModels(ramBytes, ramErr == nil, *managerModel, *workerModel)
	resolvedManager, resolvedWorker := *managerModel, *workerModel
	applied := overridden && !modelsExplicit
	switch {
	case overridden && modelsExplicit:
		fmt.Fprintf(env.Out, "\n[HOST] %s (kept: --manager-model/--worker-model given explicitly)\n", reason)
	case applied:
		resolvedManager, resolvedWorker = recManager, recWorker
		fmt.Fprintf(env.Out, "\n[HOST] %s\n", reason)
	default:
		fmt.Fprintf(env.Out, "\n[HOST] %s\n", reason)
	}

	if err := ensureEnvFile(env, applied, resolvedManager, resolvedWorker); err != nil {
		fmt.Fprintf(env.Err, "Could not set up backend/.env: %v\n", err)
		return 1
	}

	if err := installDependencies(env, python); err != nil {
		fmt.Fprintf(env.Err, "%v\n", err)
		return 1
	}

	if *pullModels {
		if !ollama.Found {
			fmt.Fprintln(env.Err, "\n--pull-models given but Ollama was not found on PATH; nothing was pulled.")
			return 1
		}
		fmt.Fprintln(env.Out, "\nPulling default models via Ollama...")
		if !pullDefaultModels(env, resolvedManager, resolvedWorker) {
			return 1
		}
	} else if ollama.Found {
		fmt.Fprintln(env.Out, "\nOllama detected. Run `wizard init --pull-models` to also fetch a default manager/worker model pair.")
	}

	fmt.Fprintln(env.Out, "\nDone. Run `wizard start` to launch the backend and frontend.")
	return 0
}

// pullDefaultModels pulls manager/worker into whichever role does not
// already have a model pinned in backend/.env (MODEL_NAME/WORKER_MODEL_NAME
// -- see backend/src/config.py), so a configured checkout does not
// re-download a default it will not use. It reports whether every requested
// pull succeeded; the caller turns that into `wizard init`'s exit code, since
// a silently skipped or failed pull under --pull-models must not look like a
// completed one.
func pullDefaultModels(env *Env, managerModel, workerModel string) bool {
	type modelPull struct {
		role   string
		envKey string
		model  string
	}
	pulls := []modelPull{
		{"manager", "MODEL_NAME", managerModel},
		{"worker", "WORKER_MODEL_NAME", workerModel},
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
