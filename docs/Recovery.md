# Recovery

Recovery downloads `base.qcow2`, then attempts to restore `latest`. If checksum verification fails, the restore engine attempts `checkpoint1`, `checkpoint2`, then `checkpoint3`.

Each checkpoint includes:

- `overlay.qcow2.zst`
- `overlay.sha256`
- `manifest.json`

The manifest uses version `1` and records creation time, checkpoint name, SHA256, compressed filename, and base object.
