#!/usr/bin/env python3
import fcntl
import argparse, datetime, hashlib, json, os, re, shutil, socket, subprocess, sys, time
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
def write_sha(path, out):
    d=sha256(path); Path(out).write_text(f"{d}  {Path(path).name}\n", encoding='utf-8'); return d
def verify_sha(path, sha_file):
    expected=Path(sha_file).read_text(encoding='utf-8').split()[0].lower(); got=sha256(path).lower()
    if expected!=got: raise SystemExit(f"checksum mismatch for {path}: expected {expected}, got {got}")
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
        '--only-show-errors',
    ]

    # AWS CLI v2 tries to preserve tags and metadata during S3-to-S3
    # multipart copies. That can trigger HeadObject/GetObjectTagging/
    # PutObjectTagging calls, which are not supported by every S3-
    # compatible provider. For checkpoint rotation we only need the
    # object bytes, so disable property copying explicitly.
    if str(src).startswith('s3://') and str(dst).startswith('s3://'):
        cmd.extend(['--copy-props', 'none'])
        log(f'remote object copy without tags/metadata: {src} -> {dst}')

    return run(cmd, env=aws_env())
def aws_ls(uri): return subprocess.run(['aws','--endpoint-url',endpoint(),'s3','ls',uri], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=aws_env()).returncode==0
def aws_rm(uri): return run(['aws','--endpoint-url',endpoint(),'s3','rm',uri,'--only-show-errors'], env=aws_env(), check=False)
def compress(src,dst):
    c=cfg('checkpoint.json'); threads=str(c.get('zstd_threads',0)); level='-'+str(c.get('compression_level',10))
    tmp=str(dst)+'.tmp'; Path(tmp).unlink(missing_ok=True)
    run(['zstd','-T'+threads,level,'--force','--rm','-o',tmp,src])
    Path(tmp).replace(dst); log(f"compressed {src} -> {dst}")
def decompress(src,dst):
    tmp=str(dst)+'.tmp'; Path(tmp).unlink(missing_ok=True)
    run(['zstd','-d','--force','--rm','-o',tmp,src])
    Path(tmp).replace(dst); log(f"decompressed {src} -> {dst}")
def parse_qemu_size(value):
    text=str(value).strip().upper()
    match=re.fullmatch(r'([1-9][0-9]*)([KMGTPE]?)B?', text)
    if not match:
        raise ValueError(f"invalid QEMU disk size: {value!r}; use values such as 220G")
    number=int(match.group(1))
    unit=match.group(2)
    power={'':0,'K':1,'M':2,'G':3,'T':4,'P':5,'E':6}[unit]
    return number*(1024**power)

def qemu_virtual_size(path):
    result=run(
        ['qemu-img','info','--output=json',path],
        stdout=subprocess.PIPE,
    )
    info=json.loads(result.stdout)
    return int(info['virtual-size'])

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
    Path(overlay).parent.mkdir(parents=True, exist_ok=True)
    if Path(overlay).exists(): return
    run(['qemu-img','create','-f','qcow2','-F','qcow2','-b',base,overlay])
    log(f"created overlay {overlay}")
class QMP:
    def __init__(self, sock_path, timeout=20): self.sock_path=str(sock_path); self.timeout=timeout; self.s=None
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
        data=b''
        while True:
            chunk=self.s.recv(65536); data+=chunk
            if b'\r\n' in data or b'\n' in data: break
        return [json.loads(x) for x in data.replace(b'\r',b'').split(b'\n') if x.strip()]
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
        '-netdev',f"user,id={v['network_user_id']},hostfwd=tcp::{v['rdp_host_port']}-:{v['rdp_guest_port']}",
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
    try: qmp_cmd('system_powerdown'); log('qmp system_powerdown sent')
    except Exception as e: log(f'qmp shutdown failed: {e}')
    pid=ROOT/cfg('vm.json')['pid_file']
    if pid.exists():
        p=int(pid.read_text().strip());
        for _ in range(120):
            if subprocess.run(['kill','-0',str(p)], stderr=subprocess.DEVNULL).returncode!=0: return
            time.sleep(5)
        raise SystemExit('qemu did not exit after ACPI shutdown')
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

            try:
                block_jobs = qmp_cmd('query-block-jobs')
                block_job = next(
                    (
                        item
                        for item in block_jobs
                        if item.get('device') == job_id
                    ),
                    None,
                )

                if block_job:
                    total = int(block_job.get('len') or 0)
                    offset = int(block_job.get('offset') or 0)

                    if total > 0:
                        percent = (offset * 100.0) / total
                        progress_text = (
                            f' progress={percent:.1f}% '
                            f'({offset}/{total} bytes)'
                        )
            except Exception as e:
                progress_text = f' progress unavailable: {e}'

            log(
                f'live backup job {job_id}: '
                f'status={status}{progress_text}'
            )
            last_progress_log = now

        if status == 'concluded':
            error = job.get('error')

            try:
                qmp_cmd('job-dismiss', {'id': job_id})
            except Exception as dismiss_error:
                log(
                    f'live backup job dismiss warning for {job_id}: '
                    f'{dismiss_error}'
                )

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

    # Clean up a stale concluded job if one somehow remains.
    try:
        jobs = qmp_cmd('query-jobs')
        stale = next(
            (item for item in jobs if item.get('id') == job_id),
            None,
        )

        if stale:
            status = stale.get('status')

            if status == 'concluded':
                qmp_cmd('job-dismiss', {'id': job_id})
            else:
                raise RuntimeError(
                    f'cannot start checkpoint because QMP job '
                    f'{job_id!r} already exists with status={status!r}'
                )
    except RuntimeError:
        raise
    except Exception as e:
        log(f'stale QMP job check warning: {e}')

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
            except Exception:
                pass

            try:
                jobs = qmp_cmd('query-jobs')
                stale = next(
                    (item for item in jobs if item.get('id') == job_id),
                    None,
                )

                if stale and stale.get('status') == 'concluded':
                    qmp_cmd('job-dismiss', {'id': job_id})
            except Exception:
                pass

        raise

    if not target.exists():
        raise RuntimeError(
            f'QEMU reported successful live backup but target is missing: '
            f'{target}'
        )

    log(f'QEMU live backup target ready: {target}')


