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