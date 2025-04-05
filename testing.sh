#!/bin/bash

DIRECTORY="$1"

validate_directory() {
    local dir="$1"
    if [[ -z "$dir" || ! -d "$dir" ]]; then
        printf "Error: Invalid or missing directory '%s'\n" "$dir" >&2
        return 1
    fi
}

get_total_size_under_2mb() {
    local dir="$1"
    local total_kb; total_kb=0
    local file; local size_kb

    while IFS= read -r -d '' file; do
        if ! size_kb=$(du -k "$file" 2>/dev/null | awk '{print $1}'); then
            printf "Warning: Could not get size for '%s'\n" "$file" >&2
            continue
        fi
        total_kb=$((total_kb + size_kb))
    done < <(find "$dir" -type f -size -2048k -print0)
    
    printf "%s\n" "$total_kb"
}

main() {
    if ! validate_directory "$DIRECTORY"; then
        return 1
    fi

    local total_kb; total_kb=$(get_total_size_under_2mb "$DIRECTORY")
    if [[ -z "$total_kb" || "$total_kb" -eq 0 ]]; then
        printf "No files under 2MB found or error calculating size.\n" >&2
        return 1
    fi

    local total_mb; total_mb=$(awk -v kb="$total_kb" 'BEGIN { printf "%.2f", kb / 1024 }')
    printf "Total size of files under 2MB in '%s': %s KB (%.2f MB)\n" "$DIRECTORY" "$total_kb" "$total_mb"
}

main
