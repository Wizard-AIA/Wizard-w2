package commands

import "fmt"

// backendInstallArgs is the `uv` argument list for the backend's packages. It
// reads nothing from backend/.env: which provider is configured does not change
// what gets installed.
func backendInstallArgs(env *Env) []string {
	return []string{"pip", "install", "--python", env.VenvPython(), "-r", "requirements.txt", "-r", "requirements-local.txt"}
}

// installDependencies runs the same steps `wizard init` and `wizard update`
// both need: a Python venv with the backend's requirements, and a built
// frontend. Shared so update cannot drift from what init actually does.
//
// The install is the same whatever provider is configured. Every provider's
// client (Ollama, OpenAI-compatible, Anthropic) is in requirements.txt, so a
// role can be pointed at a cloud model from the UI at any time without another
// `wizard init` or `wizard update`. requirements-optional.txt (Redis, database
// and object-store drivers) ships in the package but is never installed for
// you; install it by hand when you want one of those.
func installDependencies(env *Env, python ToolCheck) error {
	if err := ensureVenv(env, python); err != nil {
		return fmt.Errorf("setting up the Python environment: %w", err)
	}

	fmt.Fprintln(env.Out, "\nInstalling backend dependencies (this can take a while the first time)...")
	if err := runStreamed(env, env.RepoRoot, "uv", backendInstallArgs(env)); err != nil {
		return fmt.Errorf("uv pip install failed: %w", err)
	}

	fmt.Fprintln(env.Out, "\nInstalling frontend dependencies...")
	if err := runStreamed(env, env.FrontendDir, "pnpm", []string{"install", "--frozen-lockfile"}); err != nil {
		return fmt.Errorf("pnpm install failed: %w", err)
	}

	fmt.Fprintln(env.Out, "\nBuilding the frontend (production standalone bundle)...")
	if err := runStreamed(env, env.FrontendDir, "pnpm", []string{"run", "build"}); err != nil {
		return fmt.Errorf("pnpm run build failed: %w", err)
	}
	return nil
}
