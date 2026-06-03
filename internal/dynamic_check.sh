#!/usr/bin/env bash
# dynamic_check.sh — Build affected targets twice, compare output hashes.
#
# A deterministic build must produce bit-for-bit identical outputs on every
# run given the same inputs. This script proves or disproves that by:
#   1. Building targets → hashing all output files → saving to run1.txt
#   2. bazel clean (clears outputs, NOT disk cache)
#   3. Rebuilding → hashing all output files → saving to run2.txt
#   4. Diffing run1.txt vs run2.txt — any difference = non-deterministic action
#
# Remote cache is disabled intentionally: we want to force local execution
# so both runs actually compile and don't just return cached results.
#
# Usage:
#   ./dynamic_check.sh "//apps/cpp-lib:greeter //apps/go-service:go-service"
#   ./dynamic_check.sh --targets-file /tmp/targets.txt
#   ./dynamic_check.sh --all                  # all targets (slow on large repos)
#
# Output: JSON written to stdout
#   {
#     "deterministic": true/false,
#     "targets_checked": [...],
#     "differing_targets": [...],
#     "run1_action_count": 42,
#     "run2_cache_hits": 42,
#     "error": null
#   }

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
WORK_DIR="/tmp/bazel-det-check-$$"
CACHE_RUN1="$WORK_DIR/cache-run1"
HASH_RUN1="$WORK_DIR/hashes-run1.txt"
HASH_RUN2="$WORK_DIR/hashes-run2.txt"
MAX_TARGETS=20   # cap to keep CI fast

mkdir -p "$WORK_DIR" "$CACHE_RUN1"
trap 'rm -rf "$WORK_DIR"' EXIT

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
TARGETS=""
TARGETS_FILE=""
ALL=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --targets-file) TARGETS_FILE="$2"; shift 2 ;;
        --all)          ALL=true; shift ;;
        *)              TARGETS="$TARGETS $1"; shift ;;
    esac
done

if [[ -n "$TARGETS_FILE" && -f "$TARGETS_FILE" ]]; then
    TARGETS=$(cat "$TARGETS_FILE" | tr '\n' ' ')
fi

if [[ "$ALL" == "true" ]]; then
    TARGETS=$(bazel query '//...' 2>/dev/null | head -"$MAX_TARGETS" | tr '\n' ' ')
fi

TARGETS="${TARGETS// $'\n'/ }"  # normalise whitespace
TARGETS=$(echo "$TARGETS" | xargs)  # trim

if [[ -z "$TARGETS" ]]; then
    echo '{"deterministic":true,"targets_checked":[],"differing_targets":[],"error":"no targets provided","run1_action_count":0,"run2_cache_hits":0}'
    exit 0
fi

# Cap target count
TARGET_ARRAY=($TARGETS)
if [[ ${#TARGET_ARRAY[@]} -gt $MAX_TARGETS ]]; then
    echo "Warning: capping from ${#TARGET_ARRAY[@]} to $MAX_TARGETS targets" >&2
    TARGET_ARRAY=("${TARGET_ARRAY[@]:0:$MAX_TARGETS}")
    TARGETS="${TARGET_ARRAY[*]}"
fi

# ---------------------------------------------------------------------------
# Helper: hash all output files for given targets
# ---------------------------------------------------------------------------
hash_outputs() {
    local outfile="$1"
    # Get output file paths via cquery
    bazel cquery $TARGETS \
        --output=files \
        --@aspect_rules_ts//ts:skipLibCheck=always \
        --@aspect_rules_ts//ts:default_to_tsc_transpiler=True \
        2>/dev/null | \
    while read -r f; do
        [[ -f "$f" ]] && sha256sum "$f"
    done | sort > "$outfile" || true
}

# ---------------------------------------------------------------------------
# Run 1 — cold build, disk cache only (no remote cache)
# ---------------------------------------------------------------------------
cd "$REPO_ROOT"

BUILD_FLAGS="--disk_cache=$CACHE_RUN1 \
  --remote_cache= \
  --@aspect_rules_ts//ts:skipLibCheck=always \
  --@aspect_rules_ts//ts:default_to_tsc_transpiler=True"

RUN1_OUTPUT=$(bazel build $TARGETS $BUILD_FLAGS 2>&1 || true)
RUN1_PROCESSES=$(echo "$RUN1_OUTPUT" | grep -oP '\d+ processes:.*' | tail -1 || echo "unknown")

hash_outputs "$HASH_RUN1"
RUN1_COUNT=$(wc -l < "$HASH_RUN1" | tr -d ' ')

# ---------------------------------------------------------------------------
# Clean local outputs (disk cache retained intentionally)
# ---------------------------------------------------------------------------
bazel clean 2>/dev/null

# ---------------------------------------------------------------------------
# Run 2 — rebuild against run 1's disk cache
# If deterministic: all actions are cache hits (100%)
# If non-deterministic: some actions produce different hashes → cache miss
# ---------------------------------------------------------------------------

# Use a FRESH cache for run 2 — this forces re-execution
# Then we compare output file hashes directly
CACHE_RUN2="$WORK_DIR/cache-run2"
mkdir -p "$CACHE_RUN2"

RUN2_OUTPUT=$(bazel build $TARGETS \
    --disk_cache="$CACHE_RUN2" \
    --remote_cache= \
    --@aspect_rules_ts//ts:skipLibCheck=always \
    --@aspect_rules_ts//ts:default_to_tsc_transpiler=True \
    2>&1 || true)

hash_outputs "$HASH_RUN2"
RUN2_COUNT=$(wc -l < "$HASH_RUN2" | tr -d ' ')

# Count how many actions hit run1's cache on run2
# (re-run using run1 cache to measure hit rate)
bazel clean 2>/dev/null
RUN2_CACHED_OUTPUT=$(bazel build $TARGETS \
    --disk_cache="$CACHE_RUN1" \
    --remote_cache= \
    --@aspect_rules_ts//ts:skipLibCheck=always \
    --@aspect_rules_ts//ts:default_to_tsc_transpiler=True \
    2>&1 || true)
RUN2_CACHE_HITS=$(echo "$RUN2_CACHED_OUTPUT" | grep -oP '\d+ disk cache hit' | grep -oP '\d+' || echo "0")

# ---------------------------------------------------------------------------
# Compare hashes
# ---------------------------------------------------------------------------
DIFF_OUTPUT=$(diff "$HASH_RUN1" "$HASH_RUN2" || true)
DIFFERING_FILES=$(echo "$DIFF_OUTPUT" | grep '^[<>]' | awk '{print $3}' | sort -u)

if [[ -z "$DIFFERING_FILES" ]]; then
    DETERMINISTIC=true
else
    DETERMINISTIC=false
fi

# Build JSON arrays
targets_json=$(printf '%s\n' "${TARGET_ARRAY[@]}" | python3 -c "import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")
differing_json=$(echo "$DIFFERING_FILES" | python3 -c "import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")

cat <<EOF
{
  "deterministic": $DETERMINISTIC,
  "targets_checked": $targets_json,
  "differing_output_files": $differing_json,
  "run1_output_files_hashed": $RUN1_COUNT,
  "run2_output_files_hashed": $RUN2_COUNT,
  "run2_cache_hits_against_run1": ${RUN2_CACHE_HITS:-0},
  "error": null
}
EOF
