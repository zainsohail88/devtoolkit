"""API Gateway REST APIs (v1) and HTTP/WebSocket APIs (v2)."""

from __future__ import annotations

import re
from typing import Any

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props
from converters.cfn.hcl import Attr, Block, Raw
from converters.cfn.resources import resource


def _body_title(b: Builder) -> str | None:
    """The OpenAPI title, which API Gateway uses as the name when Name is left out."""
    body = b.props.peek("Body")
    info = body.get("info") if isinstance(body, dict) else None
    title = info.get("title") if isinstance(info, dict) else None
    return title if isinstance(title, str) else None


def _name(b: Builder, cfn_name: str = "Name") -> None:
    title = _body_title(b)
    if not b.props.has(cfn_name) and title:
        b.props.drop(cfn_name)
        b.props.set("name", title)
    else:
        b.name_attr("name", cfn_name)


def _body_location(b: Builder) -> None:
    if b.props.take("BodyS3Location") is not None:
        b.ctx.note(
            f"{b.logical_id}: BodyS3Location was not converted; download the OpenAPI file next "
            'to the Terraform files and set body = file("${path.module}/<file>")'
        )


def _suffix(value: Any, index: int) -> str:
    """'_200' for a status code, or the position when the code is computed."""
    if isinstance(value, str | int) and not isinstance(value, bool):
        cleaned = re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_")
        if cleaned:
            return f"_{cleaned}"
    return f"_{index}"


# --- REST APIs (v1) ---------------------------------------------------------------------


@resource(
    "AWS::ApiGateway::RestApi",
    "aws_api_gateway_rest_api",
    attrs={"RootResourceId": "root_resource_id", "RestApiId": "id", "Arn": "arn"},
)
def rest_api(b: Builder) -> None:
    p = b.props
    _name(b)
    p.attr("description", "Description")
    p.json("body", "Body")
    _body_location(b)
    p.json("policy", "Policy")
    p.list_attr("binary_media_types", "BinaryMediaTypes")
    p.attr("api_key_source", "ApiKeySourceType")
    p.attr("minimum_compression_size", "MinimumCompressionSize", lambda v: _text(b, v))
    p.boolean("disable_execute_api_endpoint", "DisableExecuteApiEndpoint")
    p.boolean("fail_on_warnings", "FailOnWarnings")
    p.attr("parameters", "Parameters")
    p.attr("put_rest_api_mode", "Mode")
    p.block(
        "endpoint_configuration",
        "EndpointConfiguration",
        lambda e: (
            e.list_attr("types", "Types"),
            e.list_attr("vpc_endpoint_ids", "VpcEndpointIds"),
            e.attr("ip_address_type", "IpAddressType"),
        ),
    )
    p.tags()


def _text(b: Builder, value: Any) -> Any:
    # Terraform takes minimum_compression_size as a string; CloudFormation as a number.
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return b.ctx.expr(value)


def _rest_api_body(b: Builder, value: Any) -> str | None:
    """`<rest api>.body` when RestApiId refers to a REST API in this template with a Body."""
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


@resource("AWS::ApiGateway::Deployment", "aws_api_gateway_deployment", attrs={"DeploymentId": "id"})
def rest_deployment(b: Builder) -> None:
    p = b.props
    rest_api_id = p.peek("RestApiId")
    p.attr("rest_api_id", "RestApiId")
    p.attr("description", "Description")
    body = _rest_api_body(b, rest_api_id)
    if body is not None:
        # A deployment is a snapshot: redeploy whenever the API definition changes.
        p.set("triggers", {"redeployment": Raw(f"sha1({body})")})
    else:
        b.ctx.note(
            f"{b.logical_id}: a Terraform deployment is only redeployed when its arguments "
            "change; add triggers (e.g. a hash of the methods and integrations) so API "
            "changes get deployed"
        )
    b.lifecycle.append(Attr("create_before_destroy", True))
    stage_name = p.take("StageName")
    stage = p.take("StageDescription")
    if stage_name is not None:
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
                *inner.items,
            ],
        )


@resource(
    "AWS::ApiGateway::Stage",
    "aws_api_gateway_stage",
    ref="stage_name",
    attrs={"Arn": "arn"},
)
def rest_stage(b: Builder) -> None:
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
    p.block(
        "access_log_settings",
        "AccessLogSetting",
        lambda a: a.attrs({"destination_arn": "DestinationArn", "format": "Format"}),
    )
    p.tags()
    for index, setting in enumerate(p.take("MethodSettings") or [], start=1):
        _method_settings(b, rest_api_id, setting, index)


def _method_path(resource_path: Any, http_method: Any) -> Any:
    """CloudFormation's ResourcePath ('/~1pets', '/*') and HttpMethod as Terraform's
    method_path ('pets/GET', '*/*')."""
    if not (isinstance(resource_path, str) and isinstance(http_method, str)):
        return None
    path = resource_path.replace("~1", "/").lstrip("/") or "*"
    return f"{path}/{http_method}"


def _method_settings(b: Builder, rest_api_id: Any, setting: Any, index: int) -> None:
    inner = Props(b.ctx, setting, f"{b.logical_id}.MethodSettings[{index - 1}]", b.props.owner)
    path = _method_path(inner.take("ResourcePath", "/*"), inner.take("HttpMethod", "*"))
    if path is None:
        b.ctx.note(
            f"{b.logical_id}: MethodSettings[{index - 1}] uses a computed path or method; "
            "set method_path by hand"
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
    )


