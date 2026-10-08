# Jellyfin Clean Empty Folders

A bash script that scans a films folder and reports any movie folder that
contains no video file, which usually means an empty or failed import.

For each such folder it also prints the folder's size. Folders under
`MIN_SIZE_MB` are reported as candidates for deletion, but the `rm -rf` line
is commented out, so the script only reports and never deletes.

## Configuration

Edit the top of `remove_empty_folders.sh`:

- **`MEDIA_PATH`**: the films folder to scan.
- **`MIN_SIZE_MB`**: folders smaller than this are flagged for deletion.
- **`VIDEO_EXTENSIONS`**: file types that count as video. Matching ignores case.

## Requirements

- Bash 4+ (uses `${var^}` / `${var,,}` case conversion).
