#!/bin/bash
# Test harness for the Library Cleaner scripts.
# Run under GNU bash 4.1+ with GNU findutils/coreutils.
#   ./run_tests.sh /path/to/repo
set -uo pipefail

REPO="${1:?usage: run_tests.sh /path/to/repo}"
WORK="$(mktemp -d)"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

# Inject config overrides just before the END CONFIGURATION marker,
# so they win over the defaults without editing them.
gen() {
    local src="$1" dst="$2" ovfile="$3"
    awk -v f="$ovfile" '
        /^# --------------------- END CONFIGURATION/ {
            while ((getline line < f) > 0) print line
            close(f)
        }
        { print }
    ' "$src" > "$dst"
    chmod +x "$dst"
}

# List every path under a root, relative and sorted. Directories
# get a trailing slash so we can assert on them too.
snapshot() {
    ( cd "$1" && find . -mindepth 1 \
        \( -type d -printf '%p/\n' \) -o -printf '%p\n' | LC_ALL=C sort )
}

expect_snapshot() {
    local label="$1" root="$2" expected="$3" actual
    actual="$(snapshot "$root")"
    if [ "$actual" = "$expected" ]; then
        ok "$label"
    else
        bad "$label"
        diff <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") \
            | sed 's/^/       /'
    fi
}

# =====================================================================
#  FILM FIXTURE
# =====================================================================
build_film() {
    rm -rf "$WORK/films" "$WORK/films2"

    # -- Film A: subtitle + trickplay rename, art & nfo outliers, junk
    local a="$WORK/films/Film A (2020)"
    mkdir -p "$a"
    # Main feature must be the LARGEST video in the folder.
    head -c 4096 /dev/zero > "$a/Film A (2020)-Radarr.mkv"
    : > "$a/Film A (2020) [Bluray]{imdb-tt1}-Radarr.ar.hi.srt"
    : > "$a/Film A (2020) [Bluray]-Radarr.en.srt"
    mkdir -p "$a/Film A (2020) [old].trickplay"
    : > "$a/Film A (2020) [old].trickplay/thumb.jpg"
    : > "$a/poster.jpg"                          # generic art, keep
    : > "$a/fanart2.jpg"                         # numbered generic, keep
    : > "$a/Film A (2020)-Radarr-poster.jpg"     # per-video art, keep
    : > "$a/Film A (2020)-Radarr.jpg"            # kodi thumb, keep
    : > "$a/Film A (2020) [old name]-poster.jpg" # stale art, DELETE
    : > "$a/Film A (2020)-Radarr.nfo"            # keep
    : > "$a/Film A (2020) [old].nfo"             # stale nfo, DELETE
    : > "$a/.DS_Store"                           # junk, DELETE
    : > "$a/RARBG_info.txt"                      # junk, DELETE

    # -- Film B: an extra with its own subtitle must not be hijacked
    local b="$WORK/films/Film B (2021)"
    mkdir -p "$b"
    head -c 4096 /dev/zero > "$b/Film B (2021)-Radarr.mkv"
    head -c 512  /dev/zero > "$b/Film B (2021)-behindthescenes.mkv"
    : > "$b/Film B (2021)-behindthescenes.en.srt"   # must STAY
    : > "$b/Film B (2021) [junk]-Radarr.en.srt"     # -> renamed

    # -- Film C: malformed subtitles
    local c="$WORK/films/Film C (2022)"
    mkdir -p "$c"
    head -c 4096 /dev/zero > "$c/Film C (2022)-Radarr.mkv"
    : > "$c/Film C (2022)-Radarr.3.hi.srt"      # trailing numeric, DELETE
    : > "$c/Film C (2022)-Radarr.hi.hi.srt"     # duplicate suffix, DELETE

    # -- Film D: duplicate target
    local d="$WORK/films/Film D (2023)"
    mkdir -p "$d"
    head -c 4096 /dev/zero > "$d/Film D (2023)-Radarr.mkv"
    : > "$d/Film D (2023)-Radarr.en.srt"          # already correct
    : > "$d/Film D (2023) [other]-Radarr.en.srt"  # dup, DELETE

    # -- Film E: orphan subtitle (no video at all in the folder)
    mkdir -p "$WORK/films/Film E (2024)"
    : > "$WORK/films/Film E (2024)/Film E (2024).en.srt"

    # -- empty folders, including a nested chain
    mkdir -p "$WORK/films/Empty Folder"
    mkdir -p "$WORK/films/Nested/Deep/Deeper"

    # -- second root, to prove multi-root works
    local f="$WORK/films2/Film F (2025)"
    mkdir -p "$f"
    head -c 4096 /dev/zero > "$f/Film F (2025)-Radarr.mkv"
    : > "$f/Film F (2025) [x]-Radarr.en.srt"
}

echo "=== FILM ==========================================================="
build_film
BEFORE_FILM="$(snapshot "$WORK/films")"

cat > "$WORK/ov_film" <<OV
ROOT_DIRS=("$WORK/films" "$WORK/films2" "$WORK/does-not-exist")
DRY_RUN="true"
ENABLE_LOG="true"
TRASH_DIR=""
LOG_FILE="$WORK/film.log"
DELETE_ORPHANS="true"
DELETE_OUTLIERS="true"
DELETE_OUTLIER_NFO="true"
DELETE_DUPLICATES="true"
DELETE_OUTLIER_ART="true"
DELETE_JUNK="true"
PRUNE_EMPTY_DIRS="true"
OV

gen "$REPO/Library Cleaner Film" "$WORK/film.sh" "$WORK/ov_film"

# ---- dry run must change nothing ----
"$WORK/film.sh" > "$WORK/film.dry.out" 2>&1
rc=$?
[ $rc -eq 0 ] && ok "film dry-run exits 0" || bad "film dry-run exit $rc"
if [ "$(snapshot "$WORK/films")" = "$BEFORE_FILM" ]; then
    ok "film dry-run left the library untouched"
else
    bad "film dry-run MODIFIED the library"
    diff <(printf '%s\n' "$BEFORE_FILM") <(snapshot "$WORK/films") | sed 's/^/       /'
fi
grep -q "root does not exist, skipping" "$WORK/film.dry.out" \
    && ok "film reports the missing root" || bad "film missing-root warning absent"
grep -q "Root:.*/films2" "$WORK/film.dry.out" \
    && ok "film picked up the second root" || bad "film second root missing"

# ---- live run ----
sed -i 's|^DRY_RUN="true"$|DRY_RUN="false"|' "$WORK/ov_film"
gen "$REPO/Library Cleaner Film" "$WORK/film.sh" "$WORK/ov_film"
"$WORK/film.sh" > "$WORK/film.live.out" 2>&1
rc=$?
[ $rc -eq 0 ] && ok "film live run exits 0" || bad "film live run exit $rc"

read -r -d '' EXPECT_FILM <<'EXP'
./Film A (2020)/
./Film A (2020)/Film A (2020)-Radarr.ar.hi.srt
./Film A (2020)/Film A (2020)-Radarr.en.srt
./Film A (2020)/Film A (2020)-Radarr.jpg
./Film A (2020)/Film A (2020)-Radarr.mkv
./Film A (2020)/Film A (2020)-Radarr.nfo
./Film A (2020)/Film A (2020)-Radarr-poster.jpg
./Film A (2020)/Film A (2020)-Radarr.trickplay/
./Film A (2020)/Film A (2020)-Radarr.trickplay/thumb.jpg
./Film A (2020)/fanart2.jpg
./Film A (2020)/poster.jpg
./Film B (2021)/
./Film B (2021)/Film B (2021)-behindthescenes.en.srt
./Film B (2021)/Film B (2021)-behindthescenes.mkv
./Film B (2021)/Film B (2021)-Radarr.en.srt
./Film B (2021)/Film B (2021)-Radarr.mkv
./Film C (2022)/
./Film C (2022)/Film C (2022)-Radarr.mkv
./Film D (2023)/
./Film D (2023)/Film D (2023)-Radarr.en.srt
./Film D (2023)/Film D (2023)-Radarr.mkv
EXP
EXPECT_FILM="$(printf '%s\n' "$EXPECT_FILM" | LC_ALL=C sort)"
expect_snapshot "film library ends in the expected state" "$WORK/films" "$EXPECT_FILM"

read -r -d '' EXPECT_FILM2 <<'EXP'
./Film F (2025)/
./Film F (2025)/Film F (2025)-Radarr.en.srt
./Film F (2025)/Film F (2025)-Radarr.mkv
EXP
EXPECT_FILM2="$(printf '%s\n' "$EXPECT_FILM2" | LC_ALL=C sort)"
expect_snapshot "second film root cleaned too" "$WORK/films2" "$EXPECT_FILM2"

grep -q "SUB SKIP belongs to extra" "$WORK/film.live.out" \
    && ok "extra's subtitle was protected by name" \
    || bad "extra-subtitle guard did not fire"
[ -s "$WORK/film.log" ] && ok "film log file written" || bad "film log file empty"

