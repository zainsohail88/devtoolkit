"""Lambda, EC2 and ECS mappings (converters/cfn/resources/compute.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from converters.cfn import convert_template
from converters.cfn.convert import Converter
from converters.cfn.parse import load_template
from converters.cfn.resources.compute import _inline_filename, ecs_json

FIXTURE = Path(__file__).parent / "fixtures" / "cfn" / "compute.yaml"


def convert(resources: dict, **extra) -> tuple[str, list[str]]:
    template = {"Resources": resources, **extra}
    result = convert_template(yaml.safe_dump(template), stack_name="demo")
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    return main, result["notes"]


def with_hints(resources: dict, hints: dict) -> tuple[str, bool]:
    template = load_template(yaml.safe_dump({"Resources": resources}))
    converter = Converter(template, hints=hints)
    result = converter.run("single")
    return result["files"][0]["content"], converter.uses_archive


def test_fixture_converts_everything():
    result = convert_template(FIXTURE.read_text(), stack_name="demo")
    assert result["summary"]["unsupported"] == 0
    assert not [n for n in result["notes"] if "was not converted" in n]
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    assert 'data "archive_file" "handler_code"' in main
    assert 'filename = "app.py"' in main
    assert "publish                        = true" in main
    assert 'resource "terraform_data" "version_"' in main
    assert "root_block_device" in main and "ephemeral_block_device" in main
    assert "assign_public_ip = false" in main
    assert "containerPort = 80" in main and "essential = true" in main
    assert '"com.example.Team" = "platform"' in main
    assert "aws_ecs_cluster_capacity_providers" in main


FUNCTION = {"Type": "AWS::Lambda::Function", "Properties": {"Role": "arn:role", "Runtime": "x"}}


@pytest.mark.parametrize(
    ("handler", "runtime", "expected"),
    [
        ("app.main", "python3.12", "app.py"),
        ("index.handler", "nodejs20.x", "index.js"),
        ("lambda_function.handler", "ruby3.3", "lambda_function.rb"),
        ("plain", None, "index.py"),
        (None, "java21", "index.py"),
    ],
)
def test_inline_filename(handler, runtime, expected):
    assert _inline_filename(handler, runtime) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("./src", 'source_dir  = "${path.module}/src"'),
        ("functions/api", 'source_dir  = "${path.module}/functions/api"'),
    ],
)
def test_code_path_directory_is_zipped(path, expected):
    main, archive = with_hints(
        {"Fn": {**FUNCTION, "Properties": {**FUNCTION["Properties"], "Code": {"S3Bucket": "x"}}}},
        {"Fn": {"code_path": path}},
    )
    assert archive
    assert expected in main
    assert "filename         = data.archive_file.fn_code.output_path" in main
    assert "s3_bucket" not in main


@pytest.mark.parametrize("path", ["build/app.zip", "target/app.JAR"])
def test_code_path_archive_is_used_as_is(path):
    main, archive = with_hints({"Fn": FUNCTION}, {"Fn": {"code_path": path}})
    assert not archive
    assert f'filename         = "${{path.module}}/{path}"' in main
    assert f'filebase64sha256("${{path.module}}/{path}")' in main


def test_layer_code_path_and_generated_name():
    main, archive = with_hints(
        {"Layer": {"Type": "AWS::Lambda::LayerVersion", "Properties": {}}},
        {"Layer": {"code_path": "layer"}},
    )
    assert archive
    assert 'layer_name       = "${var.stack_name}-Layer"' in main


def test_function_without_code_and_image_with_explicit_package_type():
    main, notes = convert(
        {
            "NoCode": FUNCTION,
            "Image": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Role": "r",
                    "PackageType": "Image",
                    "Code": {"ImageUri": "repo:1", "SourceKMSKeyArn": "k"},
                    "LoggingConfig": {"LogGroup": "g"},
                },
            },
        }
    )
    assert len(re.findall(r'package_type += "Image"', main)) == 1
    assert 'source_kms_key_arn = "k"' in main
    assert 'log_format = "Text"' in main
    assert any("NoCode had no FunctionName" in n for n in notes)


def test_unconverted_code_property_is_reported():
    _, notes = convert(
        {"Fn": {**FUNCTION, "Properties": {**FUNCTION["Properties"], "Code": {"Odd": 1}}}}
    )
    assert any("'Fn.Code.Odd' was not converted" in n for n in notes)


def test_versions_of_other_functions_do_not_publish():
    main, notes = convert(
        {
            "A": FUNCTION,
            "B": FUNCTION,
            "Queue": {"Type": "AWS::SQS::Queue"},
            "VersionOfB": {
                "Type": "AWS::Lambda::Version",
                "Properties": {"FunctionName": {"Fn::GetAtt": ["B", "Arn"]}},
            },
            "NotAFunction": {
                "Type": "AWS::Lambda::Version",
                "Properties": {"FunctionName": {"Ref": "Queue"}},
            },
            "Computed": {
                "Type": "AWS::Lambda::Version",
                "Properties": {
                    "FunctionName": {"Fn::Sub": "fn-${AWS::Region}"},
                    "ProvisionedConcurrencyConfig": {"ProvisionedConcurrentExecutions": 3},
                },
            },
            "External": {
                "Type": "AWS::Lambda::Version",
                "Properties": {"FunctionName": "arn:aws:lambda:us-east-1:1:function:x"},
            },
        }
    )
    assert main.count("publish") == 1
    assert "aws_lambda_function.b.qualified_arn" in main
    assert 'version = "$LATEST"' in main
    assert "qualifier                         = terraform_data.computed.output.version" in main
    assert sum("belongs to a function outside this template" in n for n in notes) == 3


def test_event_source_mapping_timestamp_passthrough_and_tags():
    main, _ = convert(
        {
            "Map": {
                "Type": "AWS::Lambda::EventSourceMapping",
                "Properties": {
                    "FunctionName": "fn",
                    "StartingPositionTimestamp": "2026-01-01T00:00:00Z",
                    "Queues": "q",
                    "MetricsConfig": {"Metrics": ["EventCount"]},
                    "Tags": [{"Key": "a", "Value": "b"}],
                },
            }
        }
    )
    assert 'starting_position_timestamp = "2026-01-01T00:00:00Z"' in main
    assert 'queues                      = ["q"]' in main
    assert 'metrics = ["EventCount"]' in main


def test_alias_with_computed_version_weight():
    main, _ = convert(
        {
            "Fn": FUNCTION,
            "Alias": {
                "Type": "AWS::Lambda::Alias",
                "Properties": {
                    "Name": "live",
                    "FunctionName": {"Ref": "Fn"},
                    "FunctionVersion": "$LATEST",
                    "RoutingConfig": {
                        "AdditionalVersionWeights": [
                            {"FunctionVersion": {"Ref": "AWS::Region"}, "FunctionWeight": 0.5}
                        ]
                    },
                },
            },
        }
    )
    assert "(data.aws_region.current.region) = 0.5" in main


def test_instance_devices_interfaces_and_user_data():
    main, notes = convert(
        {
            "Vm": {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "ImageId": "ami-1",
                    "UserData": {"Fn::Sub": "x"},
                    "BlockDeviceMappings": [
                        {"DeviceName": "/dev/sdc", "NoDevice": {}},
                        {"DeviceName": "/dev/sdd"},
                        {"DeviceName": "/dev/sde", "Ebs": {"SnapshotId": "snap-1"}, "Odd": 1},
                    ],
                    "NetworkInterfaces": [{"DeviceIndex": 1, "SubnetId": "s"}],
                },
            },
            "Pair": {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "ImageId": "ami-1",
                    "NetworkInterfaces": [{"DeviceIndex": 0}, {"DeviceIndex": 1}],
                },
            },
            "Attached": {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "ImageId": "ami-1",
                    "NetworkInterfaces": [{"NetworkInterfaceId": "eni-1", "DeviceIndex": 0}],
                },
            },
            "Odd": {
                "Type": "AWS::EC2::Instance",
                "Properties": {"ImageId": "ami-1", "NetworkInterfaces": ["eni"]},
            },
        }
    )
    assert "no_device   = true" in main
    assert 'snapshot_id = "snap-1"' in main
    assert 'user_data_base64 = "x"' in main
    assert any("'Vm.BlockDeviceMappings.Odd' was not converted" in n for n in notes)
    for name in ("Vm", "Pair", "Attached", "Odd"):
        assert any(f"'{name}.NetworkInterfaces' was not converted" in n for n in notes)


def test_launch_template_without_data():
    main, _ = convert({"Lt": {"Type": "AWS::EC2::LaunchTemplate", "Properties": {}}})
    assert 'resource "aws_launch_template" "lt" {}' in main


def test_cluster_variants():
    main, _ = convert(
        {
            "Plain": {"Type": "AWS::ECS::Cluster"},
            "Strategy": {
                "Type": "AWS::ECS::Cluster",
                "Properties": {
                    "DefaultCapacityProviderStrategy": [{"CapacityProvider": "FARGATE"}],
                    "Configuration": {"ManagedStorageConfiguration": {"KmsKeyId": "k"}},
                },
            },
        }
    )
    assert 'name = "${var.stack_name}-Plain"' in main
    assert "capacity_providers" not in main.split("strategy_capacity_providers")[1]
    assert 'kms_key_id = "k"' in main


def test_ecs_json_keeps_maps_intrinsics_and_types_values():
    value = [
        {
            "Name": "app",
            "Memory": "512",
            "Cpu": "half",
            "ReadonlyRootFilesystem": "TRUE",
            "Privileged": "maybe",
            "Image": {"Fn::Sub": "${Repo}:latest"},
            "DockerLabels": {"KeepMe": "x"},
            "LogConfiguration": {"Options": {"AwsLogs-Group": "g"}},
        }
    ]
    assert ecs_json(value) == [
        {
            "name": "app",
            "memory": 512,
            "cpu": "half",
            "readonlyRootFilesystem": True,
            "privileged": "maybe",
            "image": {"Fn::Sub": "${Repo}:latest"},
            "dockerLabels": {"KeepMe": "x"},
            "logConfiguration": {"options": {"AwsLogs-Group": "g"}},
        }
    ]


def test_task_definition_volumes_and_generated_family():
    main, _ = convert(
        {
            "Task": {
                "Type": "AWS::ECS::TaskDefinition",
                "Properties": {
                    "Volumes": [
                        {"Name": "host", "Host": {"SourcePath": "/var/log"}},
                        {
                            "Name": "docker",
                            "DockerVolumeConfiguration": {
                                "Autoprovision": True,
                                "Driver": "local",
                                "DriverOpts": {"type": "tmpfs"},
                            },
                        },
                    ],
                    "PlacementConstraints": [{"Type": "memberOf", "Expression": "x"}],
                },
            }
        }
    )
    assert 'family = "${var.stack_name}-Task"' in main
    assert 'host_path = "/var/log"' in main
    assert "autoprovision = true" in main
    assert "container_definitions" not in main


def test_service_minimal_and_computed_public_ip():
    main, _ = convert(
        {
            "Plain": {
                "Type": "AWS::ECS::Service",
                "Properties": {
                    "Cluster": "c",
                    "NetworkConfiguration": {"AwsvpcConfiguration": {"Subnets": ["s"]}},
                },
            },
            "Svc": {
                "Type": "AWS::ECS::Service",
                "Properties": {
                    "ServiceName": "svc",
                    "NetworkConfiguration": {
                        "AwsvpcConfiguration": {
                            "Subnets": ["s"],
                            "AssignPublicIp": {"Fn::Sub": "${AWS::Region}"},
                        }
                    },
                    "CapacityProviderStrategy": [{"CapacityProvider": "FARGATE_SPOT", "Weight": 2}],
                    "PlacementStrategies": [{"Type": "spread", "Field": "attribute:ecs.az"}],
                    "ServiceRegistries": [{"RegistryArn": "arn", "Port": 80}],
                    "DeploymentConfiguration": {
                        "Alarms": {"AlarmNames": ["a"], "Enable": True, "Rollback": False}
                    },
                },
            },
        }
    )
    assert 'name    = "${var.stack_name}-Plain"' in main
    assert 'assign_public_ip = data.aws_region.current.region == "ENABLED"' in main
    assert 'capacity_provider = "FARGATE_SPOT"' in main
    assert 'field = "attribute:ecs.az"' in main
    assert 'alarm_names = ["a"]' in main
