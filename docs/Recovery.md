# Recovery

Recovery is intentionally fail-closed:

1. Download and validate `windows-vm/base.qcow2`.
2. Directly download `windows-vm/latest/overlay.qcow2.zst`, showing provider errors.
3. Log `overlay archive downloaded successfully from latest`.
4. Download `windows-vm/latest/overlay.sha256`.
5. Verify the archive SHA256.
6. Download and validate `windows-vm/latest/manifest.json`, including the immutable base SHA256 for manifest version 3.
7. Decompress atomically and validate the QCOW2 overlay and virtual size.
8. Log `restored overlay from latest`.
9. Allow the workflow to boot QEMU.

There is no silent listing probe and no rollback to `checkpoint1`, `checkpoint2`, or `checkpoint3`. Restore fails when latest is missing, inaccessible, corrupt, or cannot be decompressed.

A new blank overlay is permitted only when the manually dispatched runtime explicitly sets `allow_fresh_overlay_if_missing=true`. This option is for deliberate recovery/reset operations and can discard the expectation of stored Windows state.
