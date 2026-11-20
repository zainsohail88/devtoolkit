"""API Gateway mappings (converters/cfn/resources/apigateway.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from converters.cfn import convert_template
from converters.cfn.resources.apigateway import _method_path, _suffix

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
    assert '"get_orders_integration_response_200"' in main
    assert 'name          = "spec-http"' in main
    outputs = next(f["content"] for f in result["files"] if f["path"] == "outputs.tf")
    assert "aws_api_gateway_stage.prod.stage_name" in outputs


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        ("/*", "*", "*/*"),
        ("/~1pets~1{id}", "GET", "pets/{id}/GET"),
        ("/", "POST", "*/POST"),
        ({"Ref": "X"}, "GET", None),
    ],
)
def test_method_path(path, method, expected):
    assert _method_path(path, method) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("200", "_200"), (404, "_404"), ("!!", "_3"), (True, "_3")]
)
def test_suffix(value, expected):
    assert _suffix(value, 3) == expected


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
