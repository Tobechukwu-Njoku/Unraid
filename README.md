# Unraid

Everything I run on my Unraid server, in one place — one folder per project,
each with its own README.

## Projects

### User Scripts

For the [User Scripts](https://forums.unraid.net/topic/48286-plugin-ca-user-scripts/)
plugin. Each script is self-contained, so it can be pasted straight into the
User Scripts editor.

| Project | What it does |
| --- | --- |
| [library-cleaner](user-scripts/library-cleaner) | Renames subtitles and `.trickplay` folders to match their Radarr/Sonarr videos, with optional cleanup of orphan, duplicate and outlier subs, stale `.nfo` files and junk. Tested in CI. |
| [atomic-read-cache](user-scripts/atomic-read-cache) | Moves small files from an array-backed share onto a cache pool, with a size-threshold report and a dry-run mode. |
| [jellyfin-clean-trickplay-folders](user-scripts/jellyfin-clean-trickplay-folders) | Checks every film folder has one video and a matching `.trickplay` folder, and removes orphaned trickplay folders. |
| [jellyfin-clean-empty-folders](user-scripts/jellyfin-clean-empty-folders) | Reports film folders that contain no video file. |

### Elsewhere

| Project | Why it's separate |
| --- | --- |
| [unraid-template-normaliser](https://github.com/Tobechukwu-Njoku/unraid-template-normaliser) | A standalone tool for cleaning dockerMan templates, published and versioned on its own. |

## Layout

```
user-scripts/       one folder per User Script project
docker-templates/   dockerMan XML templates          (when there are some)
plugins/            .plg plugins and their source     (when there are some)
tools/              things that run on a dev machine, not on Unraid
.github/workflows/  one workflow per tested project, filtered by path
```

## Conventions

- Every project lives in its own folder with its own README, and its tests
  sit beside it in `tests/`.
- A project's CI workflow is named after it and only runs when that folder
  changes.
- Nothing host-specific (IPs, credentials, share names beyond defaults) is
  committed. Edit the configuration block at the top of each script.
- Commit subjects say which project they touch when it isn't obvious.

## Licence

MIT — see [LICENSE](LICENSE).
