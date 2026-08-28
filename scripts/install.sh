#!/usr/bin/env bash
# Wizard Universal Installer for Linux and macOS
# Usage: curl -fsSL https://wizardw2.vercel.app/install.sh | bash
#
# Supported Operating Systems: Linux, macOS
# Supported Architectures: x86_64 (amd64), aarch64 (arm64)

set -euo pipefail

# ANSI styling
BOLD="\033[1m"
DIM="\033[2m"
GREEN="\033[32m"
PINK="\033[38;2;236;168;214m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

log_info() {
    echo -e "${PINK}[wizard-install]${RESET} $*"
}

log_success() {
    echo -e "${GREEN}[wizard-install] ✓${RESET} $*"
}

log_warn() {
    echo -e "${YELLOW}[wizard-install] ⚠${RESET} $*"
}

log_error() {
    echo -e "${RED}[wizard-install] ✗${RESET} $*" >&2
}

echo -e "\n${BOLD}${PINK}  Wizard — Autonomous AI Data Analyst Workspace${RESET}"
echo -e "${DIM}  Local-First • AST Sandboxed • Zero Cloud Telemetry${RESET}\n"

# 1. Detect Operating System
OS_TYPE="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "${OS_TYPE}" in
    linux*)  TARGET_OS="linux" ;;
    darwin*) TARGET_OS="darwin" ;;
    *)
        log_error "Unsupported operating system: ${OS_TYPE}. For Windows, please use: irm https://wizardw2.vercel.app/install.ps1 | iex"
        exit 1
        ;;
esac

# 2. Detect Architecture
ARCH_TYPE="$(uname -m)"
case "${ARCH_TYPE}" in
    x86_64|amd64)   TARGET_ARCH="amd64" ;;
    aarch64|arm64)  TARGET_ARCH="arm64" ;;
    *)
        log_error "Unsupported CPU architecture: ${ARCH_TYPE}"
        exit 1
        ;;
esac

log_info "Detected platform: ${BOLD}${TARGET_OS}-${TARGET_ARCH}${RESET}"

# 3. Resolve Target Version
REPO="Wizard-AIA/Wizard-w2"
TAG="${WIZARD_VERSION:-}"

if [ -z "${TAG}" ]; then
    log_info "Resolving latest release from GitHub..."
    if command -v curl >/dev/null 2>&1; then
        LATEST_TAG=$(curl -sSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null | grep '"tag_name":' | head -n 1 | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/' || true)
    fi
    TAG="${LATEST_TAG:-v1.0.2}"
fi

# Ensure tag has 'v' prefix
[[ "${TAG}" =~ ^v ]] || TAG="v${TAG}"
log_info "Target release: ${BOLD}${TAG}${RESET}"

# 4. Prepare Download URL and Destination
ASSET_NAME="Wizard-${TAG}-${TARGET_OS}-${TARGET_ARCH}.zip"
DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${TAG}/${ASSET_NAME}"

INSTALL_DIR="${WIZARD_HOME:-$HOME/.wizard}"
BIN_DIR="${INSTALL_DIR}/bin"
TMP_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t 'wizard-install')

cleanup() {
    rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

log_info "Downloading ${BOLD}${ASSET_NAME}${RESET}..."
if command -v curl >/dev/null 2>&1; then
    curl --http1.1 -fSL --retry 3 --retry-delay 2 "${DOWNLOAD_URL}" -o "${TMP_DIR}/${ASSET_NAME}" || {
        log_error "Failed to download asset from ${DOWNLOAD_URL}"
        exit 1
    }
elif command -v wget >/dev/null 2>&1; then
    wget --tries=3 -q --show-progress "${DOWNLOAD_URL}" -O "${TMP_DIR}/${ASSET_NAME}" || {
        log_error "Failed to download asset from ${DOWNLOAD_URL}"
        exit 1
    }
else
    log_error "Neither curl nor wget found in PATH. Please install one to continue."
    exit 1
fi

# 5. Extract and Install
log_info "Extracting to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}" "${BIN_DIR}"

if command -v unzip >/dev/null 2>&1; then
    unzip -q -o "${TMP_DIR}/${ASSET_NAME}" -d "${INSTALL_DIR}"
elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import zipfile; zipfile.ZipFile('${TMP_DIR}/${ASSET_NAME}').extractall('${INSTALL_DIR}')"
else
    log_error "Cannot unzip package: neither 'unzip' nor 'python3' is available."
    exit 1
fi

# Locate the unpacked package folder
PACKAGE_DIR="${INSTALL_DIR}/Wizard-${TAG}-${TARGET_OS}-${TARGET_ARCH}"
if [ ! -d "${PACKAGE_DIR}" ]; then
    # Fallback search if directory name differs
    PACKAGE_DIR=$(find "${INSTALL_DIR}" -maxdepth 1 -type d -name "Wizard-*" | head -n 1)
fi

if [ -n "${PACKAGE_DIR}" ] && [ -d "${PACKAGE_DIR}" ]; then
    ln -sfn "${PACKAGE_DIR}" "${INSTALL_DIR}/current"
    WIZARD_SRC_BIN="${PACKAGE_DIR}/cli/wizard"
else
    WIZARD_SRC_BIN=$(find "${INSTALL_DIR}" -type f -name "wizard" 2>/dev/null | head -n 1)
fi

WIZARD_BIN="${BIN_DIR}/wizard"
if [ -n "${WIZARD_SRC_BIN}" ] && [ -f "${WIZARD_SRC_BIN}" ]; then
    chmod +x "${WIZARD_SRC_BIN}"
    ln -sfn "${WIZARD_SRC_BIN}" "${WIZARD_BIN}"
else
    log_error "Failed to locate wizard binary in extracted archive."
    exit 1
fi

# 6. Configure Shell PATH
PATH_LINE="export PATH=\"${BIN_DIR}:\$PATH\""
UPDATED_SHELL=""

for RC_FILE in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    if [ -f "${RC_FILE}" ]; then
        if ! grep -q "${BIN_DIR}" "${RC_FILE}" 2>/dev/null; then
            echo -e "\n# Wizard CLI\n${PATH_LINE}" >> "${RC_FILE}"
            UPDATED_SHELL="${RC_FILE}"
        fi
        break
    fi
done

# Fish shell support
FISH_CONF="$HOME/.config/fish/config.fish"
if [ -d "$HOME/.config/fish" ]; then
    if [ -f "${FISH_CONF}" ] && ! grep -q "${BIN_DIR}" "${FISH_CONF}" 2>/dev/null; then
        echo -e "\n# Wizard CLI\nfish_add_path ${BIN_DIR}" >> "${FISH_CONF}"
        UPDATED_SHELL="${FISH_CONF}"
    fi
fi

echo ""
log_success "Wizard ${TAG} installed successfully to ${BOLD}${WIZARD_BIN}${RESET}!"

if [ -n "${UPDATED_SHELL}" ]; then
    echo -e "${DIM}  Added to PATH in ${UPDATED_SHELL}${RESET}"
fi

echo -e "\n${BOLD}Next steps:${RESET}"
if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
    echo -e "  1. Reload your shell:     ${GREEN}export PATH=\"${BIN_DIR}:\$PATH\"${RESET} (or restart terminal)"
fi
echo -e "  2. Initialize workspace:  ${GREEN}wizard init${RESET}"
echo -e "  3. Launch agent:          ${GREEN}wizard start${RESET}\n"
