#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Stage static ffmpeg + ffprobe binaries for the enrichment Lambda (linux/arm64).
#
#   fetch_ffmpeg.sh <dest_bin_dir>
#
# Invoked after build_lambda_package.sh so wheels are already in the package dir.
# ---------------------------------------------------------------------------
set -euo pipefail

DEST="${1:?usage: fetch_ffmpeg.sh <dest_bin_dir>}"
FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"
CACHE="${TMPDIR:-/tmp}/ffmpeg-release-arm64-static.tar.xz"

if [ -x "${DEST}/ffmpeg" ] && [ -x "${DEST}/ffprobe" ]; then
  echo "[ffmpeg] already present in ${DEST}"
  exit 0
fi

mkdir -p "${DEST}"
EXTRACT_TMP="$(mktemp -d)"
trap 'rm -rf "${EXTRACT_TMP}"' EXIT

echo "[ffmpeg] downloading ${FFMPEG_URL}"
curl -fsSL -o "${CACHE}" "${FFMPEG_URL}"
tar -xJf "${CACHE}" -C "${EXTRACT_TMP}"

ROOT="$(find "${EXTRACT_TMP}" -maxdepth 1 -type d -name 'ffmpeg-*-arm64-static' | head -1)"
if [ -z "${ROOT}" ]; then
  echo "error: could not find ffmpeg-*-arm64-static in archive" >&2
  exit 1
fi

cp "${ROOT}/ffmpeg" "${ROOT}/ffprobe" "${DEST}/"
chmod +x "${DEST}/ffmpeg" "${DEST}/ffprobe"
echo "[ffmpeg] staged $(du -sh "${DEST}" | cut -f1) in ${DEST}"
