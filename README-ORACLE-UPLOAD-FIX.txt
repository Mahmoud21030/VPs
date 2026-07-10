Oracle checkpoint upload fix

This patch fixes:
  NotImplemented: AWS chunked encoding not supported
during multipart UploadPart operations to OCI Object Storage S3 compatibility API.

Changed files:
  .github/workflows/runtime.yml
  .github/workflows/base-image.yml
  scripts/libvm.py
  scripts/libbase.py
  scripts/storage/login

The fix applies:
  AWS_REQUEST_CHECKSUM_CALCULATION=when_required
  AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
  AWS_EC2_METADATA_DISABLED=true

It also writes ~/.aws/config with:
  request_checksum_calculation = when_required
  response_checksum_validation = when_required
  addressing_style = path
  signature_version = s3v4

Expected successful final log:
  checkpoint uploaded and verified: latest (offline-stopped)
  final latest checkpoint completed successfully

Important:
  The OCI dedicated S3-compatible endpoint format ending in
  .oci.customer-oci.com is intentionally kept unchanged.
