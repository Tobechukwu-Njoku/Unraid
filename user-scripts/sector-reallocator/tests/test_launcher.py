"""No raw devices, real host state or drive utilities are accessed by this suite."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('unraid_launcher', ROOT / 'unraid_launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='launcher-text-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mount = self.root / 'mounted'
        self.mount.mkdir()
        self.evidence = self.mount / 'evidence'
        self.mountinfo = self.root / 'mountinfo'
        self.set_mount()

    def set_mount(self, filesystem='xfs', mount=None, options='rw', super_options='rw'):
        point = str(mount or self.mount).replace(' ', r'\040')
        self.mountinfo.write_text(f'32 1 8:17 / {point} {options} - {filesystem} /dev/MOCK {super_options}\n')

    def arguments(self, **overrides):
        values = dict(device='/dev/disk/by-id/ata-TEST', mode='discover', expected_serial='TEST',
                      confirm_serial='', evidence_dir=str(self.evidence), max_scan_gb=None,
                      allow_damaged_data=False, skip_surface_scan=False, resume_interrupted_scan=False,
                      allow_partial_smart_discovery=False, allow_unsupported_erc=False,
                      allow_degraded_no_verify=False, enable_hdparm_repair_fallback=False)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_manifest_pins_the_reviewed_engine(self):
        manifest = json.loads((ROOT / 'engine-manifest.json').read_text())
        self.assertEqual(manifest['sha256'], hashlib.sha256((ROOT / manifest['file']).read_bytes()).hexdigest())

    def test_quoted_unraid_ini(self):
        p = self.root / 'disks.ini'
        p.write_text('["flash"]\ndevice="sda"\nid="USB_100%"\n["disk1"]\ndevice="sdb"\nid="MODEL_TEST"\n')
        data = launcher.read_ini(p)
        self.assertEqual(data['flash']['id'], 'USB_100%')
        self.assertEqual(data['disk1']['device'], 'sdb')

    def test_flat_unraid_state(self):
        p = self.root / 'var.ini';p.write_text('mdState="STOPPED"\n')
        self.assertEqual(launcher.read_ini(p, False)['state']['mdstate'], 'STOPPED')

    def test_missing_malformed_and_duplicate_state_rejected(self):
        p = self.root / 'bad.ini'
        for content in (None, 'invalid data', '["flash"]\ndevice="sda"\ndevice="sdb"\n'):
            if content is not None:p.write_text(content)
            with self.assertRaises(launcher.GateError):launcher.read_ini(p)

    def test_assignment_rejected_by_device(self):
        with self.assertRaises(launcher.GateError):
            launcher.check_assignments({'flash': {'device': 'sda'}, 'disk1': {'device': 'sdb'}}, Path('/dev/sdb'), 'TEST')

    def test_boot_partition_excludes_its_whole_disk(self):
        with self.assertRaises(launcher.GateError):
            launcher.check_assignments({'flash': {'device': 'sda1'}}, Path('/dev/sda'), 'TEST')

    def test_assignment_rejected_by_serial_when_device_missing(self):
        with self.assertRaises(launcher.GateError):
            launcher.check_assignments({'flash': {'device': 'sda'}, 'cache': {'id': 'MODEL_TEST', 'device': ''}}, Path('/dev/sdb'), 'TEST')

    def test_unassigned_device_allowed(self):
        launcher.check_assignments({'flash': {'device': 'sda'}, 'disk1': {'device': 'sdc', 'id': 'OTHER'}}, Path('/dev/sdb'), 'TEST')

    def test_missing_flash_identity_rejected(self):
        for data in ({}, {'flash': {}}, {'disk1': {'device': 'sdc'}}):
            with self.assertRaises(launcher.GateError):launcher.check_assignments(data, Path('/dev/sdb'), 'TEST')

    def test_native_mount_allowed_without_creating_evidence(self):
        launcher.check_evidence_mount(self.mount, self.evidence, self.mountinfo)
        self.assertFalse(self.evidence.exists())

    def test_root_fallback_or_wrong_mount_rejected(self):
        self.set_mount(mount=self.root)
        with self.assertRaises(launcher.GateError):launcher.check_evidence_mount(self.mount, self.evidence, self.mountinfo)

    def test_nested_mount_cannot_replace_expected_mount(self):
        self.set_mount()
        with self.mountinfo.open('a') as f:
            f.write(f'33 32 8:18 / {self.evidence} rw - xfs /dev/OTHER rw\n')
        with self.assertRaises(launcher.GateError):launcher.check_evidence_mount(self.mount, self.evidence, self.mountinfo)

    def test_flash_fuse_and_ram_filesystems_rejected(self):
        for filesystem in ('vfat', 'fuse.shfs', 'tmpfs', 'overlay'):
            self.set_mount(filesystem=filesystem)
            with self.assertRaises(launcher.GateError):launcher.check_evidence_mount(self.mount, self.evidence, self.mountinfo)

    def test_readonly_mount_rejected(self):
        for options, super_options in [('ro', 'ro'), ('rw', 'ro')]:
            self.set_mount(options=options, super_options=super_options)
            with self.assertRaises(launcher.GateError):launcher.check_evidence_mount(self.mount, self.evidence, self.mountinfo)

    def test_evidence_symlink_rejected(self):
        other = self.root / 'other';other.mkdir();self.evidence.symlink_to(other)
        with self.assertRaises(launcher.GateError):launcher.check_evidence_mount(self.mount, self.evidence, self.mountinfo)

    def test_escaped_mount_spaces(self):
        mount = self.root / 'mount with spaces';mount.mkdir();self.set_mount(mount=mount)
        launcher.check_evidence_mount(mount, mount / 'evidence', self.mountinfo)

    def test_no_vms(self):
        proc = self.root / 'proc';proc.mkdir()
        launcher.check_virtual_machines(Path('/dev/MOCK'), proc, self.root / 'no-definitions')

    def test_running_qemu_rejected(self):
        proc = self.root / 'proc';(proc / '123').mkdir(parents=True);(proc / '123' / 'comm').write_text('qemu-system-x86\n')
        with self.assertRaises(launcher.GateError):launcher.check_virtual_machines(Path('/dev/MOCK'), proc, self.root / 'no-definitions')

    def test_vm_definitions_reject_raw_dev_and_file_references(self):
        proc = self.root / 'proc';proc.mkdir();definitions = self.root / 'qemu';definitions.mkdir()
        for attribute in ('dev', 'file'):
            (definitions / 'vm.xml').write_text(f'<domain><devices><disk><source {attribute}="/dev/MOCK"/></disk></devices></domain>')
            with self.assertRaises(launcher.GateError):launcher.check_virtual_machines(Path('/dev/MOCK'), proc, definitions)

    def test_stopped_vm_partition_and_by_id_alias_are_rejected(self):
        proc = self.root / 'proc';proc.mkdir()
        definitions = self.root / 'qemu';definitions.mkdir()
        disk = self.root / 'sdb';partition = self.root / 'sdb1'
        disk.touch();partition.touch()
        alias = self.root / 'ata-TEST-part1';alias.symlink_to(partition)
        for device in (partition, alias):
            (definitions / 'vm.xml').write_text(
                f'<domain><devices><disk><source dev="{device}"/></disk></devices></domain>')
            with self.assertRaises(launcher.GateError):
                launcher.check_virtual_machines(disk, proc, definitions)

    def test_partition_check_does_not_confuse_other_disk_names(self):
        self.assertFalse(launcher.device_matches_target_or_partition(self.root / 'sdba1', self.root / 'sdb'))
        self.assertTrue(launcher.device_matches_target_or_partition(self.root / 'nvme0n1p1', self.root / 'nvme0n1'))

    def test_wrapper_fsyncs_evidence_parent(self):
        engine = launcher.load_engine()
        launcher.install_guards(engine, types.SimpleNamespace(check=lambda *a: None))
        synced = [];original_fsync = os.fsync
        def record(fd):
            st = os.fstat(fd);synced.append((st.st_dev, st.st_ino));original_fsync(fd)
        with patch.object(launcher.os, 'fsync', side_effect=record):
            fd = engine.prepare_private_evidence_dir(self.evidence, min_free_bytes=0)
        try:
            st = self.mount.stat()
            self.assertIn((st.st_dev, st.st_ino), synced)
        finally:os.close(fd)

    def test_wrapper_stops_if_parent_sync_fails(self):
        engine = launcher.load_engine()
        launcher.install_guards(engine, types.SimpleNamespace(check=lambda *a: None))
        parent = self.mount.stat();original_fsync = os.fsync;child_descriptors = []
        def fail_parent(fd):
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) == (parent.st_dev, parent.st_ino):
                raise OSError('mock parent fsync failure')
            child_descriptors.append(fd);original_fsync(fd)
        with patch.object(launcher.os, 'fsync', side_effect=fail_parent):
            with self.assertRaisesRegex(engine.ReallocatorError, 'persist evidence'):
                engine.prepare_private_evidence_dir(self.evidence, min_free_bytes=0)
        self.assertTrue(child_descriptors)
        with self.assertRaises(OSError):os.fstat(child_descriptors[-1])

    def test_default_is_discovery(self):
        args = launcher.build_arguments(self.arguments())
        self.assertIn('--dry-run', args);self.assertNotIn('--execute', args)

    def test_execute_and_recovery_need_serial_confirmation(self):
        for mode in ('execute', 'recover'):
            with self.assertRaises(launcher.GateError):launcher.build_arguments(self.arguments(mode=mode))
            command = launcher.build_arguments(self.arguments(mode=mode, confirm_serial='TEST'))
            self.assertIn('--execute' if mode == 'execute' else '--reconcile-interrupted-state', command)

    def test_flags_and_limit_forwarded(self):
        command = launcher.build_arguments(self.arguments(max_scan_gb=1, resume_interrupted_scan=True))
        self.assertEqual(command[command.index('--max-scan-gb') + 1], '1')
        self.assertIn('--resume-interrupted-scan', command)
        with self.assertRaises(launcher.GateError):launcher.build_arguments(self.arguments(max_scan_gb=0))

    def test_engine_import_does_not_call_main(self):
        engine = launcher.load_engine()
        self.assertTrue(callable(engine.main))
        self.assertEqual(engine._ACTIVE_MUTATION_LEDGERS, {})

    def test_changed_assignment_blocks_engine_boundaries(self):
        calls = []
        def original(*args, **kwargs):calls.append('called');return None
        engine = types.SimpleNamespace(ReallocatorError=RuntimeError, prepare_private_evidence_dir=original, inspect_sata_drive=original,
                  assert_device_identity_and_capacity=original, apply_recovery_settings=original,
                  run_ddrescue_and_parse=original, verify_cleanup_target_identity=original)
        def refuse(*args):raise launcher.GateError('mock assignment change')
        launcher.install_guards(engine, types.SimpleNamespace(check=refuse))
        for name in ('assert_device_identity_and_capacity', 'apply_recovery_settings', 'run_ddrescue_and_parse'):
            with self.assertRaisesRegex(RuntimeError, 'assignment change'):getattr(engine, name)(object())
        self.assertIn('assignment change', engine.verify_cleanup_target_identity(object()))
        self.assertFalse(calls)

    def test_started_array_and_wrong_serial_rejected(self):
        guard = object.__new__(launcher.UnraidGuard)
        guard.device=Path('/dev/MOCK');guard.target=guard.device;guard.rdev=42;guard.serial='TEST'
        guard.mount=self.mount;guard.evidence=self.evidence
        with patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=42)), \
             patch.object(Path, 'resolve', return_value=guard.target):
            with self.assertRaisesRegex(launcher.GateError, 'serial'):
                guard.check(types.SimpleNamespace(serial='OTHER'))
            with patch.object(launcher, 'read_ini', return_value={'state': {'mdstate': 'STARTED'}}):
                with self.assertRaisesRegex(launcher.GateError, 'stopped'):guard.check()


if __name__ == '__main__':
    unittest.main()
