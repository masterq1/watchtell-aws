from aws_cdk import (
    Stack,
    Duration,
    aws_sqs as sqs,
)
from constructs import Construct


class QueueStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Dead-letter queue for failed ALPR jobs
        dlq = sqs.Queue(
            self, "AlprDlq",
            queue_name="watchtell-alpr-dlq",
            retention_period=Duration.days(14),
        )

        # Main ALPR processing queue (inbound jobs from camera agent).
        # Visibility timeout matches Rekognition Lambda timeout (30s) + buffer.
        self.alpr_queue = sqs.Queue(
            self, "AlprQueue",
            queue_name="watchtell-alpr-queue",
            visibility_timeout=Duration.seconds(60),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=dlq,
            ),
        )

        # Results queue — Rekognition Lambda publishes results here;
        # SqsTrigger Lambda reads from here to start Step Functions.
        self.results_queue = sqs.Queue(
            self, "AlprResultsQueue",
            queue_name="watchtell-alpr-results",
            visibility_timeout=Duration.seconds(30),
            retention_period=Duration.days(1),
        )
