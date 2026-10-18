#!/usr/bin/env bash
# Installs the pinned Terraform toolchain for the Terraform pipelines (Linux x86_64 agents).
#
# Usage: install_tf_tools.sh [tools-dir]      (default: ./.tools)
#
#   terraform  downloaded zip, SHA256 checked against the value pinned below
#   tflint     downloaded zip, SHA256 checked against the value pinned below
#   checkov    pip install into its own venv from requirements-tf.txt (exact version)
#
# Versions must match terraform/envs/*/versions.tf (tests/test_terraform_pipelines.py checks this).
# On Azure Pipelines the bin directory is added to PATH for later steps.
set -euo pipefail

TERRAFORM_VERSION="1.12.2"
TERRAFORM_SHA256="1eaed12ca41fcfe094da3d76a7e9aa0639ad3409c43be0103ee9f5a1ff4b7437"
TFLINT_VERSION="0.64.0"
TFLINT_SHA256="cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537"

TOOLS_DIR="${1:-${PWD}/.tools}"
BIN_DIR="${TOOLS_DIR}/bin"

log() { echo "[run ${RUN_ID:-local}] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# download_verified <url> <sha256> <destination>
download_verified() {
  local url="$1" expected="$2" dest="$3" actual
  curl --fail --silent --show-error --location --retry 3 --output "${dest}" "${url}" \
    || die "download failed: ${url}"
  actual="$(sha256sum "${dest}" | cut -d' ' -f1)"
  [[ "${actual}" == "${expected}" ]] || die "checksum mismatch for ${url}: expected ${expected}, got ${actual}"
}

install_terraform() {
  local zip="${TOOLS_DIR}/terraform.zip"
  download_verified \
    "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
    "${TERRAFORM_SHA256}" "${zip}"
  unzip -oq "${zip}" terraform -d "${BIN_DIR}"
  rm -f "${zip}"
}

install_tflint() {
  local zip="${TOOLS_DIR}/tflint.zip"
  download_verified \
    "https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}/tflint_linux_amd64.zip" \
    "${TFLINT_SHA256}" "${zip}"
  unzip -oq "${zip}" tflint -d "${BIN_DIR}"
  rm -f "${zip}"
}

install_checkov() {
  python3 -m venv "${TOOLS_DIR}/checkov-venv"
  "${TOOLS_DIR}/checkov-venv/bin/pip" install --quiet --disable-pip-version-check -r requirements-tf.txt
  ln -sf "${TOOLS_DIR}/checkov-venv/bin/checkov" "${BIN_DIR}/checkov"
}

main() {
  mkdir -p "${BIN_DIR}"
  install_terraform
  install_tflint
  install_checkov
  echo "##vso[task.prependpath]${BIN_DIR}"
  PATH="${BIN_DIR}:${PATH}"
  terraform version
  tflint --version
  checkov --version
  log "Terraform toolchain installed in ${TOOLS_DIR}"
}

main
