"""
Lambda handler: GET /watchlist, POST /watchlist, DELETE /watchlist/{plate}
"""
import json
import os

import boto3

from shared.auth import require_auth

_dynamodb = boto3.resource("dynamodb")
WATCHLIST_TABLE = os.environ.get("WATCHLIST_TABLE", "watchtell-watchlist")


def _table():
    return _dynamodb.Table(WATCHLIST_TABLE)


def _ok(body, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _err(status: int, msg: str) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": msg})}


def handler(event: dict, context) -> dict:
    try:
        require_auth(event)
    except ValueError:
        return _err(401, "Unauthorized")

    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path_params = event.get("pathParameters") or {}

    if method == "GET":
        resp = _table().scan(Limit=500)
        return _ok({"watchlist": resp.get("Items", [])})

    if method == "POST":
        body = json.loads(event.get("body") or "{}")
        plate = (body.get("plate") or "").upper().strip()
        note = body.get("note", "")
        if not plate:
            return _err(400, "plate is required")
        _table().put_item(Item={"PlateNumber": plate, "Note": note})
        return _ok({"added": plate}, status=201)

    if method == "DELETE":
        plate = (path_params.get("plate") or "").upper().strip()
        if not plate:
            return _err(400, "plate is required")
        _table().delete_item(Key={"PlateNumber": plate})
        return _ok({"deleted": plate})

    return _err(405, "Method not allowed")
