package commands

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// readEnvValue does a best-effort scan of a .env-style file for KEY=value,
// the last assignment winning (matching how a real .env is actually
// applied). It exists only for `wizard status`/`doctor` to report a setting
// like EXECUTION_BACKEND without a Python dependency -- it is not a general
// .env parser and does not need to handle everything python-dotenv does.
//
// A missing file is reported as (found=false, err=nil) -- that is the
// ordinary state before `wizard init` runs. A scan failure (for example a
// line past bufio.Scanner's token limit) is returned as an error instead,
// so it is not silently indistinguishable from the key simply being absent.
func readEnvValue(path, key string) (string, bool, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", false, nil
	}
	defer f.Close()

	value, found := "", false
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.SplitN(line, "=", 2)
		if len(parts) != 2 || strings.TrimSpace(parts[0]) != key {
			continue
		}
		value = parseEnvValue(parts[1])
		found = true
	}
	if err := scanner.Err(); err != nil {
		return "", false, err
	}
	return value, found, nil
}

// parseEnvValue extracts the right-hand side of a KEY=<raw> assignment.
// backend/.env.example documents most of its keys with a trailing inline
// `# comment` on the same line (see MODEL_NAME's own entry) -- an unquoted
// value ends at the first whitespace+#, and a quoted one ends at its
// matching closing quote, so the comment after it is never folded into the
// value. Without this, a freshly created backend/.env's still-empty
// MODEL_NAME="" reads back as the comment text instead of "", which made
// pullDefaultModels think a fresh install already had a model pinned.
func parseEnvValue(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw != "" && (raw[0] == '"' || raw[0] == '\'') {
		quote := raw[0]
		if end := strings.IndexByte(raw[1:], quote); end >= 0 {
			return raw[1 : end+1]
		}
		return strings.Trim(raw, `"'`)
	}
	if idx := strings.Index(raw, " #"); idx >= 0 {
		raw = raw[:idx]
	}
	return strings.TrimSpace(raw)
}

// setEnvValue rewrites the first `KEY=...` assignment in a .env-style file to
// `KEY="value"`, or appends one if the key is not present. Two callers:
// ensureEnvFile, right after it creates a fresh backend/.env from
// .env.example, and applyProviderConfig (provider.go), which applies
// --provider/--data-mode/key flags to backend/.env whether or not it already
// existed -- an explicit flag is a deliberate instruction, unlike
// ensureEnvFile's own passive "leave an existing file as is" guarantee for
// the parts of it nothing asked to change.
func setEnvValue(path, key, value string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	lines := strings.Split(string(data), "\n")
	assignment := fmt.Sprintf("%s=%q", key, value)
	replaced := false
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		parts := strings.SplitN(trimmed, "=", 2)
		if len(parts) == 2 && strings.TrimSpace(parts[0]) == key {
			lines[i] = assignment
			replaced = true
			break
		}
	}
	if !replaced {
		lines = append(lines, assignment)
	}
	return os.WriteFile(path, []byte(strings.Join(lines, "\n")), 0o600)
}

var apiVersionPattern = regexp.MustCompile(`API_VERSION\s*=\s*"([^"]+)"`)

// readAPIVersionFromSource reads API_VERSION straight out of
// backend/src/api/routes/meta.py by pattern, without running Python. Used
// only by `wizard update`, right after a git pull and before anything is
// restarted, when there is no live backend to ask via /health yet.
func readAPIVersionFromSource(env *Env) (string, error) {
	path := filepath.Join(env.BackendDir, "src", "api", "routes", "meta.py")
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	m := apiVersionPattern.FindSubmatch(data)
	if m == nil {
		return "", fmt.Errorf("API_VERSION not found in %s", path)
	}
	return string(m[1]), nil
}
