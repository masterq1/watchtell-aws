"""
RekognitionStack — replaces the EC2 Spot ALPR worker.

Instead of an always-on EC2 instance running OpenALPR, this stack deploys a
Lambda function that is SQS-triggered from the ALPR job queue.  For each job:
  1. Download the JPEG keyframe from S3.
  2. Call rekognition:DetectText (S3 object reference — no base64 transfer).
  3. Filter LINE detections for license-plate format.
  4. Publish the best match (or UNKNOWN) to the results queue.

Benefits over EC2 approach:
  - Zero idle cost (pay per invocation).
  - No AMI maintenance, no OpenALPR C++ build chain, no Spot interruption logic.
  - Scales automatically with SQS depth.
"""
import os
import shutil
import subprocess

import aws_cdk as cdk
import jsii
from aws_cdk import (
    Stack,
    Duration,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_events as events,
    aws_events_targets as targets,
    aws_sqs as sqs,
    aws_s3 as s3,
    aws_iam as iam,
)
from constructs import Construct

LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_12

_API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../api"))


@jsii.implements(cdk.ILocalBundling)
class _LocalPipBundler:
    """Bundles Lambda code + pip dependencies locally (no Docker required)."""

    def try_bundle(self, output_dir: str, _options=None, /, **_kwargs) -> bool:
        subprocess.run(
            ["pip", "install", "-r", "requirements.txt", "-t", output_dir, "--quiet"],
            cwd=_API_DIR,
            check=True,
        )
        for item in os.listdir(_API_DIR):
            src = os.path.join(_API_DIR, item)
            dst = os.path.join(output_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        return True


class RekognitionStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        media_bucket: s3.Bucket,
        alpr_queue: sqs.Queue,
        results_queue: sqs.Queue,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        fn = lambda_.Function(
            self, "RekognitionAlpr",
            function_name="watchtell-rekognition-alpr",
            runtime=LAMBDA_RUNTIME,
            handler="pipeline/rekognition_alpr.handler",
            code=lambda_.Code.from_asset(
                "../api",
                bundling=cdk.BundlingOptions(
                    image=LAMBDA_RUNTIME.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output --quiet"
                        " && cp -r . /asset-output",
                    ],
                    local=_LocalPipBundler(),
                ),
            ),
            timeout=Duration.seconds(30),
            memory_size=512,
            environment={
                "MEDIA_BUCKET": media_bucket.bucket_name,
                "RESULT_QUEUE_URL": results_queue.queue_url,
                "ALPR_COUNTRY": "us",
                "ALPR_MIN_CONFIDENCE": "50",
            },
        )

        # S3: read keyframes
        media_bucket.grant_read(fn)

        # SQS: consume from job queue, publish to results queue
        alpr_queue.grant_consume_messages(fn)
        results_queue.grant_send_messages(fn)

        # Rekognition: DetectText on S3 objects
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["rekognition:DetectText"],
            resources=["*"],
        ))

        # Wire SQS → Lambda (batch_size=1 so each job is processed independently)
        # This handles the legacy camera_relay.py path (local OpenCV agent).
        fn.add_event_source(
            event_sources.SqsEventSource(
                alpr_queue,
                batch_size=1,
                max_batching_window=Duration.seconds(0),
            )
        )

        # Wire S3 ObjectCreated → Lambda for the rtsp_relay.py path.
        # rtsp_relay.py uploads frames to kvs-frames/{camera_id}/{event_type}/*.jpg
        # with no local processing — Rekognition DetectText runs here in AWS.
        #
        # Uses EventBridge (not direct S3 notification) to avoid a cross-stack
        # dependency cycle between WatchtellStorage and WatchtellRekognition.
        # EventBridge must be enabled on the bucket (event_bridge_enabled=True in StorageStack).
        s3_frame_rule = events.Rule(
            self, "S3FrameRule",
            rule_name="watchtell-kvs-frame-trigger",
            description="Trigger Rekognition ALPR Lambda when rtsp_relay uploads a frame to S3",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [media_bucket.bucket_name]},
                    "object": {"key": [{"prefix": "kvs-frames/"}]},
                },
            ),
        )
        s3_frame_rule.add_target(targets.LambdaFunction(fn))
