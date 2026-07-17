import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import libvm
import libbase


class CoreTests(unittest.TestCase):
    def test_sha256_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            payload=root/'a.bin'
            payload.write_bytes(b'abc')
            sidecar=root/'a.sha256'
            libvm.write_sha(payload,sidecar)
            self.assertEqual(
                sidecar.read_text().split()[0],
                hashlib.sha256(b'abc').hexdigest(),
            )
            libvm.verify_sha(payload,sidecar)

    def test_invalid_sha_sidecar_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            payload=root/'a.bin'
            payload.write_bytes(b'abc')
            sidecar=root/'a.sha256'
            sidecar.write_text('not-a-digest  a.bin\n')
            with self.assertRaisesRegex(RuntimeError,'invalid SHA256'):
                libvm.verify_sha(payload,sidecar)

    def test_ultra_compression_is_atomic_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'overlay.qcow2'
            destination=root/'overlay.qcow2.zst'
            source.write_bytes(b'payload')
            commands=[]

            def fake_run(command,**kwargs):
                commands.append(command)
                output=Path(command[command.index('-o')+1])
                output.write_bytes(b'compressed')
                return subprocess.CompletedProcess(command,0)

            with mock.patch.object(
                libvm,'cfg',return_value={'compression_level':22,'zstd_threads':0}
            ), mock.patch.object(libvm,'run',side_effect=fake_run), mock.patch.object(
                libvm,'log'
            ):
                libvm.compress(source,destination)

            self.assertTrue(source.exists())
            self.assertEqual(destination.read_bytes(),b'compressed')
            self.assertFalse(Path(str(destination)+'.tmp').exists())
            self.assertIn('--ultra',commands[0])
            self.assertNotIn('--rm',commands[0])

    @unittest.skipUnless(shutil.which('qemu-img'),'qemu-img unavailable')
    def test_overlay_creation_is_sparse_and_backed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            base=root/'base.qcow2'
            overlay=root/'overlay.qcow2'
            subprocess.run(
                ['qemu-img','create','-f','qcow2',str(base),'64M'],
                check=True,
            )
            libvm.create_overlay(base,overlay)
            info=json.loads(subprocess.check_output(
                ['qemu-img','info','--output=json',str(overlay)],text=True
            ))
            self.assertEqual(info['format'],'qcow2')
            self.assertEqual(info['backing-filename'],'base.qcow2')

    def test_virtual_disk_expands_but_never_shrinks(self):
        sizes=iter([100*1024**3,150*1024**3])
        commands=[]
        with mock.patch.object(
            libvm,'qemu_virtual_size',side_effect=lambda path: next(sizes)
        ), mock.patch.object(
            libvm,'run',side_effect=lambda command,**kwargs: commands.append(command)
        ), mock.patch.object(libvm,'log'):
            libvm.ensure_virtual_disk_size('overlay.qcow2','150G')
        self.assertEqual(commands[0][-1],'150G')

        with mock.patch.object(
            libvm,'qemu_virtual_size',return_value=220*1024**3
        ), mock.patch.object(libvm,'log'):
            with self.assertRaisesRegex(SystemExit,'refusing to shrink'):
                libvm.ensure_virtual_disk_size('overlay.qcow2','150G')

    def test_completed_backup_conversion_keeps_immutable_backing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'full.qcow2'
            base=root/'base.qcow2'
            destination=root/'overlay.qcow2'
            source.write_bytes(b'full')
            base.write_bytes(b'base')
            commands=[]

            def fake_run(command,**kwargs):
                commands.append(command)
                if command[:2] == ['qemu-img','convert']:
                    (Path(kwargs['cwd'])/command[-1]).write_bytes(b'overlay')
                return subprocess.CompletedProcess(command,0)

            with mock.patch.object(
                libvm,
                'qemu_image_info',
                return_value={'actual-size':1,'virtual-size':1024},
            ), mock.patch.object(
                libvm,'require_temporary_space'
            ), mock.patch.object(libvm,'run',side_effect=fake_run), mock.patch.object(
                libvm,'log'
            ):
                libvm.convert_to_backed_overlay(source,base,destination)

            convert=commands[0]
            self.assertIn('-B',convert)
            self.assertEqual(convert[convert.index('-B')+1],'base.qcow2')
            self.assertEqual(convert[convert.index('-F')+1],'qcow2')
            self.assertTrue(destination.exists())

    def test_qmp_backup_job_errors_are_detected_and_dismissed(self):
        calls=[]

        def fake_qmp(command,arguments=None):
            calls.append((command,arguments))
            if command == 'query-jobs':
                return [{
                    'id':'persistent-checkpoint-backup',
                    'status':'concluded',
                    'error':'target write failed',
                    'current-progress':10,
                    'total-progress':10,
                }]
            if command == 'job-dismiss':
                return {}
            raise AssertionError(command)

        with mock.patch.object(libvm,'qemu_is_running',return_value=True), mock.patch.object(
            libvm,'qmp_cmd',side_effect=fake_qmp
        ), mock.patch.object(libvm,'log'):
            with self.assertRaisesRegex(RuntimeError,'target write failed'):
                libvm.wait_for_qmp_backup_job(
                    'persistent-checkpoint-backup',10
                )
        self.assertIn(
            ('job-dismiss',{'id':'persistent-checkpoint-backup'}),calls
        )

    def test_qemu_process_identity_rejects_stale_pid_reuse(self):
        vm={'pid_file':'work/qemu.pid','overlay_image':'work/overlay.qcow2'}
        with mock.patch.object(libvm,'qemu_pid',return_value=4242), mock.patch.object(
            libvm.os,'kill'
        ), mock.patch.object(
            libvm,'process_cmdline',return_value=b'/usr/bin/sleep\x001000\x00'
        ):
            self.assertFalse(libvm.qemu_is_running(vm))

        expected=str(libvm.ROOT/'work/overlay.qcow2').encode()
        with mock.patch.object(libvm,'qemu_pid',return_value=4242), mock.patch.object(
            libvm.os,'kill'
        ), mock.patch.object(
            libvm,
            'process_cmdline',
            return_value=b'/usr/bin/qemu-system-x86_64\x00-drive\x00file='+expected+b'\x00',
        ):
            self.assertTrue(libvm.qemu_is_running(vm))

    def test_checkpoint_refuses_ambiguous_qmp_socket_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            work=root/'work'
            work.mkdir()
            (work/'base.qcow2').write_bytes(b'base')
            (work/'overlay.qcow2').write_bytes(b'overlay')
            (work/'qmp.sock').touch()
            vm={
                'base_image':'work/base.qcow2',
                'overlay_image':'work/overlay.qcow2',
                'overlay_compressed':'work/overlay.qcow2.zst',
                'qmp_socket':'work/qmp.sock',
                'pid_file':'work/qemu.pid',
            }
            storage={}
            checkpoint={}

            def fake_cfg(name):
                return {
                    'vm.json':vm,
                    'storage.json':storage,
                    'checkpoint.json':checkpoint,
                }[name]

            with mock.patch.object(libvm,'ROOT',root), mock.patch.object(
                libvm,'WORK',work
            ), mock.patch.object(libvm,'cfg',side_effect=fake_cfg), mock.patch.object(
                libvm,'qemu_is_running',return_value=False
            ), mock.patch.object(libvm,'log'):
                with self.assertRaisesRegex(RuntimeError,'process identity'):
                    libvm.checkpoint()

    def _restore_fixture(self,root,allow_fresh):
        work=root/'work'
        work.mkdir()
        vm={
            'base_image':'work/base.qcow2',
            'overlay_compressed':'work/overlay.qcow2.zst',
            'overlay_image':'work/overlay.qcow2',
        }
        storage={
            'base_object':'base.qcow2',
            'latest_object':'latest/overlay.qcow2.zst',
            'latest_sha256_object':'latest/overlay.sha256',
            'manifest_object':'latest/manifest.json',
        }
        checkpoint={
            'download_retries':1,
            'retry_initial_seconds':1,
            'retry_max_seconds':1,
        }

        def fake_cfg(name):
            return {
                'vm.json':vm,
                'storage.json':storage,
                'checkpoint.json':checkpoint,
            }[name]

        def fake_download(object_name,destination,config):
            if object_name == 'base.qcow2':
                Path(destination).write_bytes(b'base')
                return
            raise FileNotFoundError(object_name)

        stack=ExitStack()
        stack.enter_context(mock.patch.object(libvm,'ROOT',root))
        stack.enter_context(mock.patch.object(libvm,'WORK',work))
        stack.enter_context(mock.patch.object(libvm,'cfg',side_effect=fake_cfg))
        stack.enter_context(mock.patch.object(
            libvm,'s3_uri',side_effect=lambda object_name:'s3://bucket/'+object_name
        ))
        stack.enter_context(mock.patch.object(
            libvm,'qemu_image_info',return_value={'format':'qcow2'}
        ))
        stack.enter_context(mock.patch.object(
            libvm,'run',side_effect=lambda command,**kwargs:subprocess.CompletedProcess(command,0)
        ))
        stack.enter_context(mock.patch.object(libvm,'log'))
        stack.enter_context(mock.patch.object(
            libvm,'download_object_atomic',side_effect=fake_download
        ))
        stack.enter_context(mock.patch.dict(
            os.environ,
            {'ALLOW_FRESH_OVERLAY_IF_MISSING':str(allow_fresh).lower()},
        ))
        return work,stack

    def test_restore_is_fail_closed_when_latest_download_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            work,stack=self._restore_fixture(root,False)
            with stack:
                with self.assertRaisesRegex(
                    RuntimeError,'refusing to create a blank overlay'
                ):
                    libvm.restore()
            self.assertFalse((work/'overlay.qcow2').exists())

    def test_restore_creates_fresh_only_when_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            work,stack=self._restore_fixture(root,True)
            with stack, mock.patch.object(
                libvm,
                'create_overlay',
                side_effect=lambda base,overlay:Path(overlay).write_bytes(b'fresh'),
            ):
                libvm.restore()
            self.assertEqual((work/'overlay.qcow2').read_bytes(),b'fresh')

    def test_checkpoint_names_other_than_latest_are_rejected(self):
        with self.assertRaisesRegex(SystemExit,'only the latest checkpoint'):
            libvm.checkpoint('checkpoint1')

    def test_storage_provider_backblaze(self):
        environment={
            'STORAGE_PROVIDER':'backblaze',
            'B2_BUCKET':'bucket',
            'B2_ENDPOINT':'https://s3.eu-central-003.backblazeb2.com',
            'B2_KEY_ID':'key',
            'B2_APPLICATION_KEY':'secret',
        }
        with mock.patch.dict(os.environ,environment,clear=True):
            context=libvm.storage_context()
        self.assertEqual(context['provider'],'backblaze')
        self.assertEqual(context['region'],'eu-central-003')

    def test_storage_provider_oracle_derives_dedicated_endpoint(self):
        environment={
            'STORAGE_PROVIDER':'oracle',
            'ORACLE_BUCKET':'vm-bucket',
            'ORACLE_NAMESPACE':'namespace',
            'ORACLE_REGION':'eu-frankfurt-1',
            'ORACLE_ACCESS_KEY_ID':'access',
            'ORACLE_SECRET_ACCESS_KEY':'secret',
        }
        with mock.patch.dict(os.environ,environment,clear=True):
            context=libvm.storage_context()
            aws_environment=libvm.aws_env()
        self.assertEqual(
            context['endpoint'],
            'https://namespace.compat.objectstorage.eu-frankfurt-1.oci.customer-oci.com',
        )
        self.assertEqual(
            aws_environment['AWS_REQUEST_CHECKSUM_CALCULATION'],'when_required'
        )
        self.assertEqual(
            aws_environment['AWS_RESPONSE_CHECKSUM_VALIDATION'],'when_required'
        )
        self.assertEqual(aws_environment['AWS_EC2_METADATA_DISABLED'],'true')

    def test_s3_to_s3_copy_disables_property_copy(self):
        captured=[]
        with mock.patch.object(
            libvm,'endpoint',return_value='https://endpoint.example'
        ), mock.patch.object(libvm,'aws_env',return_value={}), mock.patch.object(
            libvm,'run',side_effect=lambda command,**kwargs:captured.append(command)
        ), mock.patch.object(libvm,'log'):
            libvm.aws_cp('s3://bucket/source','s3://bucket/destination')
        self.assertIn('--no-progress',captured[0])
        self.assertEqual(captured[0][-2:],['--copy-props','none'])

    def test_storage_endpoints_require_https_without_embedded_credentials(self):
        self.assertEqual(
            libvm.validate_storage_endpoint('https://storage.example'),
            'https://storage.example',
        )
        for endpoint in (
            'http://storage.example',
            'https://user:secret@storage.example',
            'https://storage.example?token=secret',
        ):
            with self.assertRaises(RuntimeError):
                libvm.validate_storage_endpoint(endpoint)

    def test_vm_listeners_require_a_tailscale_ipv4_address(self):
        self.assertEqual(
            libvm.validate_tailscale_bind_address('100.100.10.20'),
            '100.100.10.20',
        )
        for address in ('0.0.0.0','127.0.0.1','192.168.1.10','not-an-ip'):
            with self.assertRaises(RuntimeError):
                libvm.validate_tailscale_bind_address(address)

    def test_base_download_urls_are_https_and_redacted(self):
        self.assertEqual(
            libbase.safe_download_url('https://example.test/windows.iso?signature=secret'),
            'https://example.test/windows.iso',
        )
        with self.assertRaises(ValueError):
            libbase.safe_download_url('http://example.test/windows.iso')

    def test_manifest_binds_archive_and_immutable_base(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path=Path(directory)/'manifest.json'
            archive_digest='a'*64
            base_digest='b'*64
            manifest_path.write_text(json.dumps({
                'version':3,
                'name':'latest',
                'sha256':archive_digest,
                'compressed':'overlay.qcow2.zst',
                'base_object':'base.qcow2',
                'base_sha256':base_digest,
            }))
            parsed=libvm.validate_checkpoint_manifest(
                manifest_path,archive_digest,base_digest,{'base_object':'base.qcow2'}
            )
            self.assertEqual(parsed['version'],3)
            with self.assertRaisesRegex(RuntimeError,'base SHA256'):
                libvm.validate_checkpoint_manifest(
                    manifest_path,archive_digest,'c'*64,{'base_object':'base.qcow2'}
                )

    def test_all_workflow_yaml_parses(self):
        workflows=sorted((ROOT/'.github'/'workflows').glob('*.yml'))
        self.assertTrue(workflows)
        for workflow in workflows:
            parsed=yaml.load(
                workflow.read_text(encoding='utf-8'),
                Loader=yaml.BaseLoader,
            )
            self.assertIsInstance(parsed,dict,workflow)
            self.assertIn('on',parsed,workflow)
            self.assertIn('jobs',parsed,workflow)
            self.assertEqual(parsed.get('permissions',{}).get('contents'),'read')
            for job in parsed['jobs'].values():
                for step in job.get('steps',[]):
                    action=step.get('uses')
                    if action:
                        self.assertRegex(action,r'^[^@]+@[0-9a-f]{40}$')
                    command=step.get('run','')
                    self.assertNotIn('${{ inputs.',command)

        dependabot=yaml.load(
            (ROOT/'.github'/'dependabot.yml').read_text(encoding='utf-8'),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(dependabot.get('version'),'2')


if __name__ == '__main__':
    unittest.main()