# =====================================================================
#  TV FIXTURE
# =====================================================================
build_tv() {
    rm -rf "$WORK/tv"
    local s1="$WORK/tv/Show One (2019) [tvdbid-1]/Season 01"
    mkdir -p "$s1"
    head -c 4096 /dev/zero > "$s1/Show One - S01E01 - Pilot.mkv"
    head -c 4096 /dev/zero > "$s1/Show One - S01E02 - Second.mkv"
    : > "$s1/Show One - S01E01 - Old Pilot.en.srt"       # -> renamed
    mkdir -p "$s1/Show One - S01E01 - Old Pilot.trickplay"
    : > "$s1/Show One - S01E01 - Old Pilot.trickplay/t.jpg"
    : > "$s1/Show One - S01E02 - Second.ar.hi.srt"       # already correct
    : > "$s1/Show One - S01E99 - Ghost.en.srt"           # no video, DELETE
    : > "$s1/NoToken.en.srt"                             # no token, DELETE
    : > "$s1/season01-poster.jpg"                        # season art, keep
    : > "$s1/poster.jpg"                                 # generic art, keep
    : > "$s1/Show One - S01E01 - Pilot-thumb.jpg"        # keep
    : > "$s1/Show One - S01E01 - Old Pilot-thumb.jpg"    # stale art, DELETE
    : > "$s1/Show One - S01E01 - Pilot.nfo"              # keep
    : > "$s1/Show One - S01E01 - Old Pilot.nfo"          # stale nfo, DELETE
    : > "$s1/Thumbs.db"                                  # junk, DELETE

    local sp="$WORK/tv/Show One (2019) [tvdbid-1]/Specials"
    mkdir -p "$sp"
    head -c 4096 /dev/zero > "$sp/Show One - S00E01 - Special.mkv"
    : > "$sp/Show One - S00E01 - Old.en.srt"             # -> renamed

    # Not a season folder: must be left completely alone.
    mkdir -p "$WORK/tv/Show One (2019) [tvdbid-1]/Extras"
    : > "$WORK/tv/Show One (2019) [tvdbid-1]/Extras/stray.en.srt"
    : > "$WORK/tv/Show One (2019) [tvdbid-1]/tvshow.nfo"

    # Two videos claiming S02E01 -> ambiguous, nothing touched.
    local s2="$WORK/tv/Show Two/Season 02"
    mkdir -p "$s2"
    head -c 4096 /dev/zero > "$s2/Show Two - S02E01 - A.mkv"
    head -c 2048 /dev/zero > "$s2/Show Two - S02E01 - B.mkv"
    : > "$s2/Show Two - S02E01 - C.en.srt"
}

echo
echo "=== TV ============================================================="
build_tv
BEFORE_TV="$(snapshot "$WORK/tv")"

cat > "$WORK/ov_tv" <<OV
ROOT_DIRS=("$WORK/tv")
DRY_RUN="true"
ENABLE_LOG="true"
TRASH_DIR=""
LOG_FILE="$WORK/tv.log"
DELETE_ORPHANS="true"
DELETE_OUTLIERS="true"
DELETE_OUTLIER_NFO="true"
DELETE_DUPLICATES="true"
DELETE_OUTLIER_ART="true"
DELETE_JUNK="true"
PRUNE_EMPTY_DIRS="true"
OV

gen "$REPO/Library Cleaner TV" "$WORK/tv.sh" "$WORK/ov_tv"
"$WORK/tv.sh" > "$WORK/tv.dry.out" 2>&1
rc=$?
[ $rc -eq 0 ] && ok "tv dry-run exits 0" || bad "tv dry-run exit $rc"
if [ "$(snapshot "$WORK/tv")" = "$BEFORE_TV" ]; then
    ok "tv dry-run left the library untouched"
else
    bad "tv dry-run MODIFIED the library"
    diff <(printf '%s\n' "$BEFORE_TV") <(snapshot "$WORK/tv") | sed 's/^/       /'
fi
grep -q "Season folders found: 3" "$WORK/tv.dry.out" \
    && ok "tv found 3 season folders (Season 01, Specials, Season 02)" \
    || bad "tv season discovery wrong: $(grep 'Season folders found' "$WORK/tv.dry.out")"

sed -i 's|^DRY_RUN="true"$|DRY_RUN="false"|' "$WORK/ov_tv"
gen "$REPO/Library Cleaner TV" "$WORK/tv.sh" "$WORK/ov_tv"
"$WORK/tv.sh" > "$WORK/tv.live.out" 2>&1
rc=$?
[ $rc -eq 0 ] && ok "tv live run exits 0" || bad "tv live run exit $rc"

read -r -d '' EXPECT_TV <<'EXP'
./Show One (2019) [tvdbid-1]/
./Show One (2019) [tvdbid-1]/Extras/
./Show One (2019) [tvdbid-1]/Extras/stray.en.srt
./Show One (2019) [tvdbid-1]/Season 01/
./Show One (2019) [tvdbid-1]/Season 01/poster.jpg
./Show One (2019) [tvdbid-1]/Season 01/season01-poster.jpg
./Show One (2019) [tvdbid-1]/Season 01/Show One - S01E01 - Pilot.en.srt
./Show One (2019) [tvdbid-1]/Season 01/Show One - S01E01 - Pilot.mkv
./Show One (2019) [tvdbid-1]/Season 01/Show One - S01E01 - Pilot.nfo
./Show One (2019) [tvdbid-1]/Season 01/Show One - S01E01 - Pilot-thumb.jpg
./Show One (2019) [tvdbid-1]/Season 01/Show One - S01E01 - Pilot.trickplay/
./Show One (2019) [tvdbid-1]/Season 01/Show One - S01E01 - Pilot.trickplay/t.jpg
./Show One (2019) [tvdbid-1]/Season 01/Show One - S01E02 - Second.ar.hi.srt
./Show One (2019) [tvdbid-1]/Season 01/Show One - S01E02 - Second.mkv
./Show One (2019) [tvdbid-1]/Specials/
./Show One (2019) [tvdbid-1]/Specials/Show One - S00E01 - Special.en.srt
./Show One (2019) [tvdbid-1]/Specials/Show One - S00E01 - Special.mkv
./Show One (2019) [tvdbid-1]/tvshow.nfo
./Show Two/
./Show Two/Season 02/
./Show Two/Season 02/Show Two - S02E01 - A.mkv
./Show Two/Season 02/Show Two - S02E01 - B.mkv
./Show Two/Season 02/Show Two - S02E01 - C.en.srt
EXP
EXPECT_TV="$(printf '%s\n' "$EXPECT_TV" | LC_ALL=C sort)"
expect_snapshot "tv library ends in the expected state" "$WORK/tv" "$EXPECT_TV"

grep -q "SUB SKIP duplicate ep S02E01" "$WORK/tv.live.out" \
    && ok "ambiguous duplicate episode was skipped" \
    || bad "ambiguous duplicate episode not skipped"

# =====================================================================
#  NFO CLEANER
# =====================================================================
echo
echo "=== NFO CLEANER ===================================================="
rm -rf "$WORK/nfo1" "$WORK/nfo2"
mkdir -p "$WORK/nfo1/a" "$WORK/nfo2/b"
: > "$WORK/nfo1/a/one.nfo"
: > "$WORK/nfo1/a/two.NFO"
: > "$WORK/nfo1/a/three.nfo-orig"
: > "$WORK/nfo1/a/keep.mkv"
: > "$WORK/nfo2/b/four.nfo"
: > "$WORK/nfo2/b/keep.mkv"

cat > "$WORK/ov_nfo" <<OV
ROOT_DIRS=("$WORK/nfo1" "$WORK/nfo2" "$WORK/does-not-exist")
DRY_RUN="true"
ENABLE_LOG="true"
TRASH_DIR=""
OV
gen "$REPO/NFO Cleaner" "$WORK/nfo.sh" "$WORK/ov_nfo"
"$WORK/nfo.sh" > "$WORK/nfo.dry.out" 2>&1
grep -q "WOULD be deleted: 4" "$WORK/nfo.dry.out" \
    && ok "nfo dry-run counts 4 across both roots" \
    || bad "nfo dry-run count wrong: $(grep 'WOULD be deleted' "$WORK/nfo.dry.out")"
[ -f "$WORK/nfo1/a/one.nfo" ] && ok "nfo dry-run deleted nothing" \
    || bad "nfo dry-run deleted files"

