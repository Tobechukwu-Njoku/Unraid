#!/bin/bash
#description=This script pulls small files from the array into your selected cache pool
#arrayStarted=true
#name=atomic_read_cache

#author=T. N.
#date=2025-04-03
#version=1.1.3
#license=MIT
#--------------------------------------------------------------------------------------------------------------------------------
# This script is designed to move small files to the cache pool of an array backed share
# It counts the number of files in each size range and displays the results in a table format (size thresholds)
# These size thresholds are dynamically generated based on user-defined parameters
# It also provides a dry run option to preview the files that would be moved
# before actually moving them
#--------------------------------------------------------------------------------------------------------------------------------
# INSTRUCTIONS:
#
# 1. Set the SHARE_NAME and CACHE_NAME variables to your desired share and cache names
# 2. Set the DRY_RUN variable to true for a dry run or false to actually move files
# 3. Set the CLEAN_EMPTY variable to true if you want to clean up empty directories after moving files
# 4. Set the START_SIZE 
# 5. Adjust the START_SIZE so a good number of files are within the size range
# 6. Set the DRY_RUN variable to false to actually move files
#--------------------------------------------------------------------------------------------------------------------------------

#Todo:
# - Add unit tests
# - Add trap for cleanup
# - Add error handling for file move failures
# - Change permission of moved files to 777
# - Check size of moved files to ensure they are not larger than the free space on the cache
# - Move larger files back to the array if they exceed the threshold
# - Do not allow this to run on 'appdata' share

#Tofix:
# - Function "count_files" is not collapsing correctly

# Configuration
SHARE_NAME="Test-Share" # Set the name of your share here
CACHE_NAME="xray"       # Set the name of your cache here
DRY_RUN=true            # Set to true for a dry run
CLEAN_EMPTY=false        # Set to true to clean up empty directories after moving files

# Moving threshold & table printing variable
START_SIZE=1           # Initial size threshold in megabytes (numeric only) default 64

# Table printing variables
SIZE_MULTIPLIER=2       # Multiplier for size increases (e.g., 64M, 128M, etc.) default 2
ITERATIONS=8            # Number of iterations for size thresholds default 8

#--------------------------------------------------------------------------------------------------------------------------------
# Don't change the variables below unless you know what you're doing
#--------------------------------------------------------------------------------------------------------------------------------
VALID_PATHS=false       # Set to true if the paths exist
VALID_PARAMS=false      # Set to true if the parameters are valid
PATH_SHARE_ARRAY="/mnt/user0/$SHARE_NAME"           # Source (only from array)
PATH_SHARE_CACHE="/mnt/$CACHE_NAME/$SHARE_NAME"     # Destination (cache disk)

# Declare arrays to store results
declare -A file_count_array
declare -A file_count_cache
size_thresholds_bytes=()
BYTES_IN_MB=$((1024 * 1024))

LOG_FILE="/var/log/atomic_read_cache.log"


log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

# Function to process files smaller than the specified size
safety_checks() {
    export -f move_files
    export PATH_SHARE_ARRAY PATH_SHARE_CACHE DRY_RUN VALID_PATHS VALID_PARAMS

    validate_parameters
    validate_paths
}

validate_parameters() {
    if ((START_SIZE > 0)); then
        VALID_PARAMS=true
    else
        echo "Error: START_SIZE must be greater than 0"
        exit 1
    fi

    if ((SIZE_MULTIPLIER > 1)); then
        VALID_PARAMS=true
    else
        echo "Error: SIZE_MULTIPLIER must be greater than 1"
        exit 1
    fi

    if ((ITERATIONS > 1)); then
        VALID_PARAMS=true
    else
        echo "Error: ITERATIONS must be greater than 0"
        exit 1
    fi

    if $VALID_PARAMS; then
        log "Parameters validated successfully."
    else
        log "Error: One or more parameters are incorrect. Please check the configuration."
        exit 1
    fi
}

