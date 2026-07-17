Windows VM live checkpoint + 15 minute interval fix

This patch fixes:
  qemu-img: Failed to get "write" lock
  Is another process using the image?

Cause:
  The running QEMU process keeps the active QCOW2 image locked even when the
  guest is paused. The old code paused the VM and then ran qemu-img directly
  against the active disk, which fails.

New online checkpoint path:
  Running Windows VM
    -> QMP drive-backup point-in-time full backup
    -> wait for QEMU backup job to conclude
    -> qemu-img checks only the completed backup target
    -> zstd compress
    -> upload to Backblaze/Oracle
    -> download and SHA256 verify

New offline shutdown path:
  Windows/QEMU stopped
    -> qemu-img check/compact inactive overlay
    -> zstd compress
    -> upload and verify

Automatic checkpoint interval:
  Default: 15 minutes
  Workflow input: checkpoint_interval_minutes

Previous fixes preserved:
  - Oracle checksum/chunked-upload compatibility
  - Oracle S3-to-S3 copy compatibility with --copy-props none
  - normal Windows shutdown offline save
  - checkpoint lock
  - 220G/resizable disk support
  - VirtIO CD support

Expected online success:
  checkpoint uploaded and verified: latest (online-live-backup)

Expected shutdown success:
  checkpoint uploaded and verified: latest (offline-stopped)
  final latest checkpoint completed successfully
