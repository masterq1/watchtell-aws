"""
API Gateway (HTTP API) + Cognito auth + Lambda handlers for all endpoints.
"""
from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_apigatewayv2_authorizers as authorizers,
    aws_cognito as cognito,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
)
from constructs import Construct

LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_12


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        events_table: dynamodb.Table,
        watchlist_table: dynamodb.Table,
        media_bucket: s3.Bucket,
        pipeline_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Cognito User Pool
        user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name="watchtell-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
        )

        user_pool_client = user_pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
            ),
            generate_secret=False,
        )

        # Shared environment for all API Lambdas
        shared_env = {
            "EVENTS_TABLE": events_table.table_name,
            "WATCHLIST_TABLE": watchlist_table.table_name,
            "MEDIA_BUCKET": media_bucket.bucket_name,
            "USER_POOL_ID": user_pool.user_pool_id,
            "USER_POOL_CLIENT_ID": user_pool_client.user_pool_client_id,
            "PIPELINE_ARN": pipeline_arn,
        }

        # Lambda handlers
        events_fn = self._lambda("Events", "events.handler", shared_env)
        plates_fn = self._lambda("Plates", "plates.handler", shared_env)
        watchlist_fn = self._lambda("Watchlist", "watchlist.handler", shared_env)
        search_fn = self._lambda("Search", "search.handler", shared_env)
        clips_fn = self._lambda("Clips", "clips.handler", shared_env)

        events_table.grant_read_data(events_fn)
        events_table.grant_read_data(search_fn)
        watchlist_table.grant_read_write_data(watchlist_fn)
        watchlist_table.grant_read_data(watchlist_fn)
        events_table.grant_read_data(plates_fn)
        media_bucket.grant_read(clips_fn)

        # HTTP API
        http_api = apigwv2.HttpApi(
            self, "HttpApi",
            api_name="watchtell-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.DELETE,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )

        jwt_authorizer = authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[user_pool_client.user_pool_client_id],
        )

        def route(integration_id: str, method: apigwv2.HttpMethod, path: str, fn: lambda_.Function) -> None:
            http_api.add_routes(
                path=path,
                methods=[method],
                integration=integrations.HttpLambdaIntegration(integration_id, fn),
                authorizer=jwt_authorizer,
            )

        route("IntEventsGet",        apigwv2.HttpMethod.GET,    "/events",            events_fn)
        route("IntEventsById",       apigwv2.HttpMethod.GET,    "/events/{id}",       events_fn)
        route("IntPlates",           apigwv2.HttpMethod.GET,    "/plates/{plate}",    plates_fn)
        route("IntWatchlistGet",     apigwv2.HttpMethod.GET,    "/watchlist",         watchlist_fn)
        route("IntWatchlistPost",    apigwv2.HttpMethod.POST,   "/watchlist",         watchlist_fn)
        route("IntWatchlistDelete",  apigwv2.HttpMethod.DELETE, "/watchlist/{plate}", watchlist_fn)
        route("IntSearch",           apigwv2.HttpMethod.GET,    "/search",            search_fn)
        route("IntClips",            apigwv2.HttpMethod.GET,    "/clips/{id+}",       clips_fn)

        self.api_url = http_api.api_endpoint
        self.http_api_id = http_api.api_id

        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)

    def _lambda(self, name: str, handler: str, env: dict) -> lambda_.Function:
        return lambda_.Function(
            self, name,
            function_name=f"watchtell-api-{name.lower()}",
            runtime=LAMBDA_RUNTIME,
            handler=handler,
            code=lambda_.Code.from_asset("../api"),
            timeout=Duration.seconds(29),
            environment=env,
            memory_size=256,
        )
