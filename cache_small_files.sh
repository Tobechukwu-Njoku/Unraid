#!/bin/bash
#description=This script pulls small files from the array into your selected cache pool
#arrayStarted=true
#name=Cache Small Files

# Configuration
SHARE_NAME="share_name"                             # Set the name of your share here
CACHE_NAME="cache"                                  # Set the name of your cache here
SIZE_LIMIT="256M"                                   # Set size limit (e.g., 10M for 10MB)
DRY_RUN=false                                       # Set to true for a dry run

# Declare environment variables
PATH_SHARE_ARRAY="/mnt/user0/$SHARE_NAME"           # Source (only from array)
PATH_SHARE_CACHE="/mnt/$CACHE_NAME/$SHARE_NAME"     # Destination (cache disk)

# Declare arrays to store results
declare -A file_count_array
declare -A file_count_cache
size_thresholds=()

# Generate size thresholds dynamically
size_thresholds[0]=$START_SIZE
for ((i = 1; i < ITERATIONS; i++)); do
    prev_size=${size_thresholds[$((i-1))]}
    next_size=$(echo "$prev_size * $SIZE_MULTIPLIER" | bc)M
    size_thresholds[i]=$next_size
done

# Count files for each size range
for ((i = 0; i < ITERATIONS; i++)); do
    lower_size=${size_thresholds[$i]}

    if [[ $i -eq 0 ]]; then
        # First range: Files smaller than START_SIZE
        file_count_array["sub$lower_size"]=$(find "$PATH_SHARE_ARRAY" -type f -size -"$lower_size" | wc -l)
        file_count_cache["sub$lower_size"]=$(find "$PATH_SHARE_CACHE" -type f -size -"$lower_size" | wc -l)
    else
        upper_size=${size_thresholds[$i]}
        file_count_array["sub$upper_size"]=$(find "$PATH_SHARE_ARRAY" -type f -size +"$lower_size" -size -"$upper_size" | wc -l)
        file_count_cache["sub$upper_size"]=$(find "$PATH_SHARE_CACHE" -type f -size +"$lower_size" -size -"$upper_size" | wc -l)
    fi
done

# Count files larger than the last threshold
final_threshold=${size_thresholds[$((ITERATIONS-1))]}
file_count_array["super$final_threshold"]=$(find "$PATH_SHARE_ARRAY" -type f -size +"$final_threshold" | wc -l)
file_count_cache["super$final_threshold"]=$(find "$PATH_SHARE_CACHE" -type f -size +"$final_threshold" | wc -l)

# Display results in table format
echo "-------------------------------------"
printf "| %-6s | %-10s | %-10s | %-10s |\n" "Size" "Array" "Cache: $CACHE_NAME" "Total"
echo "-------------------------------------"

for ((i = 0; i < ITERATIONS; i++)); do
    size_label="<${size_thresholds[$i]}"
    array_count=${file_count_array["sub${size_thresholds[$i]}"]}
    cache_count=${file_count_cache["sub${size_thresholds[$i]}"]}
    total_count=$((array_count + cache_count))

    printf "| %-6s | %-10s | %-10s | %-10s |\n" "$size_label" "$array_count" "$cache_count" "$total_count"
done

# Print final row for files larger than the last threshold
size_label=">${final_threshold}"
array_count=${file_count_array["super$final_threshold"]}
cache_count=${file_count_cache["super$final_threshold"]}
total_count=$((array_count + cache_count))

printf "| %-6s | %-10s | %-10s | %-10s |\n" "$size_label" "$array_count" "$cache_count" "$total_count"
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
        echo "Would Move: $src_file -> $dest_path"
    else
        mv "$src_file" "$dest_path"
        # This uses a lot of CPU time, only uncomment temporarily for detailed information
        echo "Moved: $src_file -> $dest_path"
    fi
}

export -f move_files
export PATH_SHARE_ARRAY PATH_SHARE_CACHE DRY_RUN

# Find and process files smaller than the specified size
find "$PATH_SHARE_ARRAY" -type f -size -"$SIZE_LIMIT" -print0 | while IFS= read -r -d '' file; do
    move_files "$file"
done
