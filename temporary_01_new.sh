#!/bin/bash

LOG_FILE="/var/log/atomic_read_cache.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

# Replace echo statements with log calls
log "Moving files smaller than $START_SIZE MB from '$PATH_SHARE_ARRAY' to '$PATH_SHARE_CACHE'"

BYTES_IN_MB=$((1024 * 1024))

generate_size_thresholds() {
    size_thresholds_bytes[0]=$((START_SIZE * BYTES_IN_MB))
    for ((i = 1; i < ITERATIONS; i++)); do
        prev_size=${size_thresholds_bytes[$((i-1))]}
        next_size=$((prev_size * SIZE_MULTIPLIER))
        size_thresholds_bytes[i]=$next_size
    done
}