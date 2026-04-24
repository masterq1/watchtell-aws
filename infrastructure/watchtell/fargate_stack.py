"""
FargateStack — runs hls_relay.sh as a managed ECS Fargate service.

Replaces the local hls_relay.sh process. Fargate keeps one task running
and restarts it automatically if FFmpeg exits.

The container image is built and pushed to ECR by the GitHub Actions workflow
(.github/workflows/build-hls-relay.yml) — no local Docker required for CDK deploys.

The RTSP URL is injected as an ECS secret from SSM Parameter Store
(/watchtell/relay/rtsp_url) — the container never sees it as plaintext.

Networking: uses the default VPC with a public subnet and auto-assigned
public IP so the task can reach the camera's RTSP stream and AWS services
(S3, SSM) without a NAT gateway.
"""
from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
    aws_ssm as ssm,
)
from constructs import Construct

ECR_REPO_NAME = "watchtell-hls-relay"


class FargateStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        hls_bucket: s3.Bucket,
        camera_id: str = "cam-doorway",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ECR repository — image is built and pushed by GitHub Actions,
        # not by CDK, so no local Docker required during deploy.
        repo = ecr.Repository(
            self, "HlsRelayRepo",
            repository_name=ECR_REPO_NAME,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                ecr.LifecycleRule(max_image_count=5, description="Keep last 5 images"),
            ],
        )

        # Default VPC — no NAT gateway needed when assign_public_ip=True
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)

        cluster = ecs.Cluster(
            self, "Cluster",
            cluster_name="watchtell",
            vpc=vpc,
        )

        # Execution role — ECS agent uses this to pull the SSM secret and ECR image
        # before starting the container. Must exist before the task definition.
        execution_role = iam.Role(
            self, "ExecutionRole",
            role_name="watchtell-hls-relay-execution",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )

        rtsp_param = ssm.StringParameter.from_secure_string_parameter_attributes(
            self, "RtspUrlParam",
            parameter_name="/watchtell/relay/rtsp_url",
        )
        rtsp_param.grant_read(execution_role)
        repo.grant_pull(execution_role)

        task_def = ecs.FargateTaskDefinition(
            self, "HlsRelayTask",
            family="watchtell-hls-relay",
            cpu=256,
            memory_limit_mib=512,
            execution_role=execution_role,
        )

        # Task role: sync HLS segments to S3
        hls_bucket.grant_read_write(task_def.task_role)

        log_group = logs.LogGroup(
            self, "HlsRelayLogs",
            log_group_name="/watchtell/hls-relay",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        task_def.add_container(
            "HlsRelay",
            container_name="hls-relay",
            image=ecs.ContainerImage.from_ecr_repository(repo, tag="latest"),
            secrets={
                "RTSP_URL": ecs.Secret.from_ssm_parameter(rtsp_param),
            },
            environment={
                "CAMERA_ID":     camera_id,
                "HLS_BUCKET":    hls_bucket.bucket_name,
                "AWS_REGION":    self.region,
                "HLS_TIME":      "2",
                "HLS_LIST_SIZE": "5",
            },
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="hls-relay",
                log_group=log_group,
            ),
        )

        sg = ec2.SecurityGroup(
            self, "HlsRelaySg",
            vpc=vpc,
            security_group_name="watchtell-hls-relay",
            description="WatchTell HLS relay Fargate task - outbound only",
            allow_all_outbound=True,
        )

        ecs.FargateService(
            self, "HlsRelayService",
            cluster=cluster,
            task_definition=task_def,
            service_name="watchtell-hls-relay",
            desired_count=1,
            assign_public_ip=True,
            security_groups=[sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            min_healthy_percent=0,
            max_healthy_percent=100,
        )

        CfnOutput(self, "EcrRepoUri", value=repo.repository_uri)
