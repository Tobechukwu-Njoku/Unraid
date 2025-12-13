#!/usr/bin/env bash
set -o pipefail
set -o nounset

# Configuration
BASE_PATH="/mnt/user/Media-Large/Films"
VIDEO_EXTENSIONS=('mkv' 'mp4' 'mov' 'avi' 'wmv' 'flv' 'webm' 'mpeg' 'mpg' 'ts')
TRICKPLAY_SUFFIX='.trickplay'

# Logging functions
info() {
    local msg="$1"
    printf 'INFO: %s\n' "$msg"
}

error() {
    local msg="$1"
    printf 'ERROR: %s\n' "$msg" >&2
}

# Sanitize file paths by removing trailing whitespace
sanitize_path() {
    printf '%s' "$1" | sed 's/[[:space:]]\+$//'
}

# Find video files in the given directory
find_video_files() {
    local dir="$1"
    local ext
    local find_expr="-iname"
    
    for ext in "${VIDEO_EXTENSIONS[@]}"; do
        find_expr+=" '*.${ext}'"
        if [[ "$ext" != "${VIDEO_EXTENSIONS[-1]}" ]]; then
            find_expr+=" -o -iname"
        fi
    done
    
    eval "find \"$dir\" -maxdepth 1 -type f \( $find_expr \) -printf '%p\n'"
}

# Extract the movie filename without extension
extract_movie_filename() {
    local filepath="$1"
    local filename="${filepath##*/}"
    printf '%s' "${filename%.*}"
}

# Handle the trickplay folders for a given movie
handle_trickplay() {
    local dir="$1"
    local movie_name="$2"
    local expected_trickplay="${dir}/${movie_name}${TRICKPLAY_SUFFIX}"
    local trickplay_dirs other_dir

    # Find trickplay directories
    if ! trickplay_dirs=$(find "$dir" -maxdepth 1 -type d -name "*${TRICKPLAY_SUFFIX}" -printf '%p\n'); then
        error "Failed to scan trickplay folders in: $dir"
        return 1
    fi

    local count
    count=$(printf '%s\n' "$trickplay_dirs" | sed '/^$/d' | wc -l)

    if [[ "$count" -eq 0 ]]; then
        error "Missing trickplay folder for movie '$movie_name' in: $dir"
        return 1
    fi

    # Remove any extra trickplay folders that don't match the movie name
    while IFS= read -r other_dir; do
        if [[ "$other_dir" != "$expected_trickplay" ]]; then
            if rm -rf -- "$other_dir"; then
                info "Removed extra trickplay folder: $other_dir"
            else
                error "Failed to delete trickplay folder: $other_dir"
            fi
        fi
    done <<<"$trickplay_dirs"

    # Verify the correct trickplay folder exists
    if [[ ! -d "$expected_trickplay" ]]; then
        error "Expected trickplay folder not found: $expected_trickplay"
        return 1
    fi

    return 0
}

# Process each movie folder
process_movie_folder() {
    local dir="$1"
    local video_files movie_file movie_name
    local count

    if ! video_files=$(find_video_files "$dir"); then
        error "Failed to search video files in: $dir"
        return 1
    fi

    count=$(printf '%s\n' "$video_files" | sed '/^$/d' | wc -l)

    if [[ "$count" -eq 0 ]]; then
        error "No video file found in: $dir"
        return 1
    fi

    if [[ "$count" -gt 1 ]]; then
        error "Multiple video files found in: $dir"
        return 1
    fi

    movie_file=$(printf '%s\n' "$video_files" | head -n 1)
    movie_name=$(extract_movie_filename "$movie_file")
    
    handle_trickplay "$dir" "$movie_name"
}

# Main execution
main() {
    local movie_dir

    if [[ ! -d "$BASE_PATH" ]]; then
        error "Base path does not exist: $BASE_PATH"
        return 1
    fi

    while IFS= read -r -d '' movie_dir; do
        movie_dir=$(sanitize_path "$movie_dir")
        if [[ -d "$movie_dir" ]]; then
            process_movie_folder "$movie_dir"
        else
            error "Not a directory: $movie_dir"
        fi
    done < <(find "$BASE_PATH" -mindepth 1 -maxdepth 1 -type d -print0)
}

main
