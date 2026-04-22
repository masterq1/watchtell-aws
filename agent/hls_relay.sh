#!/usr/bin/env bash
# WatchTell HLS Relay
# Pulls RTSP stream via FFmpeg, writes HLS segments to /tmp/hls/<CAMERA_ID>,
# and continuously syncs to S3 so CloudFront can serve them.
# Runs locally alongside camera_relay.py on the camera agent host.
set -euo pipefail

CAMERA_ID="${CAMERA_ID:?CAMERA_ID not set}"
RTSP_URL="${RTSP_URL:?RTSP_URL not set}"
HLS_BUCKET="${HLS_BUCKET:?HLS_BUCKET not set}"
AWS_REGION="${AWS_REGION:-us-east-1}"
HLS_TIME="${HLS_TIME:-2}"
HLS_LIST_SIZE="${HLS_LIST_SIZE:-5}"

HLS_DIR="/tmp/hls/${CAMERA_ID}"
S3_PREFIX="hls/${CAMERA_ID}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] hls-relay $*"; }

mkdir -p "$HLS_DIR"

# Background loop: sync HLS dir to S3 every second
sync_loop() {
    while true; do
        aws s3 sync "${HLS_DIR}/" "s3://${HLS_BUCKET}/${S3_PREFIX}/" \
            --region "$AWS_REGION" \
            --cache-control "no-cache,no-store,must-revalidate" \
            --quiet 2>/dev/null || true
        sleep 1
    done
}
sync_loop &
SYNC_PID=$!

trap 'kill "$SYNC_PID" 2>/dev/null; log "Stopped."; exit 0' TERM INT

log "Starting: camera=${CAMERA_ID} rtsp=${RTSP_URL}"

exec ffmpeg \
    -loglevel warning \
    -rtsp_transport tcp \
    -i "${RTSP_URL}" \
    -c:v copy \
    -an \
    -f hls \
    -hls_time "${HLS_TIME}" \
    -hls_list_size "${HLS_LIST_SIZE}" \
    -hls_flags delete_segments \
    -hls_segment_filename "${HLS_DIR}/seg%05d.ts" \
    "${HLS_DIR}/index.m3u8"
