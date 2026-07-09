#!/usr/bin/env python3
import argparse, datetime, hashlib, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
ROOT=Path(os.environ.get('ROOT_DIR', Path(__file__).resolve().parents[1]))
WORK=ROOT/'work'; LOGS=ROOT/'logs'; WORK.mkdir(exist_ok=True); LOGS.mkdir(exist_ok=True)
def log(m):
    s=f"[{datetime.datetime.utcnow().replace(microsecond=0).isoformat()}Z] {m}"; print(s, flush=True); (LOGS/'base-image.log').open('a',encoding='utf-8').write(s+'\n')
def run(cmd, check=True, **kw): log('exec: '+' '.join(map(str,cmd))); return subprocess.run(list(map(str,cmd)), check=check, text=True, **kw)
def cfg(n): return json.loads((ROOT/'config'/n).read_text(encoding='utf-8'))
def sha256(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()
def retry(maxn,delay,cap,func,*args):
    n=1
    while True:
        try: return func(*args)
        except Exception as e:
            if n>=maxn: raise
            log(f'retry {n}/{maxn} after error: {e}'); time.sleep(delay); n+=1; delay=min(delay*2,cap)
def aws_env():
    s=cfg('storage.json'); e=os.environ.copy(); e['AWS_ACCESS_KEY_ID']=os.environ[s['access_key_env']]; e['AWS_SECRET_ACCESS_KEY']=os.environ[s['secret_key_env']]; e['AWS_DEFAULT_REGION']=e.get('AWS_DEFAULT_REGION','us-east-005'); return e
def endpoint(): return os.environ[cfg('storage.json')['endpoint_env']]
def s3_uri(obj):
    s=cfg('storage.json'); return f"s3://{os.environ[s['bucket_env']]}/{s['remote_prefix'].strip('/')}/{obj.lstrip('/')}"
def aws_cp(src,dst): return run(['aws','--endpoint-url',endpoint(),'s3','cp',src,dst,'--only-show-errors'], env=aws_env())
def download(url, dest):
    dest=Path(dest); dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size>0: log(f'using cached {dest}'); return
    tmp=dest.with_suffix(dest.suffix+'.tmp')
    log(f'downloading {url}')
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp,'wb') as f:
        shutil.copyfileobj(r,f,1024*1024)
    tmp.replace(dest)
def wait_for_poweroff(pidfile, timeout):
    pid=int(Path(pidfile).read_text().strip()); end=time.time()+timeout
    while time.time()<end:
        if subprocess.run(['kill','-0',str(pid)], stderr=subprocess.DEVNULL).returncode!=0: return
        time.sleep(15)
    raise SystemExit('installer VM did not shut down before timeout')
def unattend(path, password, locale):
    text=f'''<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><SetupUILanguage><UILanguage>{locale}</UILanguage></SetupUILanguage><InputLocale>{locale}</InputLocale><SystemLocale>{locale}</SystemLocale><UILanguage>{locale}</UILanguage><UserLocale>{locale}</UserLocale></component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DiskConfiguration><Disk wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"><DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk><CreatePartitions><CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>100</Size></CreatePartition><CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition><CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition></CreatePartitions><ModifyPartitions><ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition><ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition></ModifyPartitions></Disk></DiskConfiguration>
      <ImageInstall><OSImage><InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo><InstallToAvailablePartition>false</InstallToAvailablePartition></OSImage></ImageInstall>
      <UserData><AcceptEula>true</AcceptEula><FullName>GitHub VM</FullName><Organization>GitHub Actions</Organization></UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-TerminalServices-LocalSessionManager" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><fDenyTSConnections>false</fDenyTSConnections></component>
    <component name="Networking-MPSSVC-Svc" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><FirewallGroups><FirewallGroup wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"><Active>true</Active><Group>@FirewallAPI.dll,-28752</Group><Profile>all</Profile></FirewallGroup></FirewallGroups></component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE><HideEULAPage>true</HideEULAPage><HideLocalAccountScreen>true</HideLocalAccountScreen><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen><HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE><NetworkLocation>Work</NetworkLocation><ProtectYourPC>3</ProtectYourPC></OOBE>
      <UserAccounts><AdministratorPassword><Value>{password}</Value><PlainText>true</PlainText></AdministratorPassword><LocalAccounts><LocalAccount wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"><Password><Value>{password}</Value><PlainText>true</PlainText></Password><DisplayName>runner</DisplayName><Group>Administrators</Group><Name>runner</Name></LocalAccount></LocalAccounts></UserAccounts>
      <AutoLogon><Password><Value>{password}</Value><PlainText>true</PlainText></Password><Enabled>true</Enabled><Username>runner</Username></AutoLogon>
      <FirstLogonCommands><SynchronousCommand wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"><Order>1</Order><CommandLine>cmd /c reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server" /v fDenyTSConnections /t REG_DWORD /d 0 /f</CommandLine></SynchronousCommand><SynchronousCommand wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"><Order>2</Order><CommandLine>cmd /c shutdown /s /t 30 /f</CommandLine></SynchronousCommand></FirstLogonCommands>
    </component>
  </settings>
</unattend>'''
    path.write_text(text, encoding='utf-8')
