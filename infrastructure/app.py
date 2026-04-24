#!/usr/bin/env python3
import aws_cdk as cdk
from watchtell.storage_stack import StorageStack
from watchtell.queue_stack import QueueStack
from watchtell.rekognition_stack import RekognitionStack
from watchtell.pipeline_stack import PipelineStack
from watchtell.api_stack import ApiStack
from watchtell.cdn_stack import CdnStack
from watchtell.security_stack import SecurityStack
from watchtell.fargate_stack import FargateStack

app = cdk.App()

env = cdk.Environment(account="916918686359", region="us-east-1")

storage = StorageStack(app, "WatchtellStorage", env=env)
queue = QueueStack(app, "WatchtellQueue", env=env)

# Rekognition Lambda replaces the EC2 Spot ALPR worker — no AMI/ASG/userdata required.
rekognition = RekognitionStack(
    app, "WatchtellRekognition",
    media_bucket=storage.media_bucket,
    alpr_queue=queue.alpr_queue,
    results_queue=queue.results_queue,
    env=env,
)

pipeline = PipelineStack(
    app, "WatchtellPipeline",
    events_table=storage.events_table,
    watchlist_table=storage.watchlist_table,
    plate_cache_table=storage.plate_cache_table,
    media_bucket=storage.media_bucket,
    results_queue=queue.results_queue,
    env=env,
)
api = ApiStack(
    app, "WatchtellApi",
    events_table=storage.events_table,
    watchlist_table=storage.watchlist_table,
    media_bucket=storage.media_bucket,
    pipeline_arn=pipeline.state_machine_arn,
    env=env,
)
cdn = CdnStack(
    app, "WatchtellCdn",
    api_url=api.api_url,
    env=env,
)
security = SecurityStack(
    app, "WatchtellSecurity",
    media_bucket=storage.media_bucket,
    events_table=storage.events_table,
    api_id=api.http_api_id,
    env=env,
)

fargate = FargateStack(
    app, "WatchtellFargate",
    hls_bucket=cdn.hls_bucket,
    camera_id="cam-doorway",
    env=env,
)

app.synth()
