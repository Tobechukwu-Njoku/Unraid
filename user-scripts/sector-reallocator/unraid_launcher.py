#!/usr/bin/env python3
"""Manual Unraid entry point for the separately versioned SATA maintenance engine."""
import argparse
import configparser
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import xml.etree.ElementTree as ET

NATIVE_FILESYSTEMS = {'xfs', 'btrfs', 'ext4', 'zfs'}
STATE_DIR = Path('/usr/local/emhttp/state')


class GateError(RuntimeError):
    pass


def read_ini(path, sectioned=True):
    """Read Unraid's quoted INI values, preserving literal percent signs."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        text = path.read_text(encoding='utf-8')
        parser.read_string(text if sectioned else '[state]\n' + text)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise GateError(f'Cannot read trustworthy Unraid state from {path}: {exc}') from exc
    sections = {}
    for section in parser.sections():
        values = {}
        for key, value in parser.items(section):
            value = value.strip()
            if value.startswith('"'):
                if len(value) < 2 or not value.endswith('"'):
                    raise GateError(f'Malformed quoted state value in {path}')
                value = value[1:-1]
            values[key] = value
        sections[section.strip('"')] = values
    return sections


def unescape_mount(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), value)


def check_evidence_mount(mount, evidence, mountinfo=Path('/proc/self/mountinfo')):
    if not mount.is_absolute() or not evidence.is_absolute():
        raise GateError('Evidence mount and evidence directory must be absolute paths.')
    try:
        resolved_mount = mount.resolve(strict=True)
    except OSError as exc:
        raise GateError(f'Evidence mount is unavailable: {mount}') from exc
    if resolved_mount != mount or mount == Path('/'):
        raise GateError('Use the real, direct evidence mount path, without symlinks.')
    # Resolve existing ancestors as well as a not-yet-created final directory.
    resolved_evidence = evidence.resolve()
    if resolved_evidence != evidence or not evidence.is_relative_to(mount) or evidence == mount:
        raise GateError('Evidence directory must be below the selected mount, without symlinks or .. components.')
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError as exc:
        raise GateError('Cannot read mount information.') from exc
    covering = []
    for line in lines:
        fields = line.split()
        if '-' not in fields or len(fields) < 10:
            raise GateError('Malformed mount information; refusing to infer evidence storage.')
        separator = fields.index('-')
        if separator < 6 or len(fields) < separator + 4:
            raise GateError('Malformed mount information.')
        point = Path(unescape_mount(fields[4]))
        if evidence.is_relative_to(point):
            covering.append((len(point.parts), point, fields[separator + 1], fields[5], fields[separator + 3]))
    if not covering:
        raise GateError('No filesystem covers the evidence directory.')
    _, point, filesystem, options, super_options = max(covering, key=lambda item: item[0])
    if point != mount:
        raise GateError('Expected evidence mount is absent or a different mount covers the evidence directory.')
    if filesystem not in NATIVE_FILESYSTEMS:
        raise GateError(f'Evidence filesystem {filesystem!r} is unsupported; use a direct XFS/Btrfs/ext4/ZFS mount.')
    if 'rw' not in options.split(',') or 'ro' in super_options.split(','):
        raise GateError('Evidence filesystem must be mounted read/write.')


def check_assignments(disks, target, serial):
    if not disks or 'flash' not in disks:
        raise GateError('Unraid disk assignments are incomplete: boot-device entry is missing.')
    for name, values in disks.items():
        # device identifies the raw device; id also covers assignments whose device is missing.
        device, disk_id = values.get('device', ''), values.get('id', '')
        if device:
            candidate = Path(device) if device.startswith('/') else Path('/dev') / device
            resolved = candidate.resolve()
            partition_of_target = (resolved.parent == target.parent and
                                   re.fullmatch(re.escape(target.name) + r'(?:p)?[0-9]+', resolved.name))
            if resolved == target or partition_of_target:
                raise GateError(f'Target is assigned to Unraid slot {name!r}.')
        if serial and disk_id and serial.casefold() in disk_id.casefold():
            raise GateError(f'Target serial appears in Unraid assignment {name!r}.')
    if not disks['flash'].get('device'):
        raise GateError('Cannot positively identify the Unraid boot device.')


def check_virtual_machines(target, proc=Path('/proc'), definitions=Path('/etc/libvirt/qemu')):
    # Read process names only, never process arguments or environment variables.
    try:
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                name = (entry / 'comm').read_text().strip()
            except FileNotFoundError:
                continue  # Process exited while enumerating.
            if name.startswith('qemu') or name == 'kvm':
                raise GateError('Stop all virtual machines before running disk maintenance.')
        if definitions.exists():
            for xml in definitions.glob('*.xml'):
                root = ET.parse(xml).getroot()
                for source in root.findall('./devices/disk/source'):
                    device = source.get('dev') or source.get('file')
                    if device and Path(device).resolve() == target:
                        raise GateError('Target is referenced by a virtual machine disk definition.')
    except (OSError, ET.ParseError) as exc:
        raise GateError(f'Cannot establish virtual-machine exclusion: {exc}') from exc


class UnraidGuard:
    def __init__(self, device, serial, mount, evidence):
        self.device, self.serial = Path(device), serial
        self.mount, self.evidence = Path(mount), Path(evidence)
        if self.device.parent != Path('/dev/disk/by-id') or '-part' in self.device.name:
            raise GateError('Choose a whole-disk /dev/disk/by-id/ path.')
        if not serial.strip() or any(ord(c) < 32 for c in serial):
            raise GateError('An exact, nonempty expected serial is required.')
        try:
            st = self.device.stat()
        except OSError as exc:
            raise GateError('Target device is unavailable.') from exc
        if not stat.S_ISBLK(st.st_mode):
            raise GateError('Target is not a block device.')
        self.rdev, self.target = st.st_rdev, self.device.resolve()

    def check(self, drive=None):
        try:
            st = self.device.stat()
        except OSError as exc:
            raise GateError('Target device disappeared.') from exc
        if not stat.S_ISBLK(st.st_mode) or st.st_rdev != self.rdev or self.device.resolve() != self.target:
            raise GateError('Target block-device identity changed.')
        if drive is not None and drive.serial != self.serial:
            raise GateError('Live drive serial does not match the configured expected serial.')
        state = read_ini(STATE_DIR / 'var.ini', sectioned=False)['state']
        if state.get('mdstate') != 'STOPPED':
            raise GateError('The Unraid array must be stopped; maintenance mode is not sufficient.')
        check_assignments(read_ini(STATE_DIR / 'disks.ini'), self.target, self.serial)
        check_virtual_machines(self.target)
        check_evidence_mount(self.mount, self.evidence)


def load_engine():
    directory = Path(__file__).resolve().parent
    path = directory / 'sata_reallocator.py'
    manifest = json.loads((directory / 'engine-manifest.json').read_text())
    if manifest.get('file') != path.name or hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get('sha256'):
        raise GateError('Engine manifest does not match the installed script.')
    spec = importlib.util.spec_from_file_location('sata_reallocator_engine', path)
    engine = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = engine
    spec.loader.exec_module(engine)
    return engine


def install_guards(engine, guard):
    """Recheck assignments/identity at the engine's existing mutation boundaries."""
    def protect(name, use_drive=True):
        original = getattr(engine, name)
        def wrapped(*args, **kwargs):
            drive = (args[0] if args else kwargs.get('drive')) if use_drive else None
            try:
                guard.check(drive)
            except GateError as exc:
                raise engine.ReallocatorError(str(exc)) from exc
            return original(*args, **kwargs)
        setattr(engine, name, wrapped)

    original_inspect = engine.inspect_sata_drive
    def inspect(*args, **kwargs):
        drive = original_inspect(*args, **kwargs)
        try:
            guard.check(drive)
        except GateError as exc:
            raise engine.ReallocatorError(str(exc)) from exc
        return drive
    engine.inspect_sata_drive = inspect
    for name in ('assert_device_identity_and_capacity', 'apply_recovery_settings', 'run_ddrescue_and_parse'):
        protect(name)
    # Cleanup and Phase-B verification use this gate before any setting mutation/readback.
    original_cleanup_identity = engine.verify_cleanup_target_identity
    def cleanup_identity(drive):
        try:
            guard.check(drive)
        except GateError as exc:
            return str(exc)
        return original_cleanup_identity(drive)
    engine.verify_cleanup_target_identity = cleanup_identity


