"""API Gateway REST APIs (v1) and HTTP/WebSocket APIs (v2)."""

from __future__ import annotations

import re
from typing import Any

from converters.cfn.builder import Builder, Props
from converters.cfn.context import Context
from converters.cfn.models import BLANK, Attr, Block, Raw
from converters.cfn.resources.base import ResourceConverter
from converters.cfn.resources.compute import both, literal_bool, optional_block


def body_title(b: Builder) -> str | None:
    """The OpenAPI title, which API Gateway uses as the name when Name is left out."""
    body = b.props.peek("Body")
    info = body.get("info") if isinstance(body, dict) else None
    title = info.get("title") if isinstance(info, dict) else None
    return title if isinstance(title, str) else None


def api_name(b: Builder) -> None:
    """The name of an API: Name, else the OpenAPI title, else one made from the stack name."""
    title = body_title(b)
    if not b.props.has("Name") and title:
        b.props.drop("Name")
        b.props.set("name", title)
    else:
        b.name_attr("name", "Name")


def body_location(b: Builder) -> None:
    """BodyS3Location, which is reported: Terraform reads the body from a local file."""
    if b.props.take("BodyS3Location") is not None:
        b.ctx.note(
            f"{b.logical_id}: BodyS3Location was left out, because Terraform reads the body from "
            "a local file. Download the OpenAPI file next to the Terraform files and set body = "
            'file("${path.module}/<file>").'
        )


def name_suffix(value: Any, index: int) -> str:
    """'_200' for a status code, or the position when the code is computed."""
    if isinstance(value, str | int) and not isinstance(value, bool):
        cleaned = re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_")
        if cleaned:
            return f"_{cleaned}"
    return f"_{index}"


