"""
WatchTell Rekognition ALPR Lambda
----------------------------------
Handles two trigger sources:

  1. EventBridge S3 ObjectCreated event (from rtsp_relay.py)
     Key format: kvs-frames/{camera_id}/{event_type}/{timestamp}.jpg
     rtsp_relay.py uploads frames directly to S3; EventBridge routes the
     ObjectCreated event here. All frame analysis happens in AWS.

     EventBridge event shape:
       { "source": "aws.s3", "detail-type": "Object Created",
         "detail": { "bucket": {"name": "..."}, "object": {"key": "..."} },
         "time": "..." }

  2. SQS message (from camera_relay.py — legacy/local-processing path)
     Body: { job_id, camera_id, s3_key, event_type, recorded_at }
     Camera agent does OpenCV motion detection locally, uploads keyframe,
     and enqueues an SQS message pointing at the S3 key.

For each job (regardless of trigger):
  1. Call rekognition:DetectText with the S3 keyframe reference.
  2. Filter LINE detections for US license plate format.
  3. Select the highest-confidence candidate.
  4. Publish structured result to the results queue (consumed by sqs_trigger → Step Functions).

Environment variables:
  MEDIA_BUCKET         - S3 bucket containing keyframes
  RESULT_QUEUE_URL     - SQS URL for results
  ALPR_MIN_CONFIDENCE  - Minimum Rekognition confidence to accept (default: 50)
  ALPR_COUNTRY         - Plate region hint, informational (default: us)
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MEDIA_BUCKET     = os.environ["MEDIA_BUCKET"]
RESULT_QUEUE_URL = os.environ["RESULT_QUEUE_URL"]
MIN_CONFIDENCE   = float(os.environ.get("ALPR_MIN_CONFIDENCE", "50"))
ALPR_COUNTRY     = os.environ.get("ALPR_COUNTRY", "us")

_rekognition = boto3.client("rekognition")
_sqs         = boto3.client("sqs")

# US plate pattern: 2-8 uppercase alphanumeric characters (spaces/hyphens allowed mid-plate)
_PLATE_RE     = re.compile(r"^[A-Z0-9]{1,4}[\s\-]?[A-Z0-9]{1,4}[\s\-]?[A-Z0-9]{0,4}$")
_MIN_PLATE_LEN = 2
_MAX_PLATE_LEN = 8


def _is_plate_candidate(text: str) -> bool:
    text = text.strip().upper()
    stripped = re.sub(r"[\s\-]", "", text)
    if not (_MIN_PLATE_LEN <= len(stripped) <= _MAX_PLATE_LEN):
        return False
    if not stripped.isalnum():
        return False
    return bool(_PLATE_RE.match(text))


def _detect_plates(s3_key: str, bucket: str = MEDIA_BUCKET) -> tuple[str, float, list]:
    """
    Run Rekognition DetectText on the S3 keyframe.
    Returns (plate_number, confidence, raw_detections).
    """
    response = _rekognition.detect_text(
        Image={"S3Object": {"Bucket": bucket, "Name": s3_key}}
    )
    detections = response.get("TextDetections", [])
    logger.info("Rekognition returned %d text detections for %s", len(detections), s3_key)

    candidates = [
        d for d in detections
        if d.get("Type") == "LINE"
        and d.get("Confidence", 0) >= MIN_CONFIDENCE
        and _is_plate_candidate(d.get("DetectedText", ""))
    ]

    if not candidates:
        logger.info("No plate candidates found in %s", s3_key)
        return "UNKNOWN", 0.0, detections

    best = max(candidates, key=lambda d: d["Confidence"])
    plate = re.sub(r"[\s\-]", "", best["DetectedText"].strip().upper())
    confidence = round(float(best["Confidence"]), 2)
    logger.info("Best plate candidate: %s (confidence=%.1f%%)", plate, confidence)
    return plate, confidence, detections


def _parse_eventbridge_event(ev: dict) -> dict | None:
    """
    Parse an EventBridge S3 ObjectCreated event into a job dict.
    Key format: kvs-frames/{camera_id}/{event_type}/{timestamp}.jpg
    """
    detail  = ev.get("detail", {})
    bucket  = detail.get("bucket", {}).get("name", MEDIA_BUCKET)
    s3_key  = urllib.parse.unquote_plus(detail.get("object", {}).get("key", ""))

    if not s3_key.lower().endswith(".jpg"):
        logger.info("Ignoring non-JPEG S3 object: %s", s3_key)
        return None

    # kvs-frames/{camera_id}/{event_type}/{timestamp}.jpg
    parts = s3_key.split("/")
    camera_id   = parts[1] if len(parts) >= 2 else "unknown"
    event_type  = parts[2] if len(parts) >= 3 else "unknown"
    recorded_at = ev.get("time") or datetime.now(timezone.utc).isoformat()

    return {
        "job_id":      str(uuid.uuid4()),
        "camera_id":   camera_id,
        "s3_key":      s3_key,
        "bucket":      bucket,
        "event_type":  event_type,
        "recorded_at": recorded_at,
        "trigger":     "eventbridge",
    }


def _parse_sqs_record(record: dict) -> dict | None:
    """Parse an SQS message body into a job dict (legacy camera_relay.py path)."""
    body = json.loads(record["body"])
    if not body.get("s3_key"):
        logger.error("job_id=%s: missing s3_key, skipping", body.get("job_id"))
        return None
    return {
        "job_id":      body.get("job_id", ""),
        "camera_id":   body.get("camera_id", "unknown"),
        "s3_key":      body.get("s3_key", ""),
        "bucket":      MEDIA_BUCKET,
        "event_type":  body.get("event_type", "unknown"),
        "recorded_at": body.get("recorded_at", ""),
        "trigger":     "sqs",
    }


def _process_job(job: dict, reraise: bool) -> None:
    s3_key    = job["s3_key"]
    bucket    = job.get("bucket", MEDIA_BUCKET)
    job_id    = job["job_id"]
    camera_id = job["camera_id"]

    try:
        plate, confidence, raw = _detect_plates(s3_key, bucket)
    except Exception as exc:
        logger.exception("job_id=%s: Rekognition failed: %s", job_id, exc)
        if reraise:
            raise   # SQS will retry
        return      # S3 trigger: log and move on (no retry mechanism)

    result = {
        "job_id":       job_id,
        "camera_id":    camera_id,
        "s3_key":       s3_key,
        "event_type":   job["event_type"],
        "recorded_at":  job["recorded_at"],
        "plate_number": plate,
        "confidence":   confidence,
        "region":       ALPR_COUNTRY,
        "alpr_raw":     raw,
    }

    _sqs.send_message(QueueUrl=RESULT_QUEUE_URL, MessageBody=json.dumps(result))
    logger.info(
        "job_id=%s trigger=%s: published result plate=%s confidence=%.1f",
        job_id, job.get("trigger", "?"), plate, confidence,
    )


def handler(event: dict, context) -> None:
    # EventBridge events arrive as a single dict (no Records wrapper).
    if event.get("source") == "aws.s3":
        job = _parse_eventbridge_event(event)
        if job:
            _process_job(job, reraise=False)
        return

    # SQS events (from camera_relay.py) arrive with a Records list.
    for record in event.get("Records", []):
        job = _parse_sqs_record(record)
        if job:
            _process_job(job, reraise=True)
