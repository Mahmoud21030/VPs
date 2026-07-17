# Setup

## Tailscale

Create an OAuth client with permission to create devices using `tag:ci`, then add `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET` as GitHub repository secrets. Tailnet policy must allow the intended administrator devices to reach the tagged CI node on TCP 3389 and 5900.

## Cloudflare Tunnel alternative

Create a remotely managed Cloudflare Tunnel and add `CLOUDFLARE_TUNNEL_TOKEN` as a repository secret. Configure two TCP published-application routes:

- The RDP hostname routes to `tcp://127.0.0.1:3389`.
- The VNC hostname routes to `tcp://127.0.0.1:5900`.

Protect both hostnames with Cloudflare Access policies. Pass the hostnames as the workflow inputs `cloudflare_rdp_hostname` and `cloudflare_vnc_hostname`. On the client, create local listeners before opening RDP or VNC:

```bash
cloudflared access tcp --hostname rdp.example.com --url localhost:13389
cloudflared access tcp --hostname vnc.example.com --url localhost:15900
```

Then connect RDP to `localhost:13389` or VNC to `localhost:15900`. Cloudflare Tunnel is outbound-only, but arbitrary TCP requires the client-side `cloudflared` process.

## SSH reverse-relay alternative

Use a VPS or bastion that allows remote TCP forwarding. Add `SSH_RELAY_PRIVATE_KEY` and a pinned `SSH_RELAY_KNOWN_HOSTS` entry as repository secrets. Select `ssh-relay`, then provide the relay host, user, SSH port, and remote RDP/VNC ports as workflow inputs.

## Pinggy public TCP tunnels

Select `pinggy` to create temporary public RDP and VNC endpoints without owning a relay VPS. Add a repository secret named `VNC_PASSWORD` containing at least 8 characters; QEMU VNC uses its first 8 characters. The workflow summary prints both endpoints as `host:port`, ready to enter in the RDP or VNC client. Free endpoints may change or expire, and they are publicly reachable, so use strong credentials.

The workflow binds the remote reverse ports to `127.0.0.1` on the relay. On your client, authenticate to the same relay and create local forwards using the command printed in the workflow summary. This avoids publishing raw RDP or VNC ports on the internet.

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