sed -i 's|^DRY_RUN="true"$|DRY_RUN="false"|' "$WORK/ov_nfo"
gen "$REPO/NFO Cleaner" "$WORK/nfo.sh" "$WORK/ov_nfo"
"$WORK/nfo.sh" > "$WORK/nfo.live.out" 2>&1
remaining="$(find "$WORK/nfo1" "$WORK/nfo2" -type f | LC_ALL=C sort | sed "s|$WORK/||")"
if [ "$remaining" = "nfo1/a/keep.mkv
nfo2/b/keep.mkv" ]; then
    ok "nfo live run removed exactly the .nfo files"
else
    bad "nfo live run left: $remaining"
fi


# =====================================================================
#  GLOB-METACHARACTER PATHS
#  Real roots look like "/mnt/user/Media-Large/Films/[1080p, OF]".
#  "[...]" is a bracket expression, so prove quoting holds throughout.
# =====================================================================
echo
echo "=== BRACKETED ROOTS ================================================"
rm -rf "$WORK/br"
FR="$WORK/br/Media-Large/Films/[1080p, OF]"
FR2="$WORK/br/Media-Huge/Films/[2160p, RX]"
mkdir -p "$FR/Film A (2020)" "$FR2/Film B (2021)"
head -c 4096 /dev/zero > "$FR/Film A (2020)/Film A (2020)-Radarr.mkv"
: > "$FR/Film A (2020)/Film A (2020) [Bluray]-Radarr.en.srt"
mkdir -p "$FR/Film A (2020)/Film A (2020) [old].trickplay"
: > "$FR/Film A (2020)/Film A (2020) [old].trickplay/t.jpg"
: > "$FR/Film A (2020)/Film A (2020) [stale]-poster.jpg"
: > "$FR/Film A (2020)/poster.jpg"
head -c 4096 /dev/zero > "$FR2/Film B (2021)/Film B (2021)-Radarr.mkv"
: > "$FR2/Film B (2021)/Film B (2021) [x]-Radarr.en.srt"

cat > "$WORK/ov_br" <<OV
ROOT_DIRS=("$FR" "$FR2")
DRY_RUN="false"
ENABLE_LOG="true"
TRASH_DIR=""
LOG_FILE="$WORK/br.log"
DELETE_OUTLIER_ART="true"
DELETE_JUNK="true"
PRUNE_EMPTY_DIRS="true"
OV
gen "$REPO/Library Cleaner Film" "$WORK/br.sh" "$WORK/ov_br"
"$WORK/br.sh" > "$WORK/br.out" 2>&1

[ -f "$FR/Film A (2020)/Film A (2020)-Radarr.en.srt" ] \
    && ok "subtitle renamed inside a [bracketed] root" \
    || bad "subtitle NOT renamed inside a [bracketed] root"
[ -d "$FR/Film A (2020)/Film A (2020)-Radarr.trickplay" ] \
    && ok "trickplay renamed inside a [bracketed] root" \
    || bad "trickplay NOT renamed inside a [bracketed] root"
[ ! -e "$FR/Film A (2020)/Film A (2020) [stale]-poster.jpg" ] \
    && ok "stale art removed inside a [bracketed] root" \
    || bad "stale art NOT removed inside a [bracketed] root"
[ -f "$FR/Film A (2020)/poster.jpg" ] \
    && ok "generic art kept inside a [bracketed] root" \
    || bad "generic art wrongly removed"
[ -f "$FR2/Film B (2021)/Film B (2021)-Radarr.en.srt" ] \
    && ok "second [bracketed] root processed" \
    || bad "second [bracketed] root NOT processed"

TR="$WORK/br/Media-Large/Shows/[2160p, MN]"
mkdir -p "$TR/Show One [tvdbid-1]/Season 01"
head -c 4096 /dev/zero > "$TR/Show One [tvdbid-1]/Season 01/Show One - S01E01 - Pilot.mkv"
: > "$TR/Show One [tvdbid-1]/Season 01/Show One - S01E01 - Old.en.srt"
cat > "$WORK/ov_brtv" <<OV
ROOT_DIRS=("$TR")
DRY_RUN="false"
ENABLE_LOG="true"
TRASH_DIR=""
LOG_FILE="$WORK/brtv.log"
OV
gen "$REPO/Library Cleaner TV" "$WORK/brtv.sh" "$WORK/ov_brtv"
"$WORK/brtv.sh" > "$WORK/brtv.out" 2>&1
grep -q "Season folders found: 1" "$WORK/brtv.out" \
    && ok "tv season discovery works under a [bracketed] root" \
    || bad "tv season discovery broken under a [bracketed] root"
[ -f "$TR/Show One [tvdbid-1]/Season 01/Show One - S01E01 - Pilot.en.srt" ] \
    && ok "tv subtitle renamed inside a [bracketed] root" \
    || bad "tv subtitle NOT renamed inside a [bracketed] root"

: > "$FR/Film A (2020)/stale.nfo"
cat > "$WORK/ov_brnfo" <<OV
ROOT_DIRS=("$WORK/br/Media-Large" "$WORK/br/Media-Huge")
DRY_RUN="false"
ENABLE_LOG="true"
TRASH_DIR=""
OV
gen "$REPO/NFO Cleaner" "$WORK/brnfo.sh" "$WORK/ov_brnfo"
"$WORK/brnfo.sh" > "$WORK/brnfo.out" 2>&1
[ ! -e "$FR/Film A (2020)/stale.nfo" ] \
    && ok "nfo cleaner reaches into [bracketed] subfolders" \
    || bad "nfo cleaner missed a [bracketed] path"


# =====================================================================
#  LANGUAGE CODES AND DUPLICATE RESOLUTION
# =====================================================================
echo
echo "=== LANGUAGES ======================================================"
lang_fixture() {
    rm -rf "$WORK/lang"
    local d="$WORK/lang/Film (2020)"
    mkdir -p "$d"
    head -c 4096 /dev/zero > "$d/Film (2020)-Radarr.mkv"
    # Release tags that are NOT languages.
    : > "$d/Film.2020.1080p.WEB.DDP.srt"
    : > "$d/Film.2020.HDR.ass"
    # Genuine language codes, which must survive.
    : > "$d/Film.2020.BluRay.x264-GRP.eng.srt"
    : > "$d/Film (2020) [x]-Radarr.pt-br.srt"
    : > "$d/Film (2020) [y]-Radarr.ar.hi.srt"
}
lang_fixture
cat > "$WORK/ov_lang" <<OV
ROOT_DIRS=("$WORK/lang")
DRY_RUN="false"
ENABLE_LOG="false"
TRASH_DIR=""
OV
gen "$REPO/Library Cleaner Film" "$WORK/lang.sh" "$WORK/ov_lang"
"$WORK/lang.sh" > "$WORK/lang.out" 2>&1
got="$(cd "$WORK/lang/Film (2020)" && ls | LC_ALL=C sort | tr '\n' ' ')"

for fake in web ddp hdr aac; do
    if printf '%s' "$got" | grep -q "\.$fake\."; then
        bad "release tag '$fake' was treated as a language"
    else
        ok "release tag '$fake' not treated as a language"
    fi
done
[ -f "$WORK/lang/Film (2020)/Film (2020)-Radarr.eng.srt" ] \
    && ok "ISO 639-2 code 'eng' preserved" || bad "'eng' lost: $got"
[ -f "$WORK/lang/Film (2020)/Film (2020)-Radarr.pt-br.srt" ] \
    && ok "regional code 'pt-br' preserved" || bad "'pt-br' lost: $got"
[ -f "$WORK/lang/Film (2020)/Film (2020)-Radarr.ar.hi.srt" ] \
    && ok "language+flag 'ar.hi' preserved" || bad "'ar.hi' lost: $got"

echo
echo "=== DUPLICATE RESOLUTION ==========================================="
# Two subs that both resolve to the same target. The bigger one is
# the better subtitle and must be the survivor, whichever order the
# shell happens to reach them in.
dup_fixture() {
    rm -rf "$WORK/dup"
    local d="$WORK/dup/Film (2020)"
    mkdir -p "$d"
    head -c 4096 /dev/zero > "$d/Film (2020)-Radarr.mkv"
    head -c 100  /dev/zero > "$d/Film.2020.WEB.DDP.srt"     # small
    head -c 5000 /dev/zero > "$d/Film.2020.HDR.srt"         # large
}

dup_fixture
cat > "$WORK/ov_dup" <<OV
ROOT_DIRS=("$WORK/dup")
DRY_RUN="false"
ENABLE_LOG="false"
TRASH_DIR=""
DELETE_DUPLICATES="true"
DUPLICATE_KEEP="largest"
OV
gen "$REPO/Library Cleaner Film" "$WORK/dup.sh" "$WORK/ov_dup"
"$WORK/dup.sh" > "$WORK/dup.out" 2>&1
target="$WORK/dup/Film (2020)/Film (2020)-Radarr.srt"
if [ -f "$target" ] && [ "$(stat -c%s "$target")" -eq 5000 ]; then
    ok "DUPLICATE_KEEP=largest kept the bigger subtitle"
else
    bad "DUPLICATE_KEEP=largest kept the wrong file (size $(stat -c%s "$target" 2>/dev/null))"
fi
n="$(find "$WORK/dup/Film (2020)" -name '*.srt' | wc -l)"
[ "$n" -eq 1 ] && ok "collision left exactly one subtitle" \
                || bad "collision left $n subtitles"

dup_fixture
sed 's|^DUPLICATE_KEEP=.*|DUPLICATE_KEEP="existing"|' "$WORK/ov_dup" > "$WORK/ov_dup2"
gen "$REPO/Library Cleaner Film" "$WORK/dup2.sh" "$WORK/ov_dup2"
"$WORK/dup2.sh" > "$WORK/dup2.out" 2>&1
# "existing" keeps whichever landed first, so only assert that it
# resolved to a single file rather than which one won.
n="$(find "$WORK/dup/Film (2020)" -name '*.srt' | wc -l)"
[ "$n" -eq 1 ] && ok "DUPLICATE_KEEP=existing also resolves to one file" \
                || bad "DUPLICATE_KEEP=existing left $n subtitles"

# With DELETE_DUPLICATES off, nothing is removed.
dup_fixture
cat > "$WORK/ov_dup3" <<OV
ROOT_DIRS=("$WORK/dup")
DRY_RUN="false"
ENABLE_LOG="false"
TRASH_DIR=""
DELETE_DUPLICATES="false"
OV
gen "$REPO/Library Cleaner Film" "$WORK/dup3.sh" "$WORK/ov_dup3"
"$WORK/dup3.sh" > "$WORK/dup3.out" 2>&1
n="$(find "$WORK/dup/Film (2020)" -name '*.srt' | wc -l)"
[ "$n" -eq 2 ] && ok "DELETE_DUPLICATES=false deletes nothing on collision" \
                || bad "DELETE_DUPLICATES=false left $n subtitles, expected 2"


# =====================================================================
#  QUARANTINE
# =====================================================================
echo
echo "=== QUARANTINE ====================================================="
rm -rf "$WORK/q" "$WORK/qtrash"
qd="$WORK/q/films/Film E (2024)"
mkdir -p "$qd"
: > "$qd/Film E (2024).en.srt"          # orphan: no video in the folder
mkdir -p "$WORK/q/films/Film F (2025)"
head -c 4096 /dev/zero > "$WORK/q/films/Film F (2025)/Film F (2025)-Radarr.mkv"
: > "$WORK/q/films/Film F (2025)/.DS_Store"

cat > "$WORK/ov_q" <<OV
ROOT_DIRS=("$WORK/q/films")
DRY_RUN="false"
ENABLE_LOG="true"
LOG_FILE="$WORK/q.log"
TRASH_DIR="$WORK/qtrash"
DELETE_ORPHANS="true"
DELETE_JUNK="true"
OV
gen "$REPO/Library Cleaner Film" "$WORK/q.sh" "$WORK/ov_q"
"$WORK/q.sh" > "$WORK/q.out" 2>&1

[ ! -e "$qd/Film E (2024).en.srt" ] \
    && ok "quarantined orphan left the library" \
    || bad "quarantined orphan still in the library"
found="$(find "$WORK/qtrash" -name 'Film E (2024).en.srt' | head -1)"
[ -n "$found" ] && ok "orphan recoverable from the trash" \
                || bad "orphan NOT found under the trash dir"
case "$found" in
    */Film\ E\ \(2024\)/Film\ E\ \(2024\).en.srt)
        ok "trash preserves the original folder structure" ;;
    *)  bad "trash path lost its structure: $found" ;;