def checkpoint(name='latest'):
    v = cfg('vm.json')
    s = cfg('storage.json')
    c = cfg('checkpoint.json')

    overlay = ROOT / v['overlay_image']
    comp = ROOT / v['overlay_compressed']
    sha = WORK / 'overlay.sha256'
    qmp_socket = ROOT / v['qmp_socket']
    lock_path = WORK / 'checkpoint.lock'
    live_backup = WORK / 'online-checkpoint-source.qcow2'

    if not overlay.exists():
        raise SystemExit('overlay missing')

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, 'a+', encoding='utf-8') as lock_file:
        log('waiting for checkpoint lock')
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        log('checkpoint lock acquired')

        checkpoint_mode = None
        checkpoint_source = None

        try:
            running = qemu_is_running(v)
            qmp_available = qmp_socket.exists()

            if running and qmp_available:
                # Do not run qemu-img directly against a disk that the live
                # QEMU process owns. QEMU keeps the image lock even while the
                # VM is paused. Instead, ask QEMU itself to create a
                # point-in-time full backup target.
                log(
                    'QEMU is running; creating point-in-time live backup '
                    'without opening the active disk with qemu-img'
                )

                create_live_point_in_time_backup(v, live_backup)

                # The backup job is finished and QEMU has released the target.
                run(
                    ['qemu-img', 'check', '-r', 'leaks', live_backup],
                    check=False,
                )

                checkpoint_source = live_backup
                checkpoint_mode = 'online-live-backup'

            elif running:
                raise RuntimeError(
                    'QEMU is running but the QMP socket is unavailable; '
                    'refusing an unsafe direct copy of the active disk'
                )

            else:
                log(
                    'QEMU is stopped; creating offline checkpoint from '
                    'the inactive VM overlay'
                )

                run(
                    ['qemu-img', 'check', '-r', 'leaks', overlay],
                    check=False,
                )

                compact = Path(str(overlay) + '.compact')
                compact.unlink(missing_ok=True)

                run([
                    'qemu-img',
                    'convert',
                    '-p',
                    '-O', 'qcow2',
                    '-c',
                    overlay,
                    compact,
                ])

                compact.replace(overlay)
                log('offline overlay compacted')

                checkpoint_source = overlay
                checkpoint_mode = 'offline-stopped'

            compress(checkpoint_source, comp)
            digest = write_sha(comp, sha)

            manifest = {
                'version': 1,
                'created_utc': datetime.datetime.now(
                    datetime.timezone.utc
                ).replace(
                    microsecond=0
                ).isoformat().replace('+00:00', 'Z'),
                'name': name,
                'sha256': digest,
                'compressed': 'overlay.qcow2.zst',
                'base_object': s['base_object'],
                'checkpoint_mode': checkpoint_mode,
                'checkpoint_interval_minutes': c.get(
                    'interval_minutes',
                    15,
                ),
            }

            (WORK / 'manifest.json').write_text(
                json.dumps(manifest, indent=2),
                encoding='utf-8',
            )

            if name == 'latest':
                targets = [
                    (s['latest_object'], comp),
                    (s['latest_sha256_object'], sha),
                    (s['manifest_object'], WORK / 'manifest.json'),
                ]
            else:
                pref = f"{s['checkpoint_prefix'].strip('/')}/{name}"
                targets = [
                    (f'{pref}/overlay.qcow2.zst', comp),
                    (f'{pref}/overlay.sha256', sha),
                    (f'{pref}/manifest.json', WORK / 'manifest.json'),
                ]

            for obj, path in targets:
                retry(
                    c['upload_retries'],
                    c['retry_initial_seconds'],
                    c['retry_max_seconds'],
                    aws_cp,
                    str(path),
                    s3_uri(obj),
                )

            tmp = WORK / 'verify-download.zst'
            tmp.unlink(missing_ok=True)

            retry(
                c['download_retries'],
                c['retry_initial_seconds'],
                c['retry_max_seconds'],
                aws_cp,
                s3_uri(targets[0][0]),
                str(tmp),
            )

            if sha256(tmp) != digest:
                raise SystemExit('upload verification checksum mismatch')

            log(
                f'checkpoint uploaded and verified: {name} '
                f'({checkpoint_mode})'
            )

        finally:
            live_backup.unlink(missing_ok=True)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            log('checkpoint lock released')

