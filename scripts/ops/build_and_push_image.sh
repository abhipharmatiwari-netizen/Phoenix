#!/bin/sh
# Build a new OCIR backend image on the VM and push it.
# Requires OCIR_AUTH_TOKEN to be passed as env var.
# Usage: OCIR_AUTH_TOKEN=xxx sh build_and_push_image.sh
set -eu

OCIR_REGION="ap-mumbai-1"
OCIR_REGISTRY="${OCIR_REGION}.ocir.io"
# Extract namespace from the running image name
RUNNING_IMAGE=$(docker inspect phoenix-oci-backend --format "{{.Config.Image}}" 2>/dev/null | sed 's|:.*||')
OCIR_NAMESPACE=$(echo "$RUNNING_IMAGE" | awk -F/ '{print $2}')
APP_DIR="/opt/phoenix/app"
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
    echo "ERROR: OCIR_AUTH_TOKEN env var required. Set it before running."
    echo "Get it from: OCI Console → User → Auth Tokens → Generate Token"
    exit 1
fi

echo
echo "=== Login to OCIR ==="
echo "${OCIR_AUTH_TOKEN}" | docker login "${OCIR_REGISTRY}" \
    -u "${OCIR_NAMESPACE}/oracleidentitycloudservice/abhipharma.tiwari@gmail.com" \
    --password-stdin

echo
echo "=== Build backend image ==="
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
echo "=== Redeploy backend with new image ==="
CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f "${APP_DIR}/docker-compose.oci-live.yml" \
    -f /opt/phoenix/phoenix-override.yml \
    --env-file /opt/phoenix/phoenix-deploy.env \
    pull backend

CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f "${APP_DIR}/docker-compose.oci-live.yml" \
    -f /opt/phoenix/phoenix-override.yml \
    --env-file /opt/phoenix/phoenix-deploy.env \
    up -d --no-deps --no-build backend

echo
echo "=== Wait 90s for health ==="
sleep 90
docker ps --filter name=phoenix-oci-backend --format "table {{.Names}}\t{{.Status}}"

echo
echo "=== New image SHA in container ==="
docker inspect phoenix-oci-backend --format "{{.Config.Image}}"
