# Setup

## Tailscale

Create an OAuth client with permission to create devices using `tag:ci`, then add `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET` as GitHub repository secrets. Tailnet policy must allow the intended administrator devices to reach the tagged CI node on TCP 3389 and 5900.

## Backblaze B2

Add `B2_BUCKET`, `B2_ENDPOINT`, `B2_KEY_ID`, and `B2_APPLICATION_KEY`. `B2_REGION` is optional because the scripts derive it from standard Backblaze S3 endpoint hostnames.

## Oracle Object Storage

Add `ORACLE_BUCKET`, `ORACLE_NAMESPACE`, `ORACLE_REGION`, `ORACLE_ACCESS_KEY_ID`, and `ORACLE_SECRET_ACCESS_KEY`. The access and secret values must be an OCI Customer Secret Key pair. `ORACLE_ENDPOINT` is optional and supports dedicated endpoints such as:

```text
https://<namespace>.compat.objectstorage.<region>.oci.customer-oci.com
```

AWS CLI uses path addressing, S3v4 signatures, disabled EC2 metadata, and `when_required` request/response checksum behavior.

## Initial sequence

1. Run `base-image.yml` and install/configure Windows through VNC.
2. Install VirtIO storage and NetKVM drivers, enable RDP, and shut Windows down normally.
3. Confirm `windows-vm/base.qcow2` exists in the selected provider.
4. Run `runtime.yml` with `allow_fresh_overlay_if_missing=true` exactly once to create the first overlay if no latest checkpoint exists.
5. Keep `allow_fresh_overlay_if_missing=false` for normal runs.
