#!/bin/sh
# Build and push an OCIR backend image using OCI instance-principal auth.
#
# Required env vars:
#   OCIR_NAMESPACE        OCI tenancy namespace
#   OCIR_USERNAME         OCIR login username (<namespace>/<domain>/<user>)
#   VAULT_SECRET_OCID     OCID of the OCI Vault secret holding the OCIR auth token.
#                         The instance must have an IAM policy granting read access
#                         to this secret via its dynamic group.
#
# Optional:
#   OCIR_AUTH_TOKEN       Bypass vault retrieval and use this token directly.
#                         Use only for testing or when vault access is unavailable.
#   OCIR_REGION           Defaults to ap-mumbai-1
#   OCI_CLI               Path to oci CLI binary. Defaults to /home/opc/bin/oci
#
# IAM policy required on the instance's dynamic group:
#   allow dynamic-group <group-name> to manage repos in tenancy
#   allow dynamic-group <group-name> to read secret-bundles in compartment <compartment>
set -eu

OCI_CLI="${OCI_CLI:-/home/opc/bin/oci}"
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
echo "=== Verify instance principal connectivity ==="
if ! OCI_CLI_AUTH=instance_principal \
    "$OCI_CLI" iam region list \
    --auth instance_principal \
    --query 'data[0].name' \
    --raw-output 2>/dev/null; then
  echo "ERROR: Instance principal auth is not working on this host."
  echo "Check the instance is in a compartment covered by an IAM dynamic-group policy."
  echo "Example policy: allow dynamic-group phoenix-deploy-group to manage repos in tenancy"
  exit 1
fi
echo "Instance principal: OK"

echo
echo "=== Retrieve OCIR auth token ==="
if [ -n "${OCIR_AUTH_TOKEN:-}" ]; then
  echo "(Using OCIR_AUTH_TOKEN env var — skipping vault retrieval)"
elif [ -n "${VAULT_SECRET_OCID:-}" ]; then
  OCIR_AUTH_TOKEN=$(OCI_CLI_AUTH=instance_principal \
    "$OCI_CLI" secrets secret-bundle get \
    --secret-id "$VAULT_SECRET_OCID" \
    --auth instance_principal \
    --query 'data."secret-bundle-content".content' \
    --raw-output 2>/dev/null | base64 -d) || true
  if [ -z "${OCIR_AUTH_TOKEN:-}" ]; then
    echo "ERROR: Failed to retrieve OCIR auth token from vault."
    echo "  Vault secret OCID : ${VAULT_SECRET_OCID}"
    echo "  Ensure the dynamic-group policy allows: read secret-bundles in compartment <X>"
    exit 1
  fi
  echo "Token retrieved from vault."
else
  echo "ERROR: Neither OCIR_AUTH_TOKEN nor VAULT_SECRET_OCID is set."
  echo "  Set VAULT_SECRET_OCID to the OCID of the OCI Vault secret holding the OCIR auth token."
  echo "  Or set OCIR_AUTH_TOKEN directly for testing (not for automated production use)."
  exit 1
fi

echo
echo "=== Login to OCIR ==="
echo "$OCIR_AUTH_TOKEN" | docker login "$OCIR_REGISTRY" \
  -u "$OCIR_USERNAME" --password-stdin

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
echo "=== Update IMAGE_TAG in phoenix-deploy.env ==="
sudo sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=${GIT_SHA}|" /opt/phoenix/phoenix-deploy.env
grep "IMAGE_TAG" /opt/phoenix/phoenix-deploy.env

echo
echo "Build and push complete. SHA: $GIT_SHA"
echo "Run the redeploy script next to deploy this image."
