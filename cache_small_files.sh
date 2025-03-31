#!/bin/bash

# Configuration
SHARE_NAME="Test-Large"
SOURCE_SHARE="/mnt/user/Test-Large"   # Change to your Unraid share path
CACHE_DISK="/mnt/cache/Test-Large"    # Change to the corresponding path on cache
SIZE_LIMIT="10M"                      # Set the size limit (e.g., 10M for 10MB)
DRY_RUN=false                         # Set to true for a dry run

# Function to move files while preserving the directory structure
move_files() {
    local src_file="$1"
    local relative_path="${src_file#$SOURCE_SHARE/}"
    local dest_path="$CACHE_DISK/$relative_path"

    # Ensure the target directory exists
    mkdir -p "$(dirname "$dest_path")"

    if [ "$DRY_RUN" = true ]; then
        echo "Would move: $src_file -> $dest_path"
    else
        mv "$src_file" "$dest_path"
        echo "Moved: $src_file -> $dest_path"
    fi
}

export -f move_files
export SOURCE_SHARE CACHE_DISK DRY_RUN

# Find and process files smaller than the specified size
find "$SOURCE_SHARE" -type f -size -"$SIZE_LIMIT" -print0 | while IFS= read -r -d '' file; do
    move_files "$file"
done
