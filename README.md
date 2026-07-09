# Persistent Windows VM on GitHub Actions

## Installation

1. Create a private Backblaze B2 bucket.
2. Enable S3-compatible access.
3. Upload `base.qcow2` to `windows-vm/base.qcow2`.
4. Add GitHub repository secrets:
   - `B2_BUCKET`
   - `B2_ENDPOINT`
   - `B2_KEY_ID`
   - `B2_APPLICATION_KEY`
5. Run `.github/workflows/runtime.yml` manually.
6. Connect to RDP on the forwarded runner port after the workflow reports RDP readiness.

## Backblaze setup

Create an application key with read/write access to the private bucket. Use the Backblaze S3 endpoint for the bucket region, for example `https://s3.us-west-004.backblazeb2.com`.

Objects are stored under:

```text
windows-vm/base.qcow2
windows-vm/latest/overlay.qcow2.zst
windows-vm/latest/overlay.sha256
windows-vm/latest/manifest.json
windows-vm/checkpoints/checkpoint1/overlay.qcow2.zst
windows-vm/checkpoints/checkpoint2/overlay.qcow2.zst
windows-vm/checkpoints/checkpoint3/overlay.qcow2.zst
```

## GitHub Secrets

`B2_BUCKET` is the private bucket name.

`B2_ENDPOINT` is the S3 endpoint URL.

`B2_KEY_ID` is the Backblaze application key ID.

`B2_APPLICATION_KEY` is the Backblaze application key value.

## Updating base image

1. Stop all runtime workflows.
2. Create a new Windows `base.qcow2` locally.
3. Install VirtIO drivers, enable RDP, install LabVIEW, configure Windows, and shut down cleanly.
4. Upload the new immutable base:

```bash
aws --endpoint-url "$B2_ENDPOINT" s3 cp base.qcow2 "s3://$B2_BUCKET/windows-vm/base.qcow2"
```

5. Delete or archive incompatible overlays if the base changed incompatibly.
6. Start `runtime.yml`.

## Running

`runtime.yml` restores the latest valid overlay, creates one if absent, boots QEMU with UEFI, VirtIO disk, VirtIO networking, and RDP forwarding, then checkpoints every 120 minutes.

`checkpoint.yml` performs a manual checkpoint.

`maintenance.yml` restores and verifies stored state.

## Recovery

Restore order is automatic:

1. `latest`
2. `checkpoint1`
3. `checkpoint2`
4. `checkpoint3`

If checksum verification fails, restore continues to the next checkpoint. If no overlay exists, a new overlay is created using `base.qcow2` as the immutable backing file.

## Troubleshooting

RDP not ready: verify Windows RDP is enabled, firewall allows port 3389, and VirtIO network drivers are installed.

Checksum failure: inspect `logs/runtime.log`, verify Backblaze object consistency, and allow automatic rollback.

QEMU boot failure: verify `base.qcow2`, UEFI firmware paths, KVM availability, and VirtIO drivers.

Checkpoint failure: ensure QMP socket exists, Backblaze credentials are valid, bucket lifecycle rules are not deleting active objects, and available runner disk space exceeds the compacted overlay size plus compressed copy.

## Tests

```bash
scripts/bootstrap/install.sh
tests/run.sh
```

## Build and upload a minimal base image

Use `.github/workflows/base-image.yml` to create `base.qcow2` from a Windows ISO and upload it to Backblaze B2. This base image is clean Windows only. It does not install LabVIEW.

Required secrets:

- `B2_BUCKET`
- `B2_ENDPOINT`
- `B2_KEY_ID`
- `B2_APPLICATION_KEY`
- `WINDOWS_ADMIN_PASSWORD`

Run **Actions → build-base-image → Run workflow** with:

- `windows_iso_url`: direct HTTPS URL to a Windows ISO
- `virtio_iso_url`: direct HTTPS URL to `virtio-win.iso`
- `disk_size`: base disk size such as `80G`
- `memory_mb`: installer VM memory
- `cpu_cores`: installer VM CPUs
- `windows_iso_sha256`: optional checksum
- `virtio_iso_sha256`: optional checksum

The workflow creates `work/base.qcow2`, verifies it with `qemu-img check`, writes `work/base.sha256` and `work/base-manifest.json`, uploads `base.qcow2`, `base.sha256`, and `base-manifest.json` to the configured Backblaze B2 prefix, then downloads `base.qcow2` again and verifies the uploaded SHA256.
