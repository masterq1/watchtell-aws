"""
WatchTell RTSP Relay — thin, AWS-side-processing edition
---------------------------------------------------------
Grabs one JPEG frame every FRAME_INTERVAL seconds from an RTSP camera using
FFmpeg and uploads it directly to S3. That's all it does.

No OpenCV. No motion detection. No SQS. No plate format logic.
All intelligence (Rekognition, Step Functions pipeline) runs in AWS, triggered
by the S3 ObjectCreated event on the uploaded frame.

Requirements:
    pip install boto3 python-dotenv
    ffmpeg must be on PATH (apt install ffmpeg / brew install ffmpeg)

Configuration via environment variables (or .env file):
    CAMERA_ID        - Unique camera identifier (e.g. "cam-driveway")
    RTSP_URL_PARAM   - SSM parameter name for the RTSP URL
                       (default: /watchtell/relay/rtsp_url)
    MEDIA_BUCKET     - S3 bucket name (watchtell-media-916918686359)
    EVENT_TYPE       - entry | exit | unknown  (default: unknown)
    AWS_REGION       - AWS region (default: us-east-1)
    FRAME_INTERVAL   - Seconds between frame captures (default: 5)

    Store the RTSP URL in SSM (run once):
        MSYS_NO_PATHCONV=1 aws ssm put-parameter \\
            --name /watchtell/relay/rtsp_url \\
            --value "rtsp://admin:pass@192.168.1.50/stream1" \\
            --type SecureString
"""
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import boto3

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rtsp-relay")

AWS_REGION     = os.environ.get("AWS_REGION", "us-east-1")
CAMERA_ID      = os.environ["CAMERA_ID"]
MEDIA_BUCKET   = os.environ["MEDIA_BUCKET"]
EVENT_TYPE     = os.environ.get("EVENT_TYPE", "unknown")
FRAME_INTERVAL = float(os.environ.get("FRAME_INTERVAL", "5"))

_ssm = boto3.client("ssm", region_name=AWS_REGION)
_s3  = boto3.client("s3",  region_name=AWS_REGION)


def _get_rtsp_url() -> str:
    """Fetch the RTSP URL from SSM Parameter Store (SecureString, decrypted)."""
    param_name = os.environ.get(
        "RTSP_URL_PARAM",
        "/watchtell/relay/rtsp_url",
    )
    try:
        resp = _ssm.get_parameter(Name=param_name, WithDecryption=True)
        url = resp["Parameter"]["Value"]
        log.info("Loaded RTSP URL from SSM parameter: %s", param_name)
        return url
    except Exception as exc:
        log.critical("Failed to fetch RTSP URL from SSM (%s): %s", param_name, exc)
        sys.exit(1)


RTSP_URL  = _get_rtsp_url()
_S3_PREFIX = f"kvs-frames/{CAMERA_ID}"


def grab_frame() -> bytes | None:
    """Use FFmpeg to pull one JPEG frame from the RTSP stream."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-i", RTSP_URL,
                "-frames:v", "1",
                "-f", "image2",
                "-vcodec", "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            timeout=20,
        )
        if result.returncode != 0:
            log.warning("ffmpeg exited %d: %s", result.returncode, result.stderr.decode(errors="replace")[:200])
            return None
        if not result.stdout:
            log.warning("ffmpeg produced no output")
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg timed out after 20s")
        return None
    except FileNotFoundError:
        log.critical("ffmpeg not found — install it and ensure it is on PATH")
        sys.exit(1)


def upload(frame: bytes, timestamp: str) -> None:
    """Upload JPEG frame to S3. Embeds EVENT_TYPE in the key so Lambda can read it."""
    s3_key = f"{_S3_PREFIX}/{EVENT_TYPE}/{timestamp}.jpg"
    _s3.put_object(
        Bucket=MEDIA_BUCKET,
        Key=s3_key,
        Body=frame,
        ContentType="image/jpeg",
        Metadata={
            "camera-id":   CAMERA_ID,
            "event-type":  EVENT_TYPE,
            "recorded-at": timestamp,
        },
    )
    log.info("Uploaded %d bytes → s3://%s/%s", len(frame), MEDIA_BUCKET, s3_key)


def main() -> None:
    log.info(
        "rtsp-relay starting: camera=%s interval=%.1fs bucket=%s",
        CAMERA_ID, FRAME_INTERVAL, MEDIA_BUCKET,
    )
    consecutive_failures = 0

    while True:
        t0 = time.monotonic()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")

        frame = grab_frame()
        if frame:
            try:
                upload(frame, timestamp)
                consecutive_failures = 0
            except Exception as exc:
                log.exception("S3 upload failed: %s", exc)
                consecutive_failures += 1
        else:
            consecutive_failures += 1

        if consecutive_failures >= 10:
            log.error("10 consecutive failures — check camera connectivity and AWS credentials")
            consecutive_failures = 0

        elapsed = time.monotonic() - t0
        sleep_for = max(0.0, FRAME_INTERVAL - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    missing = [v for v in ("CAMERA_ID", "MEDIA_BUCKET") if not os.environ.get(v)]
    if missing:
        log.critical("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)
    main()
