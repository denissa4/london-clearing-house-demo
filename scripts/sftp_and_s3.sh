#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Practise the "reports land via SFTP" side of the platform, two ways.
#
# OPTION A - Local SFTP landing zone (no cloud):
#   1. Run a throwaway SFTP server in Docker pointing at ./sftp_root
#   2. sftp the generated CSVs into it
#   3. Point the MCP server at that folder (DATA_BACKEND=local LCH_DATA_DIR=...)
#
# OPTION B - S3 landing zone (matches the Terraform/EKS deployment):
#   Sync the generated CSVs into the reports S3 bucket, under the lch-reports/
#   prefix the pod expects. The bucket is created by the central IaC repo; get
#   its name from that Terraform Cloud workspace's `reports_bucket` output:
#     terraform -chdir=../infrastructure-as-code/terraform/london-clearing-house-demo-eks output -raw reports_bucket
# ---------------------------------------------------------------------------
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$HERE/data"

usage() {
  cat <<USAGE
Usage:
  $0 sftp-up        Start a local SFTP server (Docker) + upload the CSVs
  $0 sftp-down      Stop the local SFTP server
  $0 s3-sync BUCKET Upload the CSVs to s3://BUCKET/lch-reports/
USAGE
}

sftp_up() {
  mkdir -p "$HERE/sftp_root/upload"
  echo ">> Starting SFTP server on localhost:2222 (user: lch / pass: lchpass)"
  docker run -d --name lch-sftp -p 2222:22 \
    -v "$HERE/sftp_root:/home/lch/data" \
    atmoz/sftp:alpine lch:lchpass:1001
  sleep 3
  echo ">> Uploading CSVs via sftp"
  # sshpass keeps it non-interactive; install with apt/brew if missing.
  for f in "$DATA_DIR"/*.csv; do
    sshpass -p lchpass sftp -o StrictHostKeyChecking=no -P 2222 lch@localhost <<SFTP
cd data
put "$f"
bye
SFTP
  done
  echo ">> Files now in $HERE/sftp_root/data - point the server at it:"
  echo "   DATA_BACKEND=local LCH_DATA_DIR=$HERE/sftp_root/data python mcp_server/server.py"
}

sftp_down() {
  docker rm -f lch-sftp 2>/dev/null || true
  echo ">> SFTP server stopped."
}

s3_sync() {
  local bucket="${1:?pass the bucket name}"
  echo ">> Syncing CSVs to s3://$bucket/lch-reports/"
  aws s3 sync "$DATA_DIR" "s3://$bucket/lch-reports/" \
    --exclude "*" --include "*.csv"
  echo ">> Done. The EKS pod reads from this prefix."
}

case "${1:-}" in
  sftp-up)   sftp_up ;;
  sftp-down) sftp_down ;;
  s3-sync)   s3_sync "${2:-}" ;;
  *)         usage; exit 1 ;;
esac
