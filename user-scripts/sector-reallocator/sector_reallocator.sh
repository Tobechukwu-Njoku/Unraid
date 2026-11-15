#!/bin/bash
# User Scripts: run manually only. Keep the Python files in INSTALL_DIR.
set -euo pipefail

# Configuration: all device/storage fields intentionally require local setup.
INSTALL_DIR="/boot/config/sector-reallocator"
DEVICE=""                   # Whole-disk /dev/disk/by-id/ata-... or wwn-...
EXPECTED_SERIAL=""          # Exact serial from the drive label / smartctl -i
EVIDENCE_MOUNT=""           # Separate mounted native filesystem, e.g. /mnt/disks/maintenance
EVIDENCE_DIR=""             # Private directory below that mount, unique to this drive
MODE="discover"             # discover | execute | recover
CONFIRM_SERIAL=""           # Required for execute AND recover; must equal EXPECTED_SERIAL
MAX_SCAN_GB=""              # Optional positive integer GiB limit
ALLOW_DAMAGED_DATA=false    # Explicitly accept expendable filesystem/partition signatures
SKIP_SURFACE_SCAN=false
RESUME_INTERRUPTED_SCAN=false

if [[ -z "$DEVICE" || -z "$EXPECTED_SERIAL" || -z "$EVIDENCE_MOUNT" || -z "$EVIDENCE_DIR" ]]; then
    echo "[FATAL] Set DEVICE, EXPECTED_SERIAL, EVIDENCE_MOUNT and EVIDENCE_DIR in the configuration block." >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "[FATAL] Install a host-compatible Python 3.9+ runtime first." >&2
    exit 1
fi
if [[ ! -r "$INSTALL_DIR/unraid_launcher.py" ]]; then
    echo "[FATAL] Install the project files in INSTALL_DIR before running this launcher." >&2
    exit 1
fi
args=("$DEVICE" --mode "$MODE" --expected-serial "$EXPECTED_SERIAL"
      --confirm-serial "$CONFIRM_SERIAL" --evidence-mount "$EVIDENCE_MOUNT"
      --evidence-dir "$EVIDENCE_DIR")
[[ -z "$MAX_SCAN_GB" ]] || args+=(--max-scan-gb "$MAX_SCAN_GB")
for setting in ALLOW_DAMAGED_DATA SKIP_SURFACE_SCAN RESUME_INTERRUPTED_SCAN; do
    case "${!setting}" in true|false) ;; *) echo "[FATAL] $setting must be true or false." >&2; exit 1 ;; esac
done
[[ "$ALLOW_DAMAGED_DATA" == false ]] || args+=(--allow-damaged-data)
[[ "$SKIP_SURFACE_SCAN" == false ]] || args+=(--skip-surface-scan)
[[ "$RESUME_INTERRUPTED_SCAN" == false ]] || args+=(--resume-interrupted-scan)
exec python3 -B "$INSTALL_DIR/unraid_launcher.py" "${args[@]}"