esac
[ -n "$(find "$WORK/qtrash" -name '.DS_Store' | head -1)" ] \
    && ok "quarantined junk recoverable too" \
    || bad "junk was not quarantined"
grep -q "TRASHED" "$WORK/q.out" \
    && ok "log says TRASHED rather than DELETED" \
    || bad "log did not report quarantining"
grep -q "Quarantined files are under:" "$WORK/q.out" \
    && ok "summary points at the trash folder" \
    || bad "summary omits the trash location"

# A trash dir inside a library root defeats the safety net, because
# the junk and empty-folder passes would walk straight back into it.
cat > "$WORK/ov_qbad" <<OV
ROOT_DIRS=("$WORK/q/films")
DRY_RUN="true"
ENABLE_LOG="true"
LOG_FILE="$WORK/qbad.log"
TRASH_DIR="$WORK/q/films/.trash"
OV
gen "$REPO/Library Cleaner Film" "$WORK/qbad.sh" "$WORK/ov_qbad"
"$WORK/qbad.sh" > "$WORK/qbad.out" 2>&1
rc=$?
[ "$rc" -ne 0 ] && ok "refuses to run with TRASH_DIR inside a root" \
                || bad "accepted a TRASH_DIR inside a library root"
grep -q "TRASH_DIR is inside a library root" "$WORK/qbad.out" \
    && ok "explains why it refused" || bad "refusal message missing"

# Dry run must not move anything.
rm -rf "$WORK/q2" "$WORK/q2trash"
mkdir -p "$WORK/q2/films/Film G (2026)"
: > "$WORK/q2/films/Film G (2026)/Film G (2026).en.srt"
cat > "$WORK/ov_q2" <<OV
ROOT_DIRS=("$WORK/q2/films")
DRY_RUN="true"
ENABLE_LOG="false"
TRASH_DIR="$WORK/q2trash"
DELETE_ORPHANS="true"
OV
gen "$REPO/Library Cleaner Film" "$WORK/q2.sh" "$WORK/ov_q2"
"$WORK/q2.sh" > /dev/null 2>&1
[ -f "$WORK/q2/films/Film G (2026)/Film G (2026).en.srt" ] && [ ! -d "$WORK/q2trash" ] \
    && ok "dry run quarantines nothing and creates no trash folder" \
    || bad "dry run touched files or created a trash folder"


# NFO Cleaner honours the same safety net.
rm -rf "$WORK/nfoq" "$WORK/nfoqtrash"
mkdir -p "$WORK/nfoq/a"
: > "$WORK/nfoq/a/stale.nfo"
: > "$WORK/nfoq/a/keep.mkv"
cat > "$WORK/ov_nfoq" <<OV
ROOT_DIRS=("$WORK/nfoq")
DRY_RUN="false"
ENABLE_LOG="true"
TRASH_DIR="$WORK/nfoqtrash"
OV
gen "$REPO/NFO Cleaner" "$WORK/nfoq.sh" "$WORK/ov_nfoq"
"$WORK/nfoq.sh" > "$WORK/nfoq.out" 2>&1
[ ! -e "$WORK/nfoq/a/stale.nfo" ] && [ -n "$(find "$WORK/nfoqtrash" -name 'stale.nfo' | head -1)" ] \
    && ok "nfo cleaner quarantines instead of deleting" \
    || bad "nfo cleaner did not quarantine"
[ -f "$WORK/nfoq/a/keep.mkv" ] && ok "nfo cleaner left the video alone" \
                               || bad "nfo cleaner touched the video"


# =====================================================================
#  SYMLINKED ROOTS AND SINGLE-INSTANCE LOCK
# =====================================================================
echo
echo "=== SYMLINKS AND LOCKING ==========================================="
sym_fixture() {
    rm -rf "$WORK/sym"
    mkdir -p "$WORK/sym/real/Film A (2020)"
    head -c 4096 /dev/zero > "$WORK/sym/real/Film A (2020)/Film A (2020)-Radarr.mkv"
    : > "$WORK/sym/real/Film A (2020)/Film A (2020) [x]-Radarr.en.srt"
    ln -s "$WORK/sym/real" "$WORK/sym/link"
}

sym_fixture
cat > "$WORK/ov_sym" <<OV
ROOT_DIRS=("$WORK/sym/link")
DRY_RUN="false"
ENABLE_LOG="true"
TRASH_DIR=""
LOCK_FILE=""
LOG_FILE="$WORK/sym.log"
FOLLOW_SYMLINKS="false"
OV
gen "$REPO/Library Cleaner Film" "$WORK/sym.sh" "$WORK/ov_sym"
"$WORK/sym.sh" > "$WORK/sym.out" 2>&1
grep -q "that root is a symlink" "$WORK/sym.out" \
    && ok "warns that a symlinked root will yield nothing" \
    || bad "no warning for a symlinked root"
[ -f "$WORK/sym/real/Film A (2020)/Film A (2020) [x]-Radarr.en.srt" ] \
    && ok "symlinked root is indeed skipped when not following" \
    || bad "symlinked root was processed despite FOLLOW_SYMLINKS=false"

sym_fixture
sed 's|^FOLLOW_SYMLINKS=.*|FOLLOW_SYMLINKS="true"|' "$WORK/ov_sym" > "$WORK/ov_sym2"
gen "$REPO/Library Cleaner Film" "$WORK/sym2.sh" "$WORK/ov_sym2"
"$WORK/sym2.sh" > "$WORK/sym2.out" 2>&1
[ -f "$WORK/sym/real/Film A (2020)/Film A (2020)-Radarr.en.srt" ] \
    && ok "FOLLOW_SYMLINKS=true walks a symlinked root" \
    || bad "FOLLOW_SYMLINKS=true did not walk the symlinked root"

if command -v flock >/dev/null 2>&1; then
    rm -rf "$WORK/lk"; mkdir -p "$WORK/lk/Film A (2020)"
    head -c 4096 /dev/zero > "$WORK/lk/Film A (2020)/Film A (2020)-Radarr.mkv"
    cat > "$WORK/ov_lk" <<OV
ROOT_DIRS=("$WORK/lk")
DRY_RUN="true"
ENABLE_LOG="true"
TRASH_DIR=""
LOG_FILE="$WORK/lk.log"
LOCK_FILE="$WORK/lk.lock"
OV
    gen "$REPO/Library Cleaner Film" "$WORK/lk.sh" "$WORK/ov_lk"

    "$WORK/lk.sh" > "$WORK/lk.out" 2>&1
    [ $? -eq 0 ] && ok "runs normally when the lock is free" \
                 || bad "failed to run with a free lock"

    # Hold the lock, then confirm a second run refuses to start.
    exec 200>"$WORK/lk.lock"
    flock -n 200
    "$WORK/lk.sh" > "$WORK/lk2.out" 2>&1
    rc=$?
    exec 200>&-
    [ "$rc" -ne 0 ] && ok "refuses to start while another run holds the lock" \
                    || bad "second run started despite the lock"
    grep -q "another run is already in progress" "$WORK/lk2.out" \
        && ok "explains that a run is already in progress" \
        || bad "lock refusal message missing"

    # And the lock is released afterwards.
    "$WORK/lk.sh" > "$WORK/lk3.out" 2>&1
    [ $? -eq 0 ] && ok "lock is released when the run finishes" \
                 || bad "lock was not released"
else
    ok "flock unavailable in this image - lock tests skipped"
fi


# =====================================================================
#  MULTI-EPISODE FILES
# =====================================================================
echo
echo "=== MULTI-EPISODE =================================================="
rm -rf "$WORK/me"
ms="$WORK/me/Show/Season 01"
mkdir -p "$ms"
head -c 4096 /dev/zero > "$ms/Show - S01E01-E02 - Double.mkv"
: > "$ms/Show - S01E01.en.srt"
: > "$ms/Show - S01E02.en.srt"
# A decoy that must not be read as episodes 1..720.
head -c 4096 /dev/zero > "$ms/Show - S01E05 - Solo.mkv"
: > "$ms/Show - S01E05.en.srt"

cat > "$WORK/ov_me" <<OV
ROOT_DIRS=("$WORK/me")
DRY_RUN="false"
ENABLE_LOG="true"
TRASH_DIR=""
LOCK_FILE=""
LOG_FILE="$WORK/me.log"
DELETE_ORPHANS="true"
DELETE_DUPLICATES="false"
OV
gen "$REPO/Library Cleaner TV" "$WORK/me.sh" "$WORK/ov_me"
"$WORK/me.sh" > "$WORK/me.out" 2>&1

