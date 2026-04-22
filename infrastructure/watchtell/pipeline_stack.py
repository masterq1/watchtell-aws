"""
Step Functions pipeline + Lambda functions + SNS alerts.

Pipeline flow:
  SQS message (Rekognition result) → ParseResult → ValidatePlate → StoreEvent → CheckWatchlist → [Alert]

Changes from original Watchtell:
  - alpr_queue param removed (consumed by RekognitionStack, not here).
  - Upstash Redis env vars removed; replaced with PLATE_CACHE_TABLE (DynamoDB TTL).
  - No SSM lookups for Redis credentials — validation cache is now fully AWS-native.
"""
import json
import os
import shutil
import subprocess
import aws_cdk as cdk
import jsii
from aws_cdk import (
    Stack,
    Duration,
    aws_lambda as lambda_,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_sns as sns,
    aws_sqs as sqs,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda_event_sources as event_sources,
)
from constructs import Construct

LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_12
LAMBDA_TIMEOUT = Duration.seconds(30)

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


class PipelineStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        events_table: dynamodb.Table,
        watchlist_table: dynamodb.Table,
        plate_cache_table: dynamodb.Table,
        media_bucket: s3.Bucket,
        results_queue: sqs.Queue,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # SNS topic for watchlist hit alerts
        alerts_topic = sns.Topic(
            self, "AlertsTopic",
            topic_name="watchtell-alerts",
            display_name="WatchTell Alerts",
        )
        self.alerts_topic_arn = alerts_topic.topic_arn

        # Shared Lambda environment
        shared_env = {
            "EVENTS_TABLE": events_table.table_name,
            "WATCHLIST_TABLE": watchlist_table.table_name,
            "MEDIA_BUCKET": media_bucket.bucket_name,
            "ALERTS_TOPIC_ARN": alerts_topic.topic_arn,
        }

        # Lambda: parse Rekognition result from SQS message
        parse_fn = self._lambda("ParseResult", "pipeline/parse_result.handler", shared_env)

        # Lambda: validate plate — DynamoDB cache (24h TTL) → SearchQuarry plate lookup.
        # Upstash Redis + credentials replaced by a DynamoDB table; no VPC required.
        validate_fn = self._lambda("ValidatePlate", "pipeline/validate_plate.handler", {
            **shared_env,
            "PLATE_CACHE_TABLE": plate_cache_table.table_name,
            "SEARCHQUARRY_API_KEY": "{{resolve:ssm:/watchtell/searchquarry/api_key}}",
        })

        # Lambda: store event in DynamoDB
        store_fn = self._lambda("StoreEvent", "pipeline/store_event.handler", shared_env)

        # Lambda: check watchlist and dispatch alert if hit
        alert_fn = self._lambda("CheckWatchlist", "pipeline/check_watchlist.handler", shared_env)

        # Lambda: trigger Step Functions from SQS results queue
        trigger_fn = self._lambda("SqsTrigger", "pipeline/sqs_trigger.handler", shared_env)

        # Permissions
        events_table.grant_read_write_data(store_fn)
        events_table.grant_read_write_data(trigger_fn)
        watchlist_table.grant_read_data(alert_fn)
        watchlist_table.grant_read_data(trigger_fn)
        plate_cache_table.grant_read_write_data(validate_fn)
        media_bucket.grant_read_write(parse_fn)
        alerts_topic.grant_publish(alert_fn)

        # SearchQuarry SSM param read for validate Lambda
        validate_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/watchtell/searchquarry/*"],
        ))

        # Step Functions state machine
        parse_task = tasks.LambdaInvoke(
            self, "ParseResultTask",
            lambda_function=parse_fn,
            output_path="$.Payload",
        )
        validate_task = tasks.LambdaInvoke(
            self, "ValidatePlateTask",
            lambda_function=validate_fn,
            output_path="$.Payload",
        )
        store_task = tasks.LambdaInvoke(
            self, "StoreEventTask",
            lambda_function=store_fn,
            output_path="$.Payload",
        )
        alert_task = tasks.LambdaInvoke(
            self, "CheckWatchlistTask",
            lambda_function=alert_fn,
            output_path="$.Payload",
        )

        definition = (
            parse_task
            .next(validate_task)
            .next(store_task)
            .next(alert_task)
        )

        state_machine = sfn.StateMachine(
            self, "Pipeline",
            state_machine_name="watchtell-pipeline",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.minutes(5),
        )
        self.state_machine_arn = state_machine.state_machine_arn

        # Allow SQS trigger Lambda to start executions
        state_machine.grant_start_execution(trigger_fn)
        trigger_fn.add_environment("STATE_MACHINE_ARN", state_machine.state_machine_arn)

        # Wire results queue → trigger Lambda
        results_queue.grant_consume_messages(trigger_fn)
        trigger_fn.add_event_source(
            event_sources.SqsEventSource(
                results_queue,
                batch_size=1,
                max_batching_window=Duration.seconds(0),
            )
        )

    def _lambda(self, name: str, handler: str, env: dict) -> lambda_.Function:
        return lambda_.Function(
            self, name,
            function_name=f"watchtell-{name.lower().replace(' ', '-')}",
            runtime=LAMBDA_RUNTIME,
            handler=handler,
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
            timeout=LAMBDA_TIMEOUT,
            environment=env,
            memory_size=256,
        )
