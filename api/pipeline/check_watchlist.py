"""
Step Functions Lambda: check plate against watchlist; send SNS alert on hit.
"""
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_dynamodb = boto3.resource("dynamodb")
_sns = boto3.client("sns")

WATCHLIST_TABLE = os.environ.get("WATCHLIST_TABLE", "watchtell-watchlist")
ALERTS_TOPIC_ARN = os.environ.get("ALERTS_TOPIC_ARN", "")


def handler(event: dict, context) -> dict:
    plate = event.get("plate_number", "")
    if not plate or plate == "UNKNOWN":
        return {**event, "watchlist_hit": False}

    table = _dynamodb.Table(WATCHLIST_TABLE)
    resp = table.get_item(Key={"PlateNumber": plate})
    item = resp.get("Item")

    if not item:
        return {**event, "watchlist_hit": False}

    # Watchlist hit — publish SNS alert
    logger.warning("WATCHLIST HIT: %s", plate)
    message = {
        "alert_type": "watchlist_hit",
        "plate_number": plate,
        "camera_id": event.get("camera_id"),
        "event_id": event.get("event_id"),
        "recorded_at": event.get("recorded_at"),
        "validation_status": event.get("validation_status"),
        "note": item.get("Note", ""),
    }

    if ALERTS_TOPIC_ARN:
        try:
            _sns.publish(
                TopicArn=ALERTS_TOPIC_ARN,
                Subject=f"WatchTell Alert: {plate} detected",
                Message=json.dumps(message, indent=2),
                MessageAttributes={
                    "alert_type": {
                        "DataType": "String",
                        "StringValue": "watchlist_hit",
                    }
                },
            )
        except ClientError as exc:
            logger.error("SNS publish failed: %s", exc)

    return {**event, "watchlist_hit": True, "watchlist_note": item.get("Note", "")}