n="$(find "$ms" -name '*.srt' | wc -l)"
[ "$n" -eq 3 ] \
    && ok "multi-episode subtitles survive the orphan pass" \
    || bad "orphan pass destroyed subtitles: $n of 3 left"
[ -f "$ms/Show - S01E01-E02 - Double.en.srt" ] \
    && ok "first episode's subtitle renamed onto the double file" \
    || bad "S01E01 subtitle not renamed"
[ -f "$ms/Show - S01E05 - Solo.en.srt" ] \
    && ok "an unrelated single episode is unaffected" \
    || bad "S01E05 subtitle not renamed"
# The second episode's subtitle wants the same name as the first, so
# it is a genuine duplicate. It must be left alone, not deleted.
grep -q "SUB SKIP exists" "$WORK/me.out" \
    && ok "second episode's subtitle kept as a collision, not deleted" \
    || bad "second episode's subtitle was not reported as a collision"


# =====================================================================
#  VOBSUB PAIRS
#  .idx is a small index, .sub the large payload. They must stay
#  together and come from the same release.
# =====================================================================
echo
echo "=== VOBSUB PAIRS ==================================================="
vob_fixture() {
    rm -rf "$WORK/vob"
    local d="$WORK/vob/Film (2020)"
    mkdir -p "$d"
    head -c 4096 /dev/zero > "$d/Film (2020)-Radarr.mkv"
    # Release A: small index, large payload  (total 9100)
    head -c 100  /dev/zero > "$d/ReleaseA.en.idx"
    head -c 9000 /dev/zero > "$d/ReleaseA.en.sub"
    # Release B: large index, small payload  (total 1400)
    head -c 900  /dev/zero > "$d/ReleaseB.en.idx"
    head -c 500  /dev/zero > "$d/ReleaseB.en.sub"
}

vob_fixture
cat > "$WORK/ov_vob" <<OV
ROOT_DIRS=("$WORK/vob")
DRY_RUN="false"
ENABLE_LOG="true"
TRASH_DIR=""
LOCK_FILE=""
LOG_FILE="$WORK/vob.log"
DELETE_DUPLICATES="true"
DUPLICATE_KEEP="largest"
OV
gen "$REPO/Library Cleaner Film" "$WORK/vob.sh" "$WORK/ov_vob"
"$WORK/vob.sh" > "$WORK/vob.out" 2>&1
vd="$WORK/vob/Film (2020)"
isz="$(stat -c%s "$vd/Film (2020)-Radarr.en.idx" 2>/dev/null || echo 0)"
ssz="$(stat -c%s "$vd/Film (2020)-Radarr.en.sub" 2>/dev/null || echo 0)"
if [ "$isz" -eq 100 ] && [ "$ssz" -eq 9000 ]; then
    ok "winning pair came from one release (idx $isz + sub $ssz)"
else
    bad "pair is mismatched: idx $isz, sub $ssz (A=100/9000, B=900/500)"
fi
n="$(find "$vd" \( -name '*.idx' -o -name '*.sub' \) | wc -l)"
[ "$n" -eq 2 ] && ok "exactly one pair survives" || bad "$n VobSub files left, expected 2"

# Both halves must move even when only one needs renaming.
vob_fixture
rm "$WORK/vob/Film (2020)/ReleaseB.en.idx" "$WORK/vob/Film (2020)/ReleaseB.en.sub"
gen "$REPO/Library Cleaner Film" "$WORK/vob2.sh" "$WORK/ov_vob"
"$WORK/vob2.sh" > "$WORK/vob2.out" 2>&1
[ -f "$vd/Film (2020)-Radarr.en.idx" ] && [ -f "$vd/Film (2020)-Radarr.en.sub" ] \
    && ok "both halves renamed together" \
    || bad "pair was split by the rename"

# A .sub with no .idx is a standalone text subtitle, not a VobSub.
rm -rf "$WORK/vob3"; d3="$WORK/vob3/Film (2020)"; mkdir -p "$d3"
head -c 4096 /dev/zero > "$d3/Film (2020)-Radarr.mkv"
: > "$d3/Whatever.en.sub"
cat > "$WORK/ov_vob3" <<OV
ROOT_DIRS=("$WORK/vob3")
DRY_RUN="false"
ENABLE_LOG="false"
TRASH_DIR=""
LOCK_FILE=""
OV
gen "$REPO/Library Cleaner Film" "$WORK/vob3.sh" "$WORK/ov_vob3"
"$WORK/vob3.sh" > /dev/null 2>&1
[ -f "$d3/Film (2020)-Radarr.en.sub" ] \
    && ok "a lone .sub is still handled as a normal subtitle" \
    || bad "lone .sub was not renamed"


# =====================================================================
#  Subs/ SUBFOLDER
# =====================================================================
echo
echo "=== SUBS SUBFOLDER ================================================="
subs_fixture() {
    rm -rf "$WORK/sf"
    local d="$WORK/sf/Film (2020)"
    mkdir -p "$d/Subs"
    head -c 4096 /dev/zero > "$d/Film (2020)-Radarr.mkv"
    : > "$d/Subs/2_English.srt"
    : > "$d/Subs/3_French SDH.srt"
    : > "$d/Subs/4_Brazilian Portuguese.srt"
    : > "$d/Subs/Movie.es.srt"          # already carries a code
    : > "$d/Subs/5_Klingon.srt"         # unknown language
}

subs_fixture
sd="$WORK/sf/Film (2020)"
cat > "$WORK/ov_sf" <<OV
ROOT_DIRS=("$WORK/sf")
DRY_RUN="false"
ENABLE_LOG="true"
TRASH_DIR=""
LOCK_FILE=""
LOG_FILE="$WORK/sf.log"
DELETE_ORPHANS="true"
PROMOTE_SUBS_FOLDER="true"
OV
gen "$REPO/Library Cleaner Film" "$WORK/sf.sh" "$WORK/ov_sf"
"$WORK/sf.sh" > "$WORK/sf.out" 2>&1

for want in "Film (2020)-Radarr.en.srt" \
            "Film (2020)-Radarr.fr.sdh.srt" \
            "Film (2020)-Radarr.pt-br.srt" \
            "Film (2020)-Radarr.es.srt"; do
    [ -f "$sd/$want" ] && ok "promoted: $want" || bad "missing: $want"
done
[ -f "$sd/Subs/5_Klingon.srt" ] \
    && ok "unidentifiable language left in place, not deleted" \
    || bad "unidentifiable subtitle was removed"

# The protection must hold even with promotion switched off.
subs_fixture
sed 's|^PROMOTE_SUBS_FOLDER=.*|PROMOTE_SUBS_FOLDER="false"|' "$WORK/ov_sf" > "$WORK/ov_sf2"
gen "$REPO/Library Cleaner Film" "$WORK/sf2.sh" "$WORK/ov_sf2"
"$WORK/sf2.sh" > "$WORK/sf2.out" 2>&1
n="$(find "$sd/Subs" -name '*.srt' 2>/dev/null | wc -l)"
[ "$n" -eq 5 ] \
    && ok "orphan pass leaves a Subs/ folder alone when not promoting" \
    || bad "orphan pass removed $((5-n)) of 5 subtitles from Subs/"
grep -q "ORPHAN SKIP subtitles subfolder" "$WORK/sf2.out" \
    && ok "logs why the Subs/ folder was skipped" \
    || bad "no log line explaining the Subs/ skip"

# A genuinely orphaned folder is still cleaned up.
rm -rf "$WORK/sf3"; mkdir -p "$WORK/sf3/Loose"
: > "$WORK/sf3/Loose/whatever.en.srt"
cat > "$WORK/ov_sf3" <<OV
ROOT_DIRS=("$WORK/sf3")
DRY_RUN="false"
ENABLE_LOG="false"
TRASH_DIR=""
LOCK_FILE=""
DELETE_ORPHANS="true"
OV
gen "$REPO/Library Cleaner Film" "$WORK/sf3.sh" "$WORK/ov_sf3"
"$WORK/sf3.sh" > /dev/null 2>&1
[ ! -e "$WORK/sf3/Loose/whatever.en.srt" ] \
    && ok "a real orphan is still deleted" || bad "orphan pass stopped working"


# =====================================================================
#  IDEMPOTENCY
#  Running twice must be indistinguishable from running once. This
#  is the invariant that makes the scripts safe to schedule.
# =====================================================================
echo
echo "=== IDEMPOTENCY ===================================================="
build_film
cat > "$WORK/ov_idem" <<OV
ROOT_DIRS=("$WORK/films" "$WORK/films2")
DRY_RUN="false"
ENABLE_LOG="false"
TRASH_DIR=""
LOCK_FILE=""
DELETE_ORPHANS="true"
DELETE_OUTLIERS="true"
DELETE_OUTLIER_NFO="true"
DELETE_DUPLICATES="true"
DELETE_OUTLIER_ART="true"
DELETE_JUNK="true"
PRUNE_EMPTY_DIRS="true"
OV
gen "$REPO/Library Cleaner Film" "$WORK/idem.sh" "$WORK/ov_idem"
"$WORK/idem.sh" > /dev/null 2>&1
first="$(snapshot "$WORK/films")"
"$WORK/idem.sh" > /dev/null 2>&1
second="$(snapshot "$WORK/films")"
if [ "$first" = "$second" ]; then
    ok "film script is idempotent"
else
    bad "film script changed the library on a second run"
    diff <(printf '%s\n' "$first") <(printf '%s\n' "$second") | sed 's/^/       /'
