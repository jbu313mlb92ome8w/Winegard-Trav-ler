#!/usr/bin/env bash
# ============================================================== #
# @hdr_checksums_sh
# Module : checksums.sh
# Purpose: Per-directory SHA256/MD5 checksum generation for .bin
#          firmware images, with path-derived output filenames.
# Logging: Actions and errors reported to stdout/stderr only.
# ============================================================== #

set -euo pipefail

# -------------------------------------------------------------- #
# @sec_usage
# -------------------------------------------------------------- #
usage() {
    cat <<'EOF'
Usage: checksums.sh [-y] [target ...]

Targets:
  (none)    Recursively process every subdirectory of the current
            directory that contains .bin files.
  idu odu   Process each immediate subdirectory of each named
            parent directory (idu/*, odu/*).
  idu/10    Process only the named directory itself.

Options:
  -y        Overwrite existing checksum files without prompting.
  -h        Show this help.

Naming: the last two path components of each processed directory
joined by underscores, plus "_checksums.txt".
  odu/6  -> odu/6/odu_6_checksums.txt
  idu/10 -> idu/10/idu_10_checksums.txt

Directories containing no .bin files are silently ignored.
EOF
}

# -------------------------------------------------------------- #
# @sec_globals
# -------------------------------------------------------------- #
ASSUME_YES=0
TARGETS=()

# -------------------------------------------------------------- #
# @sec_argparse
# -------------------------------------------------------------- #
while (( $# )); do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "error: unknown option: $1" >&2; usage >&2; exit 1 ;;
        *) TARGETS+=("$1") ;;
    esac
    shift
done

# -------------------------------------------------------------- #
# @sec_output_name
# Builds odu_6_checksums.txt from .../odu/6 style paths.
# -------------------------------------------------------------- #
output_name_for() {
    local abs leaf parent
    abs="$(cd "$1" && pwd)"
    leaf="$(basename "$abs")"
    parent="$(basename "$(dirname "$abs")")"
    if [[ "$parent" == "/" ]]; then
        printf '%s_checksums.txt' "$leaf"
    else
        printf '%s_%s_checksums.txt' "$parent" "$leaf"
    fi
}

# -------------------------------------------------------------- #
# @sec_generate
# Writes the checksum file into the directory being processed.
# -------------------------------------------------------------- #
generate_for_dir() {
    local dir="$1" bins name out reply
    bins=( "$dir"/*.bin )
    if [[ ! -e ${bins[0]} ]]; then
        return 0
    fi
    name="$(output_name_for "$dir")"
    out="$dir/$name"
    if [[ -e $out && $ASSUME_YES -eq 0 ]]; then
        read -rp "overwrite existing $out? [y/N] " reply
        [[ $reply == [yY]* ]] || { echo "skip (kept existing): $out"; return 0; }
    fi
    (
        cd "$dir"
        printf '# SHA256\n'
        sha256sum -- *.bin
        printf '# MD5\n'
        md5sum -- *.bin
    ) > "$out"
    echo "wrote: $out (${#bins[@]} .bin files)"
}

# -------------------------------------------------------------- #
# @sec_dispatch
# -------------------------------------------------------------- #
dispatch() {
    local t sub
    if (( ${#TARGETS[@]} == 0 )); then
        while IFS= read -r sub; do
            generate_for_dir "${sub#./}"
        done < <(find . -mindepth 1 -type d | sort)
        return 0
    fi
    for t in "${TARGETS[@]}"; do
        t="${t#./}"
        t="${t%/}"
        if [[ ! -d $t ]]; then
            echo "error: not a directory: $t" >&2
            continue
        fi
        if [[ $t == */* ]]; then
            generate_for_dir "$t"
        else
            for sub in "$t"/*/; do
                [[ -d $sub ]] || continue
                generate_for_dir "${sub%/}"
            done
        fi
    done
}

dispatch