validate_paths() {
    if [ -d "$PATH_SHARE_ARRAY" ]; then
        VALID_PATHS=true
    else
        echo "Error: Source path $PATH_SHARE_ARRAY does not exist."
        exit 1
    fi

    if [ -d "$PATH_SHARE_CACHE" ]; then
        VALID_PATHS=true
    else
        echo "Error: Destination path $PATH_SHARE_CACHE does not exist."
        exit 1
    fi

    if $VALID_PATHS; then
        log "Paths validated successfully."
    else
        log "Error: One or more paths do not exist. Please check the configuration."
        exit 1
    fi
}

validate_free_space() {
    local required_space=$1  # Space required in bytes
    local free_space

    # Get the free space on the cache disk in bytes
    free_space=$(df --output=avail -B1 "$PATH_SHARE_CACHE" | tail -n 1)

    if ((free_space >= required_space)); then
        log "Success: Sufficient free space available on cache: $((free_space / BYTES_IN_MB)) MB"
        return 0
    else
        log "Error: Inufficient free space on cache. Required: $((required_space / BYTES_IN_MB)) MB, Available: $((free_space / BYTES_IN_MB)) MB"
        return 1
    fi
}

generate_size_thresholds() {
    size_thresholds_bytes[0]=$((START_SIZE * BYTES_IN_MB))
    for ((i = 1; i < ITERATIONS; i++)); do
        prev_size=${size_thresholds_bytes[$((i-1))]}
        next_size=$((prev_size * SIZE_MULTIPLIER))
        size_thresholds_bytes[i]=$next_size
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

format_size() {
    local size_bytes=$1
    if ((size_bytes >= 1024 * 1024 * 1024)); then
        echo "$((size_bytes / 1024 / 1024 / 1024))G"
    else
        echo "$((size_bytes / 1024 / 1024))M"
    fi
}

display_results() {
    echo "--------------------------------------"
    printf "| %-10s | %-15s | %-15s | %-16s |\n" "Size" "Array" "Cache: $CACHE_NAME" "Total"
    echo "--------------------------------------"

    for ((i = 0; i < ITERATIONS; i++)); do
        size_label="<$(format_size ${size_thresholds_bytes[$i]})"
        array_count=${file_count_array["sub${size_thresholds_bytes[$i]}"]}
        cache_count=${file_count_cache["sub${size_thresholds_bytes[$i]}"]}
        total_count=$((array_count + cache_count))

        printf "| %-10s | %-15s | %-15s | %-16s |\n" "$size_label" "$array_count" "$cache_count" "$total_count"
    done

    size_label=">$(format_size ${final_threshold})"
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

process_files() {
    log "Moving files smaller than $START_SIZE MB from '$PATH_SHARE_ARRAY' to '$PATH_SHARE_CACHE'"
    find "$PATH_SHARE_ARRAY" -type f -size -"$START_SIZE"M -print0 | while IFS= read -r -d '' file; do
        move_files "$file"
        echo -n "."  # Progress indicator
    done
    echo  # Newline after progress dots
}

change_permissions() {
    local target_path="$1"

    if [ -d "$target_path" ]; then
        log "Changing permissions for files and directories in $target_path"

        # Change permissions for directories to drwxrwxrwx
        find "$target_path" -type d -exec chmod 777 {} \; || {
            log "Error: Failed to change permissions for directories in $target_path"
            return 1
        }

        # Change permissions for files to -rw-rw-rw-
        find "$target_path" -type f -exec chmod 666 {} \; || {
            log "Error: Failed to change permissions for files in $target_path"
            return 1
        }

        log "Permissions successfully updated for $target_path"
    else
        log "Error: Target path $target_path does not exist or is not a directory"
        return 1
    fi
}

cleanup_empty_dirs() {
    if $CLEAN_EMPTY; then
        log "Cleaning up empty directories in $PATH_SHARE_ARRAY"
        find "$PATH_SHARE_ARRAY" -type d -empty -delete || log "Error: Failed to clean up empty directories"
    fi
}

post_execution_message() {
    if $DRY_RUN; then
        log "Dry run completed. No files were moved."
    else
        log "Files moved successfully."
    fi
}

task_execution() {
    safety_checks
    generate_size_thresholds
    count_files
    display_results
    process_files
    change_permissions "$PATH_SHARE_CACHE"
    cleanup_empty_dirs
    post_execution_message
}

# Main script execution
task_execution
