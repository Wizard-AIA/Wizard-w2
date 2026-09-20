package commands

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"wizard/internal/appdir"
	"wizard/internal/compat"
	"wizard/internal/daemon"
	"wizard/internal/exitcode"
	"wizard/internal/healthcheck"
	"wizard/internal/installkind"
	"wizard/internal/platform"
	"wizard/internal/repo"
	"wizard/internal/ui"
)

// doctorCheck is one diagnostic result. Fix is set for anything not passing.
type doctorCheck struct {
	ID     string    `json:"id"`
	Label  string    `json:"-"`
	Status ui.Status `json:"-"`
	State  string    `json:"status"`
	Detail string    `json:"detail"`
	Fix    string    `json:"fix,omitempty"`
}

type doctorReport struct {
	Version  string        `json:"version"`
	Platform string        `json:"platform"`
	Checks   []doctorCheck `json:"checks"`
	Summary  struct {
		Pass int `json:"pass"`
		Warn int `json:"warn"`
		Fail int `json:"fail"`
	} `json:"summary"`
}

func (r *doctorReport) add(id, label string, status ui.Status, detail, fix string) {
	r.Checks = append(r.Checks, doctorCheck{ID: id, Label: label, Status: status, State: status.Word(), Detail: detail, Fix: fix})
	switch status {
	case ui.OK:
		r.Summary.Pass++
	case ui.Warn:
		r.Summary.Warn++
	case ui.Fail:
		r.Summary.Fail++
	}
}

// RunDoctor implements `wizard doctor`: a read-only health report of this
// installation. Unlike every other command it must work when the bundled
// checkout cannot be found -- that is the situation it exists to diagnose -- so
// it resolves its own paths instead of requiring an Env. It changes nothing.
func RunDoctor(out, errw io.Writer, args []string) int {
	fs := flag.NewFlagSet("doctor", flag.ContinueOnError)
	asJSON := fs.Bool("json", false, "Print the report as JSON (for scripts and support tickets).")
	network := fs.Bool("network", false, "Also test network reachability: GitHub Releases and the configured model provider.")
	verbose := fs.Bool("verbose", false, "Show extra detail for every check.")
	tmpEnv := &Env{Out: out, Err: errw}
	if code, done := parseFlags(tmpEnv, fs, args); done {
		return code
	}
	if code, done := rejectArgs(tmpEnv, "doctor", fs.Args()); done {
		return code
	}

	report := collectDoctor(*network, *verbose)
	if *asJSON {
		enc := json.NewEncoder(out)
		enc.SetIndent("", "  ")
		_ = enc.Encode(report)
	} else {
		renderDoctor(ui.New(out), report)
	}
	if report.Summary.Fail > 0 {
		return exitcode.Environment
	}
	return exitcode.OK
}

func renderDoctor(p *ui.Printer, r *doctorReport) {
	p.Banner(displayVersion(), "installation diagnostics")
	p.Section("Diagnostics")
	for _, c := range r.Checks {
		p.Check(c.Status, c.Label, c.Detail)
		if c.Fix != "" && c.Status != ui.OK {
			p.Hint(c.Fix)
		}
	}
	p.Summary(r.Summary.Pass, r.Summary.Warn, r.Summary.Fail)
	if r.Summary.Fail > 0 {
		p.Println("\nFix the failures above, then run `wizard doctor` again.")
	}
}

func displayVersion() string { return strings.TrimPrefix(compat.BuildVersion, "v") }

