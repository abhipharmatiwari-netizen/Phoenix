#!/bin/sh
# Capture non-secret OCI host storage evidence before cleanup.

set -eu

echo "== filesystem =="
df -h /

echo "== docker system df =="
docker system df

echo "== largest docker volumes =="
docker system df -v 2>/dev/null | sed -n '/Local Volumes space usage:/,$p' | head -n 80

echo "== active phoenix images =="
docker ps --filter 'name=phoenix' --format '{{.Names}} {{.Image}} {{.Status}}'

echo "== stale phoenix local images =="
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' \
  | grep '^phoenix-' \
  | sort
