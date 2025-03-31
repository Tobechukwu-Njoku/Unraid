#!/bin/bash

# Configuration
SHARE_NAME="share_name"                             # Set the name of your share here
CACHE_NAME="cache"                                  # Set the name of your cache here
SIZE_LIMIT="10M"                                    # Set size limit (e.g., 10M for 10MB)
DRY_RUN=false                                       # Set to true for a dry run

PATH_SHARE_ARRAY="/mnt/user0/$SHARE_NAME"           # Source (only from array)
PATH_SHARE_CACHE="/mnt/$CACHE_NAME/$SHARE_NAME"     # Destination (cache disk)

# Function to move files while preserving directory structure
move_files() {
    local src_file="$1"
    local relative_path="${src_file#$PATH_SHARE_ARRAY/}"
    local dest_path="$PATH_SHARE_CACHE/$relative_path"

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
export PATH_SHARE_ARRAY PATH_SHARE_CACHE DRY_RUN

# Find and process files smaller than the specified size
find "$PATH_SHARE_ARRAY" -type f -size -"$SIZE_LIMIT" -print0 | while IFS= read -r -d '' file; do
    move_files "$file"
done