def build_arguments(args):
    if args.mode != 'discover' and args.confirm_serial != args.expected_serial:
        raise GateError('Execute and recovery require --confirm-serial matching --expected-serial.')
    command = [args.device, '--evidence-dir', args.evidence_dir]
    command.append({'discover': '--dry-run', 'execute': '--execute', 'recover': '--reconcile-interrupted-state'}[args.mode])
    if args.max_scan_gb is not None:
        if args.max_scan_gb <= 0:
            raise GateError('--max-scan-gb must be positive.')
        command.extend(['--max-scan-gb', str(args.max_scan_gb)])
    for name in ('allow_damaged_data', 'skip_surface_scan', 'resume_interrupted_scan',
                 'allow_partial_smart_discovery', 'allow_unsupported_erc',
                 'allow_degraded_no_verify', 'enable_hdparm_repair_fallback'):
        if getattr(args, name):
            command.append('--' + name.replace('_', '-'))
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('device')
    parser.add_argument('--mode', choices=['discover', 'execute', 'recover'], default='discover')
    parser.add_argument('--expected-serial', required=True)
    parser.add_argument('--confirm-serial', default='')
    parser.add_argument('--evidence-mount', required=True)
    parser.add_argument('--evidence-dir', required=True)
    parser.add_argument('--max-scan-gb', type=int)
    for flag in ('allow-damaged-data', 'skip-surface-scan', 'resume-interrupted-scan',
                 'allow-partial-smart-discovery', 'allow-unsupported-erc',
                 'allow-degraded-no-verify', 'enable-hdparm-repair-fallback'):
        parser.add_argument('--' + flag, action='store_true')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 9):
        raise GateError('Python 3.9 or newer is required.')
    if os.geteuid() != 0:
        raise GateError('Run this tool as root through User Scripts or the local console.')
    command = build_arguments(args)
    guard = UnraidGuard(args.device, args.expected_serial, args.evidence_mount, args.evidence_dir)
    guard.check()
    lock = os.open('/run/lock/sector-reallocator.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(lock)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o077:
            raise GateError('Maintenance lock is not a private root-owned regular file.')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GateError('Another sector-reallocator launcher is active.') from exc
        engine = load_engine()
        install_guards(engine, guard)
        original_argv = sys.argv
        try:
            sys.argv = ['sata_reallocator.py', *command]
            engine.main()
        finally:
            sys.argv = original_argv
    finally:
        os.close(lock)


if __name__ == '__main__':
    try:
        main()
    except (GateError, OSError, ValueError) as exc:
        print(f'[FATAL] {exc}', file=sys.stderr)
        sys.exit(1)