func collectDoctor(network, verbose bool) *doctorReport {
	refreshToolPath() // same lookup as init, so doctor and init always agree
	r := &doctorReport{Version: compat.BuildVersion, Platform: platform.Current().String()}

	// Version.
	if compat.BuildVersion == "dev" {
		r.add("version", "Wizard CLI", ui.Warn, "development build (not a published release)",
			"Install a release: https://github.com/Wizard-AIA/Wizard-w2#install")
	} else {
		r.add("version", "Wizard CLI", ui.OK, fmt.Sprintf("%s, backend API compat v%s", compat.BuildVersion, compat.CompatAPIVersion), "")
	}

	// Platform.
	cur := platform.Current()
	if cur.Supported() {
		r.add("platform", "Platform", ui.OK, cur.String(), "")
	} else {
		r.add("platform", "Platform", ui.Fail, cur.String()+" (no release is published for this platform)", platform.UnsupportedMessage(cur))
	}

	// Executable and install method.
	exe, _ := os.Executable()
	root, rootErr := repo.Root()
	info := installkind.Detect(exe, root)
	if info.Kind == installkind.Direct || info.Kind == installkind.Homebrew || info.Kind == installkind.Scoop {
		root, rootErr = info.Root, nil // the binary's own package, not the directory the shell is in
	}
	r.add("install", "Installation", ui.OK, fmt.Sprintf("%s  (%s)", info.Kind, exe), "")

	// PATH.
	status, detail, fix := checkOnPath(exe, info)
	r.add("path", "On PATH", status, detail, fix)

	// Bundled checkout.
	if rootErr != nil {
		r.add("root", "Wizard files", ui.Fail, "the bundled backend/ and frontend/ were not found",
			"Reinstall Wizard (see the install instructions), or point WIZARD_ROOT at the directory containing backend/ and frontend/.")
	} else {
		r.add("root", "Wizard files", ui.OK, root, "")
	}

	// Config directory.
	cfg, cfgErr := appdir.ConfigDir()
	switch {
	case cfgErr != nil:
		r.add("config", "Config directory", ui.Fail, cfgErr.Error(), "Set WIZARD_CONFIG_DIR to a writable directory.")
	default:
		if err := dirWritable(cfg); err != nil {
			r.add("config", "Config directory", ui.Fail, fmt.Sprintf("%s is not writable: %v", cfg, err),
				"Fix the directory's permissions, or set WIZARD_CONFIG_DIR to a writable directory.")
		} else {
			r.add("config", "Config directory", ui.OK, cfg, "")
		}
	}

	// Prerequisites.
	addTool := func(id string, c ToolCheck, required bool) {
		switch {
		case c.OK:
			r.add(id, c.Name, ui.OK, strings.TrimSpace(c.Version+"  "+c.Path), "")
		case c.Found && !strings.HasPrefix(c.Version, "unusable"):
			r.add(id, c.Name, ui.Fail, fmt.Sprintf("%s at %s is older than the required %d.%d", c.Version, c.Path, c.MinMajor, c.MinMinor), c.InstallHint)
		case c.Found:
			r.add(id, c.Name, ui.Fail, fmt.Sprintf("found at %s but cannot run (%s)", c.Path, strings.TrimPrefix(c.Version, "unusable: ")), "Reinstall it: "+c.InstallHint)
		case required:
			r.add(id, c.Name, ui.Fail, "not found on PATH", c.InstallHint)
		default:
			r.add(id, c.Name, ui.Warn, "not found on PATH (optional)", c.InstallHint)
		}
	}
	addTool("python", CheckPython(minPythonMajor, minPythonMinor), true)
	addTool("node", CheckNode(minNodeMajor), true)
	addTool("uv", CheckUV(), true)
	addTool("pnpm", CheckPnpm(), true)

	// Configuration and build state need the checkout.
	var backendEnv, provider string
	if rootErr == nil {
		backendEnv = filepath.Join(repo.BackendDir(root), ".env")
		provider, _, _ = readEnvValue(backendEnv, "API_PROVIDER")
		if provider == "" {
			provider = "ollama"
		}
		if _, err := os.Stat(backendEnv); err != nil {
			r.add("env", "Configuration", ui.Warn, "backend/.env not created yet", "Run `wizard init` to configure a provider.")
		} else {
			r.add("env", "Configuration", ui.OK, fmt.Sprintf("provider %s (%s)", provider, backendEnv), "")
			if key := providerKeyName(provider); key != "" {
				if v, _, _ := readEnvValue(backendEnv, key); v == "" {
					r.add("credentials", "Credentials", ui.Warn, key+" is empty", "Run `wizard init` to enter it, or add it from the Models page.")
				} else {
					r.add("credentials", "Credentials", ui.OK, key+" is set", "")
				}
			}
		}
		if provider == "ollama" {
			addTool("ollama", CheckOllama(), false)
		}
		venvDir, _ := appdir.ConfigDir()
		venvPy := venvExe(filepath.Join(venvDir, "venv"), "python")
		_, venvErr := os.Stat(venvPy)
		_, feErr := os.Stat(filepath.Join(repo.FrontendDir(root), ".next", "standalone", "server.js"))
		switch {
		case venvErr != nil && feErr != nil:
			r.add("build", "Dependencies", ui.Warn, "not installed yet", "Run `wizard init`.")
		case venvErr != nil:
			r.add("build", "Dependencies", ui.Warn, "Python environment missing", "Run `wizard init`.")
		case feErr != nil:
			r.add("build", "Dependencies", ui.Warn, "frontend build missing", "Run `wizard init`.")
		default:
			r.add("build", "Dependencies", ui.OK, "Python environment and frontend build present", "")
		}
	}

	// Service state.
	if runDir, err := appdir.ConfigDir(); err == nil {
		if pid, alive := daemon.LiveAt(filepath.Join(runDir, "run", "daemon.pid")); alive {
			detail := fmt.Sprintf("running (pid %d)", pid)
			ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			h, herr := healthcheck.NewClient("http://127.0.0.1:" + recordedBackendPort(filepath.Join(runDir, "run"))).Health(ctx)
			cancel()
			switch {
			case herr != nil:
				r.add("service", "Service", ui.Warn, detail+", but the backend is not answering", "Check `wizard logs --tail 50`.")
			default:
				if mismatch, _ := compat.Mismatch(h.Version); mismatch {
					r.add("service", "Service", ui.Fail, detail+fmt.Sprintf("; backend API v%s does not match this CLI (v%s)", h.Version, compat.CompatAPIVersion),
						"Run `wizard update`, then `wizard start`.")
				} else {
					r.add("service", "Service", ui.OK, detail+fmt.Sprintf(", backend API v%s healthy", h.Version), "")
				}
			}
		} else {
			r.add("service", "Service", ui.OK, "not running (`wizard start` to launch)", "")
		}
	}

	// Environment that changes network behaviour.
	if proxy := firstNonEmpty(os.Getenv("HTTPS_PROXY"), os.Getenv("https_proxy"), os.Getenv("HTTP_PROXY"), os.Getenv("http_proxy")); proxy != "" {
		r.add("proxy", "Proxy", ui.OK, "using "+redactProxy(proxy), "")
	} else if verbose {
		r.add("proxy", "Proxy", ui.OK, "none configured", "")
	}

	if network {
		collectNetworkChecks(r, provider, backendEnv)
	}
	return r
}

