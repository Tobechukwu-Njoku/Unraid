# Jellyfin Clean Trickplay Folders

A bash script that validates and cleans up trickplay folders for movie files in Jellyfin.

## Features

- Validates each movie has exactly one video file
- Ensures trickplay folders exist for each movie
- Removes extra/orphaned trickplay folders
- Supports multiple video formats (mkv, mp4, mov, avi, wmv, flv, webm, mpeg, mpg, ts)
- Comprehensive error logging

## Configuration

Edit the script and update:

- **`BASE_PATH`**: Path to your films folder (default: `/mnt/user/Media-Large/Films`)
- **`VIDEO_EXTENSIONS`**: Array of supported video extensions
- **`TRICKPLAY_SUFFIX`**: Folder suffix for trickplay directories (default: `.trickplay`)

## Usage

```bash
./validate_movies_trickplay.sh
```

The script will:
1. Scan the BASE_PATH directory
2. For each subdirectory (movie folder):
   - Find the video file
   - Verify a matching trickplay folder exists
   - Remove any extra trickplay folders
   - Report errors to stderr

## Output

- `INFO:` messages logged to stdout
- `ERROR:` messages logged to stderr with exit code 1

## Requirements

- Bash 4+
- GNU find with `-printf` support
- Read/write permissions for the films folder
