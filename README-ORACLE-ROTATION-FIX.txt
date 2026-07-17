Oracle S3-to-S3 copy compatibility note

AWS CLI v2 can call HeadObject, GetObjectTagging, and PutObjectTagging while
copying S3 objects. Oracle-compatible endpoints may not implement the tagging
operations. Every S3-to-S3 copy in this project therefore adds:

  --copy-props none

Checkpoint rotation itself is disabled. The persistent layout retains only
base.qcow2 and latest/{overlay.qcow2.zst,overlay.sha256,manifest.json}; obsolete
objects are deleted only below windows-vm/checkpoints/.
