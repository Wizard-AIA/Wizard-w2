// Package exitcode names the process exit statuses `wizard` uses, so scripts
// and CI can tell a bad invocation from a missing tool from a dead network
// without parsing messages. 0-2 keep the meaning they always had in 1.0.x.
package exitcode

const (
	// OK is success.
	OK = 0
	// Failure is any other runtime failure, including internal errors.
	Failure = 1
	// Usage is a bad command line or an invalid configuration value.
	Usage = 2
	// Environment is a missing or unusable dependency or installation: a
	// prerequisite tool, the bundled checkout, a filesystem location.
	Environment = 3
	// Network is a failed or unreachable network request.
	Network = 4
)
