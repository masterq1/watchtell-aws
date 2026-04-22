"""Thin DynamoDB helpers shared across Lambda handlers."""
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

_dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "watchtell-events")
WATCHLIST_TABLE = os.environ.get("WATCHLIST_TABLE", "watchtell-watchlist")


def _normalize_event(item: dict) -> dict:
    """Coerce string Confidence to Decimal for records written before the schema fix."""
    conf = item.get("Confidence")
    if isinstance(conf, str):
        try:
            item = {**item, "Confidence": Decimal(conf)}
        except Exception:
            pass
    return item


def events_table():
    return _dynamodb.Table(EVENTS_TABLE)


def watchlist_table():
    return _dynamodb.Table(WATCHLIST_TABLE)


def get_event(event_id: str) -> dict | None:
    resp = events_table().query(
        KeyConditionExpression=Key("EventId").eq(event_id),
        Limit=1,
    )
    items = resp.get("Items", [])
    return _normalize_event(items[0]) if items else None


def list_events(limit: int = 50, last_key: dict | None = None) -> dict:
    scan_kwargs: dict[str, Any] = {"Limit": limit}
    if last_key:
        scan_kwargs["ExclusiveStartKey"] = last_key

    resp = events_table().scan(**scan_kwargs)
    return {
        "items": [_normalize_event(i) for i in resp.get("Items", [])],
        "last_key": resp.get("LastEvaluatedKey"),
    }


def query_events_by_plate(plate_number: str, limit: int = 50) -> list[dict]:
    resp = events_table().query(
        IndexName="PlateNumber-Timestamp-index",
        KeyConditionExpression=Key("PlateNumber").eq(plate_number),
        ScanIndexForward=False,
        Limit=limit,
    )
    return [_normalize_event(i) for i in resp.get("Items", [])]


def query_events_by_camera(camera_id: str, start: str, end: str, limit: int = 100) -> list[dict]:
    resp = events_table().query(
        IndexName="CameraId-Timestamp-index",
        KeyConditionExpression=(
            Key("CameraId").eq(camera_id) & Key("Timestamp").between(start, end)
        ),
        ScanIndexForward=False,
        Limit=limit,
    )
    return [_normalize_event(i) for i in resp.get("Items", [])]


def search_events(plate: str | None, start: str | None, end: str | None, limit: int = 50) -> list[dict]:
    if plate:
        items = query_events_by_plate(plate, limit)
        if start or end:
            items = [
                i for i in items
                if (not start or i.get("Timestamp", "") >= start)
                and (not end or i.get("Timestamp", "") <= end)
            ]
        return items

    # Full scan with optional date filter (low-traffic path)
    filter_parts = []
    expr_values: dict[str, Any] = {}
    if start:
        filter_parts.append("Timestamp >= :start")
        expr_values[":start"] = start
    if end:
        filter_parts.append("Timestamp <= :end")
        expr_values[":end"] = end

    kwargs: dict[str, Any] = {"Limit": limit}
    if filter_parts:
        kwargs["FilterExpression"] = " AND ".join(filter_parts)
        kwargs["ExpressionAttributeValues"] = expr_values

    resp = events_table().scan(**kwargs)
    return [_normalize_event(i) for i in resp.get("Items", [])]
