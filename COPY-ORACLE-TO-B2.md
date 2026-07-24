# Copying Windows VM from Oracle to Backblaze B2

This guide covers how to copy the Windows VM base image and latest overlay checkpoint from Oracle Object Storage to Backblaze B2.

## Overview

The repository includes two methods to copy VM data from Oracle to Backblaze:

1. **GitHub Actions Workflow** (`copy-oracle-to-backblaze.yml`) - Runs in GitHub's cloud
2. **Local Script** (`scripts/copy-oracle-to-backblaze`) - Runs on your machine

Both methods perform the same operations:
- Download base image from Oracle
- Download latest overlay and metadata from Oracle
- Verify downloaded overlay SHA256
- Upload all files to Backblaze
- Verify uploaded overlay SHA256

## Prerequisites

### Required Credentials

**Oracle Object Storage S3 Compatibility API:**
- `ORACLE_BUCKET` - Your Oracle bucket name
- `ORACLE_NAMESPACE` - Your Oracle namespace
- `ORACLE_REGION` - Your Oracle region (e.g., `us-phoenix-1`)
- `ORACLE_ACCESS_KEY_ID` - OCI Customer Secret Key (Access Key ID)
- `ORACLE_SECRET_ACCESS_KEY` - OCI Customer Secret Key (Secret Access Key)
- `ORACLE_ENDPOINT` (optional) - Custom endpoint URL

**Backblaze B2:**
- `B2_BUCKET` - Your Backblaze bucket name
- `B2_ENDPOINT` - Backblaze S3 endpoint URL
- `B2_KEY_ID` - Backblaze B2 Application Key ID
- `B2_APPLICATION_KEY` - Backblaze B2 Application Key
- `B2_REGION` (optional) - Extracted from endpoint if not provided

### Required Tools

For local script execution:
- `bash` - Shell
- `aws` - AWS CLI v2
- `sha256sum` - SHA256 checksum utility
- `zstd` - Zstandard compression (for decompression if needed)
- `jq` - JSON processor (optional, for manifest inspection)

For GitHub Actions:
- All tools are pre-installed on `ubuntu-latest` runners

## Method 1: GitHub Actions Workflow

This is the recommended method for most users as it leverages GitHub's infrastructure.

### Triggering the Workflow

1. Navigate to your repository on GitHub
2. Go to **Actions** tab
3. Select **Copy Oracle VM to Backblaze** workflow
4. Click **Run workflow**
5. The workflow will execute with default settings

### Workflow Execution

The workflow will:

1. **Setup Phase:**
   - Install required tools (zstd, jq)
   - Configure AWS CLI for both Oracle and Backblaze
   - Validate credentials and endpoints

2. **Download Phase:**
   - Download `base.qcow2` from Oracle
   - Download `overlay.qcow2.zst` from Oracle
   - Download `overlay.sha256` from Oracle
   - Download `manifest.json` from Oracle

3. **Verification Phase:**
   - Verify overlay SHA256 against checksum file

4. **Upload Phase:**
   - Upload all files to Backblaze

5. **Completion:**
   - Download overlay from Backblaze to verify
   - Verify checksums match
   - Print success message

### Monitoring Progress

- View real-time logs in the workflow run
- Each file transfer prints a timestamped completion line
- Total time depends on file sizes (typically 30 minutes to 2 hours)

### Troubleshooting

**Authentication Failed:**
- Verify GitHub repository secrets are set correctly
- Check Oracle Customer Secret Keys are valid
- Check Backblaze B2 Application Key credentials

**Network Issues:**
- GitHub Actions may retry automatically
- Check endpoint URLs are correct
- Verify firewall/network allows S3 traffic

**Checksum Mismatch:**
- Download may have been corrupted
- Try again - file may have been partially transferred
- Check both credentials are correct

**Job Timeout:**
- GitHub Actions jobs timeout after 6 hours
- For large files, use the local script instead
- Increase timeout if necessary

## Method 2: Local Script

