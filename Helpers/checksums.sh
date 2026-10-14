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
Usage: checksums.sh [-y] [-o] [target ...]

Targets:
  (none)    Recursively process every subdirectory of the current
            directory that contains .bin files.
  idu odu   Process each immediate subdirectory of each named
            parent directory (idu/*, odu/*).
  idu/10    Process only the named directory itself.

Options:
  -y        Overwrite existing checksum files without prompting.
  -o        Consolidate all checksums into a single file in the
            parent directory instead of writing per-subdir files.
  -h        Show this help.

Per-directory naming:
  odu/6  -> odu/6/odu_6_checksums.txt
  idu/10 -> idu/10/idu_10_checksums.txt

Consolidated naming (with -o):
  For targets idu odu: creates odu_checksums.txt in odu/
                       and idu_checksums.txt in idu/
  For target idu/10:   creates idu_10_checksums.txt in idu/
EOF
}

# -------------------------------------------------------------- #
# @sec_globals
# -------------------------------------------------------------- #
ASSUME_YES=0
OUTPUT_CONSOLIDATED=0
TARGETS=()

# -------------------------------------------------------------- #
# @sec_argparse
# -------------------------------------------------------------- #
while (( $# )); do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        -o|--consolidated) OUTPUT_CONSOLIDATED=1 ;;
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
# Writes the per-directory checksum file into the target.
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
        sha256sum -- *.bin | sort_bin_lines
        printf '# MD5\n'
        md5sum -- *.bin | sort_bin_lines
    ) > "$out"
    echo "wrote: $out (${#bins[@]} .bin files)"
}

# -------------------------------------------------------------- #
# @sec_consolidated_name
# Builds consolidated checksum file names for parent directories.
# -------------------------------------------------------------- #
consolidated_name_for() {
    local abs parent
    abs="$(cd "$1" && pwd)"
    parent="$(basename "$abs")"
    printf '%s_checksums.txt' "$parent"
}

# -------------------------------------------------------------- #
# @sec_sort_lines
# Sorts checksum lines from stdin: names matching the canonical
# format (unit_N_vX.Y.Z_suffix.bin) sort first, ordered by the
# token after the last underscore; non-conforming names follow.
# The original "hash  filename" line is preserved verbatim.
# -------------------------------------------------------------- #
sort_bin_lines() {
    awk '
        {
            hash = $1
            $1 = ""
            sub(/^ +/, "")
            n = split($0, p, "/")
            base = p[n]
            key = "1|zz|"
            if (base ~ /^[A-Za-z0-9-]+_[0-9]+_v[0-9]+\.[0-9]+\.[0-9]+_[a-z0-9-]+\.bin$/) {
                tail = base
                sub(/^.*_/, "", tail)
                sub(/\.bin$/, "", tail)
                key = "0|" tail "|"
            }
            print key hash "  " $0
        }
    ' | LC_ALL=C sort -t'|' -k1,1 -k2,2 | cut -d'|' -f3-
}

# -------------------------------------------------------------- #
# @sec_write_consolidated
# Collects all .bin files from target subdirs and writes one
# checksum file in the parent directory. Each hash-type section
# is collected whole, then sorted once per @sec_sort_lines.
# Subdirectories with no .bin files are skipped silently.
# -------------------------------------------------------------- #
write_consolidated_for() {
    local parent="$1"
    local out="$parent/$(consolidated_name_for "$parent")"
    local tmp reply sub rel bins block
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT

    if [[ -e $out && $ASSUME_YES -eq 0 ]]; then
        read -rp "overwrite existing $out? [y/N] " reply
        [[ $reply == [yY]* ]] || { echo "skip (kept existing): $out"; rm -f "$tmp"; trap - EXIT; return 0; }
    fi

    printf '# SHA256\n' > "$tmp"
    block=""
    for sub in "$parent"/*/; do
        [[ -d $sub ]] || continue
        bins=( "$sub"*.bin )
        [[ -e ${bins[0]} ]] || continue
        rel="${sub#"$parent"/}"
        block+="$( cd "$parent" && sha256sum -- "$rel"*.bin )"$'\n'
    done
    printf '%s' "$block" | sort_bin_lines >> "$tmp"

    printf '# MD5\n' >> "$tmp"
    block=""
    for sub in "$parent"/*/; do
        [[ -d $sub ]] || continue
        bins=( "$sub"*.bin )
        [[ -e ${bins[0]} ]] || continue
        rel="${sub#"$parent"/}"
        block+="$( cd "$parent" && md5sum -- "$rel"*.bin )"$'\n'
    done
    printf '%s' "$block" | sort_bin_lines >> "$tmp"

    mv "$tmp" "$out"
    trap - EXIT
    echo "wrote consolidated: $out"
}

# -------------------------------------------------------------- #
# @sec_dispatch
# -------------------------------------------------------------- #
dispatch() {
    local t sub explicit parent
    if (( ${#TARGETS[@]} == 0 )); then
        if (( OUTPUT_CONSOLIDATED )); then
            while IFS= read -r parent; do
                [[ -d $parent ]] || continue
                write_consolidated_for "${parent#./}"
            done < <(find . -mindepth 1 -maxdepth 1 -type d | sort)
        else
            while IFS= read -r sub; do
                generate_for_dir "${sub#./}"
            done < <(find . -mindepth 1 -type d | sort)
        fi
        return 0
    fi
    for t in "${TARGETS[@]}"; do
        t="${t#./}"
        explicit=0
        if [[ $t == */ ]]; then
            explicit=1
            t="${t%/}"
        fi
        t="${t%/}"
        if [[ ! -d $t ]]; then
            echo "error: not a directory: $t" >&2
            continue
        fi
        if (( explicit )) || [[ $t == */* ]]; then
            generate_for_dir "$t"
        elif (( OUTPUT_CONSOLIDATED )); then
            write_consolidated_for "$t"
        else
            for sub in "$t"/*/; do
                [[ -d $sub ]] || continue
                generate_for_dir "${sub%/}"
            done
        fi
    done
}

dispatch

echo Complete
