Single base + single latest overlay storage policy

Persistent objects kept:
  windows-vm/base.qcow2
  windows-vm/latest/overlay.qcow2.zst
  windows-vm/latest/overlay.sha256
  windows-vm/latest/manifest.json

The SHA256 and manifest files are tiny metadata files.

Historical copies removed:
  windows-vm/checkpoints/checkpoint1/
  windows-vm/checkpoints/checkpoint2/
  windows-vm/checkpoints/checkpoint3/
  and anything else under windows-vm/checkpoints/

The runtime now:
  1. Authenticates selected storage provider.
  2. Deletes only the old checkpoints/ prefix.
  3. Leaves base.qcow2 untouched.
  4. Leaves latest/ untouched.
  5. Restores the latest overlay.
  6. Future checkpoints overwrite latest instead of rotating old copies.

Preserved previous fixes:
  - Configurable automatic checkpoint interval
  - QEMU point-in-time live backup
  - Offline checkpoint after normal Windows shutdown
  - Oracle multipart checksum/chunked-upload compatibility
  - Oracle --copy-props none compatibility
  - 220G/resizable disk
  - VirtIO CD
  - RDP + VNC
  - Backblaze/Oracle provider selection
