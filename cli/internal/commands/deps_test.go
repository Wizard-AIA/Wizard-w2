package commands

import (
	"path/filepath"
	"reflect"
	"testing"
)

// Every provider's client is in requirements.txt (v1.0.14), so the install must
// not vary with the configured provider or data mode. Before then a cloud or
// hybrid setup also pulled requirements-optional.txt to get the clients, along
// with Redis and every database driver nobody had asked for.
func TestBackendInstallIsTheSameForEveryProvider(t *testing.T) {
	configs := map[string]string{
		"nothing configured": "",
		"ollama":             "API_PROVIDER=ollama\n",
		"anthropic":          "API_PROVIDER=anthropic\n",
		"openai":             "API_PROVIDER=openai\n",
		"gemini":             "API_PROVIDER=gemini\n",
		"custom gateway":     "API_PROVIDER=custom_gateway\n",
		"lmstudio":           "API_PROVIDER=lmstudio\n",
		"hybrid":             "DATA_MODE=hybrid\n",
		"cloud-only":         "DATA_MODE=cloud-only\n",
	}

	var baseline []string
	for name, content := range configs {
		t.Run(name, func(t *testing.T) {
			backendDir := filepath.Join(t.TempDir(), "backend")
			env := newTestEnv(t, backendDir)
			if content != "" {
				writeBackendEnv(t, backendDir, content)
			}

			// The files it installs; the venv's Python path is per-environment, not the point.
			var files []string
			args := backendInstallArgs(env)
			for i, arg := range args {
				if arg == "-r" && i+1 < len(args) {
					files = append(files, args[i+1])
				}
			}
			if !reflect.DeepEqual(files, []string{"requirements.txt", "requirements-local.txt"}) {
				t.Fatalf("%s: installs %v, want requirements.txt and requirements-local.txt only", name, files)
			}
			if baseline == nil {
				baseline = files
			} else if !reflect.DeepEqual(files, baseline) {
				t.Fatalf("%s: install differs from the other configurations\n got %v\nwant %v", name, files, baseline)
			}
		})
	}
}
