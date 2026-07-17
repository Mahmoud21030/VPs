# Persistent Windows VM in GitHub Actions

This repository runs a persistent Windows virtual machine entirely inside a GitHub-hosted `ubuntu-latest` job. It uses QEMU/KVM with a Q35 machine, OVMF UEFI, an immutable QCOW2 base, a writable sparse QCOW2 overlay, VirtIO storage/networking, selectable Tailscale/Cloudflare/SSH-relay connectivity, RDP, and a permanently available emergency VNC console.

It is intentionally not a cloud VM, self-hosted runner, Windows runner, or Windows container.

## Persistent object layout

Each selected bucket keeps one immutable base and one verified latest checkpoint:

```text
windows-vm/base.qcow2
windows-vm/latest/overlay.qcow2.zst
windows-vm/latest/overlay.sha256
windows-vm/latest/manifest.json
```

The runtime deletes obsolete objects only below `windows-vm/checkpoints/`. It never deletes `windows-vm/base.qcow2` or anything below `windows-vm/latest/`.

## Required secrets

Common Tailscale secrets:

- `TS_OAUTH_CLIENT_ID`
- `TS_OAUTH_SECRET`

The OAuth client must be allowed to create `tag:ci` devices.

Cloudflare Tunnel alternative:

- `CLOUDFLARE_TUNNEL_TOKEN`
- Two remotely managed TCP hostnames configured for `tcp://127.0.0.1:3389` and `tcp://127.0.0.1:5900`
- Cloudflare Access policies protecting both hostnames

SSH reverse-relay alternative:

- `SSH_RELAY_PRIVATE_KEY`
- `SSH_RELAY_KNOWN_HOSTS`
- A VPS/bastion account that permits remote TCP forwarding

Backblaze B2:

- `B2_BUCKET`
- `B2_ENDPOINT`
- `B2_KEY_ID`
- `B2_APPLICATION_KEY`
- Optional `B2_REGION`

Oracle Object Storage S3 compatibility API:

- `ORACLE_BUCKET`
- `ORACLE_NAMESPACE`
- `ORACLE_REGION`
- `ORACLE_ACCESS_KEY_ID`
- `ORACLE_SECRET_ACCESS_KEY`
- Optional `ORACLE_ENDPOINT`

The Oracle access and secret values are OCI Customer Secret Keys, not OCI API-signing keys. When `ORACLE_ENDPOINT` is absent, the default is:

```text
https://<namespace>.compat.objectstorage.<region>.oci.customer-oci.com
```

All AWS CLI operations use path-style S3v4 requests and the `when_required` request/response checksum settings needed by Oracle-compatible endpoints.

## Create the immutable Windows base

Run `.github/workflows/base-image.yml`. Supply direct Windows and VirtIO ISO URLs, choose the target provider, and use VNC at the Tailscale address printed by the job. During Windows Setup, load the VirtIO storage driver from the mounted driver ISO if the disk is not visible. Install NetKVM for networking, enable RDP, finish configuration, and shut Windows down normally.

The workflow validates, compacts, uploads, downloads, and SHA256-verifies `base.qcow2`. Normal runtime never modifies this object.

The copy workflow `.github/workflows/copy-base-to-oracle.yml` can stage the Backblaze base locally, upload it to Oracle, download it again, and verify the full SHA256.

## Run the VM

Run `.github/workflows/runtime.yml` and select:

- `storage_provider`: `backblaze` or `oracle`
- `network_provider`: `tailscale`, `cloudflare`, or `ssh-relay`
- `memory_mb`: default `15360`
- `cpu_cores`: default `4`
- `disk_size`: default `220G`; whole-GiB values strictly above `80G`
- `checkpoint_interval_minutes`: default `15`; any positive integer
- `compression_level`: default `10`; levels `1` through `22`
- `forced_save_after_minutes`: default `300`; maximum `330`
- `allow_fresh_overlay_if_missing`: default `false`
- `virtio_iso_url` and optional `virtio_iso_sha256`

