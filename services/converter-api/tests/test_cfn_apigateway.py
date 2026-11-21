"""API Gateway mappings (converters/cfn/resources/apigateway.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from converters.cfn.pipeline import convert_template
from converters.cfn.resources.apigateway import method_path, name_suffix

FIXTURE = Path(__file__).parent / "fixtures" / "cfn" / "apigateway.yaml"


def convert(resources: dict) -> tuple[str, list[str]]:
    result = convert_template(yaml.safe_dump({"Resources": resources}), stack_name="demo")
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    return main, result["notes"]


def test_fixture_converts_everything():
    result = convert_template(FIXTURE.read_text(), stack_name="demo")
    assert result["summary"]["unsupported"] == 0
    assert not [n for n in result["notes"] if "was not converted" in n]
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    assert "redeployment = sha1(aws_api_gateway_rest_api.spec_api.body)" in main
    assert 'resource "aws_api_gateway_stage" "spec_deployment_stage"' in main
    assert 'method_path = "orders/GET"' in main
    assert 'method_path = "~1/GET"' in main
    assert 'dynamic "request_parameter"' in main and 'dynamic "route_settings"' in main
    assert main.count("count = local.debugging ? 1 : 0") == 3
    assert '"get_orders_integration_response_200"' in main
    assert 'name          = "spec-http"' in main
    outputs = next(f["content"] for f in result["files"] if f["path"] == "outputs.tf")
    assert "aws_api_gateway_stage.prod.stage_name" in outputs


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        ("/*", "*", "*/*"),
        ("/~1pets~1{id}", "GET", "pets/{id}/GET"),
        ("/", "POST", "~1/POST"),
        ({"Ref": "X"}, "GET", None),
    ],
)
def test_method_path(path, method, expected):
    assert method_path(path, method) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("200", "_200"), (404, "_404"), ("!!", "_3"), (True, "_3")]
)
def test_suffix(value, expected):
    assert name_suffix(value, 3) == expected


def test_rest_api_defaults_and_body_location():
    main, notes = convert(
        {
            "Api": {
                "Type": "AWS::ApiGateway::RestApi",
                "Properties": {
                    "BodyS3Location": {"Bucket": "b", "Key": "k"},
                    "MinimumCompressionSize": {"Ref": "AWS::Region"},
                    "Body": {"info": "not a mapping"},
                },
            },
            "NoBody": {"Type": "AWS::ApiGateway::RestApi"},
        }
    )
    assert 'name = "${var.stack_name}-Api"' in main
    assert "minimum_compression_size = data.aws_region.current.region" in main
    assert any("Api: BodyS3Location was not converted" in n for n in notes)
    assert any("NoBody had no Name" in n for n in notes)


def test_deployment_triggers_only_for_apis_with_a_body():
    main, notes = convert(
        {
            "Queue": {"Type": "AWS::SQS::Queue"},
            "Api": {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "a"}},
            "ToQueue": {
                "Type": "AWS::ApiGateway::Deployment",
                "Properties": {"RestApiId": {"Ref": "Queue"}},
            },
            "ToApi": {
                "Type": "AWS::ApiGateway::Deployment",
                "Properties": {"RestApiId": {"Ref": "Api"}, "StageName": "dev"},
            },
            "ToId": {"Type": "AWS::ApiGateway::Deployment", "Properties": {"RestApiId": "abc"}},
        }
    )
    assert "triggers" not in main
    assert sum("only redeployed when its arguments change" in n for n in notes) == 3
    assert 'stage_name    = "dev"' in main


def test_stage_method_settings_with_computed_path():
    main, notes = convert(
        {
            "Stage": {
                "Type": "AWS::ApiGateway::Stage",
                "Properties": {
                    "RestApiId": "abc",
                    "DeploymentId": "d",
                    "MethodSettings": [{"ResourcePath": {"Ref": "AWS::Region"}, "Odd": 1}],
                },
            }
        }
    )
    assert 'stage_name    = "${var.stack_name}-Stage"' in main
    assert 'method_path = "*/*"' in main
    assert any("MethodSettings[0] uses a computed path" in n for n in notes)
    assert any("'Stage.MethodSettings[0].Odd' was not converted" in n for n in notes)


def test_method_without_integration_and_unmatched_responses():
    main, _ = convert(
        {
            "Plain": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {"RestApiId": "a", "ResourceId": "r", "HttpMethod": "GET"},
            },
            "Proxy": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {
                    "RestApiId": "a",
                    "ResourceId": "r",
                    "HttpMethod": "ANY",
                    "MethodResponses": [{"StatusCode": {"Ref": "AWS::Region"}}],
                    "Integration": {
                        "Type": "HTTP_PROXY",
                        "IntegrationResponses": [{"StatusCode": "500"}],
                    },
                },
            },
        }
    )
    assert "plain_integration" not in main
    assert '"proxy_response_0"' in main
    assert "depends_on = [aws_api_gateway_integration.proxy_integration]" in main


def test_http_api_defaults_route_parameters_and_stage_settings():
    main, _ = convert(
        {
            "Api": {"Type": "AWS::ApiGatewayV2::Api", "Properties": {"Name": "a"}},
            "Route": {
                "Type": "AWS::ApiGatewayV2::Route",
                "Properties": {
                    "ApiId": {"Ref": "Api"},
                    "RouteKey": "$connect",
                    "RequestParameters": {"a": "true", "b": False, "c": {"Required": "false"}},
                },
            },
            "Stage": {
                "Type": "AWS::ApiGatewayV2::Stage",
                "Properties": {"ApiId": {"Ref": "Api"}, "RouteSettings": "computed"},
            },
            "Auth": {
                "Type": "AWS::ApiGatewayV2::Authorizer",
                "Properties": {"ApiId": {"Ref": "Api"}, "AuthorizerType": "REQUEST"},
            },
        }
    )
    assert 'protocol_type = "HTTP"' in main
    assert main.count("required              = true") == 1
    assert main.count("required              = false") == 2
    assert 'name   = "${var.stack_name}-Stage"' in main
    assert 'name            = "${var.stack_name}-Auth"' in main


# --- regression tests for the review findings -------------------------------------------

NO = {"Ref": "AWS::NoValue"}
API = {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "a"}}
HTTP = {"Type": "AWS::ApiGatewayV2::Api", "Properties": {"Name": "h", "ProtocolType": "HTTP"}}
EVIL = 'x" ${file("/etc/passwd")} y'


def convert_with_condition(resources: dict) -> tuple[str, list[str]]:
    template = {
        "Parameters": {"Env": {"Type": "String"}, "List": {"Type": "String"}},
        "Conditions": {"On": {"Fn::Equals": [{"Ref": "Env"}, "on"]}},
        "Resources": resources,
    }
    result = convert_template(yaml.safe_dump(template), stack_name="demo")
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    return main, result["notes"]


def optional(value):
    return {"Fn::If": ["On", value, NO]}


def assert_no_injection(main: str) -> None:
    code = "\n".join(line for line in main.splitlines() if not line.lstrip().startswith("#"))
    assert 'file("' not in code
    assert not re.search(r"(?<!\$)\$\{file", code)


def test_root_resource_method_settings_apply_to_the_root_only():
    main, _ = convert(
        {
            "Api": API,
            "Stage": {
                "Type": "AWS::ApiGateway::Stage",
                "Properties": {
                    "RestApiId": {"Ref": "Api"},
                    "DeploymentId": "d",
                    "StageName": "s",
                    "MethodSettings": [{"ResourcePath": "/", "HttpMethod": "GET"}],
                },
            },
        }
    )
    assert 'method_path = "~1/GET"' in main


def test_conditional_and_computed_method_settings():
    main, notes = convert_with_condition(
        {
            "Api": API,
            "Stage": {
                "Type": "AWS::ApiGateway::Stage",
                "Properties": {
                    "RestApiId": {"Ref": "Api"},
                    "DeploymentId": "d",
                    "StageName": "s",
                    "MethodSettings": [
                        optional({"ResourcePath": "/*", "HttpMethod": "*", "MetricsEnabled": True}),
                        {"Fn::If": ["On", {"ResourcePath": "/*"}, {"ResourcePath": "/"}]},
                    ],
                },
            },
            "Whole": {
                "Type": "AWS::ApiGateway::Stage",
                "Properties": {
                    "RestApiId": {"Ref": "Api"},
                    "DeploymentId": "d",
                    "StageName": "w",
                    "MethodSettings": optional([{"ResourcePath": "/*", "HttpMethod": "*"}]),
                },
            },
        }
    )
    assert "count = local.on ? 1 : 0" in main
    assert "metrics_enabled = true" in main
    assert main.count('resource "aws_api_gateway_method_settings"') == 1
    assert main.count("# MethodSettings was not converted") == 2
    assert len([n for n in notes if "is computed by a function" in n]) == 2


def test_conditional_and_computed_method_responses_and_integration():
    main, notes = convert_with_condition(
        {
            "Api": API,
            "Get": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {
                    "RestApiId": {"Ref": "Api"},
                    "ResourceId": {"Fn::GetAtt": ["Api", "RootResourceId"]},
                    "HttpMethod": "GET",
                    "MethodResponses": [optional({"StatusCode": 500}), {"Ref": "List"}],
                    "Integration": optional(
                        {
                            "Type": "MOCK",
                            "IntegrationResponses": [
                                optional({"StatusCode": 500}),
                                {"StatusCode": 200},
                                {"Ref": "List"},
                            ],
                        }
                    ),
                },
            },
            "Computed": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {
                    "RestApiId": {"Ref": "Api"},
                    "ResourceId": "r",
                    "HttpMethod": "GET",
                    "MethodResponses": {"Ref": "List"},
                    "Integration": {"Fn::If": ["On", {"Type": "MOCK"}, {"Type": "HTTP"}]},
                },
            },
        }
    )
    response = main.split('resource "aws_api_gateway_method_response" "get_response_500"')[1]
    assert response.lstrip("{\n ").startswith("count = local.on ? 1 : 0")
    integration = main.split('resource "aws_api_gateway_integration" "get_integration"')[1]
    assert integration.lstrip("{\n ").startswith("count = local.on ? 1 : 0")
    both = main.split('"get_integration_response_500"')[1]
    assert both.lstrip("{\n ").startswith("count = local.on && local.on ? 1 : 0")
    only = main.split('"get_integration_response_200"')[1]
    assert only.lstrip("{\n ").startswith("count = local.on ? 1 : 0")
    assert "aws_api_gateway_method_response.get_response_500," in main
    assert "# MethodResponses was not converted" in main
    assert "# Integration was not converted" in main
    assert "# Integration.IntegrationResponses was not converted" in main
    assert "computed_integration" not in main


def test_conditional_and_computed_route_parameters_and_settings():
    main, notes = convert_with_condition(
        {
            "Http": HTTP,
            "Route": {
                "Type": "AWS::ApiGatewayV2::Route",
                "Properties": {
                    "ApiId": {"Ref": "Http"},
                    "RouteKey": "GET /",
                    "RequestParameters": {
                        "route.request.querystring.a": optional({"Required": True}),
                        "route.request.querystring.b": {"Fn::If": ["On", {"Required": True}, {}]},
                        "route.request.querystring.c": "true",
                    },
                },
            },
            "Whole": {
                "Type": "AWS::ApiGatewayV2::Route",
                "Properties": {
                    "ApiId": {"Ref": "Http"},
                    "RouteKey": "GET /w",
                    "RequestParameters": optional({"route.request.querystring.a": {}}),
                },
            },
            "Stage": {
                "Type": "AWS::ApiGatewayV2::Stage",
                "Properties": {
                    "ApiId": {"Ref": "Http"},
                    "StageName": "s",
                    "RouteSettings": {
                        "GET /": optional({"ThrottlingBurstLimit": 1}),
                        "GET /x": {"Ref": "List"},
                    },
                },
            },
            "WholeStage": {
                "Type": "AWS::ApiGatewayV2::Stage",
                "Properties": {
                    "ApiId": {"Ref": "Http"},
                    "StageName": "w",
                    "RouteSettings": optional({"GET /": {}}),
                },
            },
        }
    )
    assert 'dynamic "request_parameter" {\n    for_each = local.on ? [1] : []' in main
    assert '"route.request.querystring.c"\n    required              = true' in main
    assert 'Fn::If"\n' not in main and 'request_parameter_key = "Fn::If"' not in main
    assert "# RequestParameters.route.request.querystring.b was not converted" in main
    assert "# RequestParameters was not converted" in main
    assert 'dynamic "route_settings" {\n    for_each = local.on ? [1] : []' in main
    assert "# RouteSettings.GET /x was not converted" in main
    assert "# RouteSettings was not converted" in main
    assert 'route_key = "Fn::If"' not in main


def test_api_gateway_template_text_cannot_inject_hcl():
    main, _ = convert(
        {
            "Api": {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": EVIL}},
            "Stage": {
                "Type": "AWS::ApiGateway::Stage",
                "Properties": {
                    "RestApiId": {"Ref": "Api"},
                    "DeploymentId": EVIL,
                    "StageName": EVIL,
                    "MethodSettings": [{"ResourcePath": EVIL, "HttpMethod": EVIL}],
                },
            },
            "Get": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {
                    "RestApiId": {"Ref": "Api"},
                    "ResourceId": EVIL,
                    "HttpMethod": EVIL,
                    "MethodResponses": [{"StatusCode": EVIL}],
                    "Integration": {"Type": EVIL, "IntegrationResponses": [{"StatusCode": EVIL}]},
                },
            },
            "Http": {"Type": "AWS::ApiGatewayV2::Api", "Properties": {"Name": EVIL}},
            "Route": {
                "Type": "AWS::ApiGatewayV2::Route",
                "Properties": {
                    "ApiId": {"Ref": "Http"},
                    "RouteKey": EVIL,
                    "RequestParameters": {EVIL: {"Required": EVIL}},
                },
            },
            "HttpStage": {
                "Type": "AWS::ApiGatewayV2::Stage",
                "Properties": {
                    "ApiId": {"Ref": "Http"},
                    "StageName": EVIL,
                    "RouteSettings": {EVIL: {"LoggingLevel": EVIL}},
                },
            },
        }
    )
    assert_no_injection(main)


def test_http_integration_tls_config():
    main, notes = convert(
        {
            "Http": {"Type": "AWS::ApiGatewayV2::Api", "Properties": {"Name": "h"}},
            "Private": {
                "Type": "AWS::ApiGatewayV2::Integration",
                "Properties": {
                    "ApiId": {"Ref": "Http"},
                    "IntegrationType": "HTTP_PROXY",
                    "TlsConfig": {"ServerNameToVerify": "example.com"},
                },
            },
        }
    )
    assert 'tls_config {\n    server_name_to_verify = "example.com"\n  }' in main
    assert not [n for n in notes if "was not converted" in n]
