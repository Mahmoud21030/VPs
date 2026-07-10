#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK_LINK="${ROOT_DIR}/work"

echo "============================================================"
echo "Selecting largest writable filesystem for VM working storage"
echo "============================================================"

echo
echo "Available filesystems:"
df -hT

best_mount=""
best_available=0

while IFS=$'\t' read -r available mountpoint; do
    [[ -n "$available" && -n "$mountpoint" ]] || continue

    case "$mountpoint" in
        /proc*|/sys*|/dev*|/run*|/snap*)
            continue
            ;;
    esac

    probe="${mountpoint%/}/.vm-work-probe-${GITHUB_RUN_ID:-$$}-${GITHUB_RUN_ATTEMPT:-1}"

    if sudo mkdir -p "$probe" 2>/dev/null; then
        sudo rmdir "$probe" 2>/dev/null || true

        if (( available > best_available )); then
            best_available="$available"
            best_mount="$mountpoint"
        fi
    fi
done < <(
    df -B1 -P \
        -x tmpfs \
        -x devtmpfs \
        -x squashfs \
        -x proc \
        -x sysfs \
        -x cgroup \
        -x cgroup2 \
        -x efivarfs \
    | awk 'NR > 1 {print $4 "\t" $6}' \
    | sort -nr
)

if [[ -z "$best_mount" ]]; then
    echo "::error::Could not find a writable filesystem for VM working storage"
    exit 1
fi

run_id="${GITHUB_RUN_ID:-local}"
attempt="${GITHUB_RUN_ATTEMPT:-1}"

if [[ "$best_mount" == "/" ]]; then
    vm_work_dir="/windows-vm-work-${run_id}-${attempt}"
else
    vm_work_dir="${best_mount%/}/windows-vm-work-${run_id}-${attempt}"
fi

sudo mkdir -p "$vm_work_dir"
sudo chown "$(id -u):$(id -g)" "$vm_work_dir"

if [[ -L "$WORK_LINK" ]]; then
    rm -f "$WORK_LINK"
elif [[ -e "$WORK_LINK" ]]; then
    if [[ -d "$WORK_LINK" ]]; then
        cp -a "$WORK_LINK/." "$vm_work_dir/" 2>/dev/null || true
    fi
    rm -rf "$WORK_LINK"
fi

ln -s "$vm_work_dir" "$WORK_LINK"

available_human="$(df -hP "$best_mount" | awk 'NR==2 {print $4}')"

echo
echo "Selected filesystem mount: $best_mount"
echo "Selected free space:       $available_human"
echo "Physical VM work path:     $vm_work_dir"
echo "Repository work symlink:   $WORK_LINK -> $vm_work_dir"

echo "VM_WORK_DIR=$vm_work_dir" >> "${GITHUB_ENV:-/dev/null}"
echo "VM_WORK_MOUNT=$best_mount" >> "${GITHUB_ENV:-/dev/null}"

{
    echo "### VM working storage"
    echo
    echo "- Selected mount: \`$best_mount\`"
    echo "- Free space at selection: \`$available_human\`"
    echo "- Physical path: \`$vm_work_dir\`"
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"

echo
echo "Final workspace details:"
ls -ld "$WORK_LINK" "$vm_work_dir"
df -hT "$best_mount"
