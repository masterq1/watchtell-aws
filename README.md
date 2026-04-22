# WatchTell AWS

Serverless license-plate recognition and surveillance event system built entirely on AWS-native services. No EC2 instances, no always-on workers, no external caching services.

## Table of Contents

- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [AWS Services Used](#aws-services-used)
- [Prerequisites](#prerequisites)
- [First-Time Deployment](#first-time-deployment)
- [Starting the Camera Agent](#starting-the-camera-agent)
- [Starting the HLS Live Stream](#starting-the-hls-live-stream)
- [Using the Dashboard](#using-the-dashboard)
- [API Reference](#api-reference)
- [Configuration Reference](#configuration-reference)
- [Estimated Costs](#estimated-costs)
- [Design Document](#design-document)

---

## How It Works

1. **Camera agent** (`agent/camera_relay.py`) runs on any device with network access to your IP camera — a Raspberry Pi, laptop, or small home-lab VM. It captures JPEG keyframes from an RTSP stream whenever motion is detected, uploads them to S3, and drops a job message into an SQS queue.

2. **Rekognition Lambda** picks up each job from SQS (batch size 1), calls Amazon Rekognition `DetectText` on the S3 keyframe, filters the results for US license-plate patterns, and publishes the best match to a results queue.

3. **Step Functions pipeline** is triggered by the results queue. It runs four Lambda steps in sequence:
   - **ParseResult** — normalises the plate string and OCR confidence.
   - **ValidatePlate** — looks up the plate in SearchQuarry (vehicle registration API). Results are cached in DynamoDB for 24 hours so the same plate is never looked up twice in a day.
   - **StoreEvent** — persists the enriched event record to DynamoDB.
   - **CheckWatchlist** — checks the plate against your watchlist table. Sends an SNS alert if there is a hit.

4. **HTTP API** (API Gateway + Cognito JWT auth) exposes REST endpoints that the React SPA consumes. All endpoints require a valid Cognito access token.

5. **React SPA** is hosted in S3 and served through CloudFront. The same distribution proxies `/api/*` to API Gateway and `/hls/*` to the HLS S3 bucket for live video.

---

## Architecture

```
IP Camera (RTSP)
      │
      ├─── rtsp_relay.py (Option A — recommended) ─────────────────────────┐
      │    FFmpeg grabs 1 frame every N sec, uploads to S3 kvs-frames/     │
      │    No OpenCV. No motion detection. No SQS.                          │
      │                                                                     ▼
      │                                                            S3 ObjectCreated
      │                                                                     │
      │                                                            EventBridge rule
      │                                                                     │
      └─── camera_relay.py (Option B — local motion detection) ────────────┤
           OpenCV motion detection, upload keyframe, enqueue SQS job        │
                 │                                                           │
                 ▼                                                           │
         SQS alpr-queue ──► Lambda: rekognition_alpr ◄──────────────────────┘
                           │
                           ▼
                   SQS alpr-results
                           │
                           ▼
                   Lambda: sqs_trigger
                           │
                           ▼
                   Step Functions State Machine
                     ┌─────────────┐
                     │ ParseResult │
                     └──────┬──────┘
                            │
                     ┌──────▼──────┐
                     │ValidatePlate│◄── DynamoDB (plate-cache, 24h TTL)
                     │             │◄── SearchQuarry API
                     └──────┬──────┘
                            │
                     ┌──────▼──────┐
                     │ StoreEvent  │──► DynamoDB (events)
                     └──────┬──────┘
                            │
                     ┌──────▼──────────┐
                     │ CheckWatchlist  │──► DynamoDB (watchlist)
                     │                 │──► SNS alert (on hit)
                     └─────────────────┘

hls_relay.sh ──► FFmpeg ──► S3 (HLS segments) ──► CloudFront /hls/*

CloudFront
  /        ──► S3 SPA bucket (React app)
  /api/*   ──► API Gateway HTTP API ──► Lambda handlers
  /hls/*   ──► S3 HLS bucket (live stream)

API Gateway ──► Cognito JWT auth
              ──► Lambda: events, plates, watchlist, search, clips
              ──► DynamoDB (events, watchlist)
              ──► S3 (presigned clip URLs)
```

---

## AWS Services Used

| Service | Purpose |
|---|---|
| **Amazon Rekognition** | `DetectText` — extracts license plate text from JPEG keyframes |
| **Amazon S3** | Keyframe and video clip storage (Intelligent-Tiering, 365-day lifecycle); SPA hosting; HLS segment hosting |
| **Amazon SQS** | Decouples camera agent from ALPR Lambda; decouples ALPR results from Step Functions |
| **AWS Step Functions** | Orchestrates the four-step pipeline per event |
| **AWS Lambda** | All compute: ALPR, pipeline steps, API handlers |
| **Amazon DynamoDB** | Events table, watchlist table, plate validation cache (TTL) |
| **Amazon Cognito** | User pool + JWT auth for the HTTP API |
| **Amazon API Gateway (HTTP)** | REST API with Cognito JWT authorizer |
| **Amazon CloudFront** | CDN for SPA, API proxy, HLS live stream |
| **Amazon SNS** | Watchlist hit alerts |
| **AWS CloudTrail** | Audit log for all S3 data events and API calls |
| **AWS WAF** | Rate limiting and AWS managed common rule set on the API |
| **AWS KMS** | Customer-managed encryption key with annual rotation |
| **AWS CDK (Python)** | Infrastructure as code for all seven stacks |
| **AWS Systems Manager (SSM)** | Stores the SearchQuarry API key as a SecureString |

---

## Prerequisites

- AWS CLI configured with credentials for account `916918686359`
- Python 3.12+
- Node.js 18+ (for the frontend build)
- AWS CDK CLI: `npm install -g aws-cdk`
- FFmpeg (only needed on the camera agent host for HLS streaming)
- A SearchQuarry API key (for plate validation) — store it in SSM:

```bash
MSYS_NO_PATHCONV=1 aws ssm put-parameter \
  --name /watchtell/searchquarry/api_key \
  --value "YOUR_API_KEY" \
  --type SecureString \
  --region us-east-1
```

> **Windows / Git Bash users**: the `MSYS_NO_PATHCONV=1` prefix prevents Git Bash from converting the leading `/` in the parameter name to a Windows path.

---

## First-Time Deployment

```bash
# 1. Bootstrap CDK (one-time per account/region)
cd infrastructure
cdk bootstrap aws://916918686359/us-east-1

# 2. Install CDK dependencies
pip install -r requirements.txt

# 3. Deploy all stacks + build and upload the frontend
cd ..
./scripts/deploy.sh
```

The deploy script outputs the CloudFront domain at the end. Open it in a browser — that is your dashboard URL.

To deploy infrastructure only (skip the frontend build):

```bash
./scripts/deploy.sh --skip-frontend
```

To deploy a single stack:

```bash
cd infrastructure
cdk deploy WatchtellApi --require-approval never
```

**Stack deploy order** (CDK resolves dependencies automatically):

1. `WatchtellStorage` — S3 buckets, DynamoDB tables
2. `WatchtellQueue` — SQS queues
3. `WatchtellRekognition` — ALPR Lambda + SQS event source
4. `WatchtellPipeline` — Step Functions + pipeline Lambdas
5. `WatchtellApi` — API Gateway + Cognito + API Lambdas
6. `WatchtellCdn` — CloudFront distribution
7. `WatchtellSecurity` — WAF, KMS key, CloudTrail

---

## Starting the Camera Agent

There are two modes. Use **RTSP Relay** (recommended) if you want AWS to handle all analysis with no local processing. Use **Local Agent** if you want motion detection to run on-device before uploading.

---

### Option A — RTSP Relay (recommended): all processing in AWS

`rtsp_relay.py` grabs one JPEG frame every N seconds using FFmpeg and uploads it straight to S3. No OpenCV, no motion detection, no SQS. EventBridge routes each upload to the Rekognition Lambda — everything else runs in AWS.

**Requirements**: FFmpeg on PATH + `boto3`. Nothing else.

#### Run directly

```bash
cd agent
pip install boto3 python-dotenv

# agent/.env
CAMERA_ID=cam-driveway
RTSP_URL=rtsp://admin:yourpassword@192.168.1.50/stream1
MEDIA_BUCKET=watchtell-media-916918686359
EVENT_TYPE=unknown          # entry | exit | unknown
AWS_REGION=us-east-1
FRAME_INTERVAL=5            # seconds between captures

python rtsp_relay.py
```

To **update the camera URL**, change `RTSP_URL` in `.env` and restart.

#### Run with Docker (no local Python/FFmpeg install needed)

```bash
cd agent

# Edit docker-compose.yml — set CAMERA_ID, RTSP_URL, MEDIA_BUCKET
# Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, or mount ~/.aws

docker compose up -d rtsp-relay
```

The Docker image is built locally from the included `Dockerfile` (Python 3.12 + FFmpeg).

#### Running multiple cameras

One process per camera, each with its own `CAMERA_ID`:

```bash
CAMERA_ID=cam-driveway RTSP_URL=rtsp://... python rtsp_relay.py &
CAMERA_ID=cam-garage   RTSP_URL=rtsp://... python rtsp_relay.py &
```

---

### Option B — Local Agent: motion detection on-device

`camera_relay.py` uses OpenCV to detect motion locally, uploads only frames with activity, and enqueues an SQS message. More efficient on bandwidth; requires OpenCV.

#### Install dependencies

```bash
cd agent
pip install -r requirements.txt   # includes opencv-python-headless
```

#### Create a `.env` file

```bash
# agent/.env
CAMERA_ID=cam-driveway
RTSP_URL=rtsp://admin:yourpassword@192.168.1.50/stream1
EVENT_TYPE=entry          # entry | exit | unknown
MEDIA_BUCKET=watchtell-media-916918686359
QUEUE_URL=https://sqs.us-east-1.amazonaws.com/916918686359/watchtell-alpr-queue
AWS_REGION=us-east-1
CAPTURE_FPS=1
MOTION_THRESHOLD=2000     # pixel diff count; 0 = capture every frame
MIN_INTERVAL_SEC=3        # minimum seconds between uploads
```

#### Start the agent

```bash
python camera_relay.py
```

The agent reconnects automatically on stream failure with exponential backoff (cap: 60 s).

---

## Starting the HLS Live Stream

`hls_relay.sh` pulls the RTSP stream with FFmpeg, writes 2-second HLS segments to `/tmp/hls/<CAMERA_ID>/`, and syncs them to S3 every second. CloudFront serves them at `/hls/<CAMERA_ID>/index.m3u8`.

```bash
# Required env vars (same RTSP_URL as camera_relay.py)
export CAMERA_ID=cam-driveway
export RTSP_URL=rtsp://admin:yourpassword@192.168.1.50/stream1
export HLS_BUCKET=watchtell-hls-916918686359
export AWS_REGION=us-east-1

./hls_relay.sh
```

The frontend player points to `https://<cloudfront-domain>/hls/cam-driveway/index.m3u8`.

---

## Using the Dashboard

1. Open the CloudFront domain in a browser.
2. Sign in with your Cognito credentials. New users must be created in the AWS Console under **Cognito → User Pools → watchtell-users → Users** (self-signup is disabled).
3. The dashboard shows:
   - **Live feed** — HLS stream for each camera.
   - **Events** — paginated list of all detected plates with timestamp, camera, confidence, and validation status.
   - **Search** — filter events by plate number and/or date range.
   - **Plates** — full event history for a single plate.
   - **Watchlist** — add or remove plates to be alerted on.

### Adding a user

```bash
aws cognito-idp admin-create-user \
  --user-pool-id us-east-1_2noObkW1l \
  --username user@example.com \
  --temporary-password "Temp1234!" \
  --user-attributes Name=email,Value=user@example.com \
  --region us-east-1
```

### Adding a plate to the watchlist via the API

```bash
curl -s -X POST https://<cloudfront-domain>/api/watchlist \
  -H "Authorization: Bearer <jwt-token>" \
  -H "Content-Type: application/json" \
  -d '{"PlateNumber": "ABC1234", "Note": "Stolen vehicle"}'
```

---

## API Reference

All endpoints require `Authorization: Bearer <cognito-access-token>`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/events` | List recent events (paginated). Query: `?limit=50&last_key=<token>` |
| `GET` | `/events/{id}` | Get a single event by EventId |
| `GET` | `/plates/{plate}` | Get all events for a plate number |
| `GET` | `/search` | Search events. Query: `?plate=ABC1234&start=2025-01-01&end=2025-12-31` |
| `GET` | `/watchlist` | List all watchlist entries |
| `POST` | `/watchlist` | Add a plate. Body: `{"PlateNumber": "ABC1234", "Note": "..."}` |
| `DELETE` | `/watchlist/{plate}` | Remove a plate from the watchlist |
| `GET` | `/clips/{id+}` | Get a presigned S3 URL for a video clip |

---

## Configuration Reference

### `rtsp_relay.py` environment variables (Option A — recommended)

| Variable | Required | Default | Description |
|---|---|---|---|
| `CAMERA_ID` | Yes | — | Unique camera identifier (e.g. `cam-driveway`) |
| `RTSP_URL` | Yes | — | Full RTSP stream URL |
| `MEDIA_BUCKET` | Yes | — | S3 bucket name for frame uploads |
| `EVENT_TYPE` | No | `unknown` | `entry`, `exit`, or `unknown` |
| `AWS_REGION` | No | `us-east-1` | AWS region |
| `FRAME_INTERVAL` | No | `5` | Seconds between frame captures |

### `camera_relay.py` environment variables (Option B — local motion detection)

| Variable | Required | Default | Description |
|---|---|---|---|
| `CAMERA_ID` | Yes | — | Unique identifier for this camera |
| `RTSP_URL` | Yes | — | Full RTSP stream URL |
| `MEDIA_BUCKET` | Yes | — | S3 bucket name for keyframe uploads |
| `QUEUE_URL` | Yes | — | SQS URL for the ALPR job queue |
| `EVENT_TYPE` | No | `unknown` | `entry`, `exit`, or `unknown` |
| `AWS_REGION` | No | `us-east-1` | AWS region |
| `CAPTURE_FPS` | No | `1` | Frames evaluated per second |
| `MOTION_THRESHOLD` | No | `2000` | Pixel-diff count to trigger a capture; `0` disables motion gating |
| `MIN_INTERVAL_SEC` | No | `3` | Minimum seconds between uploads for this camera |

### HLS relay environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `CAMERA_ID` | Yes | — | Camera identifier (used as S3 prefix) |
| `RTSP_URL` | Yes | — | RTSP stream URL |
| `HLS_BUCKET` | Yes | — | S3 bucket for HLS segments |
| `AWS_REGION` | No | `us-east-1` | AWS region |
| `HLS_TIME` | No | `2` | Segment length in seconds |
| `HLS_LIST_SIZE` | No | `5` | Number of segments kept in the playlist |

---

## Estimated Costs

Costs below are estimates for a **single-camera home deployment** in `us-east-1` with approximately 500 motion-triggered events per day (≈15,000/month). Adjust the multiplier for additional cameras or higher-traffic environments.

All AWS services here fall within the free tier for the first 12 months of a new account except where noted.

| Service | Usage assumption | Est. monthly cost |
|---|---|---|
| **Rekognition** `DetectText` | 15,000 images/mo | ~$0.75 |
| **Lambda** | ~75,000 invocations, 512 MB, avg 2 s | < $0.10 |
| **Step Functions** | 15,000 executions × 4 steps | ~$0.36 |
| **SQS** | ~30,000 messages | < $0.01 |
| **DynamoDB** | On-demand; ~15,000 writes, ~30,000 reads | < $0.10 |
| **S3** (media) | ~15 GB keyframes + clips/mo, Intelligent-Tiering | ~$0.35 |
| **S3** (HLS + SPA) | < 1 GB, high request rate | < $0.10 |
| **API Gateway** | ~10,000 API calls/mo | < $0.05 |
| **CloudFront** | ~5 GB transfer, Price Class 100 | ~$0.50 |
| **CloudTrail** | 1 trail, management events free; data events ~$1.50/100k | ~$0.20 |
| **WAF** | 1 WebACL + 2 rules | ~$6.00 |
| **KMS** | 1 key + ~1,000 API calls | ~$1.01 |
| **SNS** | Minimal alerts | < $0.01 |
| **SearchQuarry** | External API — pricing varies by plan | See searchquarry.com |
| **Total (AWS)** | | **~$9–12 / month** |

> **WAF dominates the bill** at ~$5/WebACL + $1/rule/month. If cost is the primary concern, remove `WatchtellSecurity` from `app.py` before deploying — the API remains protected by Cognito JWT auth and API Gateway throttling.

> **Rekognition pricing**: first 1,000 images/month are free (free tier). Beyond that, $0.001–$0.0015 per image depending on volume tier.

---

## Design Document

### Goals

1. Replicate all functionality of the original Watchtell project using only AWS-managed services.
2. Eliminate the always-on EC2 Spot worker — the largest operational burden and the primary source of cost variability.
3. Remove all third-party runtime dependencies (Upstash Redis, OpenALPR C++ library).
4. Keep infrastructure reproducible and deployable with a single command.

### Key Design Decisions

#### Rekognition instead of OpenALPR

The original system ran an EC2 Spot instance with the OpenALPR C++ library compiled from source. This required: AMI management, Spot interruption handling, an ASG, a lifecycle hook, and a tarball deployment process.

Rekognition `DetectText` eliminates all of that. The Lambda accepts an S3 object reference — no base64 transfer, no large dependency layer. The trade-off is accuracy: Rekognition is a general OCR engine, not a dedicated ALPR model. The plate-format regex filter (`^[A-Z0-9]{1,4}[\s-]?[A-Z0-9]{1,4}[\s-]?[A-Z0-9]{0,4}$`) and minimum-confidence threshold (50%) are the primary quality controls. For a home deployment reading plates at close range in good lighting, this is sufficient.

#### DynamoDB TTL instead of Upstash Redis

The original system used Upstash Redis (a third-party managed Redis service) for plate validation caching. This was replaced with a DynamoDB table with a TTL attribute (`ExpiresAt`). Benefits:

- **No VPC required.** Redis clusters must live in a VPC; Lambda functions accessing them must also be in the VPC, which adds cold-start latency and networking complexity.
- **No external dependency.** Upstash is a third-party SaaS; DynamoDB is first-party AWS.
- **Same semantics.** DynamoDB TTL auto-expires items within a few minutes of the expiry time. The Lambda double-checks `ExpiresAt > now()` at read time to handle the eventual-consistency window.
- **Cost.** DynamoDB on-demand pricing is effectively zero at home-lab scale.

#### Seven CDK stacks, not one

Each stack represents a distinct infrastructure concern with its own lifecycle:

| Stack | Removal policy | Rationale |
|---|---|---|
| `WatchtellStorage` | RETAIN | Event data and media must survive stack teardown |
| `WatchtellQueue` | DESTROY | Queues are transient; messages drain within seconds |
| `WatchtellRekognition` | DESTROY | Stateless Lambda + SQS event source |
| `WatchtellPipeline` | DESTROY | Stateless Step Functions + Lambdas |
| `WatchtellApi` | DESTROY | Stateless API Lambdas + Cognito pool |
| `WatchtellCdn` | RETAIN | CloudFront distributions take ~15 min to recreate |
| `WatchtellSecurity` | RETAIN | CloudTrail bucket and KMS key must outlive everything else |

Splitting stacks also means a code change to a Lambda handler only triggers a deploy of `WatchtellApi` or `WatchtellPipeline`, not a full re-synthesis.

#### No VPC

Nothing in this deployment runs inside a VPC. All Lambda functions communicate with AWS services over HTTPS using IAM-scoped permissions. This eliminates NAT Gateways (~$32/month), VPC endpoints, subnet management, and security group rules. The trade-off is that Lambda functions cannot reach resources that are VPC-only (e.g., RDS, ElastiCache) — but this design deliberately avoids those services.

#### CloudFront as the single entry point

All client traffic — SPA, API calls, HLS stream — enters through a single CloudFront distribution. This provides:

- **HTTPS everywhere** with no certificate management (CloudFront provides the cert).
- **API proxying** without CORS complexity (same origin for the SPA and API).
- **HLS caching disabled** on `/hls/*` so the playlist is always fresh while segments are served from the edge.
- **SPA routing** — 403/404 responses from S3 are rewritten to `index.html` with a 200, enabling client-side routing.

#### Cognito for auth, not API keys

API keys are simple but offer no per-user identity, no expiry, and no revocation. Cognito JWT tokens expire after 1 hour, support MFA, and are verifiable by API Gateway without a Lambda authorizer (using the native JWT authorizer). The user pool is configured with email-only sign-in, no self-signup, and optional TOTP MFA.

#### Camera agent is a local process, not a Lambda

Capturing RTSP video in Lambda is impractical — streams are long-lived connections, Lambda has a 15-minute maximum timeout, and OpenCV with RTSP support is a large binary dependency. The camera agent is intentionally a lightweight Python script designed to run on whatever device has physical network access to the camera. It has no knowledge of the pipeline — it only uploads a JPEG and drops an SQS message.

#### SNS for watchlist alerts

SNS decouples alert delivery from the pipeline Lambda. Subscribers (email, SMS, Lambda, HTTP endpoint) can be added or removed from the SNS topic without redeploying any code. The `CheckWatchlist` Lambda publishes a structured JSON message; the topic handles fan-out.

### Data Flow Details

#### Event record schema (DynamoDB `watchtell-events`)

| Attribute | Type | Description |
|---|---|---|
| `EventId` | String (PK) | UUID assigned by `store_event.py` |
| `Timestamp` | String (SK) | ISO 8601 UTC timestamp of the recorded event |
| `CameraId` | String | Camera identifier from the agent |
| `PlateNumber` | String | Normalised plate (alphanumeric, no separators) |
| `PlateRaw` | String | Raw OCR output before normalisation |
| `Confidence` | Decimal | Rekognition confidence percentage (0–100) |
| `EventType` | String | `entry`, `exit`, or `unknown` |
| `ValidationStatus` | String | `valid`, `expired`, `suspended`, `stolen`, `unregistered`, `unknown` |
| `ValidationSource` | String | `searchquarry`, `cache`, or `none` |
| `S3Key` | String | S3 key of the source keyframe |
| `StoredAt` | String | ISO 8601 UTC timestamp of when the record was written |

#### GSIs on `watchtell-events`

| Index name | PK | SK | Use case |
|---|---|---|---|
| `PlateNumber-Timestamp-index` | PlateNumber | Timestamp | All events for a plate, newest first |
| `CameraId-Timestamp-index` | CameraId | Timestamp | Events for a camera in a time range |
| `EventType-Timestamp-index` | EventType | Timestamp | Filter by entry/exit type |

#### Plate validation cache schema (DynamoDB `watchtell-plate-cache`)

| Attribute | Type | Description |
|---|---|---|
| `PlateNumber` | String (PK) | Normalised plate number |
| `Status` | String | Cached validation result |
| `ExpiresAt` | Number | Unix epoch seconds; used by DynamoDB TTL and double-checked at read time |

### Security Posture

- **Authentication**: All API endpoints require a valid Cognito JWT. Unauthenticated requests return 401.
- **Authorisation**: IAM least-privilege. Each Lambda has exactly the permissions it needs — no wildcards except `rekognition:DetectText` (Rekognition does not support resource-level ARNs for `DetectText`).
- **Encryption at rest**: S3 (SSE-S3), DynamoDB (AWS-owned CMK). KMS customer key is provisioned for future use.
- **Encryption in transit**: HTTPS enforced at CloudFront, API Gateway, and S3 (bucket policy denies HTTP).
- **Audit**: CloudTrail logs all management events and S3 data events (clip access) for one year.
- **WAF**: Rate limiting (1,000 req/5 min per IP) and AWS managed common rule set protect the API.
- **Secrets**: SearchQuarry API key stored in SSM Parameter Store as a SecureString; retrieved at Lambda cold start via an SSM SDK call, not injected as a plaintext environment variable.
- **Self-signup disabled**: Only accounts created by an administrator can access the dashboard.
