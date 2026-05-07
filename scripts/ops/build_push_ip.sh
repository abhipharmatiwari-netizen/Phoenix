#!/bin/sh
# Build and push an OCIR backend image using OCI instance-principal auth.
#
# Required env vars:
#   OCIR_NAMESPACE   OCI tenancy namespace
#   OCIR_USERNAME    OCIR login username, usually <namespace>/<identity-domain>/<user>
#
# This script is for the OCI Compose path only. Do not hard-code tenancy,
# username, token, or registry values in this repo.
set -eu

OCI_CLI=/home/opc/bin/oci
OCIR_REGION="${OCIR_REGION:-ap-mumbai-1}"
OCIR_REGISTRY="${OCIR_REGION}.ocir.io"
OCIR_NAMESPACE="${OCIR_NAMESPACE:?Set OCIR_NAMESPACE from the ops secrets store}"
OCIR_USERNAME="${OCIR_USERNAME:?Set OCIR_USERNAME from the ops secrets store}"
APP_DIR="/opt/phoenix/app"

GIT_SHA=$(git -C "$APP_DIR" rev-parse HEAD)
IMAGE_BASE="${OCIR_REGISTRY}/${OCIR_NAMESPACE}/phoenix-prod/backend"
IMAGE_TAG_FULL="${IMAGE_BASE}:${GIT_SHA}"

echo "=== Build config ==="
echo "SHA:    $GIT_SHA"
echo "Image:  $IMAGE_TAG_FULL"

echo
echo "=== Generate OCIR token via instance principal ==="
OCIR_TOKEN=$(OCI_CLI_AUTH=instance_principal \
  $OCI_CLI login container-registry get-access-token \
  --region "$OCIR_REGION" \
  --auth instance_principal 2>&1) || {
    echo "Instance principal token generation failed:"
    echo "$OCIR_TOKEN"
    echo ""
    echo "Trying alternative: OCI registry token via auth endpoint..."
    OCIR_TOKEN=$(OCI_CLI_AUTH=instance_principal \
      $OCI_CLI iam auth-token list \
      --user-id $(curl -sS http://169.254.169.254/opc/v1/instance/id 2>/dev/null) \
      --auth instance_principal 2>&1)
    echo "$OCIR_TOKEN" | head -5
    exit 1
  }

echo "$OCIR_TOKEN" | docker login "$OCIR_REGISTRY" -u "$OCIR_USERNAME" --password-stdin

echo
echo "=== Build image (amd64) ==="
docker buildx build \
    --platform linux/amd64 \
    -t "$IMAGE_TAG_FULL" \
    -f "${APP_DIR}/Dockerfile" \
    --load \
    "${APP_DIR}"

echo
echo "=== Push to OCIR ==="
docker push "$IMAGE_TAG_FULL"

echo
echo "=== Update IMAGE_TAG ==="
sudo sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=${GIT_SHA}|" /opt/phoenix/phoenix-deploy.env
grep "IMAGE_TAG" /opt/phoenix/phoenix-deploy.env

echo
echo "Done. Run redeploy script next."
