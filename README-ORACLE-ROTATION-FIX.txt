Oracle checkpoint rotation GetObjectTagging fix

Failure fixed:
  NotImplemented when calling GetObjectTagging
  S3 Get Object tagging operation is not supported

Cause:
  AWS CLI v2 performs extra tag/metadata API calls during S3-to-S3
  multipart copies by default.

Fix:
  For remote-to-remote checkpoint rotation copies, the command now adds:
    --copy-props none

This keeps the object bytes but avoids GetObjectTagging/PutObjectTagging
calls that OCI's S3-compatible endpoint rejects.

Previous fixes retained in this libvm.py:
  - Windows normal-shutdown offline checkpoint save
  - checkpoint locking
  - Oracle checksum/chunked-upload compatibility settings
  - 220G/resizable VM disk support
  - VirtIO CD support

Expected rotation log:
  remote object copy without tags/metadata: s3://...latest/... -> s3://...checkpoint1/...

Expected final success log:
  checkpoint uploaded and verified: latest (offline-stopped)
  final latest checkpoint completed successfully
