"""
WAF, KMS encryption, CloudTrail audit logging.
"""
from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    aws_wafv2 as wafv2,
    aws_kms as kms,
    aws_cloudtrail as cloudtrail,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
    aws_logs as logs,
)
from constructs import Construct


class SecurityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        media_bucket: s3.Bucket,
        events_table: dynamodb.Table,
        api_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # KMS key for encryption at rest
        kms_key = kms.Key(
            self, "WatchtellKey",
            description="WatchTell encryption key",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        kms_key.add_alias("alias/watchtell")

        # CloudTrail — audit log for all plate lookups and searches
        trail_bucket = s3.Bucket(
            self, "TrailBucket",
            bucket_name=f"watchtell-cloudtrail-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
        )

        trail = cloudtrail.Trail(
            self, "AuditTrail",
            trail_name="watchtell-audit",
            bucket=trail_bucket,
            send_to_cloud_watch_logs=True,
            cloud_watch_logs_retention=logs.RetentionDays.ONE_YEAR,
            include_global_service_events=True,
            is_multi_region_trail=False,
        )

        # Log S3 data events for media bucket (clip access audit)
        trail.add_s3_event_selector(
            [cloudtrail.S3EventSelector(bucket=media_bucket)],
            include_management_events=False,
            read_write_type=cloudtrail.ReadWriteType.ALL,
        )

        # WAF WebACL — attach to API Gateway
        web_acl = wafv2.CfnWebACL(
            self, "ApiWaf",
            name="watchtell-api-waf",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="watchtell-api-waf",
                sampled_requests_enabled=True,
            ),
            rules=[
                # AWS managed common rule set
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesCommonRuleSet",
                    priority=1,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesCommonRuleSet",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="CommonRuleSet",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rate limiting — 1000 req/5min per IP
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimit",
                    priority=2,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=1000,
                            aggregate_key_type="IP",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="RateLimit",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )

        # NOTE: WAF WebACLAssociation with HTTP API v2 requires an explicitly
        # named stage — the $default stage is not supported for WAF association.
        # Uncomment and update the stage name when a production stage is created.
        #
        # wafv2.CfnWebACLAssociation(
        #     self, "ApiWafAssociation",
        #     resource_arn=f"arn:aws:apigateway:{self.region}::/apis/{api_id}/stages/prod",
        #     web_acl_arn=web_acl.attr_arn,
        # )

        CfnOutput(self, "WebAclArn", value=web_acl.attr_arn)
