"""
WatchTell Camera Agent — Keyframe Relay
----------------------------------------
Runs on any device with network access to your IP camera(s) and AWS
(laptop, Raspberry Pi, t3.nano, etc.).

Captures keyframes from RTSP streams, uploads to S3, and enqueues ALPR jobs
to SQS. The Rekognition Lambda in AWS then processes each job — no ALPR
libraries or heavy dependencies needed on this device.

Usage:
    python camera_relay.py

Configuration via environment variables (or .env file):
    CAMERA_ID        - Unique identifier for this camera (e.g. "cam-driveway")
    RTSP_URL         - Full RTSP URL  (e.g. "rtsp://admin:pass@192.168.1.50/stream1")
    EVENT_TYPE       - "entry" | "exit" | "unknown"  (default: "unknown")
    MEDIA_BUCKET     - S3 bucket name  (watchtell-media-916918686359)
    QUEUE_URL        - SQS queue URL   (watchtell-alpr-queue)
    AWS_REGION       - AWS region (default: us-east-1)
    CAPTURE_FPS      - Frames to evaluate per second (default: 1)
    MOTION_THRESHOLD - Pixel-diff threshold to trigger a capture (default: 2000)
                       Set to 0 to capture every CAPTURE_FPS frame regardless of motion.
    MIN_INTERVAL_SEC - Minimum seconds between uploads for the same camera (default: 3)

Requirements:
    pip install opencv-python-headless boto3 python-dotenv
"""
import io
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import boto3
import cv2
import numpy as np

# Optional: load a .env file if present in the same directory
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CAMERA_ID        = os.environ["CAMERA_ID"]
RTSP_URL         = os.environ["RTSP_URL"]
EVENT_TYPE       = os.environ.get("EVENT_TYPE", "unknown")
MEDIA_BUCKET     = os.environ["MEDIA_BUCKET"]
QUEUE_URL        = os.environ["QUEUE_URL"]
AWS_REGION       = os.environ.get("AWS_REGION", "us-east-1")
CAPTURE_FPS      = float(os.environ.get("CAPTURE_FPS", "1"))
MOTION_THRESHOLD = int(os.environ.get("MOTION_THRESHOLD", "2000"))
MIN_INTERVAL_SEC = float(os.environ.get("MIN_INTERVAL_SEC", "3"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("relay")

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
s3  = boto3.client("s3",  region_name=AWS_REGION)
sqs = boto3.client("sqs", region_name=AWS_REGION)


# ---------------------------------------------------------------------------
# Motion detection helper
# ---------------------------------------------------------------------------
class MotionDetector:
    def __init__(self, threshold: int):
        self.threshold = threshold
        self._prev_gray: np.ndarray | None = None

    def has_motion(self, frame: np.ndarray) -> bool:
        if self.threshold == 0:
            return True
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        if self._prev_gray is None:
            self._prev_gray = gray
            return False
        diff = cv2.absdiff(self._prev_gray, gray)
        self._prev_gray = gray
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        changed_pixels = int(np.sum(thresh) / 255)
        return changed_pixels >= self.threshold


# ---------------------------------------------------------------------------
# Upload + enqueue
# ---------------------------------------------------------------------------
def upload_and_enqueue(frame: np.ndarray, recorded_at: str) -> None:
    job_id = str(uuid.uuid4())
    s3_key = f"keyframes/{CAMERA_ID}/{recorded_at.replace(':', '-')}.jpg"

    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        log.error("Failed to encode frame as JPEG")
        return

    log.info("Uploading keyframe → s3://%s/%s", MEDIA_BUCKET, s3_key)
    s3.put_object(
        Bucket=MEDIA_BUCKET,
        Key=s3_key,
        Body=buf.tobytes(),
        ContentType="image/jpeg",
    )

    message = {
        "job_id":      job_id,
        "camera_id":   CAMERA_ID,
        "s3_key":      s3_key,
        "event_type":  EVENT_TYPE,
        "recorded_at": recorded_at,
    }
    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps(message),
    )
    log.info("Enqueued job %s for camera %s", job_id, CAMERA_ID)


# ---------------------------------------------------------------------------
# Main capture loop
# ---------------------------------------------------------------------------
def capture_loop() -> None:
    motion = MotionDetector(MOTION_THRESHOLD)
    frame_interval = 1.0 / CAPTURE_FPS
    last_upload = 0.0
    reconnect_delay = 5

    while True:
        log.info("Connecting to %s", RTSP_URL)
        cap = cv2.VideoCapture(RTSP_URL)

        if not cap.isOpened():
            log.error("Could not open stream — retrying in %ds", reconnect_delay)
            time.sleep(reconnect_delay)
            continue

        log.info("Stream open (camera=%s)", CAMERA_ID)
        reconnect_delay = 5  # reset backoff on successful connect

        while True:
            t_start = time.monotonic()

            ret, frame = cap.read()
            if not ret or frame is None:
                log.warning("Frame read failed — reconnecting")
                break

            now = time.monotonic()
            if (now - last_upload) >= MIN_INTERVAL_SEC and motion.has_motion(frame):
                recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
                try:
                    upload_and_enqueue(frame, recorded_at)
                    last_upload = now
                except Exception as exc:
                    log.exception("Upload/enqueue failed: %s", exc)

            elapsed = time.monotonic() - t_start
            sleep_for = frame_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

        cap.release()
        log.info("Reconnecting in %ds…", reconnect_delay)
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)


if __name__ == "__main__":
    missing = [v for v in ("CAMERA_ID", "RTSP_URL", "MEDIA_BUCKET", "QUEUE_URL") if not os.environ.get(v)]
    if missing:
        log.critical("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)
    capture_loop()