def number_text(ctx: Context, value: Any) -> Any:
    """A number as text, for the arguments Terraform takes as strings where CloudFormation
    takes numbers (minimum_compression_size, status codes)."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return ctx.expr(value)


def text_attr(p: Props, tf_name: str, cfn_name: str) -> bool:
    """A property copied as text (see number_text) when it is present. Returns whether it
    was present."""
    value = p.take(cfn_name)
    if value is None:
        return False
    p.set(tf_name, number_text(p.ctx, value))
    return True


def method_path(resource_path: Any, http_method: Any) -> Any:
    """ResourcePath ('/~1pets', '/*') and HttpMethod in CloudFormation as method_path in
    Terraform ('pets/GET', '*/*'). The root resource ('/') is '~1', as API Gateway encodes
    it in method setting paths."""
    if not (isinstance(resource_path, str) and isinstance(http_method, str)):
        return None
    path = resource_path.replace("~1", "/").lstrip("/") or "~1"
    return f"{path}/{http_method}"


def fill_access_log_settings(a: Props) -> None:
    """The access_log_settings block of a REST or HTTP stage."""
    a.attrs({"destination_arn": "DestinationArn", "format": "Format"})


# --- REST APIs (v1) ---------------------------------------------------------------------


class RestApi(ResourceConverter):
    """AWS::ApiGateway::RestApi as aws_api_gateway_rest_api."""

    cfn_type = "AWS::ApiGateway::RestApi"
    tf_type = "aws_api_gateway_rest_api"
    attrs = {"RootResourceId": "root_resource_id", "RestApiId": "id", "Arn": "arn"}

    def convert(self, b: Builder) -> None:
        """The API, its body and settings."""
        p = b.props
        api_name(b)
        p.attr("description", "Description")
        p.json("body", "Body")
        body_location(b)
        p.json("policy", "Policy")
        p.list_attr("binary_media_types", "BinaryMediaTypes")
        p.attr("api_key_source", "ApiKeySourceType")
        text_attr(p, "minimum_compression_size", "MinimumCompressionSize")
        p.boolean("disable_execute_api_endpoint", "DisableExecuteApiEndpoint")
        p.boolean("fail_on_warnings", "FailOnWarnings")
        p.attr("parameters", "Parameters")
        p.attr("put_rest_api_mode", "Mode")
        p.block("endpoint_configuration", "EndpointConfiguration", self.fill_endpoint)
        p.tags()

    def fill_endpoint(self, e: Props) -> None:
        """The endpoint_configuration block."""
        e.list_attr("types", "Types")
        e.list_attr("vpc_endpoint_ids", "VpcEndpointIds")
        e.attr("ip_address_type", "IpAddressType")


class RestDeployment(ResourceConverter):
    """AWS::ApiGateway::Deployment as aws_api_gateway_deployment, with its stage as a
    separate resource."""

    cfn_type = "AWS::ApiGateway::Deployment"
    tf_type = "aws_api_gateway_deployment"
    attrs = {"DeploymentId": "id"}

    def convert(self, b: Builder) -> None:
        """The deployment, redeployed when the API body changes, and its stage."""
        p = b.props
        rest_api_id = p.peek("RestApiId")
        p.attr("rest_api_id", "RestApiId")
        p.attr("description", "Description")
        body = self.rest_api_body(b, rest_api_id)
        if body is not None:
            # A deployment is a snapshot: redeploy whenever the API definition changes.
            p.set("triggers", {"redeployment": Raw(f"sha1({body})")})
        else:
            b.ctx.note(
                f"{b.logical_id}: Terraform only redeploys a deployment when its arguments "
                "change. Add triggers, such as a hash of the methods and integrations, so API "
                "changes get deployed."
            )
        b.lifecycle.append(Attr("create_before_destroy", True))
        stage_name = p.take("StageName")
        stage = p.take("StageDescription")
        if stage_name is None:
            return
        # Provider v6 removed stage_name from deployments: the stage is its own resource.
        inner = Props(b.ctx, stage, f"{b.logical_id}.StageDescription", p.owner)
        inner.attr("description", "Description")
        inner.boolean("xray_tracing_enabled", "TracingEnabled")
        inner.attr("variables", "Variables")
        inner.finish()
        b.add_resource(
            "aws_api_gateway_stage",
            "_stage",
            [
                Attr("rest_api_id", b.ctx.expr(rest_api_id)),
                Attr("deployment_id", b.self_ref("id")),
                Attr("stage_name", b.ctx.expr(stage_name)),
            ]
            + inner.items,
        )

    def rest_api_body(self, b: Builder, value: Any) -> str | None:
        """`<rest api>.body` when RestApiId refers to a REST API in this template with a
        Body."""
        if not (isinstance(value, dict) and set(value) == {"Ref"}):
            return None
        target = value["Ref"]
        other = b.ctx.template["Resources"].get(target)
        info = b.ctx.resources.get(str(target))
        if (
            isinstance(other, dict)
            and other.get("Type") == "AWS::ApiGateway::RestApi"
            and (other.get("Properties") or {}).get("Body") is not None
            and info is not None
            and info.kind == "aws"
        ):
            return info.expression("body")
        return None


class RestStage(ResourceConverter):
    """AWS::ApiGateway::Stage as aws_api_gateway_stage, with its method settings as
    separate resources."""

    cfn_type = "AWS::ApiGateway::Stage"
    tf_type = "aws_api_gateway_stage"
    ref = "stage_name"
    attrs = {"Arn": "arn"}

    def convert(self, b: Builder) -> None:
        """The stage, then one aws_api_gateway_method_settings per MethodSettings entry."""
        p = b.props
        rest_api_id = p.peek("RestApiId")
        p.attr("rest_api_id", "RestApiId")
        p.attr("deployment_id", "DeploymentId")
        b.name_attr("stage_name", "StageName")
        p.attrs(
            {
                "description": "Description",
                "variables": "Variables",
                "cache_cluster_size": "CacheClusterSize",
                "client_certificate_id": "ClientCertificateId",
                "documentation_version": "DocumentationVersion",
            }
        )
        p.boolean("xray_tracing_enabled", "TracingEnabled")
        p.boolean("cache_cluster_enabled", "CacheClusterEnabled")
        p.block("access_log_settings", "AccessLogSetting", fill_access_log_settings)
        p.tags()
        for index, item in enumerate(p.literal("MethodSettings", list) or [], start=1):
            when, setting = b.ctx.optional_item(item)
            setting = p.literal("MethodSettings", dict, value=setting)
            if setting is not None:
                self.method_settings(b, rest_api_id, setting, index, when)

    def method_settings(
        self, b: Builder, rest_api_id: Any, setting: Any, index: int, when: str | None
    ) -> None:
        """One MethodSettings entry as aws_api_gateway_method_settings."""
        inner = Props(b.ctx, setting, f"{b.logical_id}.MethodSettings[{index - 1}]", b.props.owner)
        path = method_path(inner.take("ResourcePath", "/*"), inner.take("HttpMethod", "*"))
        if path is None:
            b.ctx.note(
                f"{b.logical_id}: MethodSettings[{index - 1}] computes its path or method, so "
                "method_path is '*/*'. Set the right path by hand."
            )
            path = "*/*"
        inner.boolean("metrics_enabled", "MetricsEnabled")
        inner.boolean("data_trace_enabled", "DataTraceEnabled")
        inner.boolean("caching_enabled", "CachingEnabled")
        inner.boolean("cache_data_encrypted", "CacheDataEncrypted")
        inner.attr("logging_level", "LoggingLevel")
        inner.number("cache_ttl_in_seconds", "CacheTtlInSeconds")
        inner.number("throttling_burst_limit", "ThrottlingBurstLimit")
        inner.number("throttling_rate_limit", "ThrottlingRateLimit")
        inner.finish()
        b.add_resource(
            "aws_api_gateway_method_settings",
            f"_settings_{index}",
            [
                Attr("rest_api_id", b.ctx.expr(rest_api_id)),
                Attr("stage_name", b.self_ref("stage_name")),
                Attr("method_path", path),
                Block("settings", [], inner.items),
            ],
            when=when,
        )


class RestResource(ResourceConverter):
    """AWS::ApiGateway::Resource as aws_api_gateway_resource."""

    cfn_type = "AWS::ApiGateway::Resource"
    tf_type = "aws_api_gateway_resource"
    attrs = {"ResourceId": "id"}

    def convert(self, b: Builder) -> None:
        """The API, parent and path part of the resource."""
        b.props.attrs(
            {"rest_api_id": "RestApiId", "parent_id": "ParentId", "path_part": "PathPart"}
        )


class RestMethod(ResourceConverter):
    """AWS::ApiGateway::Method as aws_api_gateway_method, with its integration and responses
    as the separate resources Terraform uses for them."""

    cfn_type = "AWS::ApiGateway::Method"
    tf_type = "aws_api_gateway_method"

    def convert(self, b: Builder) -> None:
        """The method, its responses, its integration and the integration responses."""
        p = b.props
        rest_api_id = p.peek("RestApiId")
        resource_id = p.peek("ResourceId")
        p.attrs(
            {
                "rest_api_id": "RestApiId",
                "resource_id": "ResourceId",
                "http_method": "HttpMethod",
                "authorizer_id": "AuthorizerId",
                "operation_name": "OperationName",
                "request_parameters": "RequestParameters",
                "request_models": "RequestModels",
                "request_validator_id": "RequestValidatorId",
            }
        )
        if not p.attr("authorization", "AuthorizationType"):
            p.set("authorization", "NONE")
        p.boolean("api_key_required", "ApiKeyRequired")
        p.list_attr("authorization_scopes", "AuthorizationScopes")
        shared = [
            Attr("rest_api_id", b.ctx.expr(rest_api_id)),
            Attr("resource_id", b.ctx.expr(resource_id)),
            Attr("http_method", b.self_ref("http_method")),
        ]
        responses = self.method_responses(b, shared)
        self.integration(b, shared, responses)

    def method_responses(self, b: Builder, shared: list[Any]) -> dict[str, str]:
        """One aws_api_gateway_method_response per MethodResponses entry. Returns their
        addresses by status code."""
        p = b.props
        responses: dict[str, str] = {}
        for index, item in enumerate(p.literal("MethodResponses", list) or []):
            when, response = b.ctx.optional_item(item)
            response = p.literal("MethodResponses", dict, value=response)
            if response is None:
                continue
            inner = Props(b.ctx, response, f"{b.logical_id}.MethodResponses[{index}]", p.owner)
            status = inner.peek("StatusCode")
            text_attr(inner, "status_code", "StatusCode")
            inner.attrs(
                {"response_models": "ResponseModels", "response_parameters": "ResponseParameters"}
            )
            inner.finish()
            responses[str(status)] = b.add_resource(
                "aws_api_gateway_method_response",
                f"_response{name_suffix(status, index)}",
                shared + inner.items,
                when=when,
            )
        return responses

    def integration(self, b: Builder, shared: list[Any], responses: dict[str, str]) -> None:
        """Integration as aws_api_gateway_integration plus one
        aws_api_gateway_integration_response per IntegrationResponses entry, each after the
        method response for its status code (in `responses`)."""
        p = b.props
        integration_when, integration = b.ctx.optional_item(p.take("Integration"))
        integration = p.literal("Integration", dict, value=integration)
        if integration is None:
            return
        inner = Props(b.ctx, integration, f"{b.logical_id}.Integration", p.owner)
        inner.attrs(
            {
                "type": "Type",
                "integration_http_method": "IntegrationHttpMethod",
                "uri": "Uri",
                "credentials": "Credentials",
                "request_templates": "RequestTemplates",
                "request_parameters": "RequestParameters",
                "passthrough_behavior": "PassthroughBehavior",
                "content_handling": "ContentHandling",
                "connection_type": "ConnectionType",
                "connection_id": "ConnectionId",
                "cache_namespace": "CacheNamespace",
            }
        )
        inner.number("timeout_milliseconds", "TimeoutInMillis")
        inner.list_attr("cache_key_parameters", "CacheKeyParameters")
        integration_responses = inner.literal("IntegrationResponses", list) or []
        inner.finish()
        integration_address = b.add_resource(
            "aws_api_gateway_integration",
            "_integration",
            shared + inner.items,
            when=integration_when,
        )
        for index, entry in enumerate(integration_responses):
            when, response = b.ctx.optional_item(entry)
            response = p.literal("Integration.IntegrationResponses", dict, value=response)
            if response is None:
                continue
            where = f"{b.logical_id}.Integration.IntegrationResponses[{index}]"
            item = Props(b.ctx, response, where, p.owner)
            status = item.peek("StatusCode")
            text_attr(item, "status_code", "StatusCode")
            item.attrs(
                {
                    "selection_pattern": "SelectionPattern",
                    "response_templates": "ResponseTemplates",
                    "response_parameters": "ResponseParameters",
                    "content_handling": "ContentHandling",
                }
            )
            item.finish()
            # The method response for the same status code must exist first.
            depends = [Raw(integration_address)]
            if str(status) in responses:
                depends.append(Raw(responses[str(status)]))
            b.add_resource(
                "aws_api_gateway_integration_response",
                f"_integration_response{name_suffix(status, index)}",
                shared + item.items + [BLANK, Attr("depends_on", depends)],
                when=both(integration_when, when),
            )


# --- HTTP and WebSocket APIs (v2) -------------------------------------------------------


class HttpApi(ResourceConverter):
    """AWS::ApiGatewayV2::Api as aws_apigatewayv2_api."""

    cfn_type = "AWS::ApiGatewayV2::Api"
    tf_type = "aws_apigatewayv2_api"
    attrs = {"ApiEndpoint": "api_endpoint", "ApiId": "id"}

    def convert(self, b: Builder) -> None:
        """The API, its body and CORS settings."""
        p = b.props
        api_name(b)
        if not p.attr("protocol_type", "ProtocolType"):
            # Only an imported OpenAPI body can leave it out, and that is always HTTP.
            p.set("protocol_type", "HTTP")
        p.attrs(
            {
                "description": "Description",
                "route_selection_expression": "RouteSelectionExpression",
                "api_key_selection_expression": "ApiKeySelectionExpression",
                "version": "Version",
                "credentials_arn": "CredentialsArn",
                "route_key": "RouteKey",
                "target": "Target",
                "ip_address_type": "IpAddressType",
            }
        )
        p.json("body", "Body")
        body_location(b)
        p.boolean("disable_execute_api_endpoint", "DisableExecuteApiEndpoint")
        p.boolean("fail_on_warnings", "FailOnWarnings")
        p.block("cors_configuration", "CorsConfiguration", self.fill_cors)
        p.tags()

    def fill_cors(self, c: Props) -> None:
        """The cors_configuration block."""
        c.boolean("allow_credentials", "AllowCredentials")
        c.list_attr("allow_headers", "AllowHeaders")
        c.list_attr("allow_methods", "AllowMethods")
        c.list_attr("allow_origins", "AllowOrigins")
        c.list_attr("expose_headers", "ExposeHeaders")
        c.number("max_age", "MaxAge")


class HttpIntegration(ResourceConverter):
    """AWS::ApiGatewayV2::Integration as aws_apigatewayv2_integration."""

    cfn_type = "AWS::ApiGatewayV2::Integration"
    tf_type = "aws_apigatewayv2_integration"
    attrs = {"IntegrationId": "id"}

    def convert(self, b: Builder) -> None:
        """The integration and its TLS settings."""
        p = b.props
        p.attrs(
            {
                "api_id": "ApiId",
                "integration_type": "IntegrationType",
                "integration_uri": "IntegrationUri",
                "integration_method": "IntegrationMethod",
                "integration_subtype": "IntegrationSubtype",
                "payload_format_version": "PayloadFormatVersion",
                "connection_type": "ConnectionType",
                "connection_id": "ConnectionId",
                "credentials_arn": "CredentialsArn",
                "description": "Description",
                "passthrough_behavior": "PassthroughBehavior",
                "request_parameters": "RequestParameters",
                "request_templates": "RequestTemplates",
                "template_selection_expression": "TemplateSelectionExpression",
                "content_handling_strategy": "ContentHandlingStrategy",
            }
        )
        p.number("timeout_milliseconds", "TimeoutInMillis")
        p.block("tls_config", "TlsConfig", self.fill_tls_config)

    def fill_tls_config(self, t: Props) -> None:
        """The tls_config block."""
        t.attr("server_name_to_verify", "ServerNameToVerify")


class HttpRoute(ResourceConverter):
    """AWS::ApiGatewayV2::Route as aws_apigatewayv2_route."""

    cfn_type = "AWS::ApiGatewayV2::Route"
    tf_type = "aws_apigatewayv2_route"
    attrs = {"RouteId": "id"}

    def convert(self, b: Builder) -> None:
        """The route and its request parameters."""
        p = b.props
        p.attrs(
            {
                "api_id": "ApiId",
                "route_key": "RouteKey",
                "target": "Target",
                "authorization_type": "AuthorizationType",
                "authorizer_id": "AuthorizerId",
                "operation_name": "OperationName",
                "request_models": "RequestModels",
                "model_selection_expression": "ModelSelectionExpression",
                "route_response_selection_expression": "RouteResponseSelectionExpression",
            }
        )
        p.boolean("api_key_required", "ApiKeyRequired")
        p.list_attr("authorization_scopes", "AuthorizationScopes")
        for key, entry in (p.literal("RequestParameters", dict) or {}).items():
            when, value = b.ctx.optional_item(entry)
            if isinstance(value, dict):
                value = p.literal(f"RequestParameters.{key}", dict, value=value)
                if value is None:
                    continue
                value = value.get("Required", False)
            p.items.append(
                optional_block(
                    "request_parameter",
                    [
                        Attr("request_parameter_key", str(key)),
                        Attr("required", literal_bool(b.ctx.expr(value))),
                    ],
                    when,
                )
            )


def fill_route_settings(r: Props) -> None:
    """The throttling, logging and metrics settings of a route, or of all routes."""
    r.boolean("data_trace_enabled", "DataTraceEnabled")
    r.boolean("detailed_metrics_enabled", "DetailedMetricsEnabled")
    r.attr("logging_level", "LoggingLevel")
    r.number("throttling_burst_limit", "ThrottlingBurstLimit")
    r.number("throttling_rate_limit", "ThrottlingRateLimit")


class HttpStage(ResourceConverter):
    """AWS::ApiGatewayV2::Stage as aws_apigatewayv2_stage."""

    cfn_type = "AWS::ApiGatewayV2::Stage"
    tf_type = "aws_apigatewayv2_stage"
    ref = "name"

    def convert(self, b: Builder) -> None:
        """The stage, its logging and its route settings."""
        p = b.props
        p.attr("api_id", "ApiId")
        b.name_attr("name", "StageName")
        p.attrs(
            {
                "deployment_id": "DeploymentId",
                "description": "Description",
                "stage_variables": "StageVariables",
                "client_certificate_id": "ClientCertificateId",
            }
        )
        p.boolean("auto_deploy", "AutoDeploy")
        p.block("access_log_settings", "AccessLogSettings", fill_access_log_settings)
        p.block("default_route_settings", "DefaultRouteSettings", fill_route_settings)
        for route_key, entry in (p.literal("RouteSettings", dict) or {}).items():
            when, settings = b.ctx.optional_item(entry)
            settings = p.literal(f"RouteSettings.{route_key}", dict, value=settings)
            if settings is None:
                continue
            inner = Props(b.ctx, settings, f"{b.logical_id}.RouteSettings.{route_key}", p.owner)
            inner.set("route_key", str(route_key))
            fill_route_settings(inner)
            inner.finish()
            p.items.append(optional_block("route_settings", inner.items, when))
        p.tags()


class HttpAuthorizer(ResourceConverter):
    """AWS::ApiGatewayV2::Authorizer as aws_apigatewayv2_authorizer."""

    cfn_type = "AWS::ApiGatewayV2::Authorizer"
    tf_type = "aws_apigatewayv2_authorizer"
    attrs = {"AuthorizerId": "id"}

    def convert(self, b: Builder) -> None:
        """The authorizer and its JWT settings."""
        p = b.props
        p.attr("api_id", "ApiId")
        b.name_attr("name", "Name")
        p.attrs(
            {
                "authorizer_type": "AuthorizerType",
                "authorizer_uri": "AuthorizerUri",
                "authorizer_credentials_arn": "AuthorizerCredentialsArn",
                "authorizer_payload_format_version": "AuthorizerPayloadFormatVersion",
            }
        )
        p.number("authorizer_result_ttl_in_seconds", "AuthorizerResultTtlInSeconds")
        p.boolean("enable_simple_responses", "EnableSimpleResponses")
        p.list_attr("identity_sources", "IdentitySource")
        p.block("jwt_configuration", "JwtConfiguration", self.fill_jwt_configuration)

    def fill_jwt_configuration(self, j: Props) -> None:
        """The jwt_configuration block."""
        j.list_attr("audience", "Audience")
        j.attr("issuer", "Issuer")


CONVERTERS: tuple[type[ResourceConverter], ...] = (
    RestApi,
    RestDeployment,
    RestStage,
    RestResource,
    RestMethod,
    HttpApi,
    HttpIntegration,
    HttpRoute,
    HttpStage,
    HttpAuthorizer,
)
