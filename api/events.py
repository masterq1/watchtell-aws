"""
Lambda handler: GET /events, GET /events/{id}
"""
import json
from decimal import Decimal

from shared.dynamo import get_event, list_events
from shared.auth import require_auth


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def _ok(body) -> dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _err(status: int, message: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def handler(event: dict, context) -> dict:
    try:
        require_auth(event)
    except ValueError:
        return _err(401, "Unauthorized")

    path_params = event.get("pathParameters") or {}
    event_id = path_params.get("id")

    if event_id:
        item = get_event(event_id)
        if not item:
            return _err(404, "Event not found")
        return _ok(item)

    # List events
    query = event.get("queryStringParameters") or {}
    limit = min(int(query.get("limit", 50)), 200)
    last_key_raw = query.get("lastKey")
    last_key = json.loads(last_key_raw) if last_key_raw else None

    result = list_events(limit=limit, last_key=last_key)
    return _ok({
        "events": result["items"],
        "nextKey": json.dumps(result["last_key"]) if result["last_key"] else None,
    })
