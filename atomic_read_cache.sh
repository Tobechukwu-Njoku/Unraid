#!/bin/bash
#description=This script pulls small files from the array into your selected cache pool
#arrayStarted=true
#name=atomic_read_cache

#author=T. N.
#date=2025-04-03
#version=1.0.1
#license=MIT
#--------------------------------------------------------------------------------------------------------------------------------
# This script is designed to move small files to the cache pool of an array backed share
# It counts the number of files in each size range and displays the results in a table format (size thresholds)
# These size thresholds are dynamically generated based on user-defined parameters
# It also provides a dry run option to preview the files that would be moved
# before actually moving them
#--------------------------------------------------------------------------------------------------------------------------------

#Todo:
# - Add a cleanup option to remove empty directories after moving files
# - Add a progress indicator for long-running operations without using a lot of CPU time

#Tofix:
# - Fix that the table shows MB instead of changing to GB

# Configuration
SHARE_NAME="Test-Share" # Set the name of your share here
CACHE_NAME="xray"       # Set the name of your cache here
DRY_RUN=true            # Set to true for a dry run

# Declare environment variables
START_SIZE=64           # Initial size threshold in megabytes (numeric only)
SIZE_MULTIPLIER=2       # Multiplier for size increases (e.g., 64M, 128M, etc.)
ITERATIONS=8            # Number of iterations for size thresholds

# Don't change the following variables
VALID_PATHS=false       # Set to true if the paths exist
PATH_SHARE_ARRAY="/mnt/user0/$SHARE_NAME"           # Source (only from array)
PATH_SHARE_CACHE="/mnt/$CACHE_NAME/$SHARE_NAME"     # Destination (cache disk)

# Declare arrays to store results
declare -A file_count_array
declare -A file_count_cache
size_thresholds_bytes=()

LOG_FILE="/var/log/atomic_read_cache.log"


log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

# Function to generate size thresholds
generate_size_thresholds_bytes() {
    size_thresholds_bytes[0]=$((START_SIZE * 1024 * 1024))  # Convert START_SIZE to bytes
    for ((i = 1; i < ITERATIONS; i++)); do
        prev_size=${size_thresholds_bytes[$((i-1))]}
        next_size=$((prev_size * SIZE_MULTIPLIER))
        size_thresholds_bytes[i]=$next_size  # Store size in bytes
    done
}

# Function to count files for each size range
count_files() {
    for ((i = 0; i < ITERATIONS-1; i++)); do
        lower_size=${size_thresholds_bytes[$i]}

        if [[ $i -eq 0 ]]; then
            # First range: Files smaller than START_SIZE
            file_count_array["sub${lower_size}"]=$(find "$PATH_SHARE_ARRAY" -type f -size -"${lower_size}c" | wc -l)
            file_count_cache["sub${lower_size}"]=$(find "$PATH_SHARE_CACHE" -type f -size -"${lower_size}c" | wc -l)
        else
            # Looped range: Files between size thresholds
            upper_size=${size_thresholds_bytes[$i+1]}
            file_count_array["sub${upper_size}"]=$(find "$PATH_SHARE_ARRAY" -type f -size +"${lower_size}c" -size -"${upper_size}c" | wc -l)
            file_count_cache["sub${upper_size}"]=$(find "$PATH_SHARE_CACHE" -type f -size +"${lower_size}c" -size -"${upper_size}c" | wc -l)
        fi
    done

    # Count files larger than the last threshold
    final_threshold=${size_thresholds_bytes[$((ITERATIONS-1))]}
    file_count_array["super${final_threshold}"]=$(find "$PATH_SHARE_ARRAY" -type f -size +"${final_threshold}c" | wc -l)
    file_count_cache["super${final_threshold}"]=$(find "$PATH_SHARE_CACHE" -type f -size +"${final_threshold}c" | wc -l)
}

# Function to display results in a table format
display_results() {
    echo "--------------------------------------"
    printf "| %-10s | %-15s | %-15s | %-16s |\n" "Size" "Array" "Cache: $CACHE_NAME" "Total"
    echo "--------------------------------------"

    for ((i = 0; i < ITERATIONS; i++)); do
        size_label="<$((${size_thresholds_bytes[$i]} / 1024 / 1024))M"
        array_count=${file_count_array["sub${size_thresholds_bytes[$i]}"]}
        cache_count=${file_count_cache["sub${size_thresholds_bytes[$i]}"]}
        total_count=$((array_count + cache_count))

        printf "| %-10s | %-15s | %-15s | %-16s |\n" "$size_label" "$array_count" "$cache_count" "$total_count"
    done

    # Print final row for files larger than the last threshold
    size_label=">$((${final_threshold} / 1024 / 1024))M"
    array_count=${file_count_array["super${final_threshold}"]}
    cache_count=${file_count_cache["super${final_threshold}"]}
    total_count=$((array_count + cache_count))

    printf "| %-10s | %-15s | %-15s | %-16s |\n" "$size_label" "$array_count" "$cache_count" "$total_count"
    echo "--------------------------------------"
}

# Function to move files while preserving directory structure
move_files() {
    local src_file="$1"
    local relative_path="${src_file#$PATH_SHARE_ARRAY/}"
    local dest_path="$PATH_SHARE_CACHE/$relative_path"

    mkdir -p "$(dirname "$dest_path")" || {
        log "Error: Failed to create directory for $dest_path"
        return 1
    }

    if [ "$DRY_RUN" = true ]; then
        log "Would Move: $src_file"
    else
        mv "$src_file" "$dest_path" || {
            log "Error: Failed to move $src_file to $dest_path"
            return 1
        }
    fi
}

# Function to process files smaller than the specified size
safety_checks() {
    export -f move_files
    export PATH_SHARE_ARRAY PATH_SHARE_CACHE DRY_RUN

    if [ -d $PATH_SHARE_ARRAY ]; then
        log "Check 01 Passed: Directory exists on array."
        if [ -d $PATH_SHARE_CACHE ]; then
            log "Check 02 Passed: Directory exists on cache."
            VALID_PATHS=true
        else
            log "Check 02 Failed: Directory $PATH_SHARE_CACHE does not exist on cache."
        fi
    else
        log "Check 01 Failed: Directory $PATH_SHARE_ARRAY does not exist on array."
    fi
}

process_files() {
    log "Moving files smaller than $START_SIZE MB from '$PATH_SHARE_ARRAY' to '$PATH_SHARE_CACHE'"
    log "Cache conquest in progress... Stand ready for its completion!"
    find "$PATH_SHARE_ARRAY" -type f -size -"$START_SIZE"M -print0 | while IFS= read -r -d '' file; do
        move_files "$file"
    done
}

task_execution() {
    safety_checks
    if $VALID_PATHS; then
        generate_size_thresholds_bytes
        count_files
        display_results
        process_files
        if $DRY_RUN; then
            log "Dry run completed. No files were moved."
        else
            log "Files moved successfully."
        fi
    else
        log "Error: One or more paths do not exist. Please check the configuration."
    fi
}

# Main script execution
task_execution
