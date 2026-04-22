"""
Step Functions Lambda: validate plate against SearchQuarry.

Cache chain (AWS-native, no third-party Redis):
  1. DynamoDB TTL table (watchtell-plate-cache) — 24 h TTL.
     Read:  GetItem → check ExpiresAt > now.
     Write: PutItem with ExpiresAt = now + 86400.
  2. SearchQuarry plate lookup — direct plate number → registration status.

Replaces Upstash Redis with DynamoDB TTL. No VPC, no Redis cluster,
no per-request network hop to an external cache service.

Result codes: valid | expired | suspended | stolen | unregistered | unknown

Required SSM parameter (resolved at deploy time):
  /watchtell/searchquarry/api_key

Required env var:
  PLATE_CACHE_TABLE  - DynamoDB table name for validation cache
"""
from __future__ import annotations

import logging
import os
import time

import boto3
import requests
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

PLATE_CACHE_TABLE    = os.environ.get("PLATE_CACHE_TABLE", "watchtell-plate-cache")
SEARCHQUARRY_API_KEY = os.environ.get("SEARCHQUARRY_API_KEY", "")
CACHE_TTL            = 86400  # 24 hours

# SearchQuarry status strings → canonical codes
_STATUS_MAP = {
    "active":       "valid",
    "valid":        "valid",
    "expired":      "expired",
    "suspended":    "suspended",
    "revoked":      "suspended",
    "cancelled":    "suspended",
    "stolen":       "stolen",
    "unregistered": "unregistered",
    "not found":    "unregistered",
}

_dynamodb = boto3.resource("dynamodb")


def _cache_get(plate: str) -> str | None:
    """Return cached status if present and not expired, else None."""
    try:
        resp = _dynamodb.Table(PLATE_CACHE_TABLE).get_item(Key={"PlateNumber": plate})
        item = resp.get("Item")
        if not item:
            return None
        # DynamoDB TTL deletion is eventual — double-check ExpiresAt ourselves.
        if int(item.get("ExpiresAt", 0)) < int(time.time()):
            return None
        return item.get("Status")
    except ClientError as exc:
        logger.warning("DynamoDB cache GET failed: %s", exc)
        return None


def _cache_set(plate: str, status: str) -> None:
    """Write status to DynamoDB cache with a 24 h TTL."""
    if status == "unknown":
        return  # don't cache failures
    try:
        _dynamodb.Table(PLATE_CACHE_TABLE).put_item(Item={
            "PlateNumber": plate,
            "Status":      status,
            "ExpiresAt":   int(time.time()) + CACHE_TTL,
        })
    except ClientError as exc:
        logger.warning("DynamoDB cache PUT failed: %s", exc)


def _check_searchquarry(plate: str) -> str | None:
    """
    Call SearchQuarry license plate API.
    Docs: https://www.searchquarry.com/api-documentation/
    GET https://api.searchquarry.com/license_plate/?term=<PLATE>&api_key=<KEY>
    """
    if not SEARCHQUARRY_API_KEY:
        logger.warning("SEARCHQUARRY_API_KEY not set")
        return None
    try:
        resp = requests.get(
            "https://api.searchquarry.com/license_plate/",
            params={"term": plate, "api_key": SEARCHQUARRY_API_KEY},
            timeout=6,
        )
        if resp.status_code != 200:
            logger.warning("SearchQuarry HTTP %d for plate %s", resp.status_code, plate)
            return None

        data = resp.json()

        # Primary status field
        raw = (data.get("status") or data.get("registration_status") or "").lower().strip()
        if raw in _STATUS_MAP:
            return _STATUS_MAP[raw]

        # Fallback: message text
        message = (data.get("message") or "").lower()
        if "not found" in message or "no record" in message:
            return "unregistered"
        if "stolen" in message:
            return "stolen"

        logger.info("SearchQuarry unrecognised response for %s: %s", plate, data)
    except requests.RequestException as exc:
        logger.warning("SearchQuarry request error: %s", exc)
    return None


def handler(event: dict, context) -> dict:
    plate = event.get("plate_number", "")
    if not plate or plate == "UNKNOWN":
        return {**event, "validation_status": "unknown", "validation_source": "none"}

    cached = _cache_get(plate)
    if cached:
        logger.info("Cache hit: plate=%s status=%s", plate, cached)
        return {**event, "validation_status": cached, "validation_source": "cache"}

    status = _check_searchquarry(plate)
    source = "searchquarry"

    if not status:
        status = "unknown"
        source = "none"

    _cache_set(plate, status)
    logger.info("Validated plate=%s status=%s source=%s", plate, status, source)
    return {**event, "validation_status": status, "validation_source": source}