The job prints its OS, CPU, logical CPU count, RAM, swap, block devices, filesystems, mount points, available capacity, and raw free bytes before checkout. It then places the physical VM working directory on the writable Linux filesystem with the most free space and symlinks repository `work/` to it. Linux mount points are discovered dynamically; no Windows-style host drive letters are assumed.

With Tailscale, connection addresses are private tailnet endpoints:

```text
RDP: <TAILSCALE_IP>:3389
VNC: <TAILSCALE_IP>:5900
```

Cloudflare uses authenticated TCP hostnames and requires `cloudflared access tcp` on the client. SSH relay creates loopback-only reverse ports on your VPS; use an SSH local-forward command printed by the workflow to reach them. VNC remains active with every provider even when RDP works.

QEMU binds both forwarded ports directly to the Tailscale IPv4 address or to loopback for Cloudflare/SSH relay, never every host interface. Workflow inputs are passed through environment variables instead of being interpolated into shell programs, and third-party Actions are pinned to immutable commit SHAs.

## Restore safety

Restore always downloads `base.qcow2`, directly downloads `latest/overlay.qcow2.zst`, downloads the SHA256 sidecar, verifies it, and only then decompresses and checks the overlay. It does not use a quiet `aws s3 ls` existence probe.

The manifest is also validated. Version 3 manifests bind the overlay to the SHA256 of the immutable base, preventing a checkpoint from booting against a silently replaced or incompatible base image. Existing version 2 manifests are accepted for migration and upgraded by the next verified checkpoint.

AWS object transfers disable the CLI's carriage-return progress renderer because GitHub Actions expands every refresh into a separate log line. Storage errors remain fully visible, and each successful transfer prints one timestamped completion line.

Missing, inaccessible, corrupt, or undecompressible latest state fails the job by default. A blank overlay is created only when `allow_fresh_overlay_if_missing=true` was selected deliberately.

## Disk growth

The virtual disk is sparse QCOW2. Existing overlays smaller than the requested size are expanded before QEMU starts. Equal sizes are left unchanged, and shrinking is refused.

After increasing `disk_size`, extend the Windows C: partition in Disk Management or an elevated PowerShell prompt, for example:

```powershell
$size = Get-PartitionSupportedSize -DriveLetter C
Resize-Partition -DriveLetter C -Size $size.SizeMax
```

## Checkpoints and forced save

Checkpoints use an inter-process lock and never overlap. The automatic loop waits for the configured interval, runs one checkpoint to completion, and then begins the next wait. A failed automatic checkpoint is logged and the loop continues.

For a running guest, QEMU QMP `drive-backup` creates a full point-in-time QCOW2 target with `sync=full`. The job is monitored to a successful concluded state and dismissed. Only that completed target is checked and converted back into a sparse overlay backed by the immutable base.

After normal Windows shutdown, the missing QEMU PID/QMP socket is expected. The inactive overlay is checked, compacted only when sufficient temporary capacity exists, compressed, uploaded, downloaded again, and SHA256-verified.

At approximately 300 minutes from job start, the supervisor stops the automatic loop cleanly, completes a final online checkpoint, verifies the remote bytes, shuts QEMU down, and ends normally. GitHub cancellation cleanup is only best effort because the hosted runner can terminate compression or upload with signal 143.

## Compression

Compression writes `overlay.qcow2.zst.tmp` and renames it only after zstd succeeds. Upload never starts until compression is complete. Levels above 19 add `--ultra`.

Higher levels can reduce stored bytes but cost more CPU, memory, and time. Level 10 is the recommended balance. Levels 19 or 22 may not complete before the job is terminated.

## VirtIO drivers in Windows

The VirtIO ISO is mounted read-only at every runtime boot. In an elevated Windows terminal, drivers can be installed recursively with:

```cmd
pnputil /add-driver "D:\*.inf" /subdirs /install
```

The actual CD drive letter may differ. NetKVM is the most important network driver for RDP connectivity.

## Local validation

```bash
python3 -m py_compile scripts/libvm.py scripts/libbase.py tests/test_core.py
bash -n scripts/**/*.sh tests/run.sh
tests/run.sh
```
