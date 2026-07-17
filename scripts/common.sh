#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$ROOT_DIR/work"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$WORK_DIR" "$LOG_DIR"
export ROOT_DIR WORK_DIR LOG_DIR
log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_DIR/runtime.log"; }
require(){ command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 127; }; }
retry(){ local max="$1" delay="$2" cap="$3"; shift 3; local n=1; until "$@"; do if [ "$n" -ge "$max" ]; then return 1; fi; sleep "$delay"; n=$((n+1)); delay=$((delay*2)); [ "$delay" -gt "$cap" ] && delay="$cap"; done; }
qemu_is_running(){ python3 "$ROOT_DIR/scripts/libvm.py" is-running >/dev/null 2>&1; }
pid_matches_command(){
  local pid="$1" expected="$2" cmdline
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
  [[ "$cmdline" == *"$expected"* ]]
}