@resource("AWS::ApiGateway::Resource", "aws_api_gateway_resource", attrs={"ResourceId": "id"})
def rest_resource(b: Builder) -> None:
    b.props.attrs({"rest_api_id": "RestApiId", "parent_id": "ParentId", "path_part": "PathPart"})


@resource("AWS::ApiGateway::Method", "aws_api_gateway_method")
def rest_method(b: Builder) -> None:
    """A method, and its integration and responses as the separate resources Terraform
    uses for them."""
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
    responses: dict[str, str] = {}
    for index, response in enumerate(p.take("MethodResponses") or []):
        inner = Props(b.ctx, response, f"{b.logical_id}.MethodResponses[{index}]", p.owner)
        status = inner.peek("StatusCode")
        inner.attr("status_code", "StatusCode", lambda v: _text(b, v))
        inner.attrs(
            {"response_models": "ResponseModels", "response_parameters": "ResponseParameters"}
        )
        inner.finish()
        responses[str(status)] = b.add_resource(
            "aws_api_gateway_method_response",
            f"_response{_suffix(status, index)}",
            [*shared, *inner.items],
        )

    integration = p.take("Integration")
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
    integration_responses = inner.take("IntegrationResponses") or []
    inner.finish()
    integration_address = b.add_resource(
        "aws_api_gateway_integration", "_integration", [*shared, *inner.items]
    )
    for index, response in enumerate(integration_responses):
        item = Props(
            b.ctx, response, f"{b.logical_id}.Integration.IntegrationResponses[{index}]", p.owner
        )
        status = item.peek("StatusCode")
        item.attr("status_code", "StatusCode", lambda v: _text(b, v))
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
            f"_integration_response{_suffix(status, index)}",
            [*shared, *item.items, hcl.BLANK, Attr("depends_on", depends)],
        )


# --- HTTP and WebSocket APIs (v2) -------------------------------------------------------


def _cors(c: Props) -> None:
    c.boolean("allow_credentials", "AllowCredentials")
    c.list_attr("allow_headers", "AllowHeaders")
    c.list_attr("allow_methods", "AllowMethods")
    c.list_attr("allow_origins", "AllowOrigins")
    c.list_attr("expose_headers", "ExposeHeaders")
    c.number("max_age", "MaxAge")


@resource(
    "AWS::ApiGatewayV2::Api",
    "aws_apigatewayv2_api",
    attrs={"ApiEndpoint": "api_endpoint", "ApiId": "id"},
)
def http_api(b: Builder) -> None:
    p = b.props
    _name(b)
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
    _body_location(b)
    p.boolean("disable_execute_api_endpoint", "DisableExecuteApiEndpoint")
    p.boolean("fail_on_warnings", "FailOnWarnings")
    p.block("cors_configuration", "CorsConfiguration", _cors)
    p.tags()


@resource(
    "AWS::ApiGatewayV2::Integration",
    "aws_apigatewayv2_integration",
    attrs={"IntegrationId": "id"},
)
def http_integration(b: Builder) -> None:
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
    p.block(
        "tls_config", "TlsConfig", lambda t: t.attr("server_name_to_verify", "ServerNameToVerify")
    )


@resource("AWS::ApiGatewayV2::Route", "aws_apigatewayv2_route", attrs={"RouteId": "id"})
def http_route(b: Builder) -> None:
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
    parameters = p.take("RequestParameters")
    if isinstance(parameters, dict):
        for key, value in parameters.items():
            required = value.get("Required", False) if isinstance(value, dict) else value
            p.items.append(
                Block(
                    "request_parameter",
                    [],
                    [
                        Attr("request_parameter_key", str(key)),
                        Attr("required", _bool(b.ctx.expr(required))),
                    ],
                )
            )


def _bool(value: Any) -> Any:
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _route_settings(r: Props) -> None:
    r.boolean("data_trace_enabled", "DataTraceEnabled")
    r.boolean("detailed_metrics_enabled", "DetailedMetricsEnabled")
    r.attr("logging_level", "LoggingLevel")
    r.number("throttling_burst_limit", "ThrottlingBurstLimit")
    r.number("throttling_rate_limit", "ThrottlingRateLimit")


@resource("AWS::ApiGatewayV2::Stage", "aws_apigatewayv2_stage", ref="name")
def http_stage(b: Builder) -> None:
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
    p.block(
        "access_log_settings",
        "AccessLogSettings",
        lambda a: a.attrs({"destination_arn": "DestinationArn", "format": "Format"}),
    )
    p.block("default_route_settings", "DefaultRouteSettings", _route_settings)
    routes = p.take("RouteSettings")
    if isinstance(routes, dict):
        for route_key, settings in routes.items():
            inner = Props(b.ctx, settings, f"{b.logical_id}.RouteSettings.{route_key}", p.owner)
            inner.set("route_key", str(route_key))
            _route_settings(inner)
            inner.finish()
            p.items.append(Block("route_settings", [], inner.items))
    p.tags()


@resource(
    "AWS::ApiGatewayV2::Authorizer",
    "aws_apigatewayv2_authorizer",
    attrs={"AuthorizerId": "id"},
)
def http_authorizer(b: Builder) -> None:
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
    p.block(
        "jwt_configuration",
        "JwtConfiguration",
        lambda j: (j.list_attr("audience", "Audience"), j.attr("issuer", "Issuer")),
    )
