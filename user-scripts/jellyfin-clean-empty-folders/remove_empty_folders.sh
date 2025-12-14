#!/bin/bash

MEDIA_PATH="/mnt/user/Media-Large/Films/[1080p, MN]"
MIN_SIZE_MB=20  # Adjust this value as needed
VIDEO_EXTENSIONS=('mkv' 'mp4' 'mov' 'avi' 'wmv' 'flv' 'webm' 'mpeg' 'mpg' 'ts')

# Check if media path exists
if [[ ! -d "$MEDIA_PATH" ]]; then
    echo "Error: Media path does not exist: $MEDIA_PATH"
    exit 1
fi

# Iterate through movie subfolders
for movie_folder in "$MEDIA_PATH"/*; do
    if [[ ! -d "$movie_folder" ]]; then
        continue
    fi
    
    folder_name=$(basename "$movie_folder")
    
    # Check for video file
    video_found=false
    for ext in "${VIDEO_EXTENSIONS[@]}"; do
        if [[ -f "$movie_folder"/*.$ext ]]; then
            video_found=true
            break
        fi
    done
    
    if [[ "$video_found" == false ]]; then
        echo "Error: No video file found in: $folder_name"
        
        # Get folder size in MB
        folder_size=$(du -sm "$movie_folder" | cut -f1)

        # Check if size is under minimum and delete if so
        if (( folder_size < MIN_SIZE_MB )); then
            echo "Deleting $folder_name (Size: ${folder_size}MB)"
            rm -rf "$movie_folder"
        else
            echo "Keeping $folder_name (Size: ${folder_size}MB)"
        fi
    fi
done

echo "Cleanup complete"