"""
CloudFront distribution — SPA hosting + HLS live stream + API proxy.
"""
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3 as s3,
)
from constructs import Construct


class CdnStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        api_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 bucket for SPA static assets
        spa_bucket = s3.Bucket(
            self, "SpaBucket",
            bucket_name=f"watchtell-spa-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # HLS bucket — camera agent (hls_relay.sh) pushes HLS segments here.
        # No bucket resource policy for the camera agent — access is granted via IAM
        # policy on whoever runs the agent (your local credentials / assumed role).
        # CloudFront read access is handled automatically by the OAC below.
        hls_bucket = s3.Bucket(
            self, "HlsBucket",
            bucket_name=f"watchtell-hls-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        oac = cloudfront.S3OriginAccessControl(
            self, "Oac",
            description="WatchTell SPA OAC",
        )

        # CloudFront distribution
        distribution = cloudfront.Distribution(
            self, "Distribution",
            comment="watchtell-cdn",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    spa_bucket,
                    origin_access_control=oac,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(
                        cdk.Fn.select(1, cdk.Fn.split("://", api_url)),
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                ),
            },
            # SPA routing: serve index.html for all 403/404s
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )

        # HLS live stream behavior — no caching so m3u8 playlist is always fresh
        distribution.add_behavior(
            "/hls/*",
            origins.S3BucketOrigin.with_origin_access_control(hls_bucket),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        )

        CfnOutput(self, "DistributionDomain", value=distribution.distribution_domain_name)
        CfnOutput(self, "SpaBucketName", value=spa_bucket.bucket_name)
        CfnOutput(self, "HlsBucketName", value=hls_bucket.bucket_name)
