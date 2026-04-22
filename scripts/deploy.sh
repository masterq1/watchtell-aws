#!/usr/bin/env bash
# Deploy all WatchTell-AWS stacks + build and sync frontend SPA.
#
# Changes from original watchtell/scripts/deploy.sh:
#   - Worker tarball upload removed (no EC2 worker; Rekognition runs in Lambda).
#   - Stack names unchanged so the same Cognito user pool / DynamoDB tables are reused
#     if migrating from the original Watchtell deployment.
#
# Usage:
#   ./scripts/deploy.sh [--skip-frontend]
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
SKIP_FRONTEND=${1:-""}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# ---- Deploy CDK stacks ----
log "Deploying CDK stacks..."
cd "$ROOT/infrastructure"
cdk deploy --all --require-approval never

# ---- Build and deploy frontend ----
if [[ "$SKIP_FRONTEND" != "--skip-frontend" ]]; then
    log "Building frontend..."
    cd "$ROOT/frontend"
    npm run build

    # Fetch SPA bucket name from CDK outputs
    SPA_BUCKET=$(aws cloudformation describe-stacks \
        --stack-name WatchtellCdn \
        --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='SpaBucketName'].OutputValue" \
        --output text)

    DIST_DOMAIN=$(aws cloudformation describe-stacks \
        --stack-name WatchtellCdn \
        --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='DistributionDomain'].OutputValue" \
        --output text)

    log "Syncing to s3://$SPA_BUCKET..."
    # JS — must have correct MIME type for ES module loading
    aws s3 sync dist/ "s3://$SPA_BUCKET" \
        --delete \
        --cache-control "public,max-age=31536000,immutable" \
        --exclude "index.html" \
        --exclude "*.css" \
        --exclude "*.map" \
        --content-type "application/javascript"
    # CSS
    aws s3 sync dist/ "s3://$SPA_BUCKET" \
        --cache-control "public,max-age=31536000,immutable" \
        --exclude "*" \
        --include "*.css" \
        --content-type "text/css"

    # index.html must never be cached
    aws s3 cp dist/index.html "s3://$SPA_BUCKET/index.html" \
        --cache-control "no-cache,no-store,must-revalidate"

    # Invalidate CloudFront
    CF_ID=$(aws cloudfront list-distributions \
        --query "DistributionList.Items[?Comment=='watchtell-cdn'].Id" \
        --output text)
    if [[ -n "$CF_ID" ]]; then
        aws cloudfront create-invalidation --distribution-id "$CF_ID" --paths "/*"
        log "CloudFront invalidation created for $CF_ID"
    fi

    log "Frontend deployed. Domain: $DIST_DOMAIN"
fi

log "=== Deployment complete ==="
log ""
log "Next: configure and start the camera agent on your local device:"
log "  cd agent"
log "  pip install -r requirements.txt"
log "  export CAMERA_ID=cam-driveway"
log "  export RTSP_URL=rtsp://admin:pass@192.168.1.50/stream1"
log "  export MEDIA_BUCKET=watchtell-media-916918686359"
log "  export QUEUE_URL=https://sqs.us-east-1.amazonaws.com/916918686359/watchtell-alpr-queue"
log "  python camera_relay.py"