func providerKeyName(provider string) string {
	return map[string]string{
		"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "custom_gateway": "GATEWAY_API_KEY",
	}[provider]
}

// redactProxy drops any user:password from a proxy URL before it is printed.
func redactProxy(raw string) string {
	if i := strings.Index(raw, "@"); i >= 0 {
		if j := strings.Index(raw, "://"); j >= 0 && j < i {
			return raw[:j+3] + "***@" + raw[i+1:]
		}
		return "***@" + raw[i+1:]
	}
	return raw
}

func collectNetworkChecks(r *doctorReport, provider, backendEnv string) {
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()

	if req, err := http.NewRequestWithContext(ctx, http.MethodHead, "https://api.github.com/", nil); err == nil {
		req.Header.Set("User-Agent", "wizard-cli-doctor")
		if resp, derr := releaseHTTPClient.Do(req); derr != nil {
			r.add("net-github", "GitHub Releases", ui.Warn, "unreachable: "+redactURLError(derr).Error(),
				"`wizard update` needs github.com. Behind a proxy? Set HTTPS_PROXY.")
		} else {
			resp.Body.Close()
			r.add("net-github", "GitHub Releases", ui.OK, "reachable", "")
		}
	}

	if provider == "" || backendEnv == "" {
		return
	}
	src := modelSource{Provider: provider}
	if key := providerKeyName(provider); key != "" {
		src.APIKey, _, _ = readEnvValue(backendEnv, key)
	}
	if provider == "custom_gateway" {
		src.BaseURL, _, _ = readEnvValue(backendEnv, "GATEWAY_API_URL")
	} else if baseKey, ok := baseURLKeyFor(provider); ok {
		src.BaseURL, _, _ = readEnvValue(backendEnv, baseKey)
	}
	models, err := discoverModels(ctx, src)
	switch {
	case err == nil:
		r.add("net-provider", "Model provider", ui.OK, fmt.Sprintf("%s reachable, %d chat model(s) available", provider, len(models.Chat)), "")
	case strings.Contains(err.Error(), errKeyRejected.Error()):
		r.add("net-provider", "Model provider", ui.Fail, provider+" rejected the configured API key", "Run `wizard init` to enter a valid key.")
	default:
		r.add("net-provider", "Model provider", ui.Warn, fmt.Sprintf("%s: %v", provider, err), providerReachHint(provider))
	}
}

