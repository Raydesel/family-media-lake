#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build a Lambda deployment directory for a given lambda source folder.
#
#   build_lambda_package.sh <lambda_src_dir> <build_out_dir> [platform] [pyver]
#
# - Installs the lambda's requirements.txt as prebuilt wheels for the TARGET
#   runtime (default: manylinux2014_aarch64 / Python 3.11), regardless of the
#   local machine's Python. Requires network access at build time.
# - Copies the lambda's *.py sources on top.
# - Terraform's archive_file then zips <build_out_dir>.
#
# Invoked by Terraform (null_resource local-exec) — see modules/enrichment.
# ---------------------------------------------------------------------------
set -euo pipefail

SRC_DIR="${1:?usage: build_lambda_package.sh <src_dir> <out_dir> [platform] [pyver]}"
OUT_DIR="${2:?missing build output dir}"
PLATFORM="${3:-manylinux2014_aarch64}"
PYVER="${4:-3.11}"

echo "[build] src=${SRC_DIR} out=${OUT_DIR} platform=${PLATFORM} py=${PYVER}"

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"

# Does requirements.txt contain any actual (non-comment) dependency lines?
REQS="${SRC_DIR}/requirements.txt"
HAS_DEPS=0
if [ -f "${REQS}" ] && grep -qE '^[[:space:]]*[^#[:space:]]' "${REQS}"; then
  HAS_DEPS=1
fi

if [ "${HAS_DEPS}" -eq 1 ]; then
  # Use a throwaway venv with an up-to-date pip: cross-platform resolution
  # (--platform/--python-version/--only-binary) needs a reasonably new pip.
  BUILD_TMP="$(mktemp -d)"
  trap 'rm -rf "${BUILD_TMP}"' EXIT
  python3 -m venv "${BUILD_TMP}/venv"
  "${BUILD_TMP}/venv/bin/pip" install --quiet --upgrade pip

  echo "[build] installing wheels for ${PLATFORM} / cp${PYVER/./}"
  "${BUILD_TMP}/venv/bin/pip" install \
    --quiet \
    --target "${OUT_DIR}" \
    --platform "${PLATFORM}" \
    --implementation cp \
    --python-version "${PYVER}" \
    --only-binary=:all: \
    -r "${REQS}"

  # Trim dead weight to stay under Lambda's 250MB unzipped limit.
  find "${OUT_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} +
  rm -rf "${OUT_DIR}/pyarrow/tests" \
         "${OUT_DIR}/pyarrow/include" \
         "${OUT_DIR}"/*.dist-info/RECORD 2>/dev/null || true
fi

cp "${SRC_DIR}"/*.py "${OUT_DIR}/"

DU=$(du -sh "${OUT_DIR}" | cut -f1)
echo "[build] done: ${OUT_DIR} (${DU} unzipped)"
