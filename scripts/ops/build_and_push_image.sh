#!/bin/sh
# Build a new OCIR backend image on the VM and push it.
# This script only builds and pushes — it does NOT redeploy.
# To deploy after pushing, run: sh scripts/ops/redeploy_backend.sh
#
# Required env vars:
#   OCIR_AUTH_TOKEN   OCIR auth token (OCI Console → User → Auth Tokens)
#   OCIR_USERNAME     OCIR login username, usually <namespace>/<identity-domain>/<user>
#
# Optional:
#   OCIR_REGION       Defaults to ap-mumbai-1
set -eu

OCIR_REGION="${OCIR_REGION:-ap-mumbai-1}"
OCIR_REGISTRY="${OCIR_REGION}.ocir.io"
APP_DIR="/opt/phoenix/app"

# Extract namespace from the running image so it does not need to be hard-coded.
RUNNING_IMAGE=$(docker inspect phoenix-oci-backend --format "{{.Config.Image}}" 2>/dev/null | sed 's|:.*||')
OCIR_NAMESPACE=$(echo "$RUNNING_IMAGE" | awk -F/ '{print $2}')
if [ -z "${OCIR_NAMESPACE:-}" ]; then
  echo "ERROR: Cannot determine OCIR namespace from running container."
  echo "  Set OCIR_NAMESPACE env var explicitly, or ensure phoenix-oci-backend is running."
  exit 1
fi

GIT_SHA=$(git -C "$APP_DIR" rev-parse HEAD)
IMAGE_BASE="${OCIR_REGISTRY}/${OCIR_NAMESPACE}/phoenix-prod/backend"
IMAGE_TAG="${IMAGE_BASE}:${GIT_SHA}"

echo "=== Build config ==="
echo "SHA:    $GIT_SHA"
echo "Image:  $IMAGE_TAG"
echo "OCIR:   $OCIR_REGISTRY"
echo "ns:     $OCIR_NAMESPACE"

if [ -z "${OCIR_AUTH_TOKEN:-}" ]; then
    echo ""
    echo "ERROR: OCIR_AUTH_TOKEN env var required."
    echo "Get it from: OCI Console → User → Auth Tokens → Generate Token"
    exit 1
fi
if [ -z "${OCIR_USERNAME:-}" ]; then
    echo "ERROR: OCIR_USERNAME env var required (from the operator secret store)."
    exit 1
fi

echo
echo "=== Login to OCIR ==="
echo "${OCIR_AUTH_TOKEN}" | docker login "${OCIR_REGISTRY}" \
    -u "${OCIR_USERNAME}" \
    --password-stdin

echo
echo "=== Build and push backend image ==="
docker buildx build \
    --platform linux/amd64 \
    -t "${IMAGE_TAG}" \
    -f "${APP_DIR}/Dockerfile" \
    --push \
    "${APP_DIR}"

echo
echo "=== Update IMAGE_TAG in phoenix-deploy.env ==="
sudo sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=${GIT_SHA}|" /opt/phoenix/phoenix-deploy.env
grep "IMAGE_TAG" /opt/phoenix/phoenix-deploy.env

echo
echo "Build and push complete. SHA: $GIT_SHA"
echo "Image is in OCIR. Run 'sh scripts/ops/redeploy_backend.sh' to deploy."
