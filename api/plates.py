"""
Lambda handler: GET /plates/{plate}
Returns all events for a given plate number.
"""
import json
from decimal import Decimal

from shared.dynamo import query_events_by_plate
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

    path_params = event.get("pathParameters") or {}
    plate = (path_params.get("plate") or "").upper().strip()
    if not plate:
        return {"statusCode": 400, "body": json.dumps({"error": "plate is required"})}

    query = event.get("queryStringParameters") or {}
    limit = min(int(query.get("limit", 50)), 200)

    events = query_events_by_plate(plate, limit=limit)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"plate": plate, "events": events}, cls=_DecimalEncoder),
    }
