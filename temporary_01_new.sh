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