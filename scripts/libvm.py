#!/usr/bin/env python3
import argparse, datetime, hashlib, json, os, shutil, socket, subprocess, sys, time
from pathlib import Path
ROOT=Path(os.environ.get('ROOT_DIR', Path(__file__).resolve().parents[1]))
WORK=ROOT/'work'; LOGS=ROOT/'logs'
for p in (WORK, LOGS): p.mkdir(parents=True, exist_ok=True)
def cfg(name):
    with open(ROOT/'config'/name, 'r', encoding='utf-8') as f: return json.load(f)
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
    s=cfg('storage.json');
    env=os.environ.copy();
    env['AWS_ACCESS_KEY_ID']=os.environ[s['access_key_env']]
    env['AWS_SECRET_ACCESS_KEY']=os.environ[s['secret_key_env']]
    env['AWS_DEFAULT_REGION']=env.get('AWS_DEFAULT_REGION','us-east-005')
    return env
def s3_uri(obj):
    s=cfg('storage.json'); return f"s3://{os.environ[s['bucket_env']]}/{s['remote_prefix'].strip('/')}/{obj.lstrip('/')}"
def endpoint(): return os.environ[cfg('storage.json')['endpoint_env']]
def aws_cp(src,dst): return run(['aws','--endpoint-url',endpoint(),'s3','cp',src,dst,'--only-show-errors'], env=aws_env())
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
    v=cfg('vm.json'); base=ROOT/v['base_image']; overlay=ROOT/v['overlay_image']
    if not base.exists(): raise SystemExit(f"missing base image: {base}")
    create_overlay(base, overlay)
    vars_path=ROOT/v['uefi_vars']
    if not vars_path.exists(): shutil.copyfile(v['uefi_vars_template'], vars_path)
    sock=ROOT/v['qmp_socket']; sock.unlink(missing_ok=True)
    pid=ROOT/v['pid_file']; pid.unlink(missing_ok=True)
    cmd=['qemu-system-x86_64','-enable-kvm','-machine','q35,accel=kvm','-m',str(v['memory_mb']),'-smp',str(v['cpu_cores']),'-cpu','host','-drive',f"if=pflash,format=raw,readonly=on,file={v['uefi_code']}",'-drive',f"if=pflash,format=raw,file={vars_path}",'-drive',f"file={overlay},if=virtio,format=qcow2,cache=writeback,discard=unmap,id={v['disk_id']}",'-netdev',f"user,id={v['network_user_id']},hostfwd=tcp::%s-:%s"%(v['rdp_host_port'],v['rdp_guest_port']),'-device',f"virtio-net-pci,netdev={v['network_user_id']}",'-qmp',f"unix:{sock},server=on,wait=off",'-pidfile',str(pid),'-daemonize','-vnc',f"{v.get('vnc_listen','0.0.0.0')}:{v.get('vnc_display',0)}",'-serial','file:'+str(ROOT/v['monitor_log'])]
    run(cmd); log('qemu started')
def wait_rdp(timeout=900):
    port=cfg('vm.json')['rdp_host_port']; end=time.time()+timeout
    while time.time()<end:
        rc=subprocess.run(['nc','-z','127.0.0.1',str(port)]).returncode
        if rc==0: log('rdp ready'); return
        time.sleep(5)
    raise SystemExit('rdp did not become ready')
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
def checkpoint(name='latest'):
    v=cfg('vm.json'); s=cfg('storage.json'); c=cfg('checkpoint.json'); overlay=ROOT/v['overlay_image']; comp=ROOT/v['overlay_compressed']; sha=WORK/'overlay.sha256'
    if not overlay.exists(): raise SystemExit('overlay missing')
    try: qmp_cmd('guest-fsfreeze-freeze'); frozen=True; log('guest fs frozen')
    except Exception as e: frozen=False; log(f'guest fs freeze unavailable: {e}')
    qmp_cmd('stop'); log('vm paused')
    try:
        run(['qemu-img','check','-r','leaks',overlay], check=False)
        run(['qemu-img','convert','-O','qcow2','-c',overlay,str(overlay)+'.compact'])
        Path(str(overlay)+'.compact').replace(overlay); log('overlay compacted')
        compress(overlay, comp); digest=write_sha(comp, sha)
        manifest={'version':1,'created_utc':datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z'),'name':name,'sha256':digest,'compressed':'overlay.qcow2.zst','base_object':s['base_object']}
        (WORK/'manifest.json').write_text(json.dumps(manifest,indent=2), encoding='utf-8')
        if name=='latest':
            targets=[(s['latest_object'],comp),(s['latest_sha256_object'],sha),(s['manifest_object'],WORK/'manifest.json')]
        else:
            pref=f"{s['checkpoint_prefix'].strip('/')}/{name}"; targets=[(f'{pref}/overlay.qcow2.zst',comp),(f'{pref}/overlay.sha256',sha),(f'{pref}/manifest.json',WORK/'manifest.json')]
        for obj,path in targets:
            retry(c['upload_retries'], c['retry_initial_seconds'], c['retry_max_seconds'], aws_cp, str(path), s3_uri(obj))
        tmp=WORK/'verify-download.zst'; tmp.unlink(missing_ok=True)
        retry(c['download_retries'], c['retry_initial_seconds'], c['retry_max_seconds'], aws_cp, s3_uri(targets[0][0]), str(tmp))
        if sha256(tmp)!=digest: raise SystemExit('upload verification checksum mismatch')
        log(f'checkpoint uploaded and verified: {name}')
    finally:
        qmp_cmd('cont'); log('vm resumed')
        if frozen:
            try: qmp_cmd('guest-fsfreeze-thaw'); log('guest fs thawed')
            except Exception as e: log(f'guest fs thaw failed: {e}')
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
