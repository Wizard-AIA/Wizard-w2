package commands

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"
)

func TestRunSkillsWithNoVenvFailsWithInitHint(t *testing.T) {
	errBuf := &bytes.Buffer{}
	env := &Env{
		RepoRoot:   t.TempDir(),
		BackendDir: filepath.Join(t.TempDir(), "backend"),
		VenvDir:    filepath.Join(t.TempDir(), "venv"), // never created
		Out:        &bytes.Buffer{},
		Err:        errBuf,
	}

	code := RunSkills(env, []string{"list"})

	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	if !strings.Contains(errBuf.String(), "wizard init") {
		t.Fatalf("expected the error to point at `wizard init`, got: %s", errBuf.String())
	}
}