def build(args):
    v=cfg('vm.json'); c=cfg('checkpoint.json')
    iso=WORK/'windows.iso'; virtio=WORK/'virtio-win.iso'; base=ROOT/v['base_image']
    retry(c['download_retries'], c['retry_initial_seconds'], c['retry_max_seconds'], download, args.windows_iso_url, iso)
    retry(c['download_retries'], c['retry_initial_seconds'], c['retry_max_seconds'], download, args.virtio_iso_url, virtio)
    if args.windows_iso_sha256 and sha256(iso).lower()!=args.windows_iso_sha256.lower(): raise SystemExit('Windows ISO checksum mismatch')
    if args.virtio_iso_sha256 and sha256(virtio).lower()!=args.virtio_iso_sha256.lower(): raise SystemExit('VirtIO ISO checksum mismatch')
    shutil.copyfile(v['uefi_vars_template'], ROOT/v['uefi_vars'])
    base.parent.mkdir(parents=True, exist_ok=True); base.unlink(missing_ok=True)
    run(['qemu-img','create','-f','qcow2',base,args.disk_size])
    unatt=WORK/'Autounattend.xml'; unattend(unatt, args.admin_password, args.locale)
    cidata=WORK/'autounattend.iso'; cidata.unlink(missing_ok=True)
    run(['genisoimage','-quiet','-o',cidata,'-V','cidata','-J','-r',unatt])
    sock=ROOT/v['qmp_socket']; pid=ROOT/v['pid_file']; sock.unlink(missing_ok=True); pid.unlink(missing_ok=True)
    cmd=['qemu-system-x86_64','-enable-kvm','-machine','q35,accel=kvm','-m',str(args.memory_mb),'-smp',str(args.cpu_cores),'-cpu','host',
         '-drive',f"if=pflash,format=raw,readonly=on,file={v['uefi_code']}",'-drive',f"if=pflash,format=raw,file={ROOT/v['uefi_vars']}",
         '-drive',f"file={base},if=virtio,format=qcow2,cache=writeback,discard=unmap",
         '-cdrom',str(iso),'-drive',f"file={virtio},media=cdrom,readonly=on",'-drive',f"file={cidata},media=cdrom,readonly=on",
         '-boot','order=d,menu=off','-netdev','user,id=n0','-device','virtio-net-pci,netdev=n0','-qmp',f'unix:{sock},server=on,wait=off','-pidfile',str(pid),'-daemonize','-display','none','-serial','file:'+str(ROOT/v['monitor_log'])]
    run(cmd); log('base image installer VM started')
    wait_for_poweroff(pid, args.install_timeout_minutes*60)
    run(['qemu-img','check','-r','leaks',base], check=False)
    tmp=base.with_suffix('.compact.qcow2'); tmp.unlink(missing_ok=True)
    run(['qemu-img','convert','-O','qcow2','-c',base,tmp]); tmp.replace(base)
    digest=sha256(base); (WORK/'base.sha256').write_text(f'{digest}  {base.name}\n', encoding='utf-8')
    (WORK/'base-manifest.json').write_text(json.dumps({'version':1,'created_utc':datetime.datetime.utcnow().replace(microsecond=0).isoformat()+'Z','image':base.name,'sha256':digest,'disk_size':args.disk_size,'source':'github-actions-qemu'}, indent=2), encoding='utf-8')
    log(f'base image ready: {base} sha256={digest}')
def upload(args):
    v=cfg('vm.json'); s=cfg('storage.json'); c=cfg('checkpoint.json'); base=ROOT/v['base_image']; sha=WORK/'base.sha256'; manifest=WORK/'base-manifest.json'
    if not base.exists(): raise SystemExit(f'missing base image: {base}')
    digest=sha256(base); sha.write_text(f'{digest}  {base.name}\n', encoding='utf-8')
    if not manifest.exists(): manifest.write_text(json.dumps({'version':1,'created_utc':datetime.datetime.utcnow().replace(microsecond=0).isoformat()+'Z','image':base.name,'sha256':digest},indent=2),encoding='utf-8')
    for obj,path in [(s['base_object'],base),('base.sha256',sha),('base-manifest.json',manifest)]: retry(c['upload_retries'], c['retry_initial_seconds'], c['retry_max_seconds'], aws_cp, str(path), s3_uri(obj))
    verify=WORK/'base.verify.qcow2'; verify.unlink(missing_ok=True); retry(c['download_retries'], c['retry_initial_seconds'], c['retry_max_seconds'], aws_cp, s3_uri(s['base_object']), str(verify))
    if sha256(verify)!=digest: raise SystemExit('uploaded base verification failed')
    log('base image uploaded and verified')
def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='cmd', required=True)
    b=sub.add_parser('build'); b.add_argument('--windows-iso-url',required=True); b.add_argument('--virtio-iso-url',required=True); b.add_argument('--windows-iso-sha256',default=''); b.add_argument('--virtio-iso-sha256',default=''); b.add_argument('--admin-password',required=True); b.add_argument('--disk-size',default='80G'); b.add_argument('--memory-mb',type=int,default=8192); b.add_argument('--cpu-cores',type=int,default=4); b.add_argument('--locale',default='en-US'); b.add_argument('--install-timeout-minutes',type=int,default=180)
    sub.add_parser('upload')
    ns=p.parse_args(); build(ns) if ns.cmd=='build' else upload(ns)
if __name__=='__main__': main()
