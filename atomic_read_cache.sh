#!/bin/bash
#description=This script pulls small files from the array into your selected cache pool
#arrayStarted=true
#name=atomic_read_cache

#author=Tobechukwu Njoku
#date=2025-04-01
#version=0.2.0
#license=MIT
#--------------------------------------------------------------------------------------------------------------------------------
# This script is designed to move small files to the cache pool of an array backed share
# It dynamically generates size thresholds based on user-defined parameters
# It counts the number of files in each size range and displays the results in a table format
# It also provides a dry run option to preview the files that would be moved
# before actually moving them
#--------------------------------------------------------------------------------------------------------------------------------

#Todo:
# - Add error handling for invalid cache & array paths
# - Add a cleanup option to remove empty directories after moving files
# - Add a progress indicator for long-running operations without using a lot of CPU time
# - Add a feature to exclude certain file types from being moved

#Tofix:
# - Fix SIZE_LIMIT & START_SIZE do the same thing
# - Fix how the table is displayed
# - Fix that the table shows MB instead of changing to GB
# - Fix how total is calculated

# Configuration
SHARE_NAME="Test-Share" # Set the name of your share here
CACHE_NAME="xray"       # Set the name of your cache here
SIZE_LIMIT="256M"       # Set size limit (e.g., 10M for 10MB)
DRY_RUN=true            # Set to true for a dry run

START_SIZE=512          # Initial size threshold in megabytes (numeric only)
SIZE_MULTIPLIER=4       # Multiplier for size increases (8M, 16M, 32M, etc)
ITERATIONS=8            # Number of iterations for size thresholds

# Declare environment variables
PATH_SHARE_ARRAY="/mnt/user0/$SHARE_NAME"           # Source (only from array)
PATH_SHARE_CACHE="/mnt/$CACHE_NAME/$SHARE_NAME"     # Destination (cache disk)

#--------------------------------------------------------------------------------------------------------------------------------
# Declare arrays to store results
declare -A file_count_array
declare -A file_count_cache
size_thresholds=()

# Generate size thresholds dynamically
size_thresholds[0]=$((START_SIZE * 1024 * 1024))  # Convert START_SIZE to bytes
for ((i = 1; i < ITERATIONS; i++)); do
    prev_size=${size_thresholds[$((i-1))]}
    next_size=$((prev_size * SIZE_MULTIPLIER))
    size_thresholds[i]=$next_size  # Store size in bytes
done

# Count files for each size range
for ((i = 0; i < ITERATIONS; i++)); do
    lower_size=${size_thresholds[$i]}

    if [[ $i -eq 0 ]]; then
        # First range: Files smaller than START_SIZE
        file_count_array["sub${lower_size}"]=$(find "$PATH_SHARE_ARRAY" -type f -size -"${lower_size}c" | wc -l)
        file_count_cache["sub${lower_size}"]=$(find "$PATH_SHARE_CACHE" -type f -size -"${lower_size}c" | wc -l)
        echo "lower_size is " $lower_size
    else
        upper_size=${size_thresholds[$i+1]}
        file_count_array["sub${upper_size}"]=$(find "$PATH_SHARE_ARRAY" -type f -size +"${lower_size}c" -size -"${upper_size}c" | wc -l)
        file_count_cache["sub${upper_size}"]=$(find "$PATH_SHARE_CACHE" -type f -size +"${lower_size}c" -size -"${upper_size}c" | wc -l)
        echo "lower_size is " $lower_size " upper_size is " $upper_size
    fi
done

# Count files larger than the last threshold
final_threshold=${size_thresholds[$((ITERATIONS-1))]}
file_count_array["super${final_threshold}"]=$(find "$PATH_SHARE_ARRAY" -type f -size +"${final_threshold}c" | wc -l)
file_count_cache["super${final_threshold}"]=$(find "$PATH_SHARE_CACHE" -type f -size +"${final_threshold}c" | wc -l)

# Display results in table format
echo "-------------------------------------"
printf "| %-10s | %-20s | %-20s | %-20s |\n" "Size" "Array" "Cache: $CACHE_NAME" "Total"
echo "-------------------------------------"

for ((i = 0; i < ITERATIONS; i++)); do
    size_label="<$((${size_thresholds[$i]} / 1024 / 1024))M"
    array_count=${file_count_array["sub${size_thresholds[$i]}"]}
    cache_count=${file_count_cache["sub${size_thresholds[$i]}"]}
    total_count=$((array_count + cache_count))

    printf "| %-10s | %-20s | %-20s | %-20s |\n" "$size_label" "$array_count" "$cache_count" "$total_count"
done

# Print final row for files larger than the last threshold
size_label=">$((${final_threshold} / 1024 / 1024))M"
array_count=${file_count_array["super${final_threshold}"]}
cache_count=${file_count_cache["super${final_threshold}"]}
total_count=$((array_count + cache_count))

printf "| %-10s | %-20s | %-20s | %-20s |\n" "$size_label" "$array_count" "$cache_count" "$total_count"
echo "-------------------------------------"
#--------------------------------------------------------------------------------------------------------------------------------

# Function to move files while preserving directory structure
move_files() {
    local src_file="$1"
    local relative_path="${src_file#$PATH_SHARE_ARRAY/}"
    local dest_path="$PATH_SHARE_CACHE/$relative_path"

    # Ensure the target directory exists
    mkdir -p "$(dirname "$dest_path")"

    if [ "$DRY_RUN" = true ]; then
        # This uses a lot of CPU time
        echo "Would Move: $src_file -> $dest_path"
    else
        mv "$src_file" "$dest_path"
        # This uses a lot of CPU time
        echo "Moved: $src_file -> $dest_path"
    fi
}

export -f move_files
export PATH_SHARE_ARRAY PATH_SHARE_CACHE DRY_RUN

# Find and process files smaller than the specified size
find "$PATH_SHARE_ARRAY" -type f -size -"$SIZE_LIMIT" -print0 | while IFS= read -r -d '' file; do
    move_files "$file"
done