fi

build_tv
cat > "$WORK/ov_idemtv" <<OV
ROOT_DIRS=("$WORK/tv")
DRY_RUN="false"
ENABLE_LOG="false"
TRASH_DIR=""
LOCK_FILE=""
DELETE_ORPHANS="true"
DELETE_OUTLIERS="true"
DELETE_OUTLIER_NFO="true"
DELETE_DUPLICATES="true"
DELETE_OUTLIER_ART="true"
DELETE_JUNK="true"
PRUNE_EMPTY_DIRS="true"
OV
gen "$REPO/Library Cleaner TV" "$WORK/idemtv.sh" "$WORK/ov_idemtv"
"$WORK/idemtv.sh" > /dev/null 2>&1
first="$(snapshot "$WORK/tv")"
"$WORK/idemtv.sh" > /dev/null 2>&1
second="$(snapshot "$WORK/tv")"
if [ "$first" = "$second" ]; then
    ok "tv script is idempotent"
else
    bad "tv script changed the library on a second run"
    diff <(printf '%s\n' "$first") <(printf '%s\n' "$second") | sed 's/^/       /'
fi


# =====================================================================
#  TRASH RETENTION
# =====================================================================
echo
echo "=== TRASH RETENTION ================================================"
retention_fixture() {
    rm -rf "$WORK/rt" "$WORK/rttrash"
    mkdir -p "$WORK/rt/Film A (2020)"
    head -c 4096 /dev/zero > "$WORK/rt/Film A (2020)/Film A (2020)-Radarr.mkv"
    # Old run, recent run, and something that is not a run folder.
    mkdir -p "$WORK/rttrash/2020-01-01_00-00-00/mnt/old"
    : > "$WORK/rttrash/2020-01-01_00-00-00/mnt/old/ancient.srt"
    mkdir -p "$WORK/rttrash/$(date '+%Y-%m-%d_%H-%M-%S')/mnt/new"
    mkdir -p "$WORK/rttrash/my-own-notes"
    : > "$WORK/rttrash/my-own-notes/keep-me.txt"
}

retention_fixture
cat > "$WORK/ov_rt" <<OV
ROOT_DIRS=("$WORK/rt")
DRY_RUN="false"
ENABLE_LOG="true"
LOG_FILE="$WORK/rt.log"
LOCK_FILE=""
TRASH_DIR="$WORK/rttrash"
TRASH_KEEP_DAYS="30"
OV
gen "$REPO/Library Cleaner Film" "$WORK/rt.sh" "$WORK/ov_rt"
"$WORK/rt.sh" > "$WORK/rt.out" 2>&1

[ ! -d "$WORK/rttrash/2020-01-01_00-00-00" ] \
    && ok "trash run older than the retention window is pruned" \
    || bad "old trash run survived"
[ -n "$(find "$WORK/rttrash" -maxdepth 1 -name "$(date '+%Y')-*" -type d | head -1)" ] \
    && ok "recent trash run is kept" || bad "recent trash run was pruned"
[ -f "$WORK/rttrash/my-own-notes/keep-me.txt" ] \
    && ok "a folder that isn't a run timestamp is left alone" \
    || bad "pruned a folder it should not have touched"
grep -q "Old trash runs pruned:" "$WORK/rt.out" \
    && ok "summary reports the pruning" || bad "summary omits pruning"

# Dry runs prune nothing.
retention_fixture
sed 's|^DRY_RUN="false"$|DRY_RUN="true"|' "$WORK/ov_rt" > "$WORK/ov_rt2"
gen "$REPO/Library Cleaner Film" "$WORK/rt2.sh" "$WORK/ov_rt2"
"$WORK/rt2.sh" > "$WORK/rt2.out" 2>&1
[ -d "$WORK/rttrash/2020-01-01_00-00-00" ] \
    && ok "dry run prunes nothing" || bad "dry run pruned the trash"
grep -q "TRASH DRY-RUN prune" "$WORK/rt2.out" \
    && ok "dry run still reports what it would prune" \
    || bad "dry run did not report prunable runs"

# 0 disables retention entirely.
retention_fixture
sed 's|^TRASH_KEEP_DAYS=.*|TRASH_KEEP_DAYS="0"|' "$WORK/ov_rt" > "$WORK/ov_rt3"
gen "$REPO/Library Cleaner Film" "$WORK/rt3.sh" "$WORK/ov_rt3"
"$WORK/rt3.sh" > /dev/null 2>&1
[ -d "$WORK/rttrash/2020-01-01_00-00-00" ] \
    && ok "TRASH_KEEP_DAYS=0 keeps everything" || bad "pruned despite being disabled"

# NFO Cleaner honours it too.
retention_fixture
: > "$WORK/rt/Film A (2020)/stale.nfo"
cat > "$WORK/ov_rtn" <<OV
ROOT_DIRS=("$WORK/rt")
DRY_RUN="false"
ENABLE_LOG="true"
TRASH_DIR="$WORK/rttrash"
TRASH_KEEP_DAYS="30"
OV
gen "$REPO/NFO Cleaner" "$WORK/rtn.sh" "$WORK/ov_rtn"
"$WORK/rtn.sh" > "$WORK/rtn.out" 2>&1
[ ! -d "$WORK/rttrash/2020-01-01_00-00-00" ] \
    && ok "nfo cleaner prunes old trash runs too" \
    || bad "nfo cleaner did not prune"


# =====================================================================
#  CONTESTED TRICKPLAY
#  Taken from a real dry run: a folder holding stale trickplay from
#  two earlier releases, both wanting the current video's name. The
#  alphabetical-first rule picked the old Bluray release every time.
# =====================================================================
echo
echo "=== CONTESTED TRICKPLAY ============================================"
ct_fixture() {
    rm -rf "$WORK/ct"
    local d="$WORK/ct/Chappie (2015) {tmdb-198184}"
    mkdir -p "$d"
    head -c 4096 /dev/zero > "$d/Chappie (2015) [Remux-1080p 8bit AVC TrueHD Atmos]-FraMeSToR.mkv"
    mkdir -p "$d/Chappie (2015) [tmdbid-198184] - [Bluray-1080p 8bit h264]{imdb-tt1823672}-War10ck.trickplay"
    mkdir -p "$d/Chappie (2015) [tmdbid-198184] - [Remux-1080p 8bit AVC]{imdb-tt1823672}-FraMeSToR.trickplay"
    : > "$d/Chappie (2015) [tmdbid-198184] - [Remux-1080p 8bit AVC]{imdb-tt1823672}-FraMeSToR.trickplay/right.jpg"
}
ct_fixture
cd_="$WORK/ct/Chappie (2015) {tmdb-198184}"
cat > "$WORK/ov_ct" <<OV
ROOT_DIRS=("$WORK/ct")
DRY_RUN="true"
ENABLE_LOG="true"
LOG_FILE="$WORK/ct.log"
TRASH_DIR=""
LOCK_FILE=""
OV
gen "$REPO/Library Cleaner Film" "$WORK/ct.sh" "$WORK/ov_ct"
"$WORK/ct.sh" > "$WORK/ct.dry" 2>&1
n="$(grep -c '^\[TP DRY-RUN\]' "$WORK/ct.dry")"
[ "$n" -eq 1 ] && ok "dry run promises one rename, not two" \
               || bad "dry run promised $n renames for one name"

sed 's|^DRY_RUN="true"$|DRY_RUN="false"|' "$WORK/ov_ct" > "$WORK/ov_ct2"
gen "$REPO/Library Cleaner Film" "$WORK/ct2.sh" "$WORK/ov_ct2"
"$WORK/ct2.sh" > "$WORK/ct.live" 2>&1
[ -f "$cd_/Chappie (2015) [Remux-1080p 8bit AVC TrueHD Atmos]-FraMeSToR.trickplay/right.jpg" ] \
    && ok "the matching release's trickplay gets the name" \
    || bad "the wrong trickplay was renamed"
[ -d "$cd_/Chappie (2015) [tmdbid-198184] - [Bluray-1080p 8bit h264]{imdb-tt1823672}-War10ck.trickplay" ] \
    && ok "the old release's trickplay is left alone" \
    || bad "the old release's trickplay was touched"
grep -q "TP SKIP lost to a closer match" "$WORK/ct.live" \
    && ok "logs why the other one was passed over" \
    || bad "no log line for the losing trickplay"

# When nothing matches the video, neither is guessed at.
ct_fixture
mv "$cd_/Chappie (2015) [tmdbid-198184] - [Remux-1080p 8bit AVC]{imdb-tt1823672}-FraMeSToR.trickplay" \
   "$cd_/Chappie (2015) [tmdbid-198184] - [WEBDL-720p 8bit h264]{imdb-tt1823672}-Other.trickplay"
"$WORK/ct2.sh" > "$WORK/ct.tie" 2>&1
n="$(find "$cd_" -maxdepth 1 -name '*.trickplay' | wc -l)"
if [ "$n" -eq 2 ] && [ ! -e "$cd_/Chappie (2015) [Remux-1080p 8bit AVC TrueHD Atmos]-FraMeSToR.trickplay" ]; then
    ok "no clear match: both left in place rather than guessed"
else
    bad "guessed between two trickplays that matched nothing"
fi

