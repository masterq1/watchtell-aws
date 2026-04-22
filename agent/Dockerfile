FROM python:3.12-slim

# FFmpeg for RTSP frame capture
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3 python-dotenv

WORKDIR /app
COPY rtsp_relay.py .

CMD ["python", "rtsp_relay.py"]
