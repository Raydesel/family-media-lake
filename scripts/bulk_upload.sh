#!/usr/bin/env bash
# Bulk upload local media into the raw bucket with uploader tagging.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python3}"
UPLOAD_SCRIPT="$ROOT/scripts/generate_upload_url.py"
BUCKET="${RAW_BUCKET:?Set RAW_BUCKET}"

MEDIA_EXPR=(
  \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.gif'
  -o -iname '*.heic' -o -iname '*.heif' -o -iname '*.webp' -o -iname '*.tif'
  -o -iname '*.tiff' -o -iname '*.bmp' -o -iname '*.dng'
  -o -iname '*.mp4' -o -iname '*.mov' -o -iname '*.avi' -o -iname '*.mkv'
  -o -iname '*.webm' -o -iname '*.m4v' -o -iname '*.3gp' -o -iname '*.mpg'
  -o -iname '*.mpeg' \)
)

upload_batch() {
  local label="$1"
  local uploader="$2"
  local dir="$3"
  local maxdepth="$4"

  local files=()
  while IFS= read -r -d '' f; do
    files+=("$f")
  done < <(find "$dir" -maxdepth "$maxdepth" -type f "${MEDIA_EXPR[@]}" -print0 | sort -z)

  local total="${#files[@]}"
  echo "=== $label: $total files (uploader=$uploader) ==="

  local n=0 ok=0 fail=0
  for f in "${files[@]}"; do
    n=$((n + 1))
    echo "[$(date -Iseconds)] [$label $n/$total] $f"
    if out="$("$PY" "$UPLOAD_SCRIPT" "$f" --uploader "$uploader" --bucket "$BUCKET" --upload 2>&1)"; then
      ok=$((ok + 1))
      echo "$out" | grep -E '^(file_id|key)\s' || true
    else
      fail=$((fail + 1))
      echo "FAILED: $f"
      echo "$out"
    fi
  done
  echo "=== $label done: ok=$ok fail=$fail total=$total ==="
}

upload_batch "family" "family" "/home/user/Desktop/media" 1

echo "All batches complete."
