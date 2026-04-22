"""
Step Functions Lambda: persist the enriched event to DynamoDB.
"""
import os
import uuid
from decimal import Decimal
from datetime import datetime, timezone

import boto3

_dynamodb = boto3.resource("dynamodb")
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "watchtell-events")


def handler(event: dict, context) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    event_id = event.get("job_id") or str(uuid.uuid4())

    item = {
        "EventId": event_id,
        "Timestamp": event.get("recorded_at") or now,
        "CameraId": event.get("camera_id", "unknown"),
        "PlateNumber": event.get("plate_number", "UNKNOWN"),
        "PlateRaw": event.get("plate_raw", ""),
        "Confidence": Decimal(str(event.get("confidence", 0))),
        "EventType": event.get("event_type", "unknown"),
        "ValidationStatus": event.get("validation_status", "unknown"),
        "ValidationSource": event.get("validation_source", "none"),
        "S3Key": event.get("s3_key", ""),
        "StoredAt": now,
    }

    _dynamodb.Table(EVENTS_TABLE).put_item(Item=item)
    return {**event, "event_id": event_id, "stored": True}
