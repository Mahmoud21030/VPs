#!/usr/bin/env python3
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
ROOT=Path(os.environ.get('ROOT_DIR', Path(__file__).resolve().parents[1]))
WORK=ROOT/'work'; LOGS=ROOT/'logs'
for p in (WORK, LOGS): p.mkdir(parents=True, exist_ok=True)
def cfg(name):
    with open(ROOT/'config'/name, 'r', encoding='utf-8') as f: return json.load(f)

def storage_provider() -> str:
    storage = cfg("storage.json")
    provider = os.environ.get("STORAGE_PROVIDER", storage.get("default_provider", "backblaze")).strip().lower()
    if provider not in storage.get("providers", {}):
        supported = ", ".join(sorted(storage.get("providers", {})))
        raise RuntimeError(f"unsupported STORAGE_PROVIDER={provider!r}; supported providers: {supported}")
    return provider


def storage_context() -> dict[str, str]:
    storage = cfg("storage.json")
    provider = storage_provider()
    spec = storage["providers"][provider]

    def required_env(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise RuntimeError(f"required environment variable is missing: {name}")
        return value

    bucket = required_env(spec["bucket_env"])
    access_key = required_env(spec["access_key_env"])
    secret_key = required_env(spec["secret_key_env"])

    if provider == "oracle":
        region = required_env(spec["region_env"])
        namespace = required_env(spec["namespace_env"])
        endpoint = os.environ.get(spec["endpoint_env"], "").strip()
        if not endpoint:
            endpoint = f"https://{namespace}.compat.objectstorage.{region}.oci.customer-oci.com"
    else:
        endpoint = required_env(spec["endpoint_env"])
        configured_region = os.environ.get(spec.get("region_env", ""), "").strip()
        if configured_region:
            region = configured_region
        else:
            host = endpoint.split("://", 1)[-1].split("/", 1)[0]
            region = host[3:].split(".backblazeb2.com", 1)[0] if host.startswith("s3.") and ".backblazeb2.com" in host else spec.get("default_region", "us-east-1")

    if not endpoint.startswith(("https://", "http://")):
        raise RuntimeError(f"storage endpoint must include http:// or https://: {endpoint}")

    return {
        "provider": provider,
        "bucket": bucket,
        "endpoint": endpoint.rstrip("/"),
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region,
    }

def log(msg):
    s=f"[{datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z').removesuffix('Z')}Z] {msg}"
    print(s, flush=True)
    with open(LOGS/'runtime.log','a',encoding='utf-8') as f: f.write(s+'\n')
def run(cmd, check=True, **kw):
    log('exec: '+' '.join(map(str,cmd)))
    return subprocess.run(list(map(str,cmd)), check=check, text=True, **kw)
def sha256(path):
    h=hashlib.sha256();
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()
def atomic_write_text(path, content):
    path=Path(path)
    tmp=Path(str(path)+'.tmp')
    tmp.unlink(missing_ok=True)
    try:
        with tmp.open('w',encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
def write_sha(path, out):
    d=sha256(path)
    atomic_write_text(out,f"{d}  {Path(path).name}\n")
    return d
def verify_sha(path, sha_file):
    expected=Path(sha_file).read_text(encoding='utf-8').split()[0].lower(); got=sha256(path).lower()
    if not re.fullmatch(r'[0-9a-f]{64}',expected):
        raise RuntimeError(f'invalid SHA256 sidecar: {sha_file}')
    if expected!=got:
        raise RuntimeError(
            f"checksum mismatch for {path}: expected {expected}, got {got}"
        )
    log(f"sha256 verified: {path}")
def aws_env():
    context = storage_context()
    env = os.environ.copy()
    env['AWS_ACCESS_KEY_ID'] = context['access_key']
    env['AWS_SECRET_ACCESS_KEY'] = context['secret_key']
    env['AWS_DEFAULT_REGION'] = context['region']
    env['AWS_EC2_METADATA_DISABLED'] = 'true'

    # AWS CLI v2 can add request checksums using aws-chunked encoding.
    # OCI Object Storage's S3 compatibility API does not accept that
    # encoding for these multipart UploadPart requests. Restrict checksum
    # calculation/validation to API operations where it is required.
    env['AWS_REQUEST_CHECKSUM_CALCULATION'] = 'when_required'
    env['AWS_RESPONSE_CHECKSUM_VALIDATION'] = 'when_required'

    return env

def s3_uri(obj):
    s = cfg('storage.json')
    context = storage_context()
    prefix = s['remote_prefix'].strip('/')
    object_name = obj.lstrip('/')
    return f"s3://{context['bucket']}/{prefix}/{object_name}" if prefix else f"s3://{context['bucket']}/{object_name}"

def endpoint(): return storage_context()['endpoint']
def aws_cp(src, dst):
    cmd = [
        'aws',
        '--endpoint-url', endpoint(),
        's3', 'cp',
        src, dst,
        '--no-progress',
    ]

    # AWS CLI v2 tries to preserve tags and metadata during S3-to-S3
    # multipart copies. That can trigger HeadObject/GetObjectTagging/
    # PutObjectTagging calls, which are not supported by every S3-
    # compatible provider. For any S3-to-S3 copy we only need the object
    # bytes, so disable property copying explicitly.
    if str(src).startswith('s3://') and str(dst).startswith('s3://'):
        cmd.extend(['--copy-props', 'none'])
        log(f'remote object copy without tags/metadata: {src} -> {dst}')

    # GitHub Actions renders the AWS CLI's carriage-return progress refreshes
    # as hundreds of separate log lines. Disable only that progress renderer;
    # do not use --quiet or --only-show-errors, because restore failures must
    # still print the complete provider error.
    result = run(cmd, env=aws_env())
    log(f'object transfer completed: {src} -> {dst}')
    return result
def aws_rm_recursive(uri):
    return run(
        [
            'aws',
            '--endpoint-url', endpoint(),
            's3', 'rm',
            uri,
            '--recursive',
        ],
        env=aws_env(),
    )
def compress(src,dst):
    c=cfg('checkpoint.json')
    threads=str(c.get('zstd_threads',0))
    compression_level=int(c.get('compression_level',10))
    if not 1 <= compression_level <= 22:
        raise ValueError(
            f'compression_level must be between 1 and 22; got {compression_level}'
        )

    tmp=Path(str(dst)+'.tmp')
    tmp.unlink(missing_ok=True)
    command=['zstd','-T'+threads]
    if compression_level > 19:
        command.append('--ultra')
    command.extend([
        '-'+str(compression_level),
        '--force',
        '-o',str(tmp),
        str(src),
    ])

    try:
        run(command)
        tmp.replace(dst)
    finally:
        # A terminated or failed compression must never leave a temporary
        # archive that a later checkpoint could mistake for valid state.
        tmp.unlink(missing_ok=True)

    log(
        f"compressed {src} -> {dst} with zstd level {compression_level}"
    )
def decompress(src,dst):
    tmp=Path(str(dst)+'.tmp')
    tmp.unlink(missing_ok=True)
    try:
        run(['zstd','-d','--force','-o',tmp,src])
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    log(f"decompressed {src} -> {dst}")
def parse_qemu_size(value):
    text=str(value).strip().upper()
    match=re.fullmatch(r'([1-9][0-9]*)([KMGTPE]?)B?', text)
    if not match:
        raise ValueError(f"invalid QEMU disk size: {value!r}; use values such as 220G")
    number=int(match.group(1))
    unit=match.group(2)
    power={'':0,'K':1,'M':2,'G':3,'T':4,'P':5,'E':6}[unit]
    return number*(1024**power)

def qemu_image_info(path):
    result=run(
        ['qemu-img','info','--output=json',path],
        stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def qemu_virtual_size(path):
    return int(qemu_image_info(path)['virtual-size'])

def ensure_virtual_disk_size(path, requested_size):
    target_bytes=parse_qemu_size(requested_size)
    minimum_bytes=81*(1024**3)

    if target_bytes < minimum_bytes:
        raise SystemExit(
            f"requested virtual disk size must be above 80G; got {requested_size}"
        )

    current_bytes=qemu_virtual_size(path)
    current_gib=current_bytes/(1024**3)
    target_gib=target_bytes/(1024**3)

    log(
        f"virtual disk size check: current={current_gib:.2f}GiB "
        f"requested={target_gib:.2f}GiB path={path}"
    )

    if target_bytes < current_bytes:
        raise SystemExit(
            f"refusing to shrink virtual disk from {current_gib:.2f}GiB "
            f"to {target_gib:.2f}GiB"
        )

    if target_bytes == current_bytes:
        log("virtual disk already has requested size")
        return

    run(['qemu-img','resize','-f','qcow2',path,requested_size])

    resized_bytes=qemu_virtual_size(path)
    if resized_bytes != target_bytes:
        raise SystemExit(
            f"virtual disk resize verification failed: expected={target_bytes} "
            f"actual={resized_bytes}"
        )

    log(f"virtual disk expanded successfully to {requested_size}")

def create_overlay(base, overlay):
    base=Path(base)
    overlay=Path(overlay)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    if overlay.exists():
        return
    backing=os.path.relpath(base, overlay.parent)
    run(
        [
            'qemu-img','create','-f','qcow2','-F','qcow2',
            '-b',backing,overlay.name,
        ],
        cwd=overlay.parent,
    )
    log(f"created sparse overlay {overlay} backed by immutable {base}")


def require_temporary_space(directory, required_bytes, operation):
    available=shutil.disk_usage(directory).free
    log(
        f'temporary space check for {operation}: '
        f'required={required_bytes} available={available} bytes'
    )
    if available < required_bytes:
        raise RuntimeError(
            f'insufficient temporary disk space for {operation}: '
            f'required={required_bytes} available={available} bytes'
        )


def converted_overlay_space_required(source):
    info=qemu_image_info(source)
    actual_size=int(info.get('actual-size') or Path(source).stat().st_size)
    # Leave room for QCOW2 metadata growth while converting. The output is
    # sparse and backed by the immutable base, so changed allocated clusters
    # are the main space requirement.
    return max(actual_size + 1024**3, 2 * 1024**3)


def convert_to_backed_overlay(source, base, destination):
    source=Path(source)
    base=Path(base)
    destination=Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    require_temporary_space(
        destination.parent,
        converted_overlay_space_required(source),
        'QCOW2 backed-overlay conversion',
    )

    source_arg=os.path.relpath(source, destination.parent)
    backing_arg=os.path.relpath(base, destination.parent)
    try:
        run(
            [
                'qemu-img','convert','-p','-O','qcow2','-c',
                '-B',backing_arg,'-F','qcow2',
                source_arg,destination.name,
            ],
            cwd=destination.parent,
        )
        run(['qemu-img','check',destination])
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    log(
        f'created sparse persistent overlay {destination} '
        f'backed by immutable {base}'
    )


def normalize_overlay_backing(overlay, base):
    """Keep restored overlays attached to the immutable base safely.

    Current checkpoints already have a backing file and only need their
    relative backing path updated for this runner. Legacy standalone images
    are converted so unallocated clusters retain their original zero/data
    semantics; blindly adding a backing file with rebase -u would be unsafe.
    """
    overlay=Path(overlay)
    base=Path(base)
    info=qemu_image_info(overlay)
    backing=info.get('backing-filename')

    if backing:
        backing_arg=os.path.relpath(base, overlay.parent)
        run(
            [
                'qemu-img','rebase','-u','-f','qcow2','-F','qcow2',
                '-b',backing_arg,overlay.name,
            ],
            cwd=overlay.parent,
        )
        log(f'updated restored overlay backing path to {backing_arg}')
        return

    log('legacy standalone checkpoint detected; converting to a backed overlay')
    converted=overlay.with_name(overlay.name+'.backed.tmp')
    try:
        convert_to_backed_overlay(overlay,base,converted)
        converted.replace(overlay)
    finally:
        converted.unlink(missing_ok=True)
class QMP:
    def __init__(self, sock_path, timeout=20):
        self.sock_path=str(sock_path)
        self.timeout=timeout
        self.s=None
        self.buffer=b''
    def __enter__(self):
        deadline=time.time()+self.timeout
        while True:
            try:
                self.s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.s.settimeout(3); self.s.connect(self.sock_path); break
            except Exception:
                if time.time()>deadline: raise
                time.sleep(1)
        self._recv(); self.cmd('qmp_capabilities'); return self
    def __exit__(self,*a):
        try: self.s.close()
        except Exception: pass
    def _recv(self):
        messages=[]
        while not messages:
            while b'\n' not in self.buffer:
                chunk=self.s.recv(65536)
                if not chunk:
                    raise ConnectionError('QMP socket closed before a response')
                self.buffer+=chunk

            lines=self.buffer.split(b'\n')
            self.buffer=lines.pop()
            for line in lines:
                line=line.rstrip(b'\r')
                if line.strip():
                    messages.append(json.loads(line))

        return messages
    def cmd(self, execute, arguments=None):
        msg={'execute':execute}
        if arguments is not None: msg['arguments']=arguments
        self.s.sendall((json.dumps(msg)+'\n').encode())
        deadline=time.time()+self.timeout
        replies=[]
        while time.time()<deadline:
            for r in self._recv():
                replies.append(r)
                if 'return' in r: return r['return']
                if 'error' in r: raise RuntimeError(r['error'])
        raise TimeoutError(execute)
def qmp_cmd(cmd,args=None):
    v=cfg('vm.json'); c=cfg('checkpoint.json')
    with QMP(ROOT/v['qmp_socket'], c.get('qmp_timeout_seconds',20)) as q: return q.cmd(cmd,args)
def boot():
    v=cfg('vm.json')
    base=ROOT/v['base_image']
    overlay=ROOT/v['overlay_image']
    virtio_iso=WORK/'virtio-win.iso'
    requested_disk_size=v.get('disk_size','220G')

    if not base.exists():
        raise SystemExit(f"missing base image: {base}")

    if not virtio_iso.exists():
        raise SystemExit(f"missing VirtIO driver ISO: {virtio_iso}")

    create_overlay(base, overlay)
    ensure_virtual_disk_size(overlay, requested_disk_size)

    vars_path=ROOT/v['uefi_vars']
    if not vars_path.exists():
        shutil.copyfile(v['uefi_vars_template'], vars_path)

    sock=ROOT/v['qmp_socket']
    sock.unlink(missing_ok=True)

    pid=ROOT/v['pid_file']
    pid.unlink(missing_ok=True)

    cmd=[
        'qemu-system-x86_64',
        '-enable-kvm',
        '-machine','q35,accel=kvm',
        '-m',str(v['memory_mb']),
        '-smp',str(v['cpu_cores']),
        '-cpu','host',
        '-drive',f"if=pflash,format=raw,readonly=on,file={v['uefi_code']}",
        '-drive',f"if=pflash,format=raw,file={vars_path}",
        '-drive',f"file={overlay},if=virtio,format=qcow2,cache=writeback,discard=unmap,id={v['disk_id']}",
        '-drive',f"if=none,id=virtiocd,file={virtio_iso},format=raw,media=cdrom,readonly=on",
        '-device','ide-cd,drive=virtiocd',
        '-netdev',(
            f"user,id={v['network_user_id']},"
            f"hostfwd=tcp:0.0.0.0:{v['rdp_host_port']}-:{v['rdp_guest_port']}"
        ),
        '-device',f"virtio-net-pci,netdev={v['network_user_id']}",
        '-qmp',f"unix:{sock},server=on,wait=off",
        '-pidfile',str(pid),
        '-daemonize',
        '-vnc',f"{v.get('vnc_listen','0.0.0.0')}:{v.get('vnc_display',0)}",
        '-serial','file:'+str(ROOT/v['monitor_log']),
    ]

    run(cmd)
    log(
        f"qemu started with virtual disk size {requested_disk_size}; "
        f"VirtIO driver ISO mounted as CD-ROM"
    )
def wait_rdp(timeout=900):
    port=cfg('vm.json')['rdp_host_port']; end=time.time()+timeout
    while time.time()<end:
        rc=subprocess.run(['nc','-z','127.0.0.1',str(port)]).returncode
        if rc==0: log('rdp ready'); return
        time.sleep(5)
    raise SystemExit('rdp did not become ready')
def qemu_pid(v=None):
    v = v or cfg('vm.json')
    pid_file = ROOT / v['pid_file']

    try:
        text = pid_file.read_text(encoding='utf-8').strip()
        return int(text)
    except (FileNotFoundError, ValueError, OSError):
        return None

def qemu_is_running(v=None):
    v = v or cfg('vm.json')
    pid = qemu_pid(v)

    if pid is None:
        return False

    return subprocess.run(
        ['kill', '-0', str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0

def wait_for_qemu_exit(v=None, timeout=30):
    v = v or cfg('vm.json')
    deadline = time.time() + timeout

    while time.time() < deadline:
        if not qemu_is_running(v):
            return True
        time.sleep(1)

    return not qemu_is_running(v)

def shutdown():
    if not qemu_is_running():
        log('QEMU is already stopped; no QMP shutdown command is needed')
        return

    qmp_cmd('system_powerdown')
    log('qmp system_powerdown sent')
    pid=ROOT/cfg('vm.json')['pid_file']
    if pid.exists():
        p=int(pid.read_text().strip());
        for _ in range(60):
            if subprocess.run(
                ['kill','-0',str(p)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode!=0:
                log('QEMU exited after ACPI shutdown')
                return
            time.sleep(5)
        log(
            'Windows did not complete ACPI shutdown within 300 seconds; '
            'requesting QEMU quit after the verified final checkpoint'
        )
        try:
            qmp_cmd('quit')
        except Exception:
            if qemu_is_running():
                raise
            log('QMP connection closed as QEMU processed the quit command')
        if wait_for_qemu_exit(timeout=30):
            log('QEMU exited after QMP quit')
            return
        raise SystemExit('QEMU did not exit after ACPI shutdown and QMP quit')
def wait_for_qmp_backup_job(job_id, timeout_seconds):
    deadline = time.time() + timeout_seconds
    last_progress_log = 0.0

    while time.time() < deadline:
        if not qemu_is_running():
            raise RuntimeError(
                f'QEMU exited while live backup job {job_id!r} was running'
            )

        jobs = qmp_cmd('query-jobs')
        job = next((item for item in jobs if item.get('id') == job_id), None)

        if job is None:
            raise RuntimeError(
                f'live backup job {job_id!r} disappeared before completion'
            )

        status = job.get('status', 'unknown')

        now = time.time()
        if now - last_progress_log >= 15:
            progress_text = ''
            total = int(job.get('total-progress') or 0)
            current = int(job.get('current-progress') or 0)
            if total > 0:
                percent = (current * 100.0) / total
                progress_text = (
                    f' progress={percent:.1f}% '
                    f'({current}/{total})'
                )

            log(
                f'live backup job {job_id}: '
                f'status={status}{progress_text}'
            )
            last_progress_log = now

        if status == 'concluded':
            error = job.get('error')
            qmp_cmd('job-dismiss', {'id': job_id})
            log(f'live backup job dismissed: {job_id}')
            if error:
                raise RuntimeError(
                    f'live backup job {job_id!r} failed: {error}'
                )

            log(f'live backup job completed successfully: {job_id}')
            return

        time.sleep(2)

    raise TimeoutError(
        f'live backup job {job_id!r} exceeded '
        f'{timeout_seconds} seconds'
    )


def create_live_point_in_time_backup(v, target):
    target = Path(target)
    target.unlink(missing_ok=True)

    job_id = 'persistent-checkpoint-backup'
    timeout_seconds = int(
        cfg('checkpoint.json').get(
            'online_backup_timeout_seconds',
            3600,
        )
    )

    # A query failure is fatal: starting another backup without knowing the
    # current job state could overlap jobs or overwrite a target.
    jobs = qmp_cmd('query-jobs')
    stale = next(
        (item for item in jobs if item.get('id') == job_id),
        None,
    )
    if stale:
        status = stale.get('status')
        if status == 'concluded':
            qmp_cmd('job-dismiss', {'id': job_id})
            log(f'dismissed stale concluded live backup job {job_id}')
        else:
            raise RuntimeError(
                f'cannot start checkpoint because QMP job '
                f'{job_id!r} already exists with status={status!r}'
            )

    log(
        'starting QEMU point-in-time live backup '
        f'device={v["disk_id"]} target={target}'
    )

    try:
        qmp_cmd(
            'drive-backup',
            {
                'job-id': job_id,
                'device': v['disk_id'],
                'sync': 'full',
                'target': str(target),
                'format': 'qcow2',
                'compress': False,
                'auto-finalize': True,
                'auto-dismiss': False,
            },
        )

        wait_for_qmp_backup_job(job_id, timeout_seconds)

    except Exception:
        if qemu_is_running():
            try:
                qmp_cmd('job-cancel', {'id': job_id, 'force': True})
                log(f'cancel requested for failed live backup job {job_id}')
            except Exception as cancel_error:
                log(
                    f'could not cancel failed live backup job {job_id}: '
                    f'{cancel_error}'
                )

            try:
                jobs = qmp_cmd('query-jobs')
                stale = next(
                    (item for item in jobs if item.get('id') == job_id),
                    None,
                )

                if stale and stale.get('status') == 'concluded':
                    qmp_cmd('job-dismiss', {'id': job_id})
            except Exception as cleanup_error:
                log(
                    f'could not dismiss failed live backup job {job_id}: '
                    f'{cleanup_error}'
                )

        raise

    if not target.exists():
        raise RuntimeError(
            f'QEMU reported successful live backup but target is missing: '
            f'{target}'
        )

    log(f'QEMU live backup target ready: {target}')


def checkpoint(name='latest'):
    if name != 'latest':
        raise SystemExit(
            'only the latest checkpoint is supported; refusing to create '
            f'a retained checkpoint named {name!r}'
        )

    v=cfg('vm.json')
    s=cfg('storage.json')
    c=cfg('checkpoint.json')
    overlay=ROOT/v['overlay_image']
    base=ROOT/v['base_image']
    comp=ROOT/v['overlay_compressed']
    sha=WORK/'overlay.sha256'
    manifest_path=WORK/'manifest.json'
    qmp_socket=ROOT/v['qmp_socket']
    lock_path=WORK/'checkpoint.lock'
    live_backup=WORK/'online-checkpoint-full.qcow2'
    live_overlay=WORK/'online-checkpoint-overlay.qcow2'
    verify_download=WORK/'verify-download.zst.tmp'

    if not overlay.exists():
        raise SystemExit(f'overlay missing: {overlay}')
    if not base.exists():
        raise SystemExit(f'immutable base image missing: {base}')

    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with open(lock_path,'a+',encoding='utf-8') as lock_file:
        log('waiting for checkpoint lock')
        fcntl.flock(lock_file.fileno(),fcntl.LOCK_EX)
        log('checkpoint lock acquired')

        checkpoint_mode=None
        checkpoint_source=None
        try:
            running=qemu_is_running(v)
            qmp_available=qmp_socket.exists()

            if running and qmp_available:
                # QEMU retains an exclusive QCOW2 lock even while paused.
                # drive-backup is the safe point-in-time path while the guest
                # owns the active overlay.
                log(
                    'QEMU is running; starting a point-in-time full QMP '
                    'drive-backup without opening the active overlay'
                )
                create_live_point_in_time_backup(v,live_backup)

                # QEMU has concluded and dismissed the job, so it no longer
                # owns the separate full backup target. qemu-img may now check
                # and convert that completed target safely.
                run(['qemu-img','check',live_backup])
                convert_to_backed_overlay(live_backup,base,live_overlay)
                live_backup.unlink(missing_ok=True)
                checkpoint_source=live_overlay
                checkpoint_mode='online-live-backup'

            elif running:
                raise RuntimeError(
                    'QEMU is running but the QMP socket is unavailable; '
                    'refusing an unsafe direct read of the active overlay'
                )

            else:
                # Missing qemu.pid and QMP socket are expected after a normal
                # Windows shutdown. Never issue QMP commands on this path.
                log(
                    'QEMU is stopped; using the inactive overlay directly '
                    'for the final offline checkpoint'
                )
                run(['qemu-img','check',overlay])

                compact=Path(str(overlay)+'.compact.tmp')
                compact.unlink(missing_ok=True)
                required=converted_overlay_space_required(overlay)
                available=shutil.disk_usage(overlay.parent).free
                if available >= required:
                    try:
                        convert_to_backed_overlay(overlay,base,compact)
                        compact.replace(overlay)
                        log('offline overlay compacted successfully')
                    finally:
                        compact.unlink(missing_ok=True)
                else:
                    log(
                        'offline compaction skipped because temporary disk '
                        f'space is insufficient: required={required} '
                        f'available={available} bytes'
                    )

                checkpoint_source=overlay
                checkpoint_mode='offline-stopped'

            # Compression must finish completely before any upload starts.
            compress(checkpoint_source,comp)
            digest=write_sha(comp,sha)
            manifest={
                'version':2,
                'created_utc':datetime.datetime.now(
                    datetime.timezone.utc
                ).replace(microsecond=0).isoformat().replace('+00:00','Z'),
                'name':'latest',
                'sha256':digest,
                'compressed':'overlay.qcow2.zst',
                'base_object':s['base_object'],
                'checkpoint_mode':checkpoint_mode,
                'checkpoint_interval_minutes':c.get('interval_minutes',15),
                'compression_level':c.get('compression_level',10),
                'virtual_disk_size_bytes':qemu_virtual_size(checkpoint_source),
            }
            atomic_write_text(
                manifest_path,
                json.dumps(manifest,indent=2)+'\n',
            )

            targets=[
                (s['latest_object'],comp),
                (s['latest_sha256_object'],sha),
                (s['manifest_object'],manifest_path),
            ]
            for object_name,path in targets:
                retry(
                    c['upload_retries'],
                    c['retry_initial_seconds'],
                    c['retry_max_seconds'],
                    aws_cp,
                    str(path),s3_uri(object_name),
                )

            verify_download.unlink(missing_ok=True)
            retry(
                c['download_retries'],
                c['retry_initial_seconds'],
                c['retry_max_seconds'],
                aws_cp,
                s3_uri(s['latest_object']),str(verify_download),
            )
            verified_digest=sha256(verify_download)
            if verified_digest != digest:
                raise RuntimeError(
                    'uploaded latest overlay verification failed: '
                    f'expected={digest} actual={verified_digest}'
                )

            log(
                'checkpoint uploaded and verified: latest '
                f'({checkpoint_mode})'
            )
        finally:
            live_backup.unlink(missing_ok=True)
            live_overlay.unlink(missing_ok=True)
            verify_download.unlink(missing_ok=True)
            Path(str(comp)+'.tmp').unlink(missing_ok=True)
            fcntl.flock(lock_file.fileno(),fcntl.LOCK_UN)
            log('checkpoint lock released')

def prune_old_checkpoints():
    s = cfg('storage.json')
    checkpoint_prefix = s.get('checkpoint_prefix', 'checkpoints').strip('/')

    if not checkpoint_prefix:
        raise RuntimeError(
            'refusing to prune because checkpoint_prefix is empty'
        )

    uri = s3_uri(checkpoint_prefix + '/')

    log(f'deleting old rotated checkpoints only: {uri}')
    log(
        'base image and latest overlay are intentionally untouched'
    )

    aws_rm_recursive(uri)

    log(
        'old rotated checkpoints removed successfully; '
        'only base + latest overlay policy remains'
    )


def env_boolean(name, default=False):
    value=os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized=value.strip().lower()
    if normalized in {'1','true','yes','on'}:
        return True
    if normalized in {'0','false','no','off'}:
        return False
    raise ValueError(
        f'{name} must be true or false; got {value!r}'
    )


def download_object_atomic(object_name,destination,c):
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+'.download.tmp')
    temporary.unlink(missing_ok=True)
    try:
        retry(
            c['download_retries'],
            c['retry_initial_seconds'],
            c['retry_max_seconds'],
            aws_cp,
            s3_uri(object_name),str(temporary),
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def restore():
    v=cfg('vm.json')
    s=cfg('storage.json')
    c=cfg('checkpoint.json')
    base=ROOT/v['base_image']
    comp=ROOT/v['overlay_compressed']
    overlay=ROOT/v['overlay_image']
    sha=WORK/'overlay.sha256'
    allow_fresh=env_boolean('ALLOW_FRESH_OVERLAY_IF_MISSING',False)

    for path in [overlay,Path(str(overlay)+'.tmp')]:
        path.unlink(missing_ok=True)

    log(f'downloading immutable base image: {s3_uri(s["base_object"])}')
    download_object_atomic(s['base_object'],base,c)
    base_info=qemu_image_info(base)
    if base_info.get('format') != 'qcow2':
        raise RuntimeError(
            f'base image is not QCOW2: format={base_info.get("format")!r}'
        )
    run(['qemu-img','check',base])
    log('immutable base image downloaded and validated')

    try:
        # This is deliberately a direct download. Do not precede it with a
        # quiet `aws s3 ls` probe: access and provider errors must be visible.
        try:
            download_object_atomic(s['latest_object'],comp,c)
        except Exception as error:
            log(
                'latest overlay direct download failed; the complete AWS CLI '
                f'provider error is printed above: {error}'
            )
            raise

        log('overlay archive downloaded successfully from latest')
        download_object_atomic(s['latest_sha256_object'],sha,c)
        verify_sha(comp,sha)
        decompress(comp,overlay)
        normalize_overlay_backing(overlay,base)
        run(['qemu-img','check',overlay])
        log('restored overlay from latest')
    except Exception as error:
        overlay.unlink(missing_ok=True)
        Path(str(overlay)+'.tmp').unlink(missing_ok=True)
        if not allow_fresh:
            raise RuntimeError(
                'latest persistent overlay is missing, inaccessible, corrupt, '
                'or could not be decompressed; refusing to create a blank '
                'overlay because allow_fresh_overlay_if_missing=false'
            ) from error

        log(
            'WARNING: latest overlay restore failed and '
            'allow_fresh_overlay_if_missing=true; creating an explicitly '
            f'authorized blank overlay. Restore error: {error}'
        )
        create_overlay(base,overlay)
        run(['qemu-img','check',overlay])
        log('fresh overlay created by explicit workflow authorization')
    finally:
        # The verified/decompressed archive is not needed locally. Removing it
        # frees space for the next atomic compression output.
        comp.unlink(missing_ok=True)
        Path(str(comp)+'.download.tmp').unlink(missing_ok=True)
def retry(maxn,delay,cap,func,*args):
    n=1
    while True:
        try:
            return func(*args)
        except Exception as error:
            if n>=maxn:
                log(
                    f'operation failed after {maxn} attempt(s): {error}'
                )
                raise
            log(
                f'operation attempt {n}/{maxn} failed: {error}; '
                f'retrying in {delay}s'
            )
            time.sleep(delay)
            n+=1
            delay=min(delay*2,cap)
def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='cmd', required=True)
    for x in ['boot','restore','shutdown','prune-old-checkpoints']: sub.add_parser(x)
    a=sub.add_parser('wait-rdp'); a.add_argument('--timeout',type=int,default=900)
    a=sub.add_parser('checkpoint'); a.add_argument('--name',default='latest')
    a=sub.add_parser('sha256'); a.add_argument('path'); a.add_argument('out')
    a=sub.add_parser('verify-sha'); a.add_argument('path'); a.add_argument('sha')
    a=sub.add_parser('create-overlay'); a.add_argument('base'); a.add_argument('overlay')
    a=sub.add_parser('compress'); a.add_argument('src'); a.add_argument('dst')
    a=sub.add_parser('decompress'); a.add_argument('src'); a.add_argument('dst')
    ns=p.parse_args();
    {
        'boot': boot,
        'restore': restore,
        'shutdown': shutdown,
        'prune-old-checkpoints': prune_old_checkpoints,
    }.get(ns.cmd, lambda: None)() if ns.cmd in [
        'boot',
        'restore',
        'shutdown',
        'prune-old-checkpoints',
    ] else None
    if ns.cmd=='wait-rdp': wait_rdp(ns.timeout)
    elif ns.cmd=='checkpoint': checkpoint(ns.name)
    elif ns.cmd=='sha256': write_sha(ns.path, ns.out)
    elif ns.cmd=='verify-sha': verify_sha(ns.path, ns.sha)
    elif ns.cmd=='create-overlay': create_overlay(ns.base, ns.overlay)
    elif ns.cmd=='compress': compress(ns.src, ns.dst)
    elif ns.cmd=='decompress': decompress(ns.src, ns.dst)
if __name__=='__main__': main()