Use the local script when:
- You want more control over the process
- You're copying frequently
- Network conditions are unreliable (script can resume)
- Files are very large
- You prefer running on your own machine

### Setup

1. Ensure all prerequisites are installed:

```bash
# On Ubuntu/Debian
sudo apt-get install -y awscli2 zstd jq

# On macOS
brew install awscli zstd jq
```

2. Verify AWS CLI v2:

```bash
aws --version  # Should show: aws-cli/2.x.x
```

### Running the Script

#### Basic Usage

```bash
export ORACLE_BUCKET="your-oracle-bucket"
export ORACLE_NAMESPACE="your-namespace"
export ORACLE_REGION="us-phoenix-1"
export ORACLE_ACCESS_KEY_ID="your-access-key"
export ORACLE_SECRET_ACCESS_KEY="your-secret-key"

export B2_BUCKET="your-b2-bucket"
export B2_ENDPOINT="https://s3.your-region.backblazeb2.com"
export B2_KEY_ID="your-b2-key-id"
export B2_APPLICATION_KEY="your-b2-app-key"

cd /path/to/VPs
scripts/copy-oracle-to-backblaze
```

#### With Optional Parameters

```bash
# Custom object prefix (default: windows-vm)
export OBJECT_PREFIX="my-custom-prefix"

# Skip checksums (not recommended, default: true)
export VERIFY_CHECKSUMS=false

# Dry run - show what would be copied without doing it
export DRY_RUN=true

scripts/copy-oracle-to-backblaze
```

#### Using the Oracle Wrapper

```bash
# These are equivalent:
export STORAGE_PROVIDER=oracle
scripts/oracle/copy-to-backblaze

# Or directly:
scripts/copy-oracle-to-backblaze
```

### Script Output

The script prints detailed logs with timestamps:

```
[2024-07-20T14:30:45Z] Starting Oracle to Backblaze copy operation
[2024-07-20T14:30:45Z] Object prefix: windows-vm
[2024-07-20T14:30:45Z] Verify checksums: true
[2024-07-20T14:30:45Z] Dry run: false

[2024-07-20T14:30:46Z] Validating inputs...
[2024-07-20T14:30:46Z] Input validation passed
[2024-07-20T14:30:46Z] Configuring AWS CLI...
[2024-07-20T14:30:46Z] AWS CLI configured
[2024-07-20T14:30:46Z] Testing Oracle connectivity...
[2024-07-20T14:30:47Z] Oracle connectivity verified
...
[2024-07-20T15:45:22Z] ===========================================
[2024-07-20T15:45:22Z] Copy operation completed successfully!
[2024-07-20T15:45:22Z] ===========================================
```

### Dry Run Mode

Test without actually transferring data:

```bash
export DRY_RUN=true
scripts/copy-oracle-to-backblaze
```

Output shows what operations would be performed:

```
[2024-07-20T14:30:46Z] [DRY RUN] Would check: s3://oracle-bucket/windows-vm/base.qcow2
[2024-07-20T14:30:46Z] [DRY RUN] Would download: s3://oracle-bucket/windows-vm/base.qcow2 -> vm/base.qcow2
[2024-07-20T14:30:46Z] [DRY RUN] Would upload: vm/base.qcow2 -> s3://b2-bucket/windows-vm/base.qcow2
```

### Script Execution Options

#### Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| ORACLE_BUCKET | - | Yes | Oracle bucket name |
| ORACLE_NAMESPACE | - | Yes | Oracle namespace |
| ORACLE_REGION | - | Yes | Oracle region |
| ORACLE_ACCESS_KEY_ID | - | Yes | Oracle access key |
| ORACLE_SECRET_ACCESS_KEY | - | Yes | Oracle secret key |
| ORACLE_ENDPOINT | Auto | No | Custom Oracle endpoint |
| B2_BUCKET | - | Yes | Backblaze bucket name |
| B2_ENDPOINT | - | Yes | Backblaze endpoint URL |
| B2_KEY_ID | - | Yes | Backblaze key ID |
| B2_APPLICATION_KEY | - | Yes | Backblaze app key |
| B2_REGION | Auto | No | Backblaze region |
| OBJECT_PREFIX | `windows-vm` | No | VM object prefix |
| VERIFY_CHECKSUMS | `true` | No | Verify SHA256 checksums |
| DRY_RUN | `false` | No | Dry run mode |

