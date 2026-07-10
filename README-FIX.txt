# Windows VM shutdown-save fix

This patch fixes the failure where Windows shuts down, QEMU removes
`work/qemu.pid` and `work/qmp.sock`, and the final checkpoint crashes before
uploading.

Changed files:

- `.github/workflows/runtime.yml`
- `scripts/libvm.py`
- `scripts/cleanup/cleanup`

After Windows shuts down normally, the runtime now:

1. Detects the missing/exited QEMU process without treating it as an error.
2. Stops the periodic checkpoint loop.
3. Creates an offline checkpoint from the stopped QCOW2 overlay.
4. Compresses it.
5. Uploads it to the selected Backblaze or Oracle storage provider.
6. Downloads the uploaded checkpoint again and verifies SHA256.
7. Prints `checkpoint uploaded and verified: latest (offline-stopped)` on success.
