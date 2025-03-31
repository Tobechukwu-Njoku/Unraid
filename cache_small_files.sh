#!/bin/bash

# Configuration
SHARE_NAME="share_name"                             # Set the name of your share here
CACHE_NAME="cache"                                  # Set the name of your cache here
SIZE_LIMIT="10M"                                    # Set size limit (e.g., 10M for 10MB)
DRY_RUN=false                                       # Set to true for a dry run

SHARE_ARRAY_PATH="/mnt/user0/$SHARE_NAME"           # Source (only from array)
SHARE_CACHE_PATH="/mnt/$CACHE_NAME/$SHARE_NAME"     # Destination (cache disk)

# Function to move files while preserving directory structure
move_files() {
    local src_file="$1"
    local relative_path="${src_file#$SHARE_ARRAY_PATH/}"
    local dest_path="$SHARE_CACHE_PATH/$relative_path"

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
export SHARE_ARRAY_PATH SHARE_CACHE_PATH DRY_RUN

# Find and process files smaller than the specified size
find "$SHARE_ARRAY_PATH" -type f -size -"$SIZE_LIMIT" -print0 | while IFS= read -r -d '' file; do
    move_files "$file"
done