### Troubleshooting Local Script

**Command not found: aws**
```bash
# Install AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
```

**Missing credentials error**
```bash
# Verify environment variables are set
env | grep -E "ORACLE_|B2_"

# Test credentials
export ORACLE_BUCKET=... # set all required vars first
aws --endpoint-url $ORACLE_ENDPOINT s3api head-bucket --bucket $ORACLE_BUCKET
```

**Network timeout**
```bash
# The script uses `--no-progress` to avoid log spam
# For progress, add verbose flag to aws commands
# Timeout issues usually indicate network problems - try again
```

**Out of disk space**
```bash
# The script creates a working directory 'vm'
# Ensure you have enough space for:
#   - base.qcow2 (~15-100 GB depending on size)
#   - overlay.qcow2.zst (varies, typically 5-30 GB)
#   - temporary files during verification

df -h  # Check available space
rm -rf vm/  # Cleanup if needed
```

## Understanding the VM Object Structure

The script copies the following objects:

```
BUCKET/
└── windows-vm/               (or custom OBJECT_PREFIX)
    ├── base.qcow2           (immutable base image - rarely changes)
    └── latest/
        ├── overlay.qcow2.zst (compressed overlay - changes frequently)
        ├── overlay.sha256    (SHA256 checksum for verification)
        └── manifest.json     (metadata about overlay)
```

### Object Details

- **base.qcow2** (immutable)
  - The Windows VM disk image base
  - Used by all overlays
  - Typically 15-100 GB (compressed as QCOW2 sparse image)
  - Changes only when re-created

- **latest/overlay.qcow2.zst** (mutable)
  - Point-in-time snapshot of VM state
  - Compressed with Zstandard
  - Typically 5-30 GB compressed
  - Updated with each checkpoint

- **latest/overlay.sha256**
  - SHA256 checksum of the overlay
  - Single line format: `hash  filename`
  - Used to verify integrity after transfer

- **latest/manifest.json**
  - Metadata about the overlay
  - Includes base image SHA256 (v3+ manifests)
  - Prevents incompatible checkpoint/base combinations

## Security Considerations

1. **Credentials:**
   - Use environment variables (not command line arguments)
   - Never commit credentials to version control
   - Use short-lived credentials when possible

2. **Data Transfer:**
   - Both S3 endpoints use HTTPS
   - AWS CLI enforces HTTPS
   - No unencrypted transmission

3. **Verification:**
   - SHA256 checksums verify integrity after transfer
   - Script checks both source and destination
   - Corrupted files are immediately detected

4. **Access Control:**
   - Backblaze: Use IAM policies to restrict bucket access
   - Oracle: Use Customer Secret Keys with minimal scope
   - GitHub: Protect secrets from exposure in logs

## Performance Tuning

### Network Optimization

```bash
# Increase AWS CLI multipart chunk size (speeds up large files)
export AWS_S3_DISABLE_MULTIPART_CHUNKING=false
export AWS_S3_MAX_CONCURRENT_REQUESTS=10
export AWS_S3_MAX_QUEUE_SIZE=100

scripts/copy-oracle-to-backblaze
```

### Parallel Transfers (GitHub Actions Only)

The GitHub Actions workflow uses sequential transfers for safety.
To enable parallel in custom scripts:

```bash
# In a custom script using AWS CLI
aws s3 sync s3://oracle-bucket/windows-vm s3://b2-bucket/windows-vm \
  --endpoint-url $ORACLE_ENDPOINT \
  --source-region $ORACLE_REGION
```

### Resume Capability

The script checks if objects exist before downloading:

```bash
# If interrupted, clean up partial files and retry
rm -rf vm/
scripts/copy-oracle-to-backblaze
```

