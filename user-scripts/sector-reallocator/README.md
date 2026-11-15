# SATA sector reallocator

Manual disk maintenance for **unused, unassigned SATA HDDs whose contents are
blank or expendable**. It discovers unreadable regions, confirms candidate
physical sectors, and attempts zero-writes followed by verification. Writing can
allow firmware to reallocate a sector; readable results do not prove physical
spare-sector relocation or make a failing drive trustworthy.

Discovery is the default. Execute mode destroys the contents of each selected
physical sector: 512 bytes on 512n, and normally 4096 bytes on 512e/4Kn. On 512e,
that includes all eight constituent logical sectors. SAS, NVMe and 520/528-byte
formats are outside this engine's scope.

This is a manual maintenance tool, not a scheduled repair service. No real
hardware or Unraid installation has been qualified by this repository's tests.

## Files

| File | Purpose |
| --- | --- |
| `sector_reallocator.sh` | Paste into User Scripts; edit its configuration block. |
| `unraid_launcher.py` | Unraid preflight, process lock, and repeated checks at engine boundaries. |
| `sata_reallocator.py` | Reviewed V7 maintenance engine, preserved unchanged. |
| `engine-manifest.json` | Version and SHA-256 used to check the installed engine before loading it. |
| `tests/` | Offline engine scenarios and launcher tests. |

Unlike a single-file shell project, this launcher needs its Python files and
manifest installed together. It does not download packages or code while running.

## Requirements

- Root, through User Scripts or an Unraid local console.
- Python 3.9+ with Linux `O_DIRECT`, `preadv`, `pwritev` and `RWF_DSYNC` support.
- Host-compatible `smartctl`, GNU `ddrescue`, `blockdev`, `blkid`, `sg_verify`
  (sg3_utils), and `hdparm`. All are checked by the engine, even when a particular
  mode does not use every binary. Verify package availability for your Unraid
  release; this project does not install or update them.
- Current Unraid state files at `/usr/local/emhttp/state/var.ini` and `disks.ini`.
  Missing, unreadable or unsupported state is a refusal, not permission to proceed.
- A separate, healthy, mounted filesystem for recovery evidence, available while
  the array is stopped. A separately mounted Unassigned Devices disk is one
  possible arrangement. Do not put evidence on the drive being maintained.

## Unraid preparation

1. Establish the target's whole-disk `/dev/disk/by-id/` path and exact serial.
   Do not select an array disk, parity disk, pool member, boot device or VM disk.
2. Stop the array and all VMs manually. Maintenance Mode is not sufficient for
   this launcher. Assigned disks remain forbidden even while stopped.
3. Stop competing SMART tests, preclear jobs and other raw-device users. Prevent
   anyone or any automation from assigning, mounting or starting work on the
   target until maintenance and cleanup finish.
4. Mount the separate evidence filesystem and choose a per-drive directory below
   its **direct mount path**. The launcher accepts XFS, Btrfs, ext4 and ZFS, and
   refuses flash/FAT, `/mnt/user` FUSE paths, RAM filesystems, symlink paths,
   missing mounts and unexpected nested mounts.
5. Ensure existing evidence directories are root-owned mode `0700`, with evidence
   files mode `0600`. The engine can create its final directory under an existing
   parent. It refuses insecure existing permissions instead of changing them.

Keep this same evidence directory for subsequent runs and recovery. Do not delete
or edit maps/journals to bypass a failed preflight. The evidence files contain
text and defect locations, not recovered media data. Discovery writes these
files, although it does not issue sector writes to the target.

## Install and run through User Scripts

From a local checkout of this repository on Unraid, install just the code and
manifest (these commands do not start maintenance):

```bash
mkdir -p /boot/config/sector-reallocator
cp user-scripts/sector-reallocator/{sata_reallocator.py,unraid_launcher.py,engine-manifest.json} /boot/config/sector-reallocator/
```

Protect the boot share against unauthorized writes: these files run as root.
The code may live on `/boot`, but recovery evidence must live elsewhere.

Create a User Script, paste `sector_reallocator.sh`, and edit:

```bash
DEVICE="/dev/disk/by-id/ata-YOUR_DRIVE_ID"
EXPECTED_SERIAL="YOUR_EXACT_SERIAL"
EVIDENCE_MOUNT="/mnt/disks/YOUR_HEALTHY_EVIDENCE_MOUNT"
EVIDENCE_DIR="/mnt/disks/YOUR_HEALTHY_EVIDENCE_MOUNT/sector-evidence/YOUR_EXACT_SERIAL"
MODE="discover"
CONFIRM_SERIAL=""
```

