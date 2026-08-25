package commands

import "fmt"

// installDependencies runs the same steps `wizard init` and `wizard update`
// both need: a Python venv with the backend's requirements, and a built
// frontend. Shared so update cannot drift from what init actually does.
//
// Whether requirements-optional.txt joins the install is read back from
// backend/.env (see needsOptionalRequirements) rather than passed in, so
// `wizard update` -- which takes no provider flags of its own -- reinstalls
// against whatever `wizard init` (or a hand edit, or the Models page) last
// configured, with nothing to remember between the two commands.
func installDependencies(env *Env, python ToolCheck) error {
	if err := ensureVenv(env, python); err != nil {
		return fmt.Errorf("setting up the Python environment: %w", err)
	}

	args := []string{"pip", "install", "--python", env.VenvPython(), "-r", "requirements.txt", "-r", "requirements-local.txt"}
	if needsOptionalRequirements(env) {
		args = append(args, "-r", "requirements-optional.txt")
		fmt.Fprintln(env.Out, "\nA cloud or hybrid provider is configured -- also installing requirements-optional.txt (the langchain-openai/langchain-anthropic clients).")
	}

	fmt.Fprintln(env.Out, "\nInstalling backend dependencies (this can take a while the first time)...")
	if err := runStreamed(env, env.RepoRoot, "uv", args); err != nil {
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
