#!/usr/bin/env bash
# Bootstrap CDK environment and install dependencies.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

log "Installing infrastructure Python deps..."
cd "$ROOT/infrastructure"
pip install -r requirements.txt

log "CDK bootstrap..."
cdk bootstrap aws://916918686359/us-east-1

if [[ -d "$ROOT/frontend" ]]; then
    log "Installing frontend npm deps..."
    cd "$ROOT/frontend"
    npm install
fi

log "=== Bootstrap complete ==="
