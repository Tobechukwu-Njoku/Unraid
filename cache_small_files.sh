#!/bin/bash
#description=This script pulls small files from the array into your selected cache pool
#arrayStarted=true
#name=Cache Small Files

# Configuration
SHARE_NAME="share_name"                             # Set the name of your share here
CACHE_NAME="cache"                                  # Set the name of your cache here
SIZE_LIMIT="256M"                                   # Set size limit (e.g., 10M for 10MB)
DRY_RUN=false                                       # Set to true for a dry run

PATH_SHARE_ARRAY="/mnt/user0/$SHARE_NAME"           # Source (only from array)
PATH_SHARE_CACHE="/mnt/$CACHE_NAME/$SHARE_NAME"     # Destination (cache disk)

# Count files under $SIZE_LIMIT before processing
echo "Counting files under $SIZE_LIMIT in $SHARE_NAME"
file_count_array=$(find $PATH_SHARE_ARRAY -type f -size $SIZE_LIMIT | wc -l)
file_count_cache=$(find $PATH_SHARE_CACHE -type f -size $SIZE_LIMIT | wc -l)
echo "$SHARE_NAME Array: $file_count_array files under $SIZE_LIMIT"
echo "$SHARE_NAME Cache: $file_count_cache files under $SIZE_LIMIT"

# Display table to visualize level of efficacy for each level of file size
file_count_array_sub8m=$(find $PATH_SHARE_ARRAY -type f -size -8M | wc -l)
file_count_cache_sub8m=$(find $PATH_SHARE_CACHE -type f -size -8M | wc -l)
file_count_array_sub16m=$(find $PATH_SHARE_ARRAY -type f -size +8M -size -16M | wc -l)          # Count files between size parameters
file_count_cache_sub16m=$(find $PATH_SHARE_CACHE -type f -size +8M -size -16M | wc -l)
file_count_array_sub32m=$(find $PATH_SHARE_ARRAY -type f -size +16M -size -32M | wc -l)
file_count_cache_sub32m=$(find $PATH_SHARE_CACHE -type f -size +16M -size -32M | wc -l)
file_count_array_super32m=$(find $PATH_SHARE_ARRAY -type f -size +32M | wc -l)
file_count_cache_super32m=$(find $PATH_SHARE_CACHE -type f -size +32M | wc -l)
echo "-------------------------------------"
printf "| %-4s | %-10s | %-10s | %-10s |\n" "Size" "Array" "Cache: $CACHE_NAME" "Total"
echo "-------------------------------------"
printf "| %-4s | %-10s | %-10s | %-10s |\n" "<8M" "$file_count_array_sub8m" "$file_count_cache_sub8m"
printf "| %-4s | %-10s | %-10s | %-10s |\n" "<16M" "$file_count_array_sub16m" "$file_count_cache_sub16m"
printf "| %-4s | %-10s | %-10s | %-10s |\n" "<32M" "$file_count_array_sub32m" "$file_count_cache_sub32m"
printf "| %-4s | %-10s | %-10s | %-10s |\n" ">32M" "$file_count_array_super32m" "$file_count_cache_super32m"
echo "-------------------------------------"

# Function to move files while preserving directory structure
move_files() {
    local src_file="$1"
    local relative_path="${src_file#$PATH_SHARE_ARRAY/}"
    local dest_path="$PATH_SHARE_CACHE/$relative_path"

    # Ensure the target directory exists
    mkdir -p "$(dirname "$dest_path")"

    if [ "$DRY_RUN" = true ]; then
        # This uses a lot of CPU time, only uncomment temporarily for detailed information
        #echo "Would Move: $src_file -> $dest_path"
    else
        mv "$src_file" "$dest_path"
        # This uses a lot of CPU time, only uncomment temporarily for detailed information
        #echo "Moved: $src_file -> $dest_path"
    fi
}

export -f move_files
export PATH_SHARE_ARRAY PATH_SHARE_CACHE DRY_RUN

# Find and process files smaller than the specified size
find "$PATH_SHARE_ARRAY" -type f -size -"$SIZE_LIMIT" -print0 | while IFS= read -r -d '' file; do
    move_files "$file"
done
