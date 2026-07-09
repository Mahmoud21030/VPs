#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/../common.sh"
log "installing host dependencies"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils ovmf zstd jq python3 python3-pip awscli netcat-openbsd socat ca-certificates coreutils
python3 -m pip install --user --break-system-packages pytest >/dev/null
log "dependencies installed"
