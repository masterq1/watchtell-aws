"""
Lambda: triggered by SQS ALPR result messages.
Starts a Step Functions execution for each message.
"""
import json
import os
import uuid

import boto3

sfn = boto3.client("stepfunctions")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")


def handler(event: dict, context) -> None:
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        execution_name = f"watchtell-{body.get('job_id', uuid.uuid4())}"
        sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name[:80],
            input=json.dumps(body),
        )