# TV: two stale trickplays competing for one episode.
rm -rf "$WORK/ctv"; cs="$WORK/ctv/Show/Season 01"; mkdir -p "$cs"
head -c 4096 /dev/zero > "$cs/Show - S01E01 - Pilot [WEBDL-1080p 8bit h264]-NTb.mkv"
mkdir -p "$cs/Show - S01E01 - Old [Bluray-720p 8bit x264]-DEMAND.trickplay"
mkdir -p "$cs/Show - S01E01 - Old [WEBDL-1080p 8bit h264]-NTb.trickplay"
: > "$cs/Show - S01E01 - Old [WEBDL-1080p 8bit h264]-NTb.trickplay/right.jpg"
cat > "$WORK/ov_ctv" <<OV
ROOT_DIRS=("$WORK/ctv")
DRY_RUN="false"
ENABLE_LOG="false"
TRASH_DIR=""
LOCK_FILE=""
OV
gen "$REPO/Library Cleaner TV" "$WORK/ctv.sh" "$WORK/ov_ctv"
"$WORK/ctv.sh" > /dev/null 2>&1
[ -f "$cs/Show - S01E01 - Pilot [WEBDL-1080p 8bit h264]-NTb.trickplay/right.jpg" ] \
    && ok "tv: the matching release's trickplay gets the episode's name" \
    || bad "tv: the wrong trickplay was renamed"

# =====================================================================
#  DRY RUN PREDICTS THE LIVE RUN
#  Whatever a dry run says it would do, a live run on the same tree
#  must actually do - otherwise the dry run can't be trusted.
# =====================================================================
echo
echo "=== DRY RUN = LIVE RUN ============================================="
predict_fixture() {
    build_film
    ct_fixture
    mv "$WORK/ct/Chappie (2015) {tmdb-198184}" "$WORK/films/"
    # Two subtitles that resolve to the same name.
    local d="$WORK/films/Film G (2026)"
    mkdir -p "$d"
    head -c 4096 /dev/zero > "$d/Film G (2026)-Radarr.mkv"
    : > "$d/Film.2026.WEB.DDP.srt"
    : > "$d/Film.2026.HDR.srt"
}
predict_fixture
cat > "$WORK/ov_pr" <<OV
ROOT_DIRS=("$WORK/films")
DRY_RUN="true"
ENABLE_LOG="true"
LOG_FILE="$WORK/pr.log"
TRASH_DIR=""
LOCK_FILE=""
OV
gen "$REPO/Library Cleaner Film" "$WORK/pr.sh" "$WORK/ov_pr"
"$WORK/pr.sh" > "$WORK/pr.dry" 2>&1
predict_fixture
sed 's|^DRY_RUN="true"$|DRY_RUN="false"|' "$WORK/ov_pr" > "$WORK/ov_pr2"
gen "$REPO/Library Cleaner Film" "$WORK/pr2.sh" "$WORK/ov_pr2"
"$WORK/pr2.sh" > "$WORK/pr.live" 2>&1
count() { grep -E "$2" "$1" | grep -oE '[0-9]+$'; }
for what in Subtitles Trickplay; do
    d="$(count "$WORK/pr.dry"  "^ $what would rename:")"
    l="$(count "$WORK/pr.live" "^ $what (folders )?renamed:")"
    [ -n "$d" ] && [ "$d" = "$l" ] \
        && ok "$what: dry run said $d, live run did $l" \
        || bad "$what: dry run said ${d:-?}, live run did ${l:-?}"
done


# =====================================================================
#  SEVERAL FEATURE-LENGTH VIDEOS IN ONE FOLDER
#  From a real dry run: the largest file in the folder was the OLD
#  copy, so "pair with the largest" sent a subtitle the wrong way.
# =====================================================================
echo
echo "=== SEVERAL VIDEOS ================================================="
jp_old="Jurassic Park (1993) [tmdbid-329] - [Bluray-1080p 10bit x265]{imdb-tt0107290}-Radarr"
jp_new="Jurassic Park (1993) [Bluray-1080p 10bit h265 AAC]{edition-Remastered}-Remastered"
mv_fixture() {
    rm -rf "$WORK/mv"
    local d="$WORK/mv/Jurassic Park (1993) {tmdb-329}"
    mkdir -p "$d/Subs"
    head -c 6000 /dev/zero > "$d/$jp_old.mkv"        # larger, and old
    head -c 4000 /dev/zero > "$d/$jp_new.mkv"
    : > "$d/$jp_new.Park (1993) [tmdbid-329] - [Bluray-1080p 10bit x265]{imdb-tt0107290}-Radarr.en.srt"
    : > "$d/$jp_new.nl.srt"                          # names a video: extra guard
    : > "$d/Subs/2_French.srt"
    # A film beside its trailer is NOT two features.
    local t="$WORK/mv/Film T (2020)"
    mkdir -p "$t"
    head -c 6000 /dev/zero > "$t/Film T (2020)-Radarr.mkv"
    head -c 300  /dev/zero > "$t/Film T (2020)-trailer.mkv"
    : > "$t/Film T (2020) [old]-Radarr.en.srt"
}
jd="$WORK/mv/Jurassic Park (1993) {tmdb-329}"
mv_fixture
cat > "$WORK/ov_mv" <<OV
ROOT_DIRS=("$WORK/mv")
DRY_RUN="false"
ENABLE_LOG="true"
LOG_FILE="$WORK/mv.log"
TRASH_DIR=""
LOCK_FILE=""
DUPLICATES_REPORT="$WORK/dups_film.txt"
OV
gen "$REPO/Library Cleaner Film" "$WORK/mv.sh" "$WORK/ov_mv"
"$WORK/mv.sh" > "$WORK/mv.out" 2>&1

[ -f "$jd/$jp_new.Park (1993) [tmdbid-329] - [Bluray-1080p 10bit x265]{imdb-tt0107290}-Radarr.en.srt" ] \
  && [ ! -e "$jd/$jp_old.en.srt" ] \
    && ok "a subtitle naming neither video is not sent to the largest" \
    || bad "the ambiguous subtitle was paired with the old copy"
grep -q "SUB SKIP ambiguous - folder has 2 videos" "$WORK/mv.out" \
    && ok "logs it as ambiguous" || bad "no ambiguous log line"
[ -f "$jd/$jp_new.nl.srt" ] \
    && ok "a subtitle naming one of the videos is still left with it" \
    || bad "the extra guard stopped working"
[ -f "$jd/Subs/2_French.srt" ] \
    && ok "a Subs/ folder is not promoted onto a guess either" \
    || bad "Subs/ was promoted despite two videos"
[ -f "$WORK/mv/Film T (2020)/Film T (2020)-Radarr.en.srt" ] \
    && ok "a trailer doesn't count as a second feature" \
    || bad "film + trailer was treated as ambiguous"

if [ -f "$WORK/dups_film.txt" ]; then
    ok "duplicates report written"
    first="$(grep -A1 -F "$jd" "$WORK/dups_film.txt" | sed -n 2p)"
    case "$first" in
        *"$jp_old.mkv") ok "report lists the largest file first, with its size" ;;
        *)              bad "report order wrong: $first" ;;
    esac
    grep -qF "$jp_new.mkv" "$WORK/dups_film.txt" \
        && ok "report lists the other copy" || bad "report misses the other copy"
    grep -q "Film T" "$WORK/dups_film.txt" \
        && bad "report lists a film beside its trailer" \
        || ok "report leaves out a film beside its trailer"
    grep -q "^# 1 group\. .* total 3 KiB" "$WORK/dups_film.txt" \
        && ok "report totals the space outside the largest copies" \
        || bad "report total wrong: $(tail -1 "$WORK/dups_film.txt")"
else
    bad "no duplicates report written"
fi
grep -qE "Folders with several videos: *1$" "$WORK/mv.out" \
    && ok "summary counts the folder" || bad "summary count missing"

# The old behaviour is one setting away.
mv_fixture
sed 's|^DUPLICATES_REPORT=.*|AMBIGUOUS_SUBS_TO_LARGEST="true"|' "$WORK/ov_mv" > "$WORK/ov_mv2"
gen "$REPO/Library Cleaner Film" "$WORK/mv2.sh" "$WORK/ov_mv2"
"$WORK/mv2.sh" > /dev/null 2>&1
[ -f "$jd/$jp_old.en.srt" ] \
    && ok "AMBIGUOUS_SUBS_TO_LARGEST=true pairs with the largest, as before" \
    || bad "AMBIGUOUS_SUBS_TO_LARGEST=true did not restore the old pairing"

# A dry run writes the report too - it's read-only as far as media goes.
mv_fixture; rm -f "$WORK/dups_dry.txt"
sed -e 's|^DRY_RUN=.*|DRY_RUN="true"|' -e "s|^DUPLICATES_REPORT=.*|DUPLICATES_REPORT=\"$WORK/dups_dry.txt\"|" \
    "$WORK/ov_mv" > "$WORK/ov_mv3"
gen "$REPO/Library Cleaner Film" "$WORK/mv3.sh" "$WORK/ov_mv3"
"$WORK/mv3.sh" > /dev/null 2>&1
grep -qF "$jp_old.mkv" "$WORK/dups_dry.txt" 2>/dev/null \
    && ok "dry run writes the duplicates report" || bad "dry run wrote no report"

