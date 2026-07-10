# Setup

## GitHub secrets

Common:

- `TS_OAUTH_CLIENT_ID`
- `TS_OAUTH_SECRET`

Backblaze B2:

- `B2_BUCKET`
- `B2_ENDPOINT`
- `B2_KEY_ID`
- `B2_APPLICATION_KEY`
- Optional `B2_REGION`

Oracle OCI Object Storage through the S3 Compatibility API:

- `ORACLE_BUCKET`
- `ORACLE_NAMESPACE`
- `ORACLE_REGION`
- `ORACLE_ACCESS_KEY_ID`
- `ORACLE_SECRET_ACCESS_KEY`
- Optional `ORACLE_ENDPOINT`

If `ORACLE_ENDPOINT` is omitted, the scripts derive:

```text
https://<namespace>.compat.objectstorage.<region>.oci.customer-oci.com
```

## Provider selection

Choose `backblaze` or `oracle` from the `storage_provider` workflow input. Use the same provider for base creation, runtime, manual checkpoints, and recovery. For scheduled maintenance, set the repository variable `STORAGE_PROVIDER`; otherwise it defaults to `backblaze`.