func providerReachHint(provider string) string {
	switch provider {
	case "ollama":
		return "Start Ollama (`ollama serve`) or install it from https://ollama.com/download."
	case "lmstudio":
		return "Start the LM Studio local server."
	}
	return "Check your network or proxy settings."
}

// checkOnPath verifies that typing `wizard` runs this binary from any
// directory, and says exactly how to fix it when it does not.
func checkOnPath(exe string, info installkind.Info) (ui.Status, string, string) {
	found, err := exec.LookPath("wizard")
	if err != nil {
		if runtime.GOOS == "windows" {
			for _, entry := range persistedPathEntries() {
				if sameDir(entry, filepath.Dir(exe)) {
					return ui.Warn, "installed, but this terminal was opened before PATH changed",
						"Open a new terminal window and run `wizard --version`."
				}
			}
		}
		return ui.Warn, "`wizard` is not on PATH in this shell", pathFixHint(exe, info)
	}
	if !sameFile(found, exe) {
		return ui.Warn, fmt.Sprintf("`wizard` resolves to a different install: %s", found),
			"Two installs can shadow each other. Remove one, or put the directory of the one you want first on PATH."
	}
	return ui.OK, "`wizard` runs from any directory", ""
}

func pathFixHint(exe string, info installkind.Info) string {
	dir := filepath.Dir(exe)
	if info.InstallRoot != "" {
		dir = filepath.Join(info.InstallRoot, "bin")
	}
	if runtime.GOOS == "windows" {
		return fmt.Sprintf("Add it for this session:  $env:Path = \"$env:Path;%s\"\nAdd it permanently: re-run the installer (it updates your user PATH), or use Settings > Environment Variables.", dir)
	}
	return fmt.Sprintf("Add it for this session:  export PATH=\"%s:$PATH\"\nRe-run the installer to add it to your shell startup file.", dir)
}

func sameDir(a, b string) bool { return sameFile(a, b) }

// sameFile compares two paths after resolving symlinks and case rules.
func sameFile(a, b string) bool {
	ra, errA := filepath.EvalSymlinks(a)
	rb, errB := filepath.EvalSymlinks(b)
	if errA != nil || errB != nil {
		ra, rb = filepath.Clean(a), filepath.Clean(b)
	}
	if runtime.GOOS == "windows" || runtime.GOOS == "darwin" {
		return strings.EqualFold(ra, rb)
	}
	return ra == rb
}

// dirWritable reports whether files can be created in dir, or -- when it does
// not exist yet -- in its nearest existing ancestor, which is where init would
// create it. It leaves nothing behind.
func dirWritable(dir string) error {
	target := dir
	for {
		if info, err := os.Stat(target); err == nil {
			if !info.IsDir() {
				return fmt.Errorf("%s is not a directory", target)
			}
			break
		}
		parent := filepath.Dir(target)
		if parent == target {
			return fmt.Errorf("no existing parent directory")
		}
		target = parent
	}
	f, err := os.CreateTemp(target, ".wizard-doctor-*")
	if err != nil {
		return err
	}
	name := f.Name()
	f.Close()
	return os.Remove(name)
}