# TV: two files for one episode.
rm -rf "$WORK/dtv"; ds="$WORK/dtv/Show/Season 01"; mkdir -p "$ds"
head -c 5000 /dev/zero > "$ds/Show - S01E01 - Pilot [WEBDL-1080p 8bit h264]-NTb.mkv"
head -c 2000 /dev/zero > "$ds/Show [tvdbid-1] - S01E01 - Pilot [HDTV-720p 8bit x264]-LOL.mkv"
head -c 5000 /dev/zero > "$ds/Show - S01E02 - Second [WEBDL-1080p 8bit h264]-NTb.mkv"
cat > "$WORK/ov_dtv" <<OV
ROOT_DIRS=("$WORK/dtv")
DRY_RUN="true"
ENABLE_LOG="true"
LOG_FILE="$WORK/dtv.log"
TRASH_DIR=""
LOCK_FILE=""
DUPLICATES_REPORT="$WORK/dups_tv.txt"
OV
gen "$REPO/Library Cleaner TV" "$WORK/dtv.sh" "$WORK/ov_dtv"
"$WORK/dtv.sh" > "$WORK/dtv.out" 2>&1
tvfirst="$(grep -A1 -F "$ds - S01E01" "$WORK/dups_tv.txt" 2>/dev/null | sed -n 2p)"
case "$tvfirst" in
    *"4 KiB  Show - S01E01 - Pilot [WEBDL-1080p 8bit h264]-NTb.mkv") ok "tv report lists both copies of the episode, largest first" ;;
    *) bad "tv report wrong: ${tvfirst:-<missing>}" ;;
esac
grep -qF "LOL.mkv" "$WORK/dups_tv.txt" 2>/dev/null \
    && ok "tv report includes the old copy" || bad "tv report misses the old copy"
grep -q "S01E02" "$WORK/dups_tv.txt" 2>/dev/null \
    && bad "tv report lists an episode that has one file" \
    || ok "tv report leaves single-file episodes out"
grep -qE "Episodes with several videos: *1$" "$WORK/dtv.out" \
    && ok "tv summary counts the episode" || bad "tv summary count missing"


# =====================================================================
#  WHAT THE FIRST DUPLICATES REPORTS GOT WRONG
#  Found by reading the reports from a full-library rescan.
# =====================================================================
echo
echo "=== REPORT ACCURACY ================================================"

# 1. "S02E04-E14" lists two episodes; it isn't a range. Filling the gap
#    made one Teen Titans Go! file claim eleven episodes.
rm -rf "$WORK/tt"; ts="$WORK/tt/Teen Titans Go!/Season 02"; mkdir -p "$ts"
head -c 4000 /dev/zero > "$ts/Teen Titans Go! - S02E04 - Sandwich Thief [WEBDL-1080p]-Lahey.mkv"
head -c 4000 /dev/zero > "$ts/Teen Titans Go! - S02E07 - Other Episode [WEBDL-1080p]-Lahey.mkv"
head -c 3000 /dev/zero > "$ts/Teen.Titans.Go!.S02E04-E14.Sandwich.Thief.Money.Grandma.1080p-Lahey.mkv"
: > "$ts/Teen Titans Go! - S02E07 - Other Episode [old].en.srt"
cat > "$WORK/ov_tt" <<OV
ROOT_DIRS=("$WORK/tt")
DRY_RUN="false"
ENABLE_LOG="true"
LOG_FILE="$WORK/tt.log"
TRASH_DIR=""
LOCK_FILE=""
DUPLICATES_REPORT="$WORK/dups_tt.txt"
OV
gen "$REPO/Library Cleaner TV" "$WORK/tt.sh" "$WORK/ov_tt"
"$WORK/tt.sh" > "$WORK/tt.out" 2>&1
grep -q "S02E07" "$WORK/dups_tt.txt" \
    && bad "an episode between two listed ones was claimed as a duplicate" \
    || ok "E04-E14 lists two episodes; it doesn't claim E05..E13"
grep -q "Season 02 - S02E04" "$WORK/dups_tt.txt" \
    && ok "the genuine duplicate (E04 twice) is still reported" \
    || bad "lost the genuine E04 duplicate"
[ -f "$ts/Teen Titans Go! - S02E07 - Other Episode [WEBDL-1080p]-Lahey.en.srt" ] \
    && ok "the episode in the gap is processed normally again" \
    || bad "the episode in the gap was still skipped"

# 2. Jellyfin extras folders are not duplicates.
rm -rf "$WORK/ex"; ef="$WORK/ex/The Red Turtle (2016)"; mkdir -p "$ef/Featurettes/Short Films"
head -c 9000 /dev/zero > "$ef/The Red Turtle (2016)-Radarr.mkv"
head -c 3000 /dev/zero > "$ef/Featurettes/Making Of.mkv"
head -c 2000 /dev/zero > "$ef/Featurettes/Talks with Director.mkv"
head -c 1000 /dev/zero > "$ef/Featurettes/Short Films/Father and Daughter (2000).mkv"
head -c 1000 /dev/zero > "$ef/Featurettes/Short Films/Tom Sweep (1992).mkv"
cat > "$WORK/ov_ex" <<OV
ROOT_DIRS=("$WORK/ex")
DRY_RUN="true"
ENABLE_LOG="false"
TRASH_DIR=""
LOCK_FILE=""
DUPLICATES_REPORT="$WORK/dups_ex.txt"
OV
gen "$REPO/Library Cleaner Film" "$WORK/ex.sh" "$WORK/ov_ex"
"$WORK/ex.sh" > /dev/null 2>&1
grep -q "None found" "$WORK/dups_ex.txt" \
    && ok "extras folders (and folders below them) are not reported" \
    || bad "extras listed as duplicates: $(grep -c . "$WORK/dups_ex.txt") lines"

# 3. Two names for one file look identical to a duplicate, but deleting
#    either frees nothing.
rm -rf "$WORK/hl" "$WORK/hl-downloads"; hf="$WORK/hl/An Unexpected Valentine (2025)"; mkdir -p "$hf" "$WORK/hl-downloads"
head -c 7000 /dev/zero > "$hf/An Unexpected Valentine (2025) [WEBDL-1080p 8bit h264 EAC3]-MADSKY.mkv"
ln "$hf/An Unexpected Valentine (2025) [WEBDL-1080p 8bit h264 EAC3]-MADSKY.mkv" \
   "$hf/An Unexpected Valentine (2025) [tmdbid-1414160] - [WEBDL-1080p 8bit h264]{imdb-tt35304903}-MADSKY.mkv"
# A real second copy, and one that is also linked from a downloads folder.
hg="$WORK/hl/Righteous Kill (2008)"; mkdir -p "$hg"
head -c 6000 /dev/zero > "$hg/Righteous Kill (2008) [WEBDL-1080p]-ADDICTION.mkv"
head -c 5000 /dev/zero > "$hg/Righteous Kill (2008) [tmdbid-13389] - [WEBDL-1080p]-ADDICTION.mkv"
head -c 2048 /dev/zero > "$hg/Righteous Kill (2008) [HDTV-720p]-OLD.mkv"
ln "$hg/Righteous Kill (2008) [HDTV-720p]-OLD.mkv" "$WORK/hl-downloads/seeding.mkv"
cat > "$WORK/ov_hl" <<OV
ROOT_DIRS=("$WORK/hl")
DRY_RUN="true"
ENABLE_LOG="false"
TRASH_DIR=""
LOCK_FILE=""
DUPLICATES_REPORT="$WORK/dups_hl.txt"
OV
gen "$REPO/Library Cleaner Film" "$WORK/hl.sh" "$WORK/ov_hl"
"$WORK/hl.sh" > /dev/null 2>&1
grep -A3 -F "$hf" "$WORK/dups_hl.txt" | grep -q "same file as above (hardlink)" \
    && ok "a hardlinked second name is marked as the same file" \
    || bad "hardlink not recognised"
grep -A6 -F "$hg" "$WORK/dups_hl.txt" | grep -q "also linked elsewhere (2 links)" \
    && ok "a copy also linked from elsewhere is marked" \
    || bad "external link not recognised"
grep -A6 -F "$hg" "$WORK/dups_hl.txt" | grep -q "^ .*4 KiB  Righteous Kill (2008) \[tmdbid-13389\]" \
    && ok "a real second copy carries no hardlink note" \
    || bad "real copy listed wrongly"
# Spare = 6.8 KiB (hardlink) + 4.8 KiB (copy) + 2 KiB (linked elsewhere).
# Freeable = only the real copy, 5000 bytes -> 4 KiB.
grep -q "^# Of that, 4 KiB is in files with no other link" "$WORK/dups_hl.txt" \
    && ok "freeable total counts only files deleting would actually free" \
    || bad "freeable total wrong: $(grep '^# Of that' "$WORK/dups_hl.txt")"
grep -q "^# 2 groups\. .* total 13 KiB" "$WORK/dups_hl.txt" \
    && ok "the plain total still covers every non-largest file" \
    || bad "plain total wrong: $(grep '^# [0-9]* group' "$WORK/dups_hl.txt")"


# =====================================================================
#  BUILD FRESHNESS
#  The scripts at the repo root are generated from src/ by build.sh.
#  Catch the case where src/ was edited but build.sh was not re-run.
# =====================================================================
echo
echo "=== BUILD =========================================================="
check_generated() {
    local src="$1" out="$2"
    awk -v dir="$REPO/src" '
        /^#@include / {
            f = dir "/" $2
            while ((getline line < f) > 0) print line
            close(f)
            next
        }
        { print }
    ' "$REPO/$src" > "$WORK/generated"
    if diff -q "$WORK/generated" "$REPO/$out" >/dev/null 2>&1; then
        ok "$out is up to date with $src"
    else
        bad "$out is STALE - run ./build.sh and commit the result"
        diff "$REPO/$out" "$WORK/generated" | head -20 | sed 's/^/       /'
    fi
}
check_generated "src/film.sh" "Library Cleaner Film"
check_generated "src/tv.sh"   "Library Cleaner TV"

echo
echo "===================================================================="
echo " passed: $PASS   failed: $FAIL"
echo " workdir: $WORK"
[ "$FAIL" -eq 0 ]