Files in `vm/` directory that are complete won't be re-downloaded if the script is re-run (depends on AWS CLI behavior).

## Monitoring and Logging

### Script Logs

All output is timestamped and can be redirected:

```bash
scripts/copy-oracle-to-backblaze 2>&1 | tee copy.log
```

### GitHub Actions Logs

View logs in:
1. **GitHub Actions UI** → Workflow run → Job → Step logs
2. **Raw logs** → Download workflow artifacts

### Verifying Transfer Success

After the copy completes:

```bash
# Check objects exist in Backblaze
export AWS_ACCESS_KEY_ID=$B2_KEY_ID
export AWS_SECRET_ACCESS_KEY=$B2_APPLICATION_KEY
aws --endpoint-url $B2_ENDPOINT s3 ls s3://$B2_BUCKET/windows-vm/latest/

# Check file sizes match
aws --endpoint-url $ORACLE_ENDPOINT s3 ls s3://$ORACLE_BUCKET/windows-vm/ --recursive
aws --endpoint-url $B2_ENDPOINT s3 ls s3://$B2_BUCKET/windows-vm/ --recursive
```

## Rollback and Recovery

### If Transfer Failed

1. Check logs for specific error
2. Verify credentials are valid
3. Check network connectivity
4. Try again - transient failures may resolve
5. For persistent issues, contact support

### If Upload Was Corrupted

1. The verification step will catch corruption
2. Script will exit with error
3. Backblaze upload is automatically cleaned up
4. Re-run the script to try again

### Reverting to Previous Backblaze State

If you need to restore from backup:

```bash
# List all checkpoints in Backblaze
aws --endpoint-url $B2_ENDPOINT s3 ls s3://$B2_BUCKET/windows-vm/checkpoints/

# Copy a checkpoint back to latest
aws --endpoint-url $B2_ENDPOINT s3 cp \
  s3://$B2_BUCKET/windows-vm/checkpoints/CHECKPOINT_ID/overlay.qcow2.zst \
  s3://$B2_BUCKET/windows-vm/latest/overlay.qcow2.zst
```

## Scheduling Regular Copies

### Automated GitHub Actions Workflow

Create a scheduled workflow (`.github/workflows/scheduled-sync.yml`):

```yaml
name: Scheduled Oracle to B2 Sync
on:
  schedule:
    - cron: '0 2 * * 0'  # Weekly at 2 AM UTC on Sundays
  workflow_dispatch:

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run copy workflow
        run: |
          gh workflow run copy-oracle-to-backblaze.yml
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Automated Local Copies

Use cron or systemd timer:

```bash
# Add to crontab
0 2 * * 0 cd /path/to/VPs && ORACLE_BUCKET=... B2_BUCKET=... scripts/copy-oracle-to-backblaze >> /var/log/vm-sync.log 2>&1
```

## Cost Considerations

### Backblaze B2
- Upload: $0.006 per GB
- Download: $0.006 per GB per region (free on same region)
- Storage: $0.006 per GB/month

### Oracle Object Storage
- Upload: $0.0255 per GB (outbound)
- Download: No charge for Oracle egress
- Storage: $0.0255 per GB/month

### Typical Costs

For a 50 GB base image + 20 GB overlay:
- **Download from Oracle**: ~$1.81 (outbound)
- **Upload to Backblaze**: ~$0.42
- **Storage on B2**: ~$0.42/month

## Additional Resources

- [AWS CLI S3 Documentation](https://docs.aws.amazon.com/cli/latest/userguide/cli-services-s3.html)
- [Backblaze B2 S3-compatible API](https://www.backblaze.com/b2/docs/s3_compatible_api.html)
- [Oracle Object Storage S3 Compatibility](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi.htm)
- [Zstandard Compression](https://facebook.github.io/zstd/)

## Support

If you encounter issues:

1. Check this guide's troubleshooting sections
2. Review the workflow/script logs
3. Verify all credentials are set correctly
4. Try a dry run to validate configuration
5. Check repository issues on GitHub
6. Contact support for your storage provider
