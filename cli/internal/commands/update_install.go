package commands

import "wizard/internal/installkind"

// managedInstallRoot recognizes only the layout created by the official
// installers; see installkind.ManagedInstallRoot.
func managedInstallRoot(repoRoot string) (string, error) {
	return installkind.ManagedInstallRoot(repoRoot)
}
