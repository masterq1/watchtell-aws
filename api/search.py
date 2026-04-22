"""
Lambda handler: GET /search?plate=&start=&end=&limit=
"""
import json
from decimal import Decimal

from shared.dynamo import search_events
from shared.auth import require_auth


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def handler(event: dict, context) -> dict:
    try:
        require_auth(event)
    except ValueError:
        return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

    q = event.get("queryStringParameters") or {}
    plate = (q.get("plate") or "").upper().strip() or None
    start = q.get("start") or None   # ISO-8601 e.g. "2024-01-15T00:00:00Z"
    end = q.get("end") or None
    limit = min(int(q.get("limit", 50)), 200)

    items = search_events(plate=plate, start=start, end=end, limit=limit)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"results": items, "count": len(items)}, cls=_DecimalEncoder),
    }