Those paths are placeholders. Mount the real evidence filesystem and create any
missing parent directories first. Keep the User Scripts schedule **Disabled**;
launch manually. Begin with `MAX_SCAN_GB="1"` to qualify a bounded discovery run.
Review its output and journals before removing the limit for a full scan. A
limited/resumed scan is not a fresh certification of the entire drive.

For destructive work, set `MODE="execute"` and set `CONFIRM_SERIAL` to the exact
configured serial. The engine also checks the live drive serial. If signatures
exist and the drive's contents are expendable, `ALLOW_DAMAGED_DATA=true` explicitly
permits proceeding past that signature gate. Leave it false otherwise.

`SKIP_SURFACE_SCAN=true` uses SMART defect candidates only. It is not an exhaustive
scan. `RESUME_INTERRUPTED_SCAN=true` requests bounded discovery resumption; it does
not bypass pending writes, settings or unresolved confirmation state.

## Recovery

After an interrupted or failed run, inspect the journal before retrying.
Use the same target, evidence directory and scan limit. Set `MODE="recover"`
and the matching `CONFIRM_SERIAL`, then launch manually with the array stopped.
Recovery can restore kernel timeout and drive error-recovery settings; it is not
an entirely read-only operation. It does not issue sector zero-writes.

Recovery restores outstanding settings, verifies settings already recorded as
restored, and inspects unresolved target offsets. A previously pending write is
reconciled by observation, without claiming its completion was proven. Unknown
identity, mismatching settings or indeterminate target state remain failures.
After recovery succeeds, return to discovery before requesting execution again.

Do not configure boot-time repair, automatic recovery, or automatic retries.
The journal supports investigation and resumption; it cannot make power loss,
controller resets or concurrent raw I/O safe.

## Optional console use

The same guarded launcher is available without User Scripts:

```bash
python3 -B /boot/config/sector-reallocator/unraid_launcher.py --help
```

It accepts the engine's bounded scan, discovery and explicitly degraded options.
The shell entry point deliberately exposes only common settings. Options such as
`--allow-degraded-no-verify`, `--allow-unsupported-erc` and
`--enable-hdparm-repair-fallback` require a deliberate console invocation. The
hdparm fallback remains restricted by the engine to 512n drives.

## Checks and limits

The launcher checks `mdState="STOPPED"`, disk assignments by device/serial, active
QEMU process names, and accessible VM disk definitions. It verifies target
major/minor/path identity and the expected live serial. It rechecks host state
before drive-setting changes, the scan, target checks and cleanup gates. Only one
launcher instance can run at a time; the engine also holds its own journal and
exclusive-device claims.

These checks do not lock Unraid's GUI, libvirt, preclear or other root processes.
They cannot identify every third-party passthrough configuration or prevent a
concurrent administrator action between checks or inside a retry. Maintain an
exclusive maintenance window and independently confirm the drive is unused.
No services, assignments, mounts or installed packages are changed by the launcher.

The raw engine is also present for inspection, but invoking it directly bypasses
the Unraid wrapper. Use `unraid_launcher.py` on Unraid.

One known minor V7 reporting issue remains: after unsuccessful write-error
corroboration, the returned final status may be more specific than the durable
attempt status. Both are failures, so the engine still stops; the final
corroboration detail may be missing from the journal. Neither a successful VERIFY
nor zero readback proves physical spare-sector relocation.

The Unraid state conventions used here are visible in the upstream
[webGUI helpers](https://github.com/unraid/webgui/blob/master/emhttp/plugins/dynamix/include/Helpers.php)
and [array controls](https://github.com/unraid/webgui/blob/master/emhttp/plugins/dynamix/ArrayOperation.page).
The launcher fails closed if its expected state format is unavailable.

## Offline tests

On a Linux development host with Python 3.9+ and Bash:

```bash
./user-scripts/sector-reallocator/tests/run_tests.sh
```

The suite compiles the Python, checks Bash syntax, executes 43 engine scenarios,
and tests the launcher. Drive I/O, sysfs, host assignments and drive commands are
mocked. Temporary real files contain only text journals/maps. The recovery checks
include all ten durable-record boundaries in a mixed settings/target recovery.
They do not test torn records, actual hardware or an installed Unraid system.
