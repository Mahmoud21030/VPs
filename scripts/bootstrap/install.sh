#!/usr/bin/env bash
set -Eeuo pipefail

log() {
    printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

log "installing host dependencies"

export DEBIAN_FRONTEND=noninteractive

sudo apt-get update

sudo apt-get install -y \
    ca-certificates \
    curl \
    jq \
    unzip \
    zstd \
    qemu-system-x86 \
    qemu-utils \
    ovmf \
    python3-pytest \
    python3-yaml \
    socat \
    netcat-openbsd \
    genisoimage

if ! command -v aws >/dev/null 2>&1; then
    log "installing AWS CLI v2"

    arch="$(uname -m)"

    case "$arch" in
        x86_64|amd64)
            aws_arch="x86_64"
            ;;
        aarch64|arm64)
            aws_arch="aarch64"
            ;;
        *)
            log "unsupported architecture: $arch"
            exit 1
            ;;
    esac

    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT

    curl \
        --fail \
        --location \
        --retry 5 \
        --retry-delay 3 \
        --retry-all-errors \
        "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}.zip" \
        --output "${tmp_dir}/awscliv2.zip"

    unzip -q "${tmp_dir}/awscliv2.zip" -d "$tmp_dir"

    sudo "${tmp_dir}/aws/install"

    rm -rf "$tmp_dir"
    trap - EXIT
fi

log "verifying dependencies"

required_commands=(
    aws
    curl
    genisoimage
    jq
    qemu-img
    qemu-system-x86_64
    socat
    unzip
    zstd
)

for command_name in "${required_commands[@]}"; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        log "required command missing: $command_name"
        exit 1
    fi
done

log "AWS CLI version: $(aws --version 2>&1)"
log "QEMU version: $(qemu-system-x86_64 --version | head -n 1)"
log "host dependencies installed successfully"
