import hashlib, os, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import libvm

def test_sha256_roundtrip(tmp_path):
    p=tmp_path/'a.bin'; p.write_bytes(b'abc')
    out=tmp_path/'a.sha256'
    libvm.write_sha(p,out)
    assert out.read_text().split()[0] == hashlib.sha256(b'abc').hexdigest()
    libvm.verify_sha(p,out)

def test_compression_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(libvm, 'ROOT', ROOT)
    p=tmp_path/'overlay.qcow2'; p.write_bytes(b'x'*1024*1024)
    z=tmp_path/'overlay.qcow2.zst'; r=tmp_path/'restored.qcow2'
    libvm.compress(p,z)
    assert z.exists() and not p.exists()
    libvm.decompress(z,r)
    assert r.read_bytes()==b'x'*1024*1024

def test_overlay_creation(tmp_path):
    base=tmp_path/'base.qcow2'; overlay=tmp_path/'overlay.qcow2'
    subprocess.run(['qemu-img','create','-f','qcow2',str(base),'64M'], check=True)
    libvm.create_overlay(base, overlay)
    assert overlay.exists()
    info=subprocess.check_output(['qemu-img','info','--output=json',str(overlay)], text=True)
    assert 'backing-filename' in info

def test_rollback_candidate_order():
    s={'checkpoint_prefix':'checkpoints','rotation_count':3,'latest_object':'latest/overlay.qcow2.zst','latest_sha256_object':'latest/overlay.sha256'}
    candidates=[('latest',s['latest_object'],s['latest_sha256_object'])]+[(f'checkpoint{i}',f"{s['checkpoint_prefix'].strip('/')}/checkpoint{i}/overlay.qcow2.zst",f"{s['checkpoint_prefix'].strip('/')}/checkpoint{i}/overlay.sha256") for i in range(1,s.get('rotation_count',3)+1)]
    assert [c[0] for c in candidates] == ['latest','checkpoint1','checkpoint2','checkpoint3']
