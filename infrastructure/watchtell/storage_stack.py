from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class StorageStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 bucket — video clips and keyframes, Intelligent-Tiering, 365-day retention.
        # event_bridge_enabled=True allows EventBridge to route ObjectCreated events
        # to the Rekognition Lambda without creating a cross-stack notification dependency.
        self.media_bucket = s3.Bucket(
            self, "MediaBucket",
            bucket_name=f"watchtell-media-{self.account}",
            event_bridge_enabled=True,
            intelligent_tiering_configurations=[
                s3.IntelligentTieringConfiguration(
                    name="DefaultTiering",
                    archive_access_tier_time=Duration.days(90),
                    deep_archive_access_tier_time=Duration.days(180),
                )
            ],
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="Expire365Days",
                    enabled=True,
                    expiration=Duration.days(365),
                ),
            ],
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=False,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # DynamoDB events table
        # PK: EventId (String), SK: Timestamp (String)
        self.events_table = dynamodb.Table(
            self, "EventsTable",
            table_name="watchtell-events",
            partition_key=dynamodb.Attribute(name="EventId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="Timestamp", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

        # GSI: PlateNumber → find all events for a plate
        self.events_table.add_global_secondary_index(
            index_name="PlateNumber-Timestamp-index",
            partition_key=dynamodb.Attribute(name="PlateNumber", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="Timestamp", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI: CameraId+Timestamp → find all events for a camera in time range
        self.events_table.add_global_secondary_index(
            index_name="CameraId-Timestamp-index",
            partition_key=dynamodb.Attribute(name="CameraId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="Timestamp", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI: EventType → filter by event type (entry/exit/unknown)
        self.events_table.add_global_secondary_index(
            index_name="EventType-Timestamp-index",
            partition_key=dynamodb.Attribute(name="EventType", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="Timestamp", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # DynamoDB watchlist table
        # PK: PlateNumber (String)
        self.watchlist_table = dynamodb.Table(
            self, "WatchlistTable",
            table_name="watchtell-watchlist",
            partition_key=dynamodb.Attribute(name="PlateNumber", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # DynamoDB plate validation cache — replaces Upstash Redis.
        # PK: PlateNumber. TTL attribute: ExpiresAt (Unix epoch seconds).
        # Lambda writes ExpiresAt = now + 86400 (24 h); DynamoDB auto-expires entries.
        # No VPC, no Redis cluster, no cross-stack networking required.
        self.plate_cache_table = dynamodb.Table(
            self, "PlateCacheTable",
            table_name="watchtell-plate-cache",
            partition_key=dynamodb.Attribute(name="PlateNumber", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ExpiresAt",
            removal_policy=RemovalPolicy.DESTROY,
        )
