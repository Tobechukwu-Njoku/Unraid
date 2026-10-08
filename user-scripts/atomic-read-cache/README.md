# atomic_read_cache

A User Script that moves small files from an array-backed share onto a cache
pool, so they're read from fast storage instead of spinning up array disks.

Before moving anything it prints a table of how many files fall into each size
band. The bands start at `START_SIZE` MB and grow by `SIZE_MULTIPLIER` for
`ITERATIONS` steps, which helps when choosing a threshold.

## Usage

1. Paste `atomic_read_cache.sh` into a new User Script.
2. Set `SHARE_NAME` and `CACHE_NAME`.
3. Run with `DRY_RUN=true` (the default) and adjust `START_SIZE` until a
   sensible number of files fall under it.
4. Set `DRY_RUN=false` to move them. `CLEAN_EMPTY=true` also removes the
   directories left empty on the array.

The script refuses to run against the `appdata` share.

## Files

| File | Purpose |
| --- | --- |
| `atomic_read_cache.sh` | The User Script. |
| `testing.sh` | Scratch helper: totals the size of files under 2 MB in a directory. |
