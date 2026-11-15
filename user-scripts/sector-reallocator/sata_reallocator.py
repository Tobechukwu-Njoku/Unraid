#!/usr/bin/env python3
"""
SATA HDD Targeted Bad-Block Reallocator (v8 — Durable Corroboration & Qualification Edition)

Scope & Explicit Operational Constraints:
  - Target: Unmounted, unused SATA HDDs (512n, 512e, 4Kn) whose contents are blank or expendable.
  - Granularity: Writes full reported physical sectors (512B on 512n; 4096B on 512e/4Kn). On 512e
    drives, zeroing a 4096B physical sector overwrites all 8 constituent 512B logical sectors.
  - Concurrency & Locking: Holds an exclusive flock on the evidence journal and an O_EXCL descriptor
    on the target block device across the run. Linux O_EXCL blocks mounts, kernel holders (LVM/mdadm/dm),
    and other O_EXCL openers, but does not prevent unrelated root processes from issuing non-O_EXCL raw I/O.
  - Atomicity, Cache & Bounded Discovery Caveats: Physical alignment and RWF_DSYNC/O_DIRECT do not
    guarantee hardware write atomicity across power loss or firmware stalls, nor does logical O_DIRECT
    read-back prove magnetic platter access or physical spare-sector relocation. ATA SMART logs have
    bounded entry capacities and resumed '+' ddrescue extents reflect prior scan passes rather than a
    fresh surface read; a zero-defect run does not imply exhaustive media certification.
"""

import argparse
import dataclasses
import datetime
import errno
import fcntl
import hashlib
import json
import logging
import mmap
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

# Upstream sg3_utils exit status codes (include/sg_lib.h)
SG_LIB_OK = 0
SG_LIB_SYNTAX_ERROR = 1
SG_LIB_CAT_NOT_READY = 2
SG_LIB_CAT_MEDIUM_HARD = 3
SG_LIB_CAT_ILLEGAL_REQ = 5
SG_LIB_CAT_UNIT_ATTENTION = 6
SG_LIB_CAT_DATA_PROTECT = 7
SG_LIB_CAT_INVALID_OP = 9
SG_LIB_CAT_COPY_ABORTED = 10
SG_LIB_CAT_ABORTED_COMMAND = 11
SG_LIB_CAT_MISCOMPARE = 14
SG_LIB_CAT_MEDIUM_HARD_WITH_INFO = 18
SG_LIB_CAT_TIMEOUT = 33

DDRESCUE_DEV_NULL = "/dev/null"
MIN_EVIDENCE_FREE_BYTES = 64 * 1024 * 1024  # 64 MiB minimum free space in evidence dir
RESTRICTIVE_UMASK = 0o077

RESOLVED_TARGET_STATUSES = frozenset({
    "VERIFIED_LOGICAL_READBACK_AND_MEDIA_VERIFY",
    "DEGRADED_LOGICAL_READBACK_ONLY_NO_MEDIA_VERIFY",
})

DISCOVERY_ABORT_JOURNAL_EVENTS = frozenset({
    "FATAL_ABORT",
    "UNHANDLED_EXCEPTION",
})


class ReallocatorError(Exception):
    """Base exception for deterministic fail-closed aborts."""


class MapfileValidationError(ReallocatorError):
    """Raised when a ddrescue mapfile or its bound metadata fails strict validation."""


class DeviceIdentityError(ReallocatorError):
    """Raised when device identity, geometry, or capacity drifts or fails validation."""


@dataclasses.dataclass(frozen=True)
class SataDriveInfo:
    input_path: str
    real_path: str
    persistent_path: str
    sysfs_name: str
    dev_major: int
    dev_minor: int
    model: str
    serial: str
    sanitized_serial: str
    logical_bs: int
    physical_bs: int
    alignment_offset: int
    size_bytes: int
    orig_kernel_timeout: Optional[str]
    orig_sct_erc: Optional[Tuple[int, int]]  # (read_deciseconds, write_deciseconds); 0 = disabled


@dataclasses.dataclass
class MutationLedger:
    """
    Caller-owned mutation ledger updated BEFORE each state-change attempt so partial
    or failed mutations are always tracked and reconciled during cleanup.
    """
    timeout_attempted: bool = False
    timeout_applied: bool = False
    orig_kernel_timeout: Optional[str] = None
    target_kernel_timeout: Optional[str] = None
    erc_attempted: bool = False
    erc_applied: bool = False
    orig_sct_erc: Optional[Tuple[int, int]] = None
    target_sct_erc: Optional[Tuple[int, int]] = None


_ACTIVE_MUTATION_LEDGERS: Dict[Tuple[str, str], MutationLedger] = {}


def _drive_ledger_key(drive: Any) -> Tuple[str, str]:
    return (str(getattr(drive, "serial", "")), str(getattr(drive, "sysfs_name", "")))


def get_or_bind_mutation_ledger(drive: Any, ledger: Optional[MutationLedger] = None) -> MutationLedger:
    key = _drive_ledger_key(drive)
    if ledger is not None:
        _ACTIVE_MUTATION_LEDGERS[key] = ledger
        return ledger
    if key not in _ACTIVE_MUTATION_LEDGERS:
        _ACTIVE_MUTATION_LEDGERS[key] = MutationLedger(
            orig_kernel_timeout=getattr(drive, "orig_kernel_timeout", None),
            orig_sct_erc=getattr(drive, "orig_sct_erc", None),
        )
    return _ACTIVE_MUTATION_LEDGERS[key]


@dataclasses.dataclass
class RunReplayState:
    run_id: str
    run_start_seen: bool = False
    is_open: bool = False
    closure_exit_code: Optional[int] = None
    aborted_by_exception: Optional[str] = None
    discovery_abort_events: List[str] = dataclasses.field(default_factory=list)
    had_write_activity: bool = False
    had_hard_unresolved_event: bool = False
    setting_baselines: Dict[str, Any] = dataclasses.field(default_factory=dict)
    restored_settings: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class JournalReplayState:
    """
    Authoritative replayed state of a journal file consumed by both preflight and recovery.
    """
    runs: Dict[str, RunReplayState] = dataclasses.field(default_factory=dict)
    pending_intents: Dict[Tuple[str, int, int, str, str], Dict[str, Any]] = dataclasses.field(default_factory=dict)
    unresolved_outcomes: Dict[int, Dict[str, Any]] = dataclasses.field(default_factory=dict)
    unresolved_confirmation_stops: Dict[Tuple[str, int], Dict[str, Any]] = dataclasses.field(default_factory=dict)
    pending_setting_mutations: Dict[Tuple[str, str], Dict[str, Any]] = dataclasses.field(default_factory=dict)
    unresolved_restoration_events: Dict[Tuple[str, int], Dict[str, Any]] = dataclasses.field(default_factory=dict)
    interrupted_runs: Dict[str, RunReplayState] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class VerifyResult:
    category: str          # "OK", "DEFECT", "UNSUPPORTED", "INDETERMINATE"
    return_code: int
    sense_key: Optional[str]
    asc_ascq: Optional[str]
    info_lba: Optional[int]
    detail: str


@dataclasses.dataclass
class AtaSmartSnapshot:
    reallocated_sector_ct: Optional[int] = None      # ID 5
    reallocated_event_ct: Optional[int] = None       # ID 196
    current_pending_sector: Optional[int] = None     # ID 197
    offline_uncorrectable: Optional[int] = None      # ID 198


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sanitize_serial_token(serial: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", serial.strip())[:48]
    digest = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned or 'serial'}_{digest}"


def _validate_required_dir_fd(dir_fd: int) -> int:
    if isinstance(dir_fd, bool) or not isinstance(dir_fd, int) or dir_fd < 0:
        raise ReallocatorError(f"Invalid directory descriptor {dir_fd!r}: must be a non-negative integer fd.")
    try:
        st = os.fstat(dir_fd)
    except OSError as e:
        raise ReallocatorError(f"Invalid or closed directory descriptor {dir_fd}: {e}") from e
    if not stat.S_ISDIR(st.st_mode):
        raise ReallocatorError(f"Descriptor {dir_fd} is not a directory (mode=0o{st.st_mode:o}).")
    return dir_fd


def _open_parent_dir_fd(path: pathlib.Path) -> int:
    parent = path.parent if str(path.parent) else pathlib.Path(".")
    validate_ancestor_chain_no_symlinks(parent / "anchor")
    try:
        fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise ReallocatorError(f"Failed to open parent directory descriptor for {parent}: {e}") from e
    try:
        _validate_required_dir_fd(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_all_bytes(fd: int, data: bytes) -> None:
    total = len(data)
    written = 0
    while written < total:
        n = os.write(fd, data[written:])
        if n <= 0:
            raise ReallocatorError(f"Short/zero write on fd {fd} ({written}/{total} bytes).")
        written += n


def read_all_bytes_from_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: List[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def run_cmd(cmd: List[str], timeout: int = 300, umask: int = RESTRICTIVE_UMASK) -> subprocess.CompletedProcess:
    logging.debug("Executing: %s", " ".join(cmd))
    prev_umask = os.umask(umask)
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            umask=umask,
        )
    finally:
        os.umask(prev_umask)


def check_dependencies() -> None:
    required = ["smartctl", "ddrescue", "blockdev", "blkid", "sg_verify", "hdparm"]
    missing = [t for t in required if shutil.which(t) is None]
    if missing:
        raise ReallocatorError(f"Missing required system binaries: {', '.join(missing)}")


def get_block_rdev(path: str) -> Tuple[int, int]:
    st = os.stat(path)
    if not stat.S_ISBLK(st.st_mode):
        raise ReallocatorError(f"{path} is not a block device.")
    return os.major(st.st_rdev), os.minor(st.st_rdev)


def validate_ancestor_chain_no_symlinks(target_dir: pathlib.Path) -> None:
    abs_str = os.path.abspath(str(target_dir))
    parts = [p for p in abs_str.split(os.sep) if p]
    curr = pathlib.Path(os.sep)
    for part in parts[:-1]:
        curr = curr / part
        st = os.lstat(curr)
        if stat.S_ISLNK(st.st_mode):
            raise ReallocatorError(f"Ancestor directory {curr} is a symlink; refusing evidence path.")
        if not stat.S_ISDIR(st.st_mode):
            raise ReallocatorError(f"Ancestor path {curr} is not a directory.")


def prepare_private_evidence_dir(
    evidence_dir: pathlib.Path,
    min_free_bytes: int = MIN_EVIDENCE_FREE_BYTES,
) -> int:
    validate_ancestor_chain_no_symlinks(evidence_dir)
    created_new = False
    if not evidence_dir.exists():
        prev_umask = os.umask(RESTRICTIVE_UMASK)
        try:
            os.mkdir(evidence_dir, 0o700)
        finally:
            os.umask(prev_umask)
        created_new = True

    st = os.lstat(evidence_dir)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ReallocatorError(f"Evidence path {evidence_dir} must be a real directory, not a symlink.")
    if st.st_uid != os.geteuid():
        raise ReallocatorError(f"Evidence directory {evidence_dir} is not owned by uid {os.geteuid()}.")

    dir_fd = os.open(str(evidence_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st_fd = os.fstat(dir_fd)
        if (st_fd.st_dev, st_fd.st_ino) != (st.st_dev, st.st_ino):
            raise ReallocatorError(f"Evidence directory {evidence_dir} changed identity during open.")
        if (st_fd.st_mode & 0o077) != 0:
            if created_new:
                os.fchmod(dir_fd, 0o700)
            else:
                raise ReallocatorError(
                    f"Existing evidence directory {evidence_dir} has group/other permissions "
                    f"(mode=0o{stat.S_IMODE(st_fd.st_mode):o}). Refusing to chmod an existing "
                    "directory; provide a dedicated 0700 directory or a new directory path."
                )

        vfs = os.fstatvfs(dir_fd)
        free_bytes = vfs.f_bavail * vfs.f_frsize
        if free_bytes < min_free_bytes:
            raise ReallocatorError(
                f"Insufficient free space in {evidence_dir}: {free_bytes}B available, "
                f"require at least {min_free_bytes}B."
            )
        os.fsync(dir_fd)
        return dir_fd
    except BaseException:
        os.close(dir_fd)
        raise


def open_safe_regular_file(
    path: pathlib.Path,
    flags: int,
    forbidden_inodes: Optional[Set[Tuple[int, int]]],
    dir_fd: int,
) -> Tuple[int, Tuple[int, int]]:
    _validate_required_dir_fd(dir_fd)
    if path.name in ("", ".", "..") or os.sep in path.name:
        raise ReallocatorError(f"Invalid relative evidence filename {path.name!r}.")

    safe_flags = flags | os.O_NOFOLLOW | os.O_CLOEXEC
    prev_umask = os.umask(RESTRICTIVE_UMASK)
    try:
        fd = os.open(path.name, safe_flags, 0o600, dir_fd=dir_fd)
    except OSError as e:
        raise ReallocatorError(f"Failed to safely open regular file {path}: {e}") from e
    finally:
        os.umask(prev_umask)

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ReallocatorError(f"Path {path} is not a regular file (mode=0o{st.st_mode:o}).")
        if st.st_nlink != 1:
            raise ReallocatorError(f"Path {path} has multiple hardlinks (st_nlink={st.st_nlink}).")
        if st.st_uid != os.geteuid():
            raise ReallocatorError(f"Path {path} is not owned by uid {os.geteuid()}.")
        if (st.st_mode & 0o077) != 0:
            raise ReallocatorError(
                f"Evidence file {path} has insecure permissions (mode=0o{stat.S_IMODE(st.st_mode):o})."
            )
        dev_ino = (st.st_dev, st.st_ino)
        if forbidden_inodes is not None and dev_ino in forbidden_inodes:
            raise ReallocatorError(f"Path {path} aliases a forbidden file or device inode ({dev_ino}).")
        return fd, dev_ino
    except BaseException:
        os.close(fd)
        raise


def compute_file_sha256(
    path: pathlib.Path,
    dir_fd: int,
    forbidden_inodes: Optional[Set[Tuple[int, int]]] = None,
) -> Tuple[str, Tuple[int, int]]:
    _validate_required_dir_fd(dir_fd)
    fd, dev_ino = open_safe_regular_file(path, os.O_RDONLY, forbidden_inodes, dir_fd=dir_fd)
    try:
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            h.update(chunk)
        return h.hexdigest(), dev_ino
    finally:
        os.close(fd)


def atomic_write_safe_json(
    path: pathlib.Path,
    payload: Dict[str, Any],
    dir_fd: int,
    forbidden_inodes: Set[Tuple[int, int]],
) -> Tuple[int, int]:
    _validate_required_dir_fd(dir_fd)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        os.unlink(tmp_path.name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass

    fd, _ = open_safe_regular_file(
        tmp_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        forbidden_inodes,
        dir_fd=dir_fd,
    )
    try:
        raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        write_all_bytes(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(tmp_path.name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.fsync(dir_fd)
    st = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
    dev_ino = (st.st_dev, st.st_ino)
    if dev_ino in forbidden_inodes:
        raise ReallocatorError(f"Metadata file {path} aliases a forbidden inode ({dev_ino}).")
    return dev_ino


def _normalize_setting_value(setting: str, val: Any) -> Any:
    if setting == "kernel_timeout":
        if not isinstance(val, str) or not val.strip():
            raise ReallocatorError(f"Invalid kernel_timeout baseline value {val!r} in journal.")
        return val.strip()
    if setting == "sct_erc":
        if not isinstance(val, (list, tuple)) or len(val) != 2 or not all(isinstance(x, int) and x >= 0 for x in val):
            raise ReallocatorError(f"Invalid sct_erc baseline value {val!r} in journal.")
        return (int(val[0]), int(val[1]))
    raise ReallocatorError(f"Unknown setting name {setting!r} in journal.")


def replay_journal_state(
    raw_bytes: bytes,
    drive: Any,
    journal_path: pathlib.Path,
) -> JournalReplayState:
    """
    Shared deterministic journal replay routine consumed by both normal preflight
    (`WriteAheadJournal.reconcile_prior_records`) and explicit recovery
    (`reconcile_interrupted_journal_state`).
    """
    state = JournalReplayState()
    if not raw_bytes:
        return state

    def get_run(r_id: str) -> RunReplayState:
        if r_id not in state.runs:
            state.runs[r_id] = RunReplayState(run_id=r_id)
        return state.runs[r_id]

    for line_no, raw_line in enumerate(raw_bytes.decode("utf-8", errors="strict").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            rec = json.loads(raw_line)
        except json.JSONDecodeError as e:
            raise ReallocatorError(
                f"Corrupted journal record at {journal_path}:{line_no} (possible interrupted write): {e}"
            ) from e

        if (
            rec.get("device_serial") != drive.serial
            or rec.get("device_model") != drive.model
            or rec.get("size_bytes") != drive.size_bytes
            or rec.get("physical_bs") != drive.physical_bs
        ):
            raise ReallocatorError(
                f"Journal {journal_path} is bound to a different drive/geometry at line {line_no}."
            )

        ev_type = str(rec.get("event_type", ""))
        rec_run_id = str(rec.get("run_id", "LEGACY_NO_RUN_ID"))
        run_obj = get_run(rec_run_id)
        seq_no = int(rec.get("seq", line_no))

        if ev_type == "RUN_START":
            run_obj.run_start_seen = True
            run_obj.is_open = True
            run_obj.closure_exit_code = None
            run_obj.aborted_by_exception = None

        elif ev_type == "RUN_CLOSURE":
            run_obj.is_open = False
            run_obj.closure_exit_code = int(rec.get("exit_code", 1))
            run_obj.aborted_by_exception = rec.get("aborted_by_exception")

        elif ev_type == "SETTING_MUTATION_INTENT":
            s_name = str(rec.get("setting", ""))
            norm_orig = _normalize_setting_value(s_name, rec.get("original"))
            if s_name in run_obj.setting_baselines and run_obj.setting_baselines[s_name] != norm_orig:
                raise ReallocatorError(
                    f"Contradictory baseline for {s_name!r} within run {rec_run_id} at line {line_no}."
                )
            run_obj.setting_baselines[s_name] = norm_orig
            run_obj.restored_settings.pop(s_name, None)
            state.pending_setting_mutations[(rec_run_id, s_name)] = {
                **rec,
                "normalized_original": norm_orig,
                "line_no": line_no,
            }

        elif ev_type == "SETTING_RESTORED":
            s_name = str(rec.get("setting", ""))
            target_r_id = str(rec.get("target_run_id", rec_run_id))
            norm_restored = _normalize_setting_value(s_name, rec.get("restored_value"))
            mut_key = (target_r_id, s_name)
            if mut_key not in state.pending_setting_mutations:
                raise ReallocatorError(
                    f"Orphan SETTING_RESTORED for run={target_r_id!r}, setting={s_name!r} at line {line_no}."
                )
            expected_orig = state.pending_setting_mutations[mut_key]["normalized_original"]
            if norm_restored != expected_orig:
                raise ReallocatorError(
                    f"SETTING_RESTORED value mismatch for run={target_r_id!r}, setting={s_name!r} at line {line_no}: "
                    f"restored={norm_restored!r}, expected={expected_orig!r}."
                )
            state.pending_setting_mutations.pop(mut_key)
            target_run_obj = get_run(target_r_id)
            target_run_obj.restored_settings[s_name] = norm_restored

        elif ev_type == "UNRESOLVED_RESTORATION_STATE":
            run_obj.had_hard_unresolved_event = True
            state.unresolved_restoration_events[(rec_run_id, seq_no)] = rec

        elif ev_type == "RESTORATION_STATE_RECONCILED":
            target_r_id = str(rec.get("target_run_id", ""))
            target_seq = int(rec.get("target_seq", -1))
            r_key = (target_r_id, target_seq)
            if r_key not in state.unresolved_restoration_events:
                raise ReallocatorError(
                    f"Orphan RESTORATION_STATE_RECONCILED for {r_key} at line {line_no}."
                )
            target_run_obj = state.runs.get(target_r_id)
            if target_run_obj is None or not target_run_obj.setting_baselines:
                raise ReallocatorError(
                    f"RESTORATION_STATE_RECONCILED at line {line_no} references run {target_r_id!r} "
                    "without recorded setting mutation baselines."
                )
            unrestored_for_run = [
                s_name for s_name in target_run_obj.setting_baselines
                if (target_r_id, s_name) in state.pending_setting_mutations
                or s_name not in target_run_obj.restored_settings
            ]
            if unrestored_for_run:
                raise ReallocatorError(
                    f"RESTORATION_STATE_RECONCILED at line {line_no} precedes SETTING_RESTORED "
                    f"for run {target_r_id!r} settings: {', '.join(sorted(unrestored_for_run))}."
                )
            state.unresolved_restoration_events.pop(r_key)

        elif ev_type == "CONFIRMATION_INDETERMINATE_STOP":
            run_obj.had_hard_unresolved_event = True
            if "offset_bytes" not in rec or not isinstance(rec["offset_bytes"], int):
                raise ReallocatorError(f"CONFIRMATION_INDETERMINATE_STOP missing offset_bytes at line {line_no}.")
            offset = int(rec["offset_bytes"])
            state.unresolved_confirmation_stops[(rec_run_id, offset)] = rec

        elif ev_type == "CONFIRMATION_STOP_RECONCILED":
            target_r_id = str(rec.get("target_run_id", ""))
            offset = int(rec.get("offset_bytes", -1))
            obs_state = str(rec.get("observed_state", ""))
            c_key = (target_r_id, offset)
            if c_key not in state.unresolved_confirmation_stops:
                raise ReallocatorError(
                    f"Orphan CONFIRMATION_STOP_RECONCILED for run={target_r_id!r}, offset=0x{offset:x} at line {line_no}."
                )
            if obs_state not in ("READABLE", "CONFIRMED_DEFECT"):
                raise ReallocatorError(
                    f"Invalid observed_state {obs_state!r} in CONFIRMATION_STOP_RECONCILED at line {line_no}."
                )
            state.unresolved_confirmation_stops.pop(c_key)

        elif ev_type in DISCOVERY_ABORT_JOURNAL_EVENTS:
            run_obj.discovery_abort_events.append(f"line={line_no}:{ev_type}")

        elif ev_type == "WRITE_INTENT":
            run_obj.had_write_activity = True
            offset = int(rec["offset_bytes"])
            attempt = int(rec["attempt"])
            op = str(rec.get("operation", "pwritev_zero"))
            op_id = str(rec.get("op_id", f"{rec_run_id}:{offset}:{attempt}:{op}"))
            key = (rec_run_id, offset, attempt, op, op_id)
            state.pending_intents[key] = rec

        elif ev_type == "ATTEMPT_OUTCOME":
            run_obj.had_write_activity = True
            offset = int(rec["offset_bytes"])
            attempt = int(rec["attempt"])
            op = str(rec.get("operation", "pwritev_zero"))
            op_id = str(rec.get("op_id", f"{rec_run_id}:{offset}:{attempt}:{op}"))
            key = (rec_run_id, offset, attempt, op, op_id)
            if key not in state.pending_intents:
                raise ReallocatorError(
                    f"Orphan or cross-run ATTEMPT_OUTCOME at line {line_no} "
                    f"(run_id={rec_run_id!r}, offset=0x{offset:x}, attempt={attempt}, op={op!r}) "
                    "does not match any active WRITE_INTENT."
                )
            state.pending_intents.pop(key)
            status = str(rec.get("status", "UNKNOWN"))
            if status in RESOLVED_TARGET_STATUSES:
                state.unresolved_outcomes.pop(offset, None)
            else:
                state.unresolved_outcomes[offset] = {
                    **rec,
                    "run_id": rec_run_id,
                    "operation": op,
                    "op_id": op_id,
                }

        elif ev_type == "RETRY_CORROBORATION_FAILED":
            # v7 Low-Severity Finding Fix: Updates the operation-correlated unresolved
            # attempt outcome with the durable corroboration classification and detail.
            run_obj.had_write_activity = True
            offset = int(rec["offset_bytes"])
            attempt = int(rec["attempt"])
            op = str(rec.get("operation", "pwritev_zero"))
            op_id = str(rec.get("op_id", f"{rec_run_id}:{offset}:{attempt}:{op}"))
            existing = state.unresolved_outcomes.get(offset)
            if (
                existing is None
                or existing.get("run_id") != rec_run_id
                or int(existing.get("attempt", -1)) != attempt
                or str(existing.get("operation", "")) != op
                or str(existing.get("op_id", "")) != op_id
            ):
                raise ReallocatorError(
                    f"Orphan or mismatched RETRY_CORROBORATION_FAILED at line {line_no} "
                    f"(run_id={rec_run_id!r}, offset=0x{offset:x}, attempt={attempt}, op={op!r}, op_id={op_id!r})."
                )
            corroborated_status = str(rec.get("status", "")).strip()
            if not corroborated_status or corroborated_status in RESOLVED_TARGET_STATUSES:
                raise ReallocatorError(
                    f"Invalid status {corroborated_status!r} in RETRY_CORROBORATION_FAILED at line {line_no}."
                )
            state.unresolved_outcomes[offset] = {
                **existing,
                "prior_attempt_status": rec.get("prior_attempt_status", existing.get("status")),
                "corroboration_verify_status": rec.get("corroboration_verify_status"),
                "corroboration_verify_rc": rec.get("corroboration_verify_rc"),
                "corroboration_detail": rec.get("corroboration_detail"),
                "status": corroborated_status,
            }

        elif ev_type == "OPERATION_RECONCILED":
            target_r_id = str(rec.get("target_run_id", ""))
            offset = int(rec["offset_bytes"])
            attempt = int(rec["attempt"])
            op = str(rec.get("operation", "pwritev_zero"))
            op_id = str(rec.get("op_id", f"{target_r_id}:{offset}:{attempt}:{op}"))
            obs_state = str(rec.get("observed_state", ""))
            key = (target_r_id, offset, attempt, op, op_id)
            if key not in state.pending_intents:
                raise ReallocatorError(
                    f"Orphan OPERATION_RECONCILED at line {line_no} for key={key} "
                    "does not match any pending WRITE_INTENT."
                )
            if obs_state not in ("READABLE", "CONFIRMED_DEFECT"):
                raise ReallocatorError(
                    f"Invalid observed_state {obs_state!r} in OPERATION_RECONCILED at line {line_no}."
                )
            state.pending_intents.pop(key)

        elif ev_type == "TARGET_RECONCILED":
            offset = int(rec["offset_bytes"])
            obs_state = str(rec.get("observed_state", ""))
            if obs_state not in ("READABLE", "CONFIRMED_DEFECT") or rec.get("reconciled_status") != "MANUALLY_RECONCILED":
                raise ReallocatorError(f"Invalid TARGET_RECONCILED record at line {line_no}.")
            if offset not in state.unresolved_outcomes:
                raise ReallocatorError(
                    f"Orphan TARGET_RECONCILED at line {line_no} for offset=0x{offset:x} "
                    "(note: pending WRITE_INTENTs require OPERATION_RECONCILED)."
                )
            state.unresolved_outcomes.pop(offset)

        elif ev_type == "DISCOVERY_INTERRUPTION_RECONCILED":
            reconciled_ids = rec.get("reconciled_run_ids", [])
            if not isinstance(reconciled_ids, list):
                raise ReallocatorError(f"Invalid reconciled_run_ids at line {line_no}.")
            for r_id in reconciled_ids:
                if r_id in state.runs:
                    r_obj = state.runs[r_id]
                    r_obj.is_open = False
                    r_obj.discovery_abort_events.clear()
                    r_obj.aborted_by_exception = None
                    if r_obj.closure_exit_code is None or r_obj.closure_exit_code != 0:
                        r_obj.closure_exit_code = 0

        elif ev_type == "STATE_RECONCILED":
            reconciled_ids = rec.get("reconciled_run_ids")
            target_ids = (
                [str(x) for x in reconciled_ids]
                if isinstance(reconciled_ids, list)
                else list(state.runs.keys())
            )
            for r_id in target_ids:
                if r_id in state.runs and r_id != rec_run_id:
                    r_obj = state.runs[r_id]
                    r_obj.is_open = False
                    r_obj.discovery_abort_events.clear()
                    r_obj.aborted_by_exception = None
                    if r_obj.closure_exit_code is None or r_obj.closure_exit_code != 0:
                        r_obj.closure_exit_code = 0

    runs_with_open_obligations: Set[str] = set()
    for (r_id, _, _, _, _) in state.pending_intents:
        runs_with_open_obligations.add(r_id)
    for rec in state.unresolved_outcomes.values():
        if "run_id" in rec:
            runs_with_open_obligations.add(str(rec["run_id"]))
    for (r_id, _) in state.unresolved_confirmation_stops:
        runs_with_open_obligations.add(r_id)
    for (r_id, _) in state.pending_setting_mutations:
        runs_with_open_obligations.add(r_id)
    for (r_id, _) in state.unresolved_restoration_events:
        runs_with_open_obligations.add(r_id)

    for r_id, r_obj in state.runs.items():
        has_run_failure = (
            r_obj.is_open
            or (r_obj.closure_exit_code is not None and r_obj.closure_exit_code != 0)
            or (r_obj.aborted_by_exception is not None)
            or bool(r_obj.discovery_abort_events)
            or (r_id in runs_with_open_obligations)
        )
        if has_run_failure:
            state.interrupted_runs[r_id] = r_obj

    return state


def resolve_outstanding_setting_baselines(
    replay: JournalReplayState,
) -> Tuple[Optional[str], List[str], Optional[Tuple[int, int]], List[str]]:
    timeout_originals: Set[str] = set()
    timeout_runs: List[str] = []
    erc_originals: Set[Tuple[int, int]] = set()
    erc_runs: List[str] = []

    for (r_id, s_name), rec in sorted(replay.pending_setting_mutations.items()):
        norm_val = rec["normalized_original"]
        if s_name == "kernel_timeout":
            timeout_originals.add(norm_val)
            timeout_runs.append(r_id)
        elif s_name == "sct_erc":
            erc_originals.add(norm_val)
            erc_runs.append(r_id)

    if len(timeout_originals) > 1:
        raise ReallocatorError(
            f"Contradictory unresolved kernel_timeout baselines across runs: {sorted(timeout_originals)}."
        )
    if len(erc_originals) > 1:
        raise ReallocatorError(
            f"Contradictory unresolved sct_erc baselines across runs: {sorted(erc_originals)}."
        )

    t_base = next(iter(timeout_originals)) if timeout_originals else None
    e_base = next(iter(erc_originals)) if erc_originals else None
    return (t_base, timeout_runs, e_base, erc_runs)


def resolve_reverification_setting_baselines(
    replay: JournalReplayState,
    pending_t_base: Optional[str] = None,
    pending_e_base: Optional[Tuple[int, int]] = None,
) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    for (r_id, _) in sorted(replay.unresolved_restoration_events.keys()):
        r_obj = replay.runs.get(r_id)
        if r_obj is None or not r_obj.setting_baselines:
            raise ReallocatorError(
                "Cannot reconcile UNRESOLVED_RESTORATION_STATE without recorded setting mutation baselines."
            )

    reverify_timeout_originals: Set[str] = set()
    reverify_erc_originals: Set[Tuple[int, int]] = set()

    active_run_ids = set(replay.interrupted_runs.keys()) | {
        r_id for (r_id, _) in replay.unresolved_restoration_events.keys()
    }
    for r_id in sorted(active_run_ids):
        r_obj = replay.runs.get(r_id)
        if r_obj is None:
            continue
        for s_name, norm_val in r_obj.restored_settings.items():
            if (r_id, s_name) not in replay.pending_setting_mutations:
                if s_name == "kernel_timeout":
                    reverify_timeout_originals.add(norm_val)
                elif s_name == "sct_erc":
                    reverify_erc_originals.add(norm_val)

    combined_timeouts = set(reverify_timeout_originals)
    if pending_t_base is not None:
        combined_timeouts.add(pending_t_base)
    if len(combined_timeouts) > 1:
        raise ReallocatorError(
            f"Contradictory unresolved kernel_timeout baselines across runs: {sorted(combined_timeouts)}."
        )

    combined_ercs = set(reverify_erc_originals)
    if pending_e_base is not None:
        combined_ercs.add(pending_e_base)
    if len(combined_ercs) > 1:
        raise ReallocatorError(
            f"Contradictory unresolved sct_erc baselines across runs: {sorted(combined_ercs)}."
        )

    rev_t = next(iter(reverify_timeout_originals)) if reverify_timeout_originals else None
    rev_e = next(iter(reverify_erc_originals)) if reverify_erc_originals else None
    return (rev_t, rev_e)


class WriteAheadJournal:
    """
    Durable, flock-protected, fsync-backed write-ahead JSONL journal bound to drive identity.
    Uses `replay_journal_state` for both startup validation and discovery-resume checks.
    """

    def __init__(
        self,
        journal_path: pathlib.Path,
        dir_fd: int,
        drive: SataDriveInfo,
        run_id: str,
        forbidden_inodes: Set[Tuple[int, int]],
    ) -> None:
        _validate_required_dir_fd(dir_fd)
        self.path = journal_path
        self.dir_fd = dir_fd
        self.drive = drive
        self.run_id = run_id
        self.seq = 0
        self.fd, self.dev_ino = open_safe_regular_file(
            journal_path,
            os.O_CREAT | os.O_RDWR | os.O_APPEND,
            forbidden_inodes,
            dir_fd=dir_fd,
        )
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(self.fd)
            self.fd = -1
            raise ReallocatorError(
                f"Cannot acquire exclusive run lock on journal {journal_path}: another process is active ({e})."
            ) from e

        os.fsync(self.dir_fd)

    def reconcile_prior_records(self, allow_discovery_resume: bool = False) -> None:
        raw_bytes = read_all_bytes_from_fd(self.fd)
        if not raw_bytes:
            return

        replay = replay_journal_state(raw_bytes, self.drive, self.path)

        if replay.pending_intents:
            summary = ", ".join(
                f"run_id={k[0]} offset=0x{k[1]:x} attempt={k[2]} op={k[3]}" for k in replay.pending_intents
            )
            raise ReallocatorError(f"Unreconciled prior WRITE_INTENT(s) in {self.path}: {summary}.")

        if replay.unresolved_outcomes:
            u_list = ", ".join(
                f"offset=0x{off:x}:UNRESOLVED_OUTCOME({rec.get('status')})"
                for off, rec in sorted(replay.unresolved_outcomes.items())
            )
            raise ReallocatorError(f"Unresolved prior target outcome(s) in {self.path}: {u_list}.")

        if replay.pending_setting_mutations:
            s_list = ", ".join(
                f"run_id={k[0]} setting={k[1]}" for k in replay.pending_setting_mutations
            )
            raise ReallocatorError(
                f"Unrestored prior setting mutation(s) in {self.path}: {s_list}. "
                "Run with --reconcile-interrupted-state to restore and verify baseline settings."
            )

        if replay.unresolved_confirmation_stops:
            c_list = ", ".join(
                f"run_id={k[0]} offset=0x{k[1]:x}:CONFIRMATION_INDETERMINATE_STOP"
                for k in sorted(replay.unresolved_confirmation_stops)
            )
            raise ReallocatorError(f"Unresolved confirmation stop(s) in {self.path}: {c_list}.")

        if replay.unresolved_restoration_events:
            r_list = ", ".join(
                f"run_id={k[0]} seq={k[1]}:UNRESOLVED_RESTORATION_STATE"
                for k in sorted(replay.unresolved_restoration_events)
            )
            raise ReallocatorError(f"Unresolved error/restoration state(s) in {self.path}: {r_list}.")

        if replay.interrupted_runs:
            interrupted_with_writes = [
                r_id for r_id, r_obj in replay.interrupted_runs.items() if r_obj.had_write_activity
            ]
            interrupted_with_hard_events = [
                r_id for r_id, r_obj in replay.interrupted_runs.items() if r_obj.had_hard_unresolved_event
            ]
            if not allow_discovery_resume or interrupted_with_writes or interrupted_with_hard_events:
                reasons: List[str] = []
                for r_id, r_obj in sorted(replay.interrupted_runs.items()):
                    reasons.extend(r_obj.discovery_abort_events)
                    if r_obj.is_open:
                        reasons.append(f"open_run={r_id}")
                    if r_obj.closure_exit_code is not None and r_obj.closure_exit_code != 0:
                        reasons.append(f"run_id={r_id} exit_code={r_obj.closure_exit_code}")
                raise ReallocatorError(
                    f"Unresolved error/restoration state(s) in {self.path}: {', '.join(reasons)}."
                )

            interrupted_timeout_baselines: Set[str] = set()
            interrupted_erc_baselines: Set[Tuple[int, int]] = set()
            for r_obj in replay.interrupted_runs.values():
                if "kernel_timeout" in r_obj.setting_baselines:
                    interrupted_timeout_baselines.add(r_obj.setting_baselines["kernel_timeout"])
                if "sct_erc" in r_obj.setting_baselines:
                    interrupted_erc_baselines.add(r_obj.setting_baselines["sct_erc"])

            if len(interrupted_timeout_baselines) > 1 or len(interrupted_erc_baselines) > 1:
                raise ReallocatorError(
                    "Contradictory setting baselines across interrupted discovery runs; cannot auto-reconcile."
                )
            if len(interrupted_timeout_baselines) == 1:
                exp_t = next(iter(interrupted_timeout_baselines))
                if getattr(self.drive, "orig_kernel_timeout", None) != exp_t:
                    raise ReallocatorError(
                        f"Cannot auto-reconcile interrupted discovery: live kernel_timeout "
                        f"{getattr(self.drive, 'orig_kernel_timeout', None)!r} != baseline {exp_t!r}."
                    )
            if len(interrupted_erc_baselines) == 1:
                exp_erc = next(iter(interrupted_erc_baselines))
                if getattr(self.drive, "orig_sct_erc", None) != exp_erc:
                    raise ReallocatorError(
                        f"Cannot auto-reconcile interrupted discovery: live sct_erc "
                        f"{getattr(self.drive, 'orig_sct_erc', None)!r} != baseline {exp_erc!r}."
                    )

            reconciled_payload = {
                "reconciled_run_ids": sorted(replay.interrupted_runs.keys()),
                "verified_timeout_baseline": getattr(self.drive, "orig_kernel_timeout", None),
                "verified_sct_erc_baseline": (
                    list(self.drive.orig_sct_erc) if getattr(self.drive, "orig_sct_erc", None) is not None else None
                ),
            }
            if callable(getattr(self, "append", None)):
                self.append("DISCOVERY_INTERRUPTION_RECONCILED", reconciled_payload)

    def append(self, event_type: str, payload: Dict[str, Any]) -> None:
        self.seq += 1
        record = {
            "run_id": self.run_id,
            "seq": self.seq,
            "timestamp": utc_now_iso(),
            "device_serial": self.drive.serial,
            "device_model": self.drive.model,
            "dev_major": self.drive.dev_major,
            "dev_minor": self.drive.dev_minor,
            "size_bytes": self.drive.size_bytes,
            "physical_bs": self.drive.physical_bs,
            "event_type": event_type,
            **payload,
        }
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        write_all_bytes(self.fd, line)
        os.fsync(self.fd)

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.fsync(self.fd)
            finally:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(self.fd)
                self.fd = -1


def resolve_and_validate_device(dev_path: str) -> Tuple[str, str, str, int, int, Tuple[int, int]]:
    real_p = os.path.realpath(dev_path)
    if not os.path.exists(real_p):
        raise ReallocatorError(f"Device {dev_path} does not exist.")

    st = os.stat(real_p)
    if not stat.S_ISBLK(st.st_mode):
        raise ReallocatorError(f"{real_p} is not a block device.")

    major_num, minor_num = os.major(st.st_rdev), os.minor(st.st_rdev)
    dev_inode = (st.st_dev, st.st_ino)
    sysfs_name = os.path.basename(real_p)

    sys_block = pathlib.Path(f"/sys/class/block/{sysfs_name}")
    if (sys_block / "partition").exists() or not (sys_block / "device").exists():
        raise ReallocatorError(
            f"{dev_path} ({real_p}) is a partition or virtual device. Specify a whole-disk block node."
        )

    persistent = real_p
    by_id_dir = pathlib.Path("/dev/disk/by-id")
    if by_id_dir.exists():
        candidates = [
            str(entry)
            for entry in sorted(by_id_dir.iterdir())
            if entry.is_symlink()
            and os.path.realpath(str(entry)) == real_p
            and "-part" not in entry.name
        ]
        for prefix in ("wwn-", "ata-", "scsi-"):
            for cand in candidates:
                if os.path.basename(cand).startswith(prefix):
                    persistent = cand
                    break
            if persistent != real_p:
                break

    return real_p, persistent, sysfs_name, major_num, minor_num, dev_inode


def acquire_exclusive_device_and_verify_blank(
    real_path: str,
    persistent_path: str,
    sysfs_name: str,
    execute_mode: bool,
    allow_damaged_data: bool,
) -> int:
    sys_block = pathlib.Path(f"/sys/class/block/{sysfs_name}")
    target_names = [sysfs_name] + [
        p.name for p in sys_block.glob(f"{sysfs_name}*") if p.is_dir() and p.name != sysfs_name
    ]

    target_rdevs: Dict[str, str] = {}
    for name in target_names:
        dev_file = (sys_block / name / "dev") if name != sysfs_name else (sys_block / "dev")
        if dev_file.exists():
            target_rdevs[dev_file.read_text(encoding="utf-8").strip()] = f"/dev/{name}"

    with open("/proc/self/mountinfo", "r", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 5 and parts[2] in target_rdevs:
                raise ReallocatorError(
                    f"Safety gate: {target_rdevs[parts[2]]} ({parts[2]}) is mounted at {parts[4]}."
                )

    for name in target_names:
        holders_dir = (sys_block / name / "holders") if name != sysfs_name else (sys_block / "holders")
        if holders_dir.exists():
            holders = [h.name for h in holders_dir.iterdir()]
            if holders:
                raise ReallocatorError(
                    f"Safety gate: /dev/{name} has active kernel holders: {', '.join(holders)}."
                )

    open_flags = (
        (os.O_RDWR | os.O_DIRECT | os.O_EXCL | os.O_CLOEXEC)
        if execute_mode
        else (os.O_RDONLY | os.O_DIRECT | os.O_EXCL | os.O_CLOEXEC)
    )
    try:
        dev_fd = os.open(persistent_path, open_flags)
    except OSError as e:
        raise ReallocatorError(f"Cannot acquire persistent O_EXCL claim on {persistent_path}: {e}") from e

    try:
        for name in target_names:
            node = f"/dev/{name}"
            res = run_cmd(["blkid", "-p", node])
            if res.returncode == 0 and res.stdout.strip():
                if not allow_damaged_data:
                    raise ReallocatorError(
                        f"Existing signature on {node}: {res.stdout.strip()}. "
                        "Pass --allow-damaged-data if contents are expendable."
                    )
                logging.warning("Signature on %s ignored due to --allow-damaged-data.", node)
            elif res.returncode not in (0, 2):
                if not allow_damaged_data:
                    raise ReallocatorError(
                        f"blkid probe failed on {node} (rc={res.returncode}: {res.stderr.strip()}). Failing closed."
                    )
        return dev_fd
    except BaseException:
        os.close(dev_fd)
        raise


def parse_ata_sct_erc_json(data: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    erc = data.get("ata_sct_erc")
    if not isinstance(erc, dict):
        return None
    read_obj = erc.get("read")
    write_obj = erc.get("write")
    if not isinstance(read_obj, dict) or not isinstance(write_obj, dict):
        return None

    def extract_side(obj: Dict[str, Any]) -> Optional[int]:
        enabled = obj.get("enabled")
        if enabled is False:
            return 0
        if enabled is True:
            ds = obj.get("deciseconds")
            if isinstance(ds, int) and ds >= 0:
                return ds
        return None

    r_ds = extract_side(read_obj)
    w_ds = extract_side(write_obj)
    if r_ds is None or w_ds is None:
        return None
    return (r_ds, w_ds)


def query_current_sct_erc(persistent_path: str) -> Optional[Tuple[int, int]]:
    res = run_cmd(["smartctl", "-j", "-l", "scterc", persistent_path])
    if (res.returncode & 0b111) != 0:
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    return parse_ata_sct_erc_json(data)


def inspect_sata_drive(
    input_path: str,
    real_path: str,
    persistent_path: str,
    sysfs_name: str,
    dev_major: int,
    dev_minor: int,
) -> SataDriveInfo:
    res = run_cmd(["smartctl", "-j", "-a", persistent_path])
    if (res.returncode & 0b111) != 0:
        raise ReallocatorError(
            f"smartctl -a failed or reported SMART command/checksum error on {persistent_path} (rc={res.returncode})."
        )
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise ReallocatorError(f"smartctl returned invalid JSON for {persistent_path}: {e}") from e

    protocol = data.get("device", {}).get("protocol", "").upper()
    if protocol != "ATA" and "ata_smart_attributes" not in data:
        raise ReallocatorError(f"Device {persistent_path} reported protocol '{protocol}', not ATA/SATA.")

    if data.get("rotation_rate") == 0:
        raise ReallocatorError(f"Device {persistent_path} is a solid-state drive (rotation_rate=0).")

    model = (data.get("model_name") or "").strip()
    serial = (data.get("serial_number") or "").strip()
    if not model or not serial:
        raise ReallocatorError(f"Missing model or serial_number in smartctl output for {persistent_path}.")

    lbs_res = run_cmd(["blockdev", "--getss", persistent_path])
    pbs_res = run_cmd(["blockdev", "--getpbsz", persistent_path])
    al_res = run_cmd(["blockdev", "--getalignoff", persistent_path])
    sz_res = run_cmd(["blockdev", "--getsize64", persistent_path])
    if any(r.returncode != 0 for r in (lbs_res, pbs_res, al_res, sz_res)):
        raise ReallocatorError("blockdev geometry query failed.")

    logical_bs = int(lbs_res.stdout.strip())
    physical_bs = int(pbs_res.stdout.strip())
    align_off = int(al_res.stdout.strip())
    size_bytes = int(sz_res.stdout.strip())

    sys_align_file = pathlib.Path(f"/sys/class/block/{sysfs_name}/alignment_offset")
    sys_align = int(sys_align_file.read_text(encoding="utf-8").strip()) if sys_align_file.exists() else 0
    if align_off != 0 or sys_align != 0:
        raise ReallocatorError(
            f"Non-zero alignment_offset (blockdev={align_off}, sysfs={sys_align}) is unsupported."
        )

    smart_lbs = data.get("logical_block_size")
    smart_pbs = data.get("physical_block_size")
    smart_cap = data.get("user_capacity", {}).get("bytes")
    if isinstance(smart_lbs, int) and smart_lbs != logical_bs:
        raise ReallocatorError(f"Logical sector mismatch: blockdev={logical_bs}B vs smartctl={smart_lbs}B.")
    if isinstance(smart_pbs, int) and smart_pbs != physical_bs:
        raise ReallocatorError(f"Physical sector mismatch: blockdev={physical_bs}B vs smartctl={smart_pbs}B.")
    if isinstance(smart_cap, int) and smart_cap != size_bytes:
        raise ReallocatorError(f"Capacity mismatch: blockdev={size_bytes}B vs smartctl={smart_cap}B.")

    if logical_bs not in (512, 4096) or physical_bs not in (512, 4096):
        raise ReallocatorError(f"Unsupported sector sizes: logical={logical_bs}B, physical={physical_bs}B.")
    if physical_bs < logical_bs or (physical_bs % logical_bs) != 0:
        raise ReallocatorError(f"Invalid geometry: physical_bs ({physical_bs}) not a multiple of logical_bs ({logical_bs}).")
    if size_bytes <= 0 or (size_bytes % physical_bs) != 0:
        raise ReallocatorError(f"Drive capacity ({size_bytes}B) is not aligned to physical_bs ({physical_bs}B).")

    timeout_file = pathlib.Path(f"/sys/class/block/{sysfs_name}/device/timeout")
    orig_timeout = timeout_file.read_text(encoding="utf-8").strip() if timeout_file.exists() else None
    orig_erc = query_current_sct_erc(persistent_path)

    return SataDriveInfo(
        input_path=input_path,
        real_path=real_path,
        persistent_path=persistent_path,
        sysfs_name=sysfs_name,
        dev_major=dev_major,
        dev_minor=dev_minor,
        model=model,
        serial=serial,
        sanitized_serial=sanitize_serial_token(serial),
        logical_bs=logical_bs,
        physical_bs=physical_bs,
        alignment_offset=0,
        size_bytes=size_bytes,
        orig_kernel_timeout=orig_timeout,
        orig_sct_erc=orig_erc,
    )


def assert_device_identity_and_capacity(drive: SataDriveInfo, fd: Optional[int] = None) -> None:
    if fd is not None:
        st = os.fstat(fd)
        if not stat.S_ISBLK(st.st_mode) or (os.major(st.st_rdev), os.minor(st.st_rdev)) != (
            drive.dev_major,
            drive.dev_minor,
        ):
            raise DeviceIdentityError("Open descriptor major:minor drifted during execution.")

    cur_maj, cur_min = get_block_rdev(drive.persistent_path)
    if (cur_maj, cur_min) != (drive.dev_major, drive.dev_minor):
        raise DeviceIdentityError("Device path major:minor changed (possible link reset/re-enumeration).")

    sz_res = run_cmd(["blockdev", "--getsize64", drive.persistent_path])
    if sz_res.returncode != 0 or int(sz_res.stdout.strip()) != drive.size_bytes:
        raise DeviceIdentityError("Device capacity changed or failed to query during execution.")

    res = run_cmd(["smartctl", "-j", "-i", drive.persistent_path])
    if (res.returncode & 0b111) != 0:
        raise DeviceIdentityError(f"smartctl identity check failed (rc={res.returncode}).")
    try:
        info = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise DeviceIdentityError("smartctl identity check returned invalid JSON.") from e

    if info.get("serial_number") != drive.serial or info.get("model_name") != drive.model:
        raise DeviceIdentityError(
            f"Drive identity mismatch: expected {drive.model}/{drive.serial}, "
            f"got {info.get('model_name')}/{info.get('serial_number')}."
        )


def apply_recovery_settings(
    drive: SataDriveInfo,
    execute: bool,
    allow_unsupported_erc: bool,
    ledger: Optional[MutationLedger] = None,
    journal: Optional[WriteAheadJournal] = None,
) -> Tuple[bool, bool]:
    active_ledger = get_or_bind_mutation_ledger(drive, ledger)
    active_ledger.orig_kernel_timeout = drive.orig_kernel_timeout
    active_ledger.orig_sct_erc = drive.orig_sct_erc

    if not execute:
        return (False, False)

    if drive.orig_sct_erc is None and not allow_unsupported_erc:
        raise ReallocatorError(
            "Drive does not report readable SATA SCT ERC (`smartctl -l scterc`). "
            "Without bounded ERC, firmware recovery stalls can exceed kernel command timeouts. "
            "Pass --allow-unsupported-erc to override."
        )

    timeout_file = pathlib.Path(f"/sys/class/block/{drive.sysfs_name}/device/timeout")
    if drive.orig_kernel_timeout is not None and timeout_file.exists():
        active_ledger.timeout_attempted = True
        active_ledger.target_kernel_timeout = "60"
        if journal is not None:
            journal.append(
                "SETTING_MUTATION_INTENT",
                {
                    "setting": "kernel_timeout",
                    "original": drive.orig_kernel_timeout,
                    "target": "60",
                },
            )
        timeout_file.write_text("60\n", encoding="utf-8")
        actual_t = timeout_file.read_text(encoding="utf-8").strip()
        if actual_t != "60":
            raise ReallocatorError(f"Failed to verify kernel timeout update (expected 60, got {actual_t}).")
        active_ledger.timeout_applied = True

    if drive.orig_sct_erc is not None:
        active_ledger.erc_attempted = True
        active_ledger.target_sct_erc = (70, 70)
        if journal is not None:
            journal.append(
                "SETTING_MUTATION_INTENT",
                {
                    "setting": "sct_erc",
                    "original": list(drive.orig_sct_erc),
                    "target": [70, 70],
                },
            )
        res = run_cmd(["smartctl", "-l", "scterc,70,70", drive.persistent_path])
        verified_erc = query_current_sct_erc(drive.persistent_path)
        if res.returncode != 0 or verified_erc != (70, 70):
            if not allow_unsupported_erc:
                raise ReallocatorError("Failed to apply and verify SATA SCT ERC 70,70.")
            logging.warning(
                "SCT ERC 70,70 could not be verified (rc=%d, readback=%r); "
                "proceeding due to --allow-unsupported-erc (ERC remains marked attempted for cleanup).",
                res.returncode,
                verified_erc,
            )
        else:
            active_ledger.erc_applied = True

    return (active_ledger.timeout_attempted, active_ledger.erc_attempted)


def verify_cleanup_target_identity(drive: SataDriveInfo) -> Optional[str]:
    if not os.path.exists(drive.persistent_path):
        return f"Cleanup aborted: device path {drive.persistent_path} does not exist"

    try:
        cur_maj, cur_min = get_block_rdev(drive.persistent_path)
    except BaseException as e:
        return f"Cleanup aborted: block-device stat check failed on {drive.persistent_path}: {type(e).__name__}: {e}"

    if (cur_maj, cur_min) != (drive.dev_major, drive.dev_minor):
        return (
            f"Cleanup aborted: device node major:minor changed from "
            f"{drive.dev_major}:{drive.dev_minor} to {cur_maj}:{cur_min}"
        )

    try:
        id_res = run_cmd(["smartctl", "-j", "-i", drive.persistent_path])
    except BaseException as e:
        return f"Cleanup aborted: identity verification command failed: {type(e).__name__}: {e}"

    if id_res.returncode != 0:
        return f"Cleanup aborted: smartctl identity check failed (rc={id_res.returncode})"

    try:
        info = json.loads(id_res.stdout)
    except BaseException as e:
        return f"Cleanup aborted: invalid JSON from smartctl identity check: {type(e).__name__}: {e}"

    if info.get("serial_number") != drive.serial or info.get("model_name") != drive.model:
        return (
            f"Cleanup aborted: drive identity changed to "
            f"{info.get('model_name')}/{info.get('serial_number')} "
            f"(expected {drive.model}/{drive.serial})"
        )

    return None


def restore_recovery_settings(
    drive: SataDriveInfo,
    timeout_mutated: bool = False,
    erc_mutated: bool = False,
    ledger: Optional[MutationLedger] = None,
    journal: Optional[WriteAheadJournal] = None,
    timeout_target_run_ids: Optional[List[str]] = None,
    erc_target_run_ids: Optional[List[str]] = None,
) -> List[str]:
    errors: List[str] = []
    active_ledger = ledger if ledger is not None else get_or_bind_mutation_ledger(drive, None)
    need_timeout = bool(timeout_mutated or active_ledger.timeout_attempted)
    need_erc = bool(erc_mutated or active_ledger.erc_attempted)

    if not need_timeout and not need_erc:
        return errors

    id_err = verify_cleanup_target_identity(drive)
    if id_err is not None:
        errors.append(id_err)
        return errors

    if need_timeout and drive.orig_kernel_timeout is not None:
        try:
            timeout_file = pathlib.Path(f"/sys/class/block/{drive.sysfs_name}/device/timeout")
            if timeout_file.exists():
                timeout_file.write_text(f"{drive.orig_kernel_timeout}\n", encoding="utf-8")
                restored_t = timeout_file.read_text(encoding="utf-8").strip()
                if restored_t != drive.orig_kernel_timeout:
                    errors.append(
                        f"Kernel timeout mismatch after restore: expected {drive.orig_kernel_timeout}, got {restored_t}"
                    )
                elif journal is not None:
                    run_targets = timeout_target_run_ids or [journal.run_id]
                    for t_run_id in run_targets:
                        journal.append(
                            "SETTING_RESTORED",
                            {
                                "target_run_id": t_run_id,
                                "setting": "kernel_timeout",
                                "restored_value": restored_t,
                            },
                        )
            else:
                errors.append(f"Kernel timeout sysfs path missing for {drive.sysfs_name}")
        except BaseException as e:
            errors.append(f"Failed to restore kernel timeout to {drive.orig_kernel_timeout}: {type(e).__name__}: {e}")

    if need_erc and drive.orig_sct_erc is not None:
        orig_r, orig_w = drive.orig_sct_erc
        try:
            res = run_cmd(["smartctl", "-l", f"scterc,{orig_r},{orig_w}", drive.persistent_path])
            actual_erc = query_current_sct_erc(drive.persistent_path)
            if res.returncode != 0 or actual_erc != (orig_r, orig_w):
                errors.append(
                    f"Failed to restore SATA SCT ERC to ({orig_r}, {orig_w}); "
                    f"observed {actual_erc} (rc={res.returncode})"
                )
            elif journal is not None:
                run_targets = erc_target_run_ids or [journal.run_id]
                for t_run_id in run_targets:
                    journal.append(
                        "SETTING_RESTORED",
                        {
                            "target_run_id": t_run_id,
                            "setting": "sct_erc",
                            "restored_value": [orig_r, orig_w],
                        },
                    )
        except BaseException as e:
            errors.append(
                f"Exception restoring SATA SCT ERC to ({orig_r}, {orig_w}): {type(e).__name__}: {e}"
            )

    return errors


def verify_live_restored_settings(
    drive: SataDriveInfo,
    expected_timeout: Optional[str],
    expected_erc: Optional[Tuple[int, int]],
) -> List[str]:
    errors: List[str] = []
    if expected_timeout is None and expected_erc is None:
        return errors

    id_err = verify_cleanup_target_identity(drive)
    if id_err is not None:
        errors.append(id_err)
        return errors

    if expected_timeout is not None:
        try:
            timeout_file = pathlib.Path(f"/sys/class/block/{drive.sysfs_name}/device/timeout")
            if not timeout_file.exists():
                errors.append(f"Kernel timeout sysfs path missing for {drive.sysfs_name}")
            else:
                live_t = timeout_file.read_text(encoding="utf-8").strip()
                if live_t != expected_timeout:
                    errors.append(
                        f"Live kernel timeout mismatch during recovery verification: "
                        f"expected {expected_timeout}, got {live_t}"
                    )
        except BaseException as e:
            errors.append(
                f"Failed to verify live kernel timeout {expected_timeout}: {type(e).__name__}: {e}"
            )

    if expected_erc is not None:
        orig_r, orig_w = expected_erc
        try:
            live_erc = query_current_sct_erc(drive.persistent_path)
            if live_erc != (orig_r, orig_w):
                errors.append(
                    f"Live SATA SCT ERC mismatch during recovery verification: "
                    f"expected ({orig_r}, {orig_w}), observed {live_erc}"
                )
        except BaseException as e:
            errors.append(
                f"Exception verifying live SATA SCT ERC ({orig_r}, {orig_w}): {type(e).__name__}: {e}"
            )

    return errors


def _classify_smart_log_section(
    section: Any,
    count_keys: Tuple[str, ...] = ("count", "error_count", "error_count_total"),
) -> Tuple[str, List[Dict[str, Any]]]:
    if section is None:
        return ("UNAVAILABLE", [])
    if not isinstance(section, dict):
        return ("MALFORMED_OR_FAILED", [])

    if "table" in section:
        tbl = section["table"]
        if not isinstance(tbl, list):
            return ("MALFORMED_OR_FAILED", [])
        if len(tbl) == 0:
            return ("VALID_EMPTY", [])
        if not all(isinstance(item, dict) for item in tbl):
            return ("MALFORMED_OR_FAILED", [])
        return ("POPULATED", tbl)

    for ck in count_keys:
        if ck in section:
            val = section[ck]
            if isinstance(val, int) and val == 0:
                return ("VALID_EMPTY", [])
            return ("MALFORMED_OR_FAILED", [])

    return ("MALFORMED_OR_FAILED", [])


def extract_ata_smart_candidate_lbas(
    drive: SataDriveInfo,
    limit_bytes: int,
    smart_only_mode: bool = False,
    allow_partial_smart: bool = False,
) -> Tuple[Set[int], List[str]]:
    max_lba = limit_bytes // drive.logical_bs
    res = run_cmd(["smartctl", "-j", "-x", drive.persistent_path])
    if (res.returncode & 0b011) != 0:
        raise ReallocatorError(f"smartctl -x failed to access device (rc={res.returncode}).")
    if (res.returncode & 0b100) != 0 and (smart_only_mode or not allow_partial_smart):
        raise ReallocatorError(
            f"smartctl -x reported bit 2 command/checksum failure (rc={res.returncode}); "
            "failing closed on incomplete SMART log read."
        )

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise ReallocatorError(f"smartctl -x returned invalid JSON: {e}") from e

    candidates: Set[int] = set()
    source_summaries: List[str] = []
    usable_families = 0

    p_state, p_tbl = _classify_smart_log_section(data.get("ata_pending_defects_log"))
    if p_state == "MALFORMED_OR_FAILED":
        raise ReallocatorError("Malformed ata_pending_defects_log in smartctl JSON.")
    if p_state in ("POPULATED", "VALID_EMPTY"):
        usable_families += 1
        source_summaries.append(f"ata_pending_defects_log:{p_state}")
        for entry in p_tbl:
            lba = entry.get("lba")
            if not isinstance(lba, int) or lba < 0:
                raise ReallocatorError(f"Invalid LBA {lba!r} in ata_pending_defects_log.")
            if lba < max_lba:
                candidates.add(lba)

    st_root = data.get("ata_smart_self_test_log")
    if st_root is not None and not isinstance(st_root, dict):
        raise ReallocatorError("Malformed ata_smart_self_test_log container in smartctl JSON.")
    st_usable = False
    if isinstance(st_root, dict):
        for subkey in ("extended", "standard"):
            s_state, s_tbl = _classify_smart_log_section(st_root.get(subkey))
            if s_state == "MALFORMED_OR_FAILED":
                raise ReallocatorError(f"Malformed ata_smart_self_test_log.{subkey} in smartctl JSON.")
            if s_state in ("POPULATED", "VALID_EMPTY"):
                st_usable = True
                source_summaries.append(f"ata_smart_self_test_log.{subkey}:{s_state}")
                for entry in s_tbl:
                    status = entry.get("status", {})
                    if not isinstance(status, dict):
                        raise ReallocatorError("Malformed status in self-test log entry.")
                    if status.get("passed") is False:
                        lba = entry.get("lba")
                        if isinstance(lba, int) and 0 <= lba < max_lba:
                            candidates.add(lba)
    if st_usable:
        usable_families += 1

    err_root = data.get("ata_smart_error_log")
    if err_root is not None and not isinstance(err_root, dict):
        raise ReallocatorError("Malformed ata_smart_error_log container in smartctl JSON.")
    err_usable = False
    if isinstance(err_root, dict):
        for subkey in ("extended", "summary"):
            e_state, e_tbl = _classify_smart_log_section(err_root.get(subkey))
            if e_state == "MALFORMED_OR_FAILED":
                raise ReallocatorError(f"Malformed ata_smart_error_log.{subkey} in smartctl JSON.")
            if e_state in ("POPULATED", "VALID_EMPTY"):
                err_usable = True
                source_summaries.append(f"ata_smart_error_log.{subkey}:{e_state}")
                for entry in e_tbl:
                    comp = entry.get("completion_registers", {})
                    lba = comp.get("lba") if isinstance(comp, dict) else None
                    if lba is None:
                        lba = entry.get("lba")
                    err_desc = str(entry.get("error_description", "")).upper()
                    if "ABRT" in err_desc and "UNC" not in err_desc:
                        continue
                    if isinstance(lba, int) and 0 <= lba < max_lba:
                        candidates.add(lba)
    if err_usable:
        usable_families += 1

    if usable_families == 0:
        raise ReallocatorError(
            f"smartctl -x returned no usable or valid-empty ATA defect logs (rc={res.returncode})."
        )
    if smart_only_mode and not (st_usable and err_usable):
        raise ReallocatorError(
            f"SMART-only mode requires both self-test and error logs to be readable; got {source_summaries}."
        )

    return candidates, source_summaries


def parse_strict_ddrescue_mapfile(
    mapfile: pathlib.Path,
    physical_bs: int,
    limit_bytes: int,
    allow_incomplete_map: bool = False,
    dir_fd: Optional[int] = None,
    forbidden_inodes: Optional[Set[Tuple[int, int]]] = None,
) -> Set[int]:
    if limit_bytes <= 0 or (limit_bytes % physical_bs) != 0:
        raise MapfileValidationError(f"Invalid limit_bytes ({limit_bytes}) for physical_bs ({physical_bs}).")

    local_dir_fd: Optional[int] = None
    if dir_fd is None:
        local_dir_fd = _open_parent_dir_fd(mapfile)
        active_dir_fd = local_dir_fd
    else:
        active_dir_fd = _validate_required_dir_fd(dir_fd)

    try:
        if not mapfile.exists():
            raise MapfileValidationError(f"ddrescue mapfile {mapfile} does not exist.")
        fd, _ = open_safe_regular_file(mapfile, os.O_RDONLY, forbidden_inodes, dir_fd=active_dir_fd)
        try:
            content = read_all_bytes_from_fd(fd).decode("utf-8", errors="strict")
        finally:
            os.close(fd)
    finally:
        if local_dir_fd is not None:
            os.close(local_dir_fd)

    bad_offsets: Set[int] = set()
    incomplete_bytes = 0
    header_seen = False
    extent_count = 0
    expected_pos = 0
    valid_statuses = {"?", "*", "/", "-", "+", "F"}

    for line_num, raw_line in enumerate(content.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not header_seen:
            if len(parts) < 2:
                raise MapfileValidationError(f"Malformed mapfile status header at line {line_num}: {raw_line!r}")
            try:
                hdr_pos = int(parts[0], 0)
            except ValueError as e:
                raise MapfileValidationError(f"Invalid header position at line {line_num}: {raw_line!r}") from e
            if hdr_pos < 0 or parts[1] not in valid_statuses:
                raise MapfileValidationError(f"Invalid header status at line {line_num}: {raw_line!r}")
            header_seen = True
            continue

        if len(parts) < 3:
            raise MapfileValidationError(f"Malformed extent line at {line_num}: {raw_line!r}")
        try:
            pos = int(parts[0], 0)
            length = int(parts[1], 0)
        except ValueError as e:
            raise MapfileValidationError(f"Non-integer extent at line {line_num}: {raw_line!r}") from e
        status = parts[2]

        if pos < 0 or length <= 0:
            raise MapfileValidationError(f"Negative or zero-length extent at line {line_num}: {raw_line!r}")
        if status not in ("?", "*", "/", "-", "+"):
            raise MapfileValidationError(f"Invalid extent status {status!r} at line {line_num}.")

        if pos != expected_pos:
            if pos > expected_pos:
                raise MapfileValidationError(
                    f"Gap in mapfile coverage at line {line_num}: expected 0x{expected_pos:x}, got 0x{pos:x}."
                )
            raise MapfileValidationError(
                f"Overlapping or unsorted extent at line {line_num}: got 0x{pos:x} < 0x{expected_pos:x}."
            )

        expected_pos = pos + length
        extent_count += 1

        if pos >= limit_bytes:
            continue
        clamped_end = min(pos + length, limit_bytes)

        if status in ("?", "*", "/"):
            incomplete_bytes += (clamped_end - pos)
        elif status == "-":
            if (pos % physical_bs) != 0 or ((clamped_end - pos) % physical_bs) != 0:
                raise MapfileValidationError(
                    f"Unaligned bad-sector ('-') extent at line {line_num}: "
                    f"pos=0x{pos:x} clamped_end=0x{clamped_end:x} physical_bs={physical_bs}."
                )
            for off in range(pos, clamped_end, physical_bs):
                bad_offsets.add(off)

    if not header_seen or extent_count == 0:
        raise MapfileValidationError(f"Mapfile {mapfile} is empty or contains only a header with no extents.")

    if expected_pos < limit_bytes:
        raise MapfileValidationError(
            f"Mapfile coverage ends at 0x{expected_pos:x} ({expected_pos}B), "
            f"which is less than required limit_bytes 0x{limit_bytes:x} ({limit_bytes}B)."
        )

    if incomplete_bytes > 0 and not allow_incomplete_map:
        raise MapfileValidationError(
            f"Mapfile {mapfile} contains {incomplete_bytes}B of incomplete ('?', '*', '/') regions."
        )

    return bad_offsets


def validate_mapfile_metadata(
    drive: SataDriveInfo,
    mapfile: pathlib.Path,
    meta_path: pathlib.Path,
    limit_bytes: int,
    allow_in_progress: bool = False,
    dir_fd: Optional[int] = None,
    forbidden_inodes: Optional[Set[Tuple[int, int]]] = None,
) -> Dict[str, Any]:
    map_exists = mapfile.exists()
    meta_exists = meta_path.exists()
    if not map_exists and not meta_exists:
        return {}
    if map_exists and not meta_exists:
        raise MapfileValidationError(
            f"Mapfile {mapfile} exists without bound metadata file {meta_path}."
        )

    local_dir_fd: Optional[int] = None
    if dir_fd is None:
        local_dir_fd = _open_parent_dir_fd(meta_path)
        active_dir_fd = local_dir_fd
    else:
        active_dir_fd = _validate_required_dir_fd(dir_fd)

    try:
        meta_fd, meta_ino = open_safe_regular_file(
            meta_path, os.O_RDONLY, forbidden_inodes, dir_fd=active_dir_fd
        )
        try:
            raw_meta = read_all_bytes_from_fd(meta_fd).decode("utf-8", errors="strict")
        finally:
            os.close(meta_fd)

        try:
            actual = json.loads(raw_meta)
        except json.JSONDecodeError as e:
            raise MapfileValidationError(f"Invalid JSON in {meta_path}: {e}") from e

        core_expected = {
            "serial": drive.serial,
            "model": drive.model,
            "size_bytes": drive.size_bytes,
            "logical_bs": drive.logical_bs,
            "physical_bs": drive.physical_bs,
            "scan_limit_bytes": limit_bytes,
        }
        for key, exp_val in core_expected.items():
            if actual.get(key) != exp_val:
                raise MapfileValidationError(
                    f"Mapfile metadata mismatch on '{key}': recorded={actual.get(key)!r}, expected={exp_val!r}."
                )

        scan_state = actual.get("scan_state", "COMPLETED")
        if not map_exists:
            if (
                scan_state in ("PRE_SCAN_BOUND", "IN_PROGRESS_STAGE_2A")
                and actual.get("mapfile_sha256") is None
                and actual.get("stage_2a_mapfile_sha256") is None
            ):
                if not allow_in_progress:
                    raise MapfileValidationError(
                        f"Pre-checkpoint scan metadata {meta_path} exists without mapfile {mapfile} "
                        f"(scan_state={scan_state!r}). Pass --resume-interrupted-scan to restart discovery."
                    )
                return actual
            raise MapfileValidationError(
                f"Committed mapfile {mapfile} is missing for metadata {meta_path} (scan_state={scan_state!r})."
            )

        check_inodes = set(forbidden_inodes or set()) | {meta_ino}
        current_sha256, _ = compute_file_sha256(mapfile, dir_fd=active_dir_fd, forbidden_inodes=check_inodes)

        if scan_state == "COMPLETED":
            if actual.get("mapfile_sha256") != current_sha256:
                raise MapfileValidationError(
                    f"Mapfile metadata mismatch on 'mapfile_sha256': recorded={actual.get('mapfile_sha256')!r}, "
                    f"expected={current_sha256!r}."
                )
        elif scan_state in ("PRE_SCAN_BOUND", "IN_PROGRESS_STAGE_2A", "IN_PROGRESS_STAGE_2B"):
            if not allow_in_progress:
                raise MapfileValidationError(
                    f"Mapfile {mapfile} is from an interrupted scan (scan_state={scan_state!r}). "
                    "Pass --resume-interrupted-scan to validate and resume discovery."
                )
            parse_strict_ddrescue_mapfile(
                mapfile,
                drive.physical_bs,
                limit_bytes,
                allow_incomplete_map=True,
                dir_fd=active_dir_fd,
                forbidden_inodes=check_inodes,
            )
        else:
            raise MapfileValidationError(f"Unknown scan_state {scan_state!r} in {meta_path}.")

        return actual
    finally:
        if local_dir_fd is not None:
            os.close(local_dir_fd)


def verify_dev_null_sink() -> str:
    st = os.lstat(DDRESCUE_DEV_NULL)
    if not stat.S_ISCHR(st.st_mode) or (os.major(st.st_rdev), os.minor(st.st_rdev)) != (1, 3):
        raise ReallocatorError(f"{DDRESCUE_DEV_NULL} is not the standard (1, 3) null character device.")
    return DDRESCUE_DEV_NULL


def run_ddrescue_and_parse(
    drive: SataDriveInfo,
    mapfile: pathlib.Path,
    meta_path: pathlib.Path,
    dir_fd: int,
    forbidden_inodes: Set[Tuple[int, int]],
    limit_bytes: int,
    allow_resume_interrupted: bool = False,
) -> Set[int]:
    _validate_required_dir_fd(dir_fd)
    prev_umask = os.umask(RESTRICTIVE_UMASK)
    try:
        if mapfile.exists() or meta_path.exists():
            validate_mapfile_metadata(
                drive,
                mapfile,
                meta_path,
                limit_bytes,
                allow_in_progress=allow_resume_interrupted,
                dir_fd=dir_fd,
                forbidden_inodes=forbidden_inodes,
            )

        null_sink = verify_dev_null_sink()
        ver_res = run_cmd(["ddrescue", "--version"])
        if ver_res.returncode != 0 or "GNU ddrescue" not in ver_res.stdout:
            raise ReallocatorError("Could not qualify GNU ddrescue binary (--version check failed).")

        base_meta = {
            "serial": drive.serial,
            "model": drive.model,
            "size_bytes": drive.size_bytes,
            "logical_bs": drive.logical_bs,
            "physical_bs": drive.physical_bs,
            "scan_limit_bytes": limit_bytes,
        }

        initial_state = "IN_PROGRESS_STAGE_2A" if mapfile.exists() else "PRE_SCAN_BOUND"
        atomic_write_safe_json(
            meta_path,
            {
                **base_meta,
                "scan_state": initial_state,
                "mapfile_sha256": None,
                "updated_at": utc_now_iso(),
            },
            dir_fd,
            forbidden_inodes,
        )

        common_args = [
            "ddrescue",
            "--force",
            "-d",
            "-b", str(drive.physical_bs),
            "-s", str(limit_bytes),
            drive.persistent_path,
            null_sink,
            str(mapfile),
        ]

        cmd_a = [*common_args[:3], "-n", *common_args[3:]]
        logging.info("Running ddrescue Stage 2A (fast sweep): %s", " ".join(cmd_a))
        res_a = run_cmd(cmd_a, timeout=172800)
        if res_a.returncode != 0:
            raise ReallocatorError(f"ddrescue Stage 2A failed (rc={res_a.returncode}): {res_a.stderr.strip()}")

        sha_after_2a, _ = compute_file_sha256(mapfile, dir_fd=dir_fd, forbidden_inodes=forbidden_inodes)
        atomic_write_safe_json(
            meta_path,
            {
                **base_meta,
                "scan_state": "IN_PROGRESS_STAGE_2B",
                "stage_2a_mapfile_sha256": sha_after_2a,
                "mapfile_sha256": None,
                "updated_at": utc_now_iso(),
            },
            dir_fd,
            forbidden_inodes,
        )

        cmd_b = [*common_args[:3], "-r", "1", *common_args[3:]]
        logging.info("Running ddrescue Stage 2B (trim/scrape): %s", " ".join(cmd_b))
        res_b = run_cmd(cmd_b, timeout=172800)
        if res_b.returncode != 0:
            raise ReallocatorError(f"ddrescue Stage 2B failed (rc={res_b.returncode}): {res_b.stderr.strip()}")

        bad_offsets = parse_strict_ddrescue_mapfile(
            mapfile,
            drive.physical_bs,
            limit_bytes,
            allow_incomplete_map=False,
            dir_fd=dir_fd,
            forbidden_inodes=forbidden_inodes,
        )
        final_sha, map_ino = compute_file_sha256(mapfile, dir_fd=dir_fd, forbidden_inodes=forbidden_inodes)
        meta_ino = atomic_write_safe_json(
            meta_path,
            {
                **base_meta,
                "scan_state": "COMPLETED",
                "mapfile_sha256": final_sha,
                "updated_at": utc_now_iso(),
            },
            dir_fd,
            forbidden_inodes | {map_ino},
        )
        forbidden_inodes.add(map_ino)
        forbidden_inodes.add(meta_ino)
        return bad_offsets
    finally:
        os.umask(prev_umask)


def _extract_verify_lba_tokens(combined: str) -> Tuple[Optional[int], Optional[str]]:
    if re.search(r"\bvalid\s*=\s*0\b", combined, re.IGNORECASE):
        return (None, "Sense Information field has valid=0 (address not valid)")

    parsed_values: Set[int] = set()
    patterns = [
        r"reported\s+lba\s*[=:]\s*([^\s,;\]\)]+)",
        r"Info(?:rmation)?\s*(?:fld|field)\s*[=:]\s*([^\s,;\]\)]+)",
        r"Information\s*[=:]\s*([^\s,;\]\)]+)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, combined, re.IGNORECASE):
            token = m.group(1).strip()
            try:
                if token.lower().startswith("0x"):
                    val = int(token, 16)
                elif re.search(r"[a-fA-F]", token):
                    val = int(token, 16)
                else:
                    val = int(token, 10)
            except ValueError:
                return (None, f"Undecodable LBA token {token!r} in sg_verify output")
            if val < 0:
                return (None, f"Negative LBA value {val} in sg_verify output")
            parsed_values.add(val)

    if len(parsed_values) > 1:
        return (None, f"Contradictory LBA values in sg_verify output: {sorted(parsed_values)}")
    if len(parsed_values) == 1:
        return (next(iter(parsed_values)), None)
    return (None, None)


def run_ata_media_verify(drive: SataDriveInfo, start_lba: int, count: int) -> VerifyResult:
    end_lba = start_lba + count - 1
    res = run_cmd([
        "sg_verify",
        "--16",
        "--dpo",
        "--verbose",
        f"--lba={start_lba}",
        f"--count={count}",
        drive.persistent_path,
    ])
    rc = res.returncode
    combined = f"{res.stdout}\n{res.stderr}".strip()

    sense_key: Optional[str] = None
    m_sk = re.search(r"Sense Key:\s*([A-Za-z0-9 _-]+?)(?:\s*\[|$|\n|,)", combined, re.IGNORECASE)
    if m_sk:
        sense_key = m_sk.group(1).strip()

    asc_ascq: Optional[str] = None
    m_asc = re.search(r"Additional sense:\s*([^\n\r]+)", combined, re.IGNORECASE)
    if m_asc:
        asc_ascq = m_asc.group(1).strip()

    info_lba, lba_err = _extract_verify_lba_tokens(combined)

    if rc == SG_LIB_OK:
        if lba_err is not None:
            return VerifyResult("INDETERMINATE", rc, sense_key, asc_ascq, None, lba_err)
        return VerifyResult("OK", rc, sense_key, asc_ascq, info_lba, "VERIFY(16) succeeded")

    if rc in (SG_LIB_CAT_MEDIUM_HARD, SG_LIB_CAT_MEDIUM_HARD_WITH_INFO):
        if lba_err is not None:
            return VerifyResult("INDETERMINATE", rc, sense_key, asc_ascq, None, f"{lba_err} (rc={rc})")

        if sense_key is not None and sense_key.lower() == "hardware error":
            return VerifyResult(
                "INDETERMINATE", rc, sense_key, asc_ascq, info_lba,
                f"Hardware Error sense key reported by sg_verify (rc={rc})"
            )

        if rc == SG_LIB_CAT_MEDIUM_HARD_WITH_INFO and info_lba is None:
            return VerifyResult(
                "INDETERMINATE", rc, sense_key, asc_ascq, None,
                "sg_verify returned rc=18 (medium/hardware error with info) without a decodable failing LBA"
            )

        if info_lba is not None and not (start_lba <= info_lba <= end_lba):
            return VerifyResult(
                "INDETERMINATE", rc, sense_key, asc_ascq, info_lba,
                f"Out-of-range failing LBA {info_lba} for target {start_lba}..{end_lba} (rc={rc})"
            )

        if sense_key is not None and sense_key.lower() != "medium error":
            return VerifyResult(
                "INDETERMINATE", rc, sense_key, asc_ascq, info_lba,
                f"Unexpected sense key {sense_key!r} for rc={rc}"
            )

        return VerifyResult(
            "DEFECT", rc, sense_key, asc_ascq, info_lba,
            f"Media defect from sg_verify (rc={rc}, sense_key={sense_key!r}, info_lba={info_lba})"
        )

    if rc == SG_LIB_CAT_INVALID_OP:
        if (
            sense_key is not None
            and sense_key.lower() == "illegal request"
            and asc_ascq is not None
            and "invalid command operation code" in asc_ascq.lower()
        ):
            return VerifyResult(
                "UNSUPPORTED", rc, sense_key, asc_ascq, info_lba,
                "VERIFY(16) opcode unsupported by device/SATL (rc=9, Invalid command operation code)"
            )
        return VerifyResult(
            "INDETERMINATE", rc, sense_key, asc_ascq, info_lba,
            f"Unqualified invalid-op exit status (rc=9, sense_key={sense_key!r}, asc={asc_ascq!r})"
        )

    label_map = {
        SG_LIB_SYNTAX_ERROR: "SYNTAX_ERROR",
        SG_LIB_CAT_NOT_READY: "DEVICE_NOT_READY",
        SG_LIB_CAT_ILLEGAL_REQ: "ILLEGAL_REQUEST",
        SG_LIB_CAT_UNIT_ATTENTION: "UNIT_ATTENTION",
        SG_LIB_CAT_DATA_PROTECT: "DATA_PROTECT",
        SG_LIB_CAT_COPY_ABORTED: "COPY_ABORTED",
        SG_LIB_CAT_ABORTED_COMMAND: "ABORTED_COMMAND",
        SG_LIB_CAT_MISCOMPARE: "MISCOMPARE",
        SG_LIB_CAT_TIMEOUT: "COMMAND_TIMEOUT",
    }
    label = label_map.get(rc, "UNKNOWN_ERROR")
    return VerifyResult(
        "INDETERMINATE", rc, sense_key, asc_ascq, info_lba,
        f"Indeterminate sg_verify error {label} (rc={rc}, sense_key={sense_key!r}, asc={asc_ascq!r})"
    )


def confirm_live_defect(
    fd: int,
    drive: SataDriveInfo,
    offset_bytes: int,
    read_buf: mmap.mmap,
) -> Tuple[str, str]:
    start_lba = offset_bytes // drive.logical_bs
    logical_count = drive.physical_bs // drive.logical_bs

    pread_eio = False
    pread_detail = "ok"
    try:
        n = os.preadv(fd, [read_buf], offset_bytes)
        if n != drive.physical_bs:
            return ("INDETERMINATE", f"short_read({n}_of_{drive.physical_bs})")
    except OSError as e:
        if e.errno != errno.EIO:
            return ("INDETERMINATE", f"preadv_non_eio_errno({e.errno}:{e.strerror})")
        pread_eio = True
        pread_detail = f"preadv_EIO({e.strerror})"

    v_res = run_ata_media_verify(drive, start_lba, logical_count)

    if not pread_eio and v_res.category == "OK":
        return ("READABLE", "preadv_ok_and_verify_ok")

    if v_res.category == "INDETERMINATE":
        return ("INDETERMINATE", f"{pread_detail}_and_{v_res.detail}")

    if v_res.category == "DEFECT":
        has_medium_sense = v_res.sense_key is not None and v_res.sense_key.lower() == "medium error"
        if has_medium_sense:
            return (
                "CONFIRMED_DEFECT",
                f"{pread_detail};sg_verify_rc={v_res.return_code};sense={v_res.sense_key};info_lba={v_res.info_lba}",
            )
        return (
            "INDETERMINATE",
            f"uncorroborated_defect_without_medium_error_sense({pread_detail};{v_res.detail})",
        )

    if pread_eio:
        return ("INDETERMINATE", f"{pread_detail}_uncorroborated_by_verify({v_res.category}:rc={v_res.return_code})")

    if v_res.category == "UNSUPPORTED":
        return ("READABLE", f"preadv_ok_verify_unsupported(rc={v_res.return_code})")

    return ("INDETERMINATE", f"unhandled_confirmation_state({pread_detail};{v_res.detail})")


def flush_drive_caches(fd: int, drive: SataDriveInfo) -> Tuple[bool, str]:
    try:
        os.fsync(fd)
    except OSError as e:
        return (False, f"fsync_failed({e})")

    r1 = run_cmd(["blockdev", "--flushbufs", drive.persistent_path])
    r2 = run_cmd(["hdparm", "-F", drive.persistent_path])
    if r1.returncode != 0 or r2.returncode != 0:
        return (False, f"flush_cmd_failed(blockdev_rc={r1.returncode},hdparm_rc={r2.returncode})")
    return (True, "ok")


def _execute_verified_post_write_sequence(
    fd: int,
    drive: SataDriveInfo,
    offset_bytes: int,
    start_lba: int,
    logical_count: int,
    read_buf: mmap.mmap,
    expected_zeroes: bytes,
    attempt_rec: Dict[str, Any],
    allow_degraded_no_verify: bool,
) -> str:
    flush_ok, flush_detail = flush_drive_caches(fd, drive)
    attempt_rec["cache_flush_accepted"] = flush_ok
    if not flush_ok:
        attempt_rec["status"] = f"FLUSH_FAILED_INDETERMINATE({flush_detail})"
        return "BREAK_FATAL"

    v_post = run_ata_media_verify(drive, start_lba, logical_count)
    attempt_rec["media_verify_status"] = f"{v_post.category}(rc={v_post.return_code})"
    attempt_rec["media_verify_rc"] = v_post.return_code

    if v_post.category == "INDETERMINATE":
        attempt_rec["status"] = f"INDETERMINATE_POST_WRITE_VERIFY(rc={v_post.return_code}:{v_post.detail})"
        return "BREAK_FATAL"

    if v_post.category == "DEFECT":
        attempt_rec["status"] = f"FAILED_POST_WRITE_VERIFY_DEFECT(rc={v_post.return_code})"
        return "CONTINUE_RETRY"

    if v_post.category == "UNSUPPORTED" and not allow_degraded_no_verify:
        attempt_rec["status"] = f"VERIFY_UNSUPPORTED_REJECTED(rc={v_post.return_code})"
        return "BREAK_FATAL"

    read_buf.seek(0)
    read_buf.write(b"\xff" * drive.physical_bs)
    try:
        n_read = os.preadv(fd, [read_buf], offset_bytes)
    except OSError as e:
        if e.errno == errno.EIO:
            attempt_rec["status"] = f"FAILED_POST_WRITE_READBACK_EIO({e.strerror})"
            return "CONTINUE_RETRY"
        attempt_rec["status"] = f"INDETERMINATE_POST_WRITE_READBACK_ERROR(errno={e.errno})"
        return "BREAK_FATAL"

    if n_read != drive.physical_bs:
        attempt_rec["status"] = f"INDETERMINATE_POST_WRITE_SHORT_READ({n_read})"
        return "BREAK_FATAL"

    read_buf.seek(0)
    if read_buf.read(drive.physical_bs) != expected_zeroes:
        attempt_rec["status"] = "FAILED_POST_WRITE_CONTENT_MISMATCH"
        return "BREAK_FATAL"

    attempt_rec["logical_readback_matched_zeroes"] = True
    if v_post.category == "OK":
        attempt_rec["status"] = "VERIFIED_LOGICAL_READBACK_AND_MEDIA_VERIFY"
    else:
        attempt_rec["status"] = "DEGRADED_LOGICAL_READBACK_ONLY_NO_MEDIA_VERIFY"
    return "DONE_SUCCESS"


def overwrite_and_verify_physical_sector(
    fd: int,
    drive: SataDriveInfo,
    offset_bytes: int,
    limit_bytes: int,
    zero_buf: mmap.mmap,
    read_buf: mmap.mmap,
    journal: Optional[Any] = None,
    max_retries: int = 2,
    enable_hdparm_fallback: bool = False,
    allow_degraded_no_verify: bool = False,
) -> Dict[str, Any]:
    if offset_bytes < 0 or (offset_bytes % drive.physical_bs) != 0:
        raise ReallocatorError(f"Unaligned or negative write offset 0x{offset_bytes:x}.")
    if (offset_bytes + drive.physical_bs) > limit_bytes or (offset_bytes + drive.physical_bs) > drive.size_bytes:
        raise ReallocatorError(f"Write offset 0x{offset_bytes:x} exceeds configured write domain ({limit_bytes}B).")

    start_lba = offset_bytes // drive.logical_bs
    logical_count = drive.physical_bs // drive.logical_bs
    end_lba = start_lba + logical_count - 1
    expected_zeroes = b"\x00" * drive.physical_bs
    attempt_history: List[Dict[str, Any]] = []
    run_id_str = getattr(journal, "run_id", "RUN") if journal is not None else "NO_JOURNAL"

    for attempt in range(1, max_retries + 1):
        op_name = "pwritev_zero"
        op_id = f"{run_id_str}:{offset_bytes}:{attempt}:{op_name}"
        attempt_rec: Dict[str, Any] = {
            "attempt": attempt,
            "operation": op_name,
            "op_id": op_id,
            "offset_bytes": offset_bytes,
            "start_lba": start_lba,
            "end_lba": end_lba,
            "physical_bs": drive.physical_bs,
            "write_accepted": False,
            "hdparm_fallback_used": False,
            "cache_flush_accepted": False,
            "media_verify_status": None,
            "media_verify_rc": None,
            "logical_readback_matched_zeroes": False,
            "status": "INDETERMINATE_INCOMPLETE",
        }

        if journal is not None:
            journal.append(
                "WRITE_INTENT",
                {
                    "offset_bytes": offset_bytes,
                    "start_lba": start_lba,
                    "end_lba": end_lba,
                    "attempt": attempt,
                    "operation": op_name,
                    "op_id": op_id,
                },
            )

        pwrite_eio = False
        loop_action = "BREAK_FATAL"
        try:
            try:
                written = os.pwritev(fd, [zero_buf], offset_bytes, os.RWF_DSYNC)
                if written == drive.physical_bs:
                    attempt_rec["write_accepted"] = True
                else:
                    attempt_rec["status"] = f"PARTIAL_WRITE_INDETERMINATE({written}_of_{drive.physical_bs})"
                    loop_action = "BREAK_FATAL"
            except OSError as e:
                if e.errno != errno.EIO:
                    attempt_rec["status"] = f"WRITE_ERROR_INDETERMINATE(errno={e.errno}:{e.strerror})"
                    loop_action = "BREAK_FATAL"
                else:
                    pwrite_eio = True
                    attempt_rec["status"] = "WRITE_EIO_FAILED"
                    loop_action = "CONTINUE_RETRY"

            if attempt_rec["write_accepted"]:
                loop_action = _execute_verified_post_write_sequence(
                    fd,
                    drive,
                    offset_bytes,
                    start_lba,
                    logical_count,
                    read_buf,
                    expected_zeroes,
                    attempt_rec,
                    allow_degraded_no_verify,
                )
        finally:
            attempt_history.append(dict(attempt_rec))
            if journal is not None:
                journal.append("ATTEMPT_OUTCOME", dict(attempt_rec))

        if loop_action in ("DONE_SUCCESS", "BREAK_FATAL"):
            break

        v_recheck = run_ata_media_verify(drive, start_lba, logical_count)
        is_localized_medium_err = (
            v_recheck.category == "DEFECT"
            and v_recheck.sense_key is not None
            and v_recheck.sense_key.lower() == "medium error"
        )
        if not is_localized_medium_err:
            # v7 Low-Severity Finding Fix: Preserve the immediate attempt outcome and
            # durably append an operation-correlated RETRY_CORROBORATION_FAILED record
            # so returned status and replayed journal status match identically.
            prior_status = attempt_history[-1]["status"]
            corroborated_status = f"EIO_OR_DEFECT_UNCORROBORATED_INDETERMINATE({v_recheck.detail})"
            corroboration_payload = {
                "attempt": attempt,
                "operation": op_name,
                "op_id": op_id,
                "offset_bytes": offset_bytes,
                "start_lba": start_lba,
                "end_lba": end_lba,
                "prior_attempt_status": prior_status,
                "corroboration_verify_status": f"{v_recheck.category}(rc={v_recheck.return_code})",
                "corroboration_verify_rc": v_recheck.return_code,
                "corroboration_detail": v_recheck.detail,
                "status": corroborated_status,
            }
            attempt_history[-1]["prior_attempt_status"] = prior_status
            attempt_history[-1]["corroboration_verify_status"] = corroboration_payload["corroboration_verify_status"]
            attempt_history[-1]["corroboration_verify_rc"] = v_recheck.return_code
            attempt_history[-1]["corroboration_detail"] = v_recheck.detail
            attempt_history[-1]["status"] = corroborated_status
            if journal is not None:
                journal.append("RETRY_CORROBORATION_FAILED", corroboration_payload)
            break

        if pwrite_eio and enable_hdparm_fallback and drive.logical_bs == 512 and drive.physical_bs == 512:
            fb_op = "hdparm_repair_and_zero"
            fb_op_id = f"{run_id_str}:{offset_bytes}:{attempt}:{fb_op}"
            fb_rec: Dict[str, Any] = {
                "attempt": attempt,
                "operation": fb_op,
                "op_id": fb_op_id,
                "offset_bytes": offset_bytes,
                "start_lba": start_lba,
                "end_lba": end_lba,
                "physical_bs": drive.physical_bs,
                "write_accepted": False,
                "hdparm_fallback_used": True,
                "cache_flush_accepted": False,
                "media_verify_status": None,
                "media_verify_rc": None,
                "logical_readback_matched_zeroes": False,
                "status": "INDETERMINATE_INCOMPLETE",
            }
            if journal is not None:
                journal.append(
                    "WRITE_INTENT",
                    {
                        "offset_bytes": offset_bytes,
                        "start_lba": start_lba,
                        "end_lba": end_lba,
                        "attempt": attempt,
                        "operation": fb_op,
                        "op_id": fb_op_id,
                    },
                )
            fb_action = "BREAK_FATAL"
            try:
                res_hd = run_cmd([
                    "hdparm",
                    "--yes-i-know-what-i-am-doing",
                    "--repair-sector",
                    str(start_lba),
                    drive.persistent_path,
                ])
                if res_hd.returncode == 0:
                    try:
                        written2 = os.pwritev(fd, [zero_buf], offset_bytes, os.RWF_DSYNC)
                        if written2 == drive.physical_bs:
                            fb_rec["write_accepted"] = True
                            fb_action = _execute_verified_post_write_sequence(
                                fd,
                                drive,
                                offset_bytes,
                                start_lba,
                                logical_count,
                                read_buf,
                                expected_zeroes,
                                fb_rec,
                                allow_degraded_no_verify,
                            )
                        else:
                            fb_rec["status"] = f"PARTIAL_POST_HDPARM_WRITE_INDETERMINATE({written2})"
                    except OSError as e2:
                        fb_rec["status"] = f"POST_HDPARM_WRITE_ERROR(errno={e2.errno}:{e2.strerror})"
                else:
                    fb_rec["status"] = f"HDPARM_REPAIR_FAILED(rc={res_hd.returncode})"
            finally:
                attempt_history.append(dict(fb_rec))
                if journal is not None:
                    journal.append("ATTEMPT_OUTCOME", dict(fb_rec))

            if fb_action in ("DONE_SUCCESS", "BREAK_FATAL"):
                break

    final_attempt = attempt_history[-1]
    return {
        **final_attempt,
        "attempts_total": len(attempt_history),
        "attempt_history": attempt_history,
    }


def reconcile_interrupted_journal_state(
    drive: SataDriveInfo,
    dev_fd: int,
    journal: WriteAheadJournal,
) -> None:
    """
    Restartable Two-Phase Reconciliation Procedure.
    """
    assert_device_identity_and_capacity(drive, dev_fd)
    raw_bytes = read_all_bytes_from_fd(journal.fd)
    if not raw_bytes:
        logging.info("Journal %s is empty; nothing to reconcile.", journal.path)
        return

    replay = replay_journal_state(raw_bytes, drive, journal.path)
    t_base, t_runs, e_base, e_runs = resolve_outstanding_setting_baselines(replay)
    reverify_t_base, reverify_e_base = resolve_reverification_setting_baselines(
        replay, pending_t_base=t_base, pending_e_base=e_base
    )

    effective_t_base = t_base if t_base is not None else reverify_t_base
    effective_e_base = e_base if e_base is not None else reverify_e_base

    if replay.unresolved_restoration_events and effective_t_base is None and effective_e_base is None:
        raise ReallocatorError(
            "Cannot reconcile UNRESOLVED_RESTORATION_STATE without recorded setting mutation baselines."
        )

    journal.append("RUN_START", {"mode": "RECONCILE_INTERRUPTED_STATE", "limit_bytes": drive.size_bytes})

    if t_base is not None or e_base is not None:
        baseline_drive = dataclasses.replace(
            drive,
            orig_kernel_timeout=t_base if t_base is not None else drive.orig_kernel_timeout,
            orig_sct_erc=e_base if e_base is not None else drive.orig_sct_erc,
        )
        detached_ledger = MutationLedger(
            orig_kernel_timeout=baseline_drive.orig_kernel_timeout,
            orig_sct_erc=baseline_drive.orig_sct_erc,
        )
        restore_errs = restore_recovery_settings(
            baseline_drive,
            timeout_mutated=(t_base is not None),
            erc_mutated=(e_base is not None),
            ledger=detached_ledger,
            journal=journal,
            timeout_target_run_ids=t_runs,
            erc_target_run_ids=e_runs,
        )
        if restore_errs:
            raise ReallocatorError(f"Settings reconciliation failed: {'; '.join(restore_errs)}")

    check_live_t = reverify_t_base if t_base is None else None
    check_live_e = reverify_e_base if e_base is None else None
    if check_live_t is not None or check_live_e is not None:
        verify_errs = verify_live_restored_settings(drive, check_live_t, check_live_e)
        if verify_errs:
            raise ReallocatorError(f"Live settings verification failed: {'; '.join(verify_errs)}")

    for (r_id, seq_no) in sorted(replay.unresolved_restoration_events.keys()):
        journal.append(
            "RESTORATION_STATE_RECONCILED",
            {"target_run_id": r_id, "target_seq": seq_no},
        )

    offsets_to_inspect: Set[int] = set()
    for (_, off, _, _, _) in replay.pending_intents:
        offsets_to_inspect.add(off)
    for off in replay.unresolved_outcomes:
        offsets_to_inspect.add(off)
    for (_, off) in replay.unresolved_confirmation_stops:
        offsets_to_inspect.add(off)

    observed_by_offset: Dict[int, Tuple[str, str]] = {}
    if offsets_to_inspect:
        read_buf = mmap.mmap(-1, drive.physical_bs, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        try:
            for off in sorted(offsets_to_inspect):
                state, reason = confirm_live_defect(dev_fd, drive, off, read_buf)
                if state == "INDETERMINATE":
                    raise ReallocatorError(
                        f"Cannot reconcile offset 0x{off:x}: sector remains in INDETERMINATE state ({reason})."
                    )
                observed_by_offset[off] = (state, reason)
        finally:
            read_buf.close()

    for (t_run_id, off, attempt, op, op_id) in sorted(replay.pending_intents.keys()):
        obs_state, obs_reason = observed_by_offset[off]
        journal.append(
            "OPERATION_RECONCILED",
            {
                "target_run_id": t_run_id,
                "offset_bytes": off,
                "attempt": attempt,
                "operation": op,
                "op_id": op_id,
                "observed_state": obs_state,
                "observed_reason": obs_reason,
                "write_completion_proven": False,
                "reconciled_status": "PENDING_OPERATION_RECONCILED_BY_INSPECTION",
            },
        )

    for off, rec in sorted(replay.unresolved_outcomes.items()):
        obs_state, obs_reason = observed_by_offset[off]
        journal.append(
            "TARGET_RECONCILED",
            {
                "target_run_id": rec.get("run_id"),
                "offset_bytes": off,
                "observed_state": obs_state,
                "observed_reason": obs_reason,
                "reconciled_status": "MANUALLY_RECONCILED",
            },
        )

    for (t_run_id, off) in sorted(replay.unresolved_confirmation_stops.keys()):
        obs_state, obs_reason = observed_by_offset[off]
        journal.append(
            "CONFIRMATION_STOP_RECONCILED",
            {
                "target_run_id": t_run_id,
                "offset_bytes": off,
                "observed_state": obs_state,
                "observed_reason": obs_reason,
                "reconciled_status": "CONFIRMATION_STOP_RECONCILED_BY_INSPECTION",
            },
        )

    journal.append(
        "STATE_RECONCILED",
        {
            "reconciled_run_ids": sorted(replay.interrupted_runs.keys()),
            "restored_timeout_baseline": effective_t_base,
            "restored_sct_erc_baseline": list(effective_e_base) if effective_e_base is not None else None,
            "reconciled_offsets": sorted(offsets_to_inspect),
        },
    )


def capture_ata_smart(drive: SataDriveInfo) -> AtaSmartSnapshot:
    res = run_cmd(["smartctl", "-j", "-A", drive.persistent_path])
    snap = AtaSmartSnapshot()
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return snap

    for attr in data.get("ata_smart_attributes", {}).get("table", []):
        attr_id = attr.get("id")
        raw_val = attr.get("raw", {}).get("value")
        if attr_id == 5:
            snap.reallocated_sector_ct = raw_val
        elif attr_id == 196:
            snap.reallocated_event_ct = raw_val
        elif attr_id == 197:
            snap.current_pending_sector = raw_val
        elif attr_id == 198:
            snap.offline_uncorrectable = raw_val
    return snap


def print_ata_telemetry_report(before: AtaSmartSnapshot, after: AtaSmartSnapshot) -> None:
    print("\n" + "=" * 78)
    print("ATA SMART ATTRIBUTE SUMMARY (Aggregate counters; cannot attribute physical")
    print("relocation to an individual LBA; bounded logs do not certify entire media)")
    print("=" * 78)
    print(f"{'SMART Attribute':<38} | {'Before':<14} | {'After':<14} | {'Delta':<8}")
    print("-" * 78)
    rows = [
        ("5   Reallocated_Sector_Ct", before.reallocated_sector_ct, after.reallocated_sector_ct),
        ("196 Reallocated_Event_Count", before.reallocated_event_ct, after.reallocated_event_ct),
        ("197 Current_Pending_Sector", before.current_pending_sector, after.current_pending_sector),
        ("198 Offline_Uncorrectable", before.offline_uncorrectable, after.offline_uncorrectable),
    ]
    for name, b_val, a_val in rows:
        delta_str = f"{a_val - b_val:+d}" if (isinstance(b_val, int) and isinstance(a_val, int)) else "N/A"
        print(f"{name:<38} | {str(b_val):<14} | {str(a_val):<14} | {delta_str:<8}")
    print("=" * 78 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hardened v8 SATA HDD bad-sector discovery and physical-sector zeroing utility."
    )
    parser.add_argument("device", help="Whole-disk block node (e.g., /dev/sdX or /dev/disk/by-id/ata-...)")
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument("--dry-run", action="store_true", default=True, help="Read-only discovery mode (default).")
    mode_grp.add_argument("--execute", action="store_true", help="Write zeroes to confirmed bad physical sectors.")
    mode_grp.add_argument(
        "--reconcile-interrupted-state",
        action="store_true",
        help="Read-only/settings-restoration mode to verify and reconcile prior interrupted journal records.",
    )
    parser.add_argument("--allow-damaged-data", action="store_true", help="Proceed if blkid finds signatures.")
    parser.add_argument("--skip-surface-scan", action="store_true", help="Use only ATA SMART defect logs.")
    parser.add_argument("--resume-interrupted-scan", action="store_true", help="Resume a bound IN_PROGRESS ddrescue map.")
    parser.add_argument("--allow-partial-smart-discovery", action="store_true", help="Allow surface scan if smartctl bit 2 is set.")
    parser.add_argument("--max-scan-gb", type=int, default=None, help="Positive integer GiB scan/write limit.")
    parser.add_argument("--evidence-dir", type=pathlib.Path, default=None, help="Private 0700 directory for map/journal.")
    parser.add_argument("--allow-unsupported-erc", action="store_true", help="Allow --execute without SATA SCT ERC.")
    parser.add_argument(
        "--allow-degraded-no-verify",
        action="store_true",
        help="Allow post-write readback-only if VERIFY(16) returns opcode unsupported (rc=9) after defect confirmation.",
    )
    parser.add_argument("--enable-hdparm-repair-fallback", action="store_true", help="Enable hdparm fallback on 512n drives.")

    args = parser.parse_args()
    if getattr(args, "max_scan_gb", None) is not None and args.max_scan_gb <= 0:
        sys.exit("[FATAL] --max-scan-gb must be a positive integer greater than 0.")

    execute_mode = bool(getattr(args, "execute", False))
    reconcile_mode = bool(getattr(args, "reconcile_interrupted_state", False))
    resume_scan_mode = bool(getattr(args, "resume_interrupted_scan", False))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if os.geteuid() != 0:
        sys.exit("[FATAL] Root privileges are required.")

    os.umask(RESTRICTIVE_UMASK)

    dev_fd: Optional[int] = None
    dir_fd: Optional[int] = None
    journal: Optional[WriteAheadJournal] = None

    try:
        check_dependencies()
        real_path, persistent_path, sysfs_name, dev_maj, dev_min, dev_inode = resolve_and_validate_device(args.device)
        dev_fd = acquire_exclusive_device_and_verify_blank(
            real_path, persistent_path, sysfs_name, execute_mode, bool(getattr(args, "allow_damaged_data", False))
        )

        drive = inspect_sata_drive(args.device, real_path, persistent_path, sysfs_name, dev_maj, dev_min)
        max_scan_gb = getattr(args, "max_scan_gb", None)
        limit_bytes = (
            min(drive.size_bytes, max_scan_gb * (1024 ** 3))
            if max_scan_gb is not None
            else drive.size_bytes
        )
        limit_bytes = (limit_bytes // drive.physical_bs) * drive.physical_bs
        if limit_bytes <= 0:
            raise ReallocatorError("Computed scan/write limit_bytes is 0.")

        evidence_dir = getattr(args, "evidence_dir", None) or pathlib.Path(f"./sata_evidence_{drive.sanitized_serial}")
        dir_fd = prepare_private_evidence_dir(evidence_dir)
        forbidden_inodes: Set[Tuple[int, int]] = {dev_inode}

        mapfile = evidence_dir / f"ddrescue_{drive.sanitized_serial}.map"
        meta_path = evidence_dir / f"ddrescue_{drive.sanitized_serial}.map.meta.json"
        journal_path = evidence_dir / f"journal_{drive.sanitized_serial}.jsonl"

        run_id = str(uuid.uuid4())
        journal = WriteAheadJournal(journal_path, dir_fd, drive, run_id, forbidden_inodes)
        forbidden_inodes.add(journal.dev_ino)

    except ReallocatorError as e:
        if dev_fd is not None:
            os.close(dev_fd)
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass
        sys.exit(f"[FATAL] {e}")

    mutation_ledger = get_or_bind_mutation_ledger(drive, MutationLedger(
        orig_kernel_timeout=drive.orig_kernel_timeout,
        orig_sct_erc=drive.orig_sct_erc,
    ))
    timeout_mutated = False
    erc_mutated = False
    exit_code = 0
    caught_exc: Optional[BaseException] = None

    try:
        if reconcile_mode:
            reconcile_interrupted_journal_state(drive, dev_fd, journal)
        else:
            journal.reconcile_prior_records(allow_discovery_resume=resume_scan_mode)
            journal.append("RUN_START", {"mode": "EXECUTE" if execute_mode else "DRY_RUN", "limit_bytes": limit_bytes})
            smart_before = capture_ata_smart(drive)

            timeout_mutated, erc_mutated = apply_recovery_settings(
                drive, execute_mode, bool(getattr(args, "allow_unsupported_erc", False)), mutation_ledger, journal
            )

            smart_lbas, smart_sources = extract_ata_smart_candidate_lbas(
                drive,
                limit_bytes,
                smart_only_mode=bool(getattr(args, "skip_surface_scan", False)),
                allow_partial_smart=bool(getattr(args, "allow_partial_smart_discovery", False)),
            )

            candidate_offsets: Set[int] = {
                ((lba * drive.logical_bs) // drive.physical_bs) * drive.physical_bs
                for lba in smart_lbas
            }

            if not getattr(args, "skip_surface_scan", False):
                dd_offsets = run_ddrescue_and_parse(
                    drive,
                    mapfile,
                    meta_path,
                    dir_fd,
                    forbidden_inodes,
                    limit_bytes,
                    allow_resume_interrupted=resume_scan_mode,
                )
                candidate_offsets.update(dd_offsets)

            assert_device_identity_and_capacity(drive, dev_fd)
            sorted_candidates = sorted(off for off in candidate_offsets if 0 <= off < limit_bytes)

            journal.append(
                "DISCOVERY_COMPLETED",
                {
                    "mode": "EXECUTE" if execute_mode else "DRY_RUN",
                    "smart_sources": smart_sources,
                    "surface_scan_performed": not getattr(args, "skip_surface_scan", False),
                    "candidate_count": len(sorted_candidates),
                    "limit_bytes": limit_bytes,
                },
            )

            if not sorted_candidates:
                logging.info("Zero candidate sectors discovered in configured bounded scope.")
            else:
                zero_buf = mmap.mmap(-1, drive.physical_bs, prot=mmap.PROT_READ | mmap.PROT_WRITE)
                zero_buf.write(b"\x00" * drive.physical_bs)
                read_buf = mmap.mmap(-1, drive.physical_bs, prot=mmap.PROT_READ | mmap.PROT_WRITE)

                try:
                    for idx, off in enumerate(sorted_candidates):
                        assert_device_identity_and_capacity(drive, dev_fd)
                        state, reason = confirm_live_defect(dev_fd, drive, off, read_buf)

                        if state == "READABLE":
                            journal.append("CANDIDATE_SKIPPED", {"offset_bytes": off, "reason": reason})
                            logging.info("Skipping readable candidate offset 0x%x (%s).", off, reason)
                            continue

                        if state == "INDETERMINATE":
                            journal.append("CONFIRMATION_INDETERMINATE_STOP", {"offset_bytes": off, "reason": reason})
                            for rem_off in sorted_candidates[idx + 1:]:
                                journal.append("TARGET_UNATTEMPTED", {"offset_bytes": rem_off, "reason": "stopped_on_prior_indeterminate"})
                            logging.error("Indeterminate defect confirmation at offset 0x%x (%s). Stopping.", off, reason)
                            exit_code = max(exit_code, 3)
                            break

                        if not execute_mode:
                            journal.append("DRY_RUN_CONFIRMED_DEFECT", {"offset_bytes": off, "reason": reason})
                            logging.info("[DRY-RUN] Confirmed defective %dB physical sector at 0x%x (%s).", drive.physical_bs, off, reason)
                            continue

                        outcome = overwrite_and_verify_physical_sector(
                            fd=dev_fd,
                            drive=drive,
                            offset_bytes=off,
                            limit_bytes=limit_bytes,
                            zero_buf=zero_buf,
                            read_buf=read_buf,
                            journal=journal,
                            enable_hdparm_fallback=bool(getattr(args, "enable_hdparm_repair_fallback", False)),
                            allow_degraded_no_verify=bool(getattr(args, "allow_degraded_no_verify", False)),
                        )
                        if outcome["status"] not in RESOLVED_TARGET_STATUSES:
                            for rem_off in sorted_candidates[idx + 1:]:
                                journal.append("TARGET_UNATTEMPTED", {"offset_bytes": rem_off, "reason": "stopped_on_prior_write_failure"})
                            logging.error("Target at offset 0x%x ended with status %s. Stopping.", off, outcome["status"])
                            exit_code = max(exit_code, 2)
                            break
                finally:
                    read_buf.close()
                    zero_buf.close()

            print_ata_telemetry_report(smart_before, capture_ata_smart(drive))

    except ReallocatorError as e:
        logging.error("[FATAL] %s", e)
        exit_code = max(exit_code, 1)
        try:
            journal.append("FATAL_ABORT", {"error": str(e), "exception_type": type(e).__name__})
        except BaseException:
            pass
    except BaseException as e:
        caught_exc = e
        logging.error("[UNHANDLED_EXCEPTION] %s: %s", type(e).__name__, e)
        exit_code = max(exit_code, 1)
        try:
            journal.append("UNHANDLED_EXCEPTION", {"error": str(e), "exception_type": type(e).__name__})
        except BaseException:
            pass
    finally:
        try:
            restore_errors = restore_recovery_settings(
                drive, timeout_mutated, erc_mutated, mutation_ledger, journal
            )
        except BaseException as re_exc:
            restore_errors = [f"Exception during restore_recovery_settings: {type(re_exc).__name__}: {re_exc}"]

        if restore_errors:
            for err in restore_errors:
                logging.critical("[UNRESOLVED_RESTORATION_STATE] %s", err)
            try:
                journal.append("UNRESOLVED_RESTORATION_STATE", {"errors": restore_errors})
            except BaseException:
                pass
            exit_code = max(exit_code, 4)

        try:
            closure_payload: Dict[str, Any] = {"exit_code": exit_code}
            if caught_exc is not None:
                closure_payload["aborted_by_exception"] = type(caught_exc).__name__
            journal.append("RUN_CLOSURE", closure_payload)
        except BaseException:
            exit_code = max(exit_code, 1)

        try:
            journal.close()
        except BaseException:
            exit_code = max(exit_code, 1)

        if dev_fd is not None:
            try:
                os.close(dev_fd)
            except BaseException:
                pass

        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except BaseException:
                pass

    if caught_exc is not None:
        raise caught_exc
    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
