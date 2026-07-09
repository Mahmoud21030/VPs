# Architecture

The system runs a Windows guest on QEMU inside an Ubuntu GitHub Actions runner. The immutable disk is `base.qcow2`. Persistent state is stored only in `overlay.qcow2`, a QCOW2 backing overlay referencing the base image.

The overlay preserves installed applications, Windows configuration, files, registry changes, and user profile state. Checkpoints compress the overlay with zstd, generate SHA256 metadata, upload it to Backblaze B2 through the S3 API, and verify the uploaded object by downloading it and checking the digest.

QMP controls shutdown, pause, resume, and filesystem-freeze operations. QEMU is not killed directly during normal operation.
