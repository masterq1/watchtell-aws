"""
Lambda handler: GET /clips/{id}
Returns a pre-signed S3 URL for a video clip or keyframe.
URL expires after 15 minutes.
"""
import json
import os
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError

from shared.auth import require_auth

MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "")
PRESIGN_EXPIRY = 900  # 15 minutes

s3 = boto3.client("s3")


def handler(event: dict, context) -> dict:
    try:
        require_auth(event)
    except ValueError:
        return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

    path_params = event.get("pathParameters") or {}
    clip_id = path_params.get("id") or ""
    if not clip_id:
        return {"statusCode": 400, "body": json.dumps({"error": "id is required"})}

    s3_key = unquote(clip_id)

    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": MEDIA_BUCKET, "Key": s3_key},
            ExpiresIn=PRESIGN_EXPIRY,
        )
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"url": url, "expires_in": PRESIGN_EXPIRY}),
        }
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchKey":
            return {"statusCode": 404, "body": json.dumps({"error": "Clip not found"})}
        raise