def rotate():
    s=cfg('storage.json');
    for i in range(s.get('rotation_count',3),0,-1):
        src='latest' if i==1 else f"{s['checkpoint_prefix'].strip('/')}/checkpoint{i-1}"
        dst=f"{s['checkpoint_prefix'].strip('/')}/checkpoint{i}"
        for f in ['overlay.qcow2.zst','overlay.sha256','manifest.json']:
            srcobj=(s['latest_object'] if src=='latest' and f=='overlay.qcow2.zst' else s['latest_sha256_object'] if src=='latest' and f=='overlay.sha256' else s['manifest_object'] if src=='latest' else f'{src}/{f}')
            srcuri=s3_uri(srcobj)
            if aws_ls(srcuri): aws_cp(srcuri, s3_uri(f'{dst}/{f}'))
def restore():
    v=cfg('vm.json'); s=cfg('storage.json'); c=cfg('checkpoint.json')
    base=ROOT/v['base_image']; comp=ROOT/v['overlay_compressed']; overlay=ROOT/v['overlay_image']; sha=WORK/'overlay.sha256'
    retry(c['download_retries'], c['retry_initial_seconds'], c['retry_max_seconds'], aws_cp, s3_uri(s['base_object']), str(base))
    candidates=[('latest',s['latest_object'],s['latest_sha256_object'])]+[(f'checkpoint{i}',f"{s['checkpoint_prefix'].strip('/')}/checkpoint{i}/overlay.qcow2.zst",f"{s['checkpoint_prefix'].strip('/')}/checkpoint{i}/overlay.sha256") for i in range(1,s.get('rotation_count',3)+1)]
    restored=False
    for name,obj,shaobj in candidates:
        if not aws_ls(s3_uri(obj)): continue
        try:
            retry(c['download_retries'], c['retry_initial_seconds'], c['retry_max_seconds'], aws_cp, s3_uri(obj), str(comp))
            retry(c['download_retries'], c['retry_initial_seconds'], c['retry_max_seconds'], aws_cp, s3_uri(shaobj), str(sha))
            verify_sha(comp, sha); decompress(comp, overlay); restored=True; log(f'restored overlay from {name}'); break
        except Exception as e: log(f'restore candidate failed {name}: {e}')
    if not restored: create_overlay(base, overlay)
def retry(maxn,delay,cap,func,*args):
    n=1
    while True:
        try: return func(*args)
        except Exception:
            if n>=maxn: raise
            time.sleep(delay); n+=1; delay=min(delay*2,cap)
def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='cmd', required=True)
    for x in ['boot','restore','shutdown','rotate']: sub.add_parser(x)
    a=sub.add_parser('wait-rdp'); a.add_argument('--timeout',type=int,default=900)
    a=sub.add_parser('checkpoint'); a.add_argument('--name',default='latest')
    a=sub.add_parser('sha256'); a.add_argument('path'); a.add_argument('out')
    a=sub.add_parser('verify-sha'); a.add_argument('path'); a.add_argument('sha')
    a=sub.add_parser('create-overlay'); a.add_argument('base'); a.add_argument('overlay')
    a=sub.add_parser('compress'); a.add_argument('src'); a.add_argument('dst')
    a=sub.add_parser('decompress'); a.add_argument('src'); a.add_argument('dst')
    ns=p.parse_args();
    {'boot':boot,'restore':restore,'shutdown':shutdown,'rotate':rotate}.get(ns.cmd,lambda:None)() if ns.cmd in ['boot','restore','shutdown','rotate'] else None
    if ns.cmd=='wait-rdp': wait_rdp(ns.timeout)
    elif ns.cmd=='checkpoint': rotate(); checkpoint(ns.name)
    elif ns.cmd=='sha256': write_sha(ns.path, ns.out)
    elif ns.cmd=='verify-sha': verify_sha(ns.path, ns.sha)
    elif ns.cmd=='create-overlay': create_overlay(ns.base, ns.overlay)
    elif ns.cmd=='compress': compress(ns.src, ns.dst)
    elif ns.cmd=='decompress': decompress(ns.src, ns.dst)
if __name__=='__main__': main()
