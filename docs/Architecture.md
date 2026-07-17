# Architecture

The Windows guest runs under QEMU/KVM on `ubuntu-latest` with Q35 and OVMF UEFI. QEMU attaches a read-only VirtIO driver ISO, a VirtIO network adapter, and a sparse writable QCOW2 overlay backed by the immutable `base.qcow2`. The base object is downloaded for each ephemeral runner but is never opened as the writable guest disk.

QEMU user networking forwards TCP 3389 for RDP. The QEMU VNC server listens on TCP 5900 as an independent emergency console. Both listeners bind specifically to the runner's ephemeral Tailscale IPv4 address under `tag:ci`; they do not bind to every host interface.

Checkpoint manifest version 3 records both the compressed overlay SHA256 and the immutable base SHA256. Restore rejects archive, manifest, virtual-size, and base-identity mismatches before boot. A legacy version 2 manifest is accepted for migration and upgraded by the next verified checkpoint.

Online persistence uses QMP `drive-backup` with the configured disk ID and `sync=full`. QMP job status is monitored until the job concludes, errors and timeouts fail the checkpoint, and the concluded job is dismissed. `qemu-img` touches only the released backup target. The full point-in-time image is converted to a sparse QCOW2 overlay backed by the immutable base before compression.

Offline persistence begins only after QEMU has stopped. It checks the inactive overlay, compacts it when temporary capacity is sufficient, compresses to an atomic temporary file, uploads the archive and metadata, downloads the archive again, and reports success only after the downloaded SHA256 matches.

All workflows that read or mutate VM object storage share one non-canceling GitHub Actions concurrency group. Runtime, manual checkpoint, maintenance, base-image creation, and Oracle base-copy operations therefore queue rather than racing over `base.qcow2` or `latest/`.
