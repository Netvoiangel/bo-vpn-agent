#!/usr/bin/env bash
set -euo pipefail

IMAGE_TAG="${UNIVPN_IMAGE:-local/univpn:10781.16.0.0730}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Building UniVPN image: ${IMAGE_TAG}"
docker build \
  -t "${IMAGE_TAG}" \
  -f "${REPO_DIR}/Dockerfile.univpn" \
  "${REPO_DIR}"

echo "==> Successfully built UniVPN image: ${IMAGE_TAG}"
