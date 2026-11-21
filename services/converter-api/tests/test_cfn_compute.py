"""Lambda, EC2 and ECS mappings (converters/cfn/resources/compute.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from converters.cfn.convert import Converter
from converters.cfn.parse import load_template
from converters.cfn.pipeline import convert_template
from converters.cfn.resources.apigateway import method_path
from converters.cfn.resources.compute import ContainerDefinitions, LambdaCode

FIXTURE = Path(__file__).parent / "fixtures" / "cfn" / "compute.yaml"


def convert(resources: dict, extra: dict | None = None) -> tuple[str, list[str]]:
    template = {"Resources": resources}
    template.update(extra or {})
    result = convert_template(yaml.safe_dump(template), stack_name="demo")
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    return main, result["notes"]


def with_properties(resource: dict, extra: dict) -> dict:
    """A copy of `resource` with `extra` added to its properties."""
    properties = dict(resource["Properties"])
    properties.update(extra)
    return dict(resource, Properties=properties)


def with_hints(resources: dict, hints: dict) -> tuple[str, bool]:
    template = load_template(yaml.safe_dump({"Resources": resources}))
    converter = Converter(template, hints=hints)
    result = converter.run("single")
    return result["files"][0]["content"], converter.uses_archive


def test_fixture_converts_everything():
    result = convert_template(FIXTURE.read_text(), stack_name="demo")
    assert result["summary"]["unsupported"] == 0
    assert not [n for n in result["notes"] if "was left out" in n]
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
    # Review regressions: defaults, conditional items and string coercion.
    worker = main.split('resource "aws_ecs_service" "worker"')[1].split("\n}\n")[0]
    agent = main.split('resource "aws_ecs_service" "agent"')[1].split("\n}\n")[0]
    assert re.search(r"desired_count += 1", worker) and "desired_count" not in agent
    assert re.search(r'no_device += "true"', main)
    assert 'dynamic "ebs_block_device"' in main and 'dynamic "routing_config"' in main
    assert 'value = "8080"' in main and 'value = "false"' in main and "size = 64" in main
    assert "item if item != null" in main


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
    assert LambdaCode.inline_filename(handler, runtime) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("./src", 'source_dir  = "${path.module}/src"'),
        ("functions/api", 'source_dir  = "${path.module}/functions/api"'),
    ],
)
def test_code_path_directory_is_zipped(path, expected):
    main, archive = with_hints(
        {"Fn": with_properties(FUNCTION, {"Code": {"S3Bucket": "x"}})},
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
    assert any("NoCode: the template leaves out FunctionName" in n for n in notes)


def test_unconverted_code_property_is_reported():
    _, notes = convert({"Fn": with_properties(FUNCTION, {"Code": {"Odd": 1}})})
    assert any("'Fn.Code.Odd' yet" in n for n in notes)


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
    assert any("'Vm.BlockDeviceMappings.Odd' yet" in n for n in notes)
    for name in ("Vm", "Pair", "Attached", "Odd"):
        assert any(f"'{name}.NetworkInterfaces' yet" in n for n in notes)


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
    assert ContainerDefinitions().convert(value) == [
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
    assert 'name          = "${var.stack_name}-Plain"' in main
    assert main.count("desired_count = 1") == 2
    assert 'assign_public_ip = data.aws_region.current.region == "ENABLED"' in main
    assert 'capacity_provider = "FARGATE_SPOT"' in main
    assert 'field = "attribute:ecs.az"' in main
    assert 'alarm_names = ["a"]' in main


# --- regression tests for the review findings -------------------------------------------

CONDITION = {
    "Parameters": {"Env": {"Type": "String"}},
    "Conditions": {"On": {"Fn::Equals": [{"Ref": "Env"}, "on"]}},
}
NO = {"Ref": "AWS::NoValue"}


def optional(value):
    return {"Fn::If": ["On", value, NO]}


SERVICE = {"Type": "AWS::ECS::Service", "Properties": {"ServiceName": "s", "Cluster": "c"}}


def test_service_desired_count_defaults_to_one_like_cloudformation():
    main, _ = convert(
        {
            "Replica": SERVICE,
            "Daemon": with_properties(SERVICE, {"SchedulingStrategy": "daemon"}),
            "Explicit": with_properties(SERVICE, {"DesiredCount": 0}),
            "Chosen": with_properties(
                SERVICE, {"SchedulingStrategy": {"Fn::If": ["On", "DAEMON", "REPLICA"]}}
            ),
            "NoValue": with_properties(SERVICE, {"SchedulingStrategy": NO}),
        },
        CONDITION,
    )
    blocks = {
        m.group(1): m.group(2)
        for m in re.finditer(r'"aws_ecs_service" "(\w+)" \{(.*?)\n\}', main, re.S)
    }
    assert re.search(r"desired_count += 1", blocks["replica"])
    assert re.search(r"desired_count += 1", blocks["no_value"])
    assert "desired_count" not in blocks["daemon"]
    assert re.search(r"desired_count += 0", blocks["explicit"])
    assert (
        'desired_count       = (local.on ? "DAEMON" : "REPLICA") == "DAEMON" ? null : 1'
        in blocks["chosen"]
    )


def test_computed_assign_public_ip_is_parenthesised():
    main, _ = convert(
        {
            "Svc": with_properties(
                SERVICE,
                {
                    "NetworkConfiguration": {
                        "AwsvpcConfiguration": {
                            "Subnets": ["s"],
                            "AssignPublicIp": {"Fn::If": ["On", "ENABLED", "DISABLED"]},
                        }
                    },
                },
            )
        },
        CONDITION,
    )
    assert 'assign_public_ip = (local.on ? "ENABLED" : "DISABLED") == "ENABLED"' in main


def test_launch_template_no_device_is_never_empty():
    main, _ = convert(
        {
            "Lt": {
                "Type": "AWS::EC2::LaunchTemplate",
                "Properties": {
                    "LaunchTemplateData": {
                        "BlockDeviceMappings": [
                            {"DeviceName": "/dev/sdb", "NoDevice": ""},
                            {"DeviceName": "/dev/sdc", "NoDevice": "yes"},
                            {"DeviceName": "/dev/sdd", "NoDevice": {"Ref": "Env"}},
                        ]
                    }
                },
            }
        },
        CONDITION,
    )
    assert 'no_device   = "true"' in main
    assert 'no_device   = "yes"' in main
    assert 'no_device   = coalesce(var.env, "true")' in main


def test_root_method_path_is_not_every_resource():
    assert method_path("/", "GET") == "~1/GET"


def test_inline_code_using_cfn_response_is_noted():
    code = {"ZipFile": {"Fn::Sub": "import cfnresponse\n"}}
    _, notes = convert({"Fn": with_properties(FUNCTION, {"Code": code})})
    assert any("cfn-response module" in n for n in notes)
    plain = {"ZipFile": "def handler(e, c): pass\n"}
    _, notes = convert({"Fn": with_properties(FUNCTION, {"Code": plain})})
    assert not any("cfn-response" in n for n in notes)


def test_ref_of_function_url_is_the_function_arn():
    result = convert_template(
        yaml.safe_dump(
            {
                "Resources": {
                    "Url": {
                        "Type": "AWS::Lambda::Url",
                        "Properties": {"TargetFunctionArn": "arn:fn", "AuthType": "NONE"},
                    }
                },
                "Outputs": {"U": {"Value": {"Ref": "Url"}}},
            }
        ),
        stack_name="demo",
    )
    outputs = next(f["content"] for f in result["files"] if f["path"] == "outputs.tf")
    assert "aws_lambda_function_url.url.function_arn" in outputs


def test_primary_interface_kept_on_termination_is_noted():
    main, notes = convert(
        {
            "Vm": {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "ImageId": "ami-1",
                    "NetworkInterfaces": [
                        {"DeviceIndex": 0, "SubnetId": "s", "DeleteOnTermination": "false"}
                    ],
                },
            },
            "Default": {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "ImageId": "ami-1",
                    "NetworkInterfaces": [
                        {"DeviceIndex": 0, "SubnetId": "s", "DeleteOnTermination": "True"}
                    ],
                },
            },
        }
    )
    assert '# DeleteOnTermination was left out. CloudFormation value: "false"' in main
    assert sum("primary network interface" in n for n in notes) == 1


def test_container_definitions_types_strings_and_conditional_containers():
    value = [
        {
            "Name": "app",
            "Environment": [
                {"Name": "PORT", "Value": 8080},
                {"Name": "DEBUG", "Value": False},
                {"Name": "RATIO", "Value": 0.5},
                {"Fn::If": ["On", {"Name": "X", "Value": 1}, NO]},
                "odd",
                {"Name": "NOVALUE"},
            ],
            "DockerLabels": {"version": 2},
            "LogConfiguration": {"Options": {"awslogs-create-group": True}},
            "LinuxParameters": {"Tmpfs": [{"ContainerPath": "/scratch", "Size": "64"}]},
            "Memory": {"Fn::If": ["On", "512", "256"]},
        },
        {"Fn::If": ["On", {"Name": "sidecar", "Essential": "false"}, NO]},
        {"Fn::If": ["On"]},
        {"Ref": "Containers"},
        {"Environment": "odd", "DockerLabels": "odd"},
    ]
    assert ContainerDefinitions().convert(value) == [
        {
            "name": "app",
            "environment": [
                {"name": "PORT", "value": "8080"},
                {"name": "DEBUG", "value": "false"},
                {"name": "RATIO", "value": "0.5"},
                {"Fn::If": ["On", {"name": "X", "value": "1"}, NO]},
                "odd",
                {"name": "NOVALUE"},
            ],
            "dockerLabels": {"version": "2"},
            "logConfiguration": {"options": {"awslogs-create-group": "true"}},
            "linuxParameters": {"tmpfs": [{"containerPath": "/scratch", "size": 64}]},
            "memory": {"Fn::If": ["On", 512, 256]},
        },
        {"Fn::If": ["On", {"name": "sidecar", "essential": False}, NO]},
        {"Fn::If": ["On"]},
        {"Ref": "Containers"},
        {"environment": "odd", "dockerLabels": "odd"},
    ]


def test_conditional_container_is_filtered_out_of_the_list():
    main, _ = convert(
        {
            "Task": {
                "Type": "AWS::ECS::TaskDefinition",
                "Properties": {
                    "Family": "f",
                    "ContainerDefinitions": [
                        {"Name": "app", "Image": "i"},
                        optional({"Name": "sidecar", "Image": "busybox", "Memory": "64"}),
                    ],
                    "Volumes": [
                        {"Name": "logs", "Host": optional({"SourcePath": "/var/log"})},
                        {"Name": "odd", "Host": {"Fn::If": ["On", {"SourcePath": "/a"}, {}]}},
                    ],
                },
            }
        },
        CONDITION,
    )
    assert "item if item != null" in main
    assert re.search(r"local\.on \? \{ [^}]*memory = 64[^}]* \} : null", main)
    assert 'host_path = local.on ? "/var/log" : null' in main
    assert "# Host was left out" in main


def test_namespace_name_is_noted():
    main, notes = convert(
        {
            "Name": {
                "Type": "AWS::ECS::Cluster",
                "Properties": {"ClusterName": "a", "ServiceConnectDefaults": {"Namespace": "ns"}},
            },
            "Arn": {
                "Type": "AWS::ECS::Cluster",
                "Properties": {
                    "ClusterName": "b",
                    "ServiceConnectDefaults": {"Namespace": "arn:aws:servicediscovery:ns"},
                },
            },
        }
    )
    assert 'namespace = "ns"' in main
    assert sum("Terraform needs the namespace ARN" in n for n in notes) == 1


def test_conditional_alias_routing_and_concurrency():
    main, notes = convert(
        {
            "Fn": FUNCTION,
            "Alias": {
                "Type": "AWS::Lambda::Alias",
                "Properties": {
                    "Name": "live",
                    "FunctionName": {"Ref": "Fn"},
                    "FunctionVersion": "1",
                    "RoutingConfig": optional(
                        {
                            "AdditionalVersionWeights": [
                                {"FunctionVersion": "2", "FunctionWeight": 0.5},
                                {"Ref": "Weight"},
                            ]
                        }
                    ),
                    "ProvisionedConcurrencyConfig": optional(
                        {"ProvisionedConcurrentExecutions": 1}
                    ),
                },
            },
            "Computed": {
                "Type": "AWS::Lambda::Alias",
                "Properties": {
                    "Name": "c",
                    "FunctionName": {"Ref": "Fn"},
                    "FunctionVersion": "1",
                    "RoutingConfig": {"AdditionalVersionWeights": {"Ref": "Weights"}},
                    "ProvisionedConcurrencyConfig": {"Fn::If": ["On", {"A": 1}, {"B": 2}]},
                },
            },
        },
        {
            "Parameters": {
                "Env": {"Type": "String"},
                "Weight": {"Type": "String"},
                "Weights": {"Type": "String"},
            },
            "Conditions": CONDITION["Conditions"],
        },
    )
    assert 'dynamic "routing_config" {\n    for_each = local.on ? [1] : []' in main
    assert '"2" = 0.5' in main
    assert "count = local.on ? 1 : 0" in main
    assert "additional_version_weights = {}" in main
    assert "# AdditionalVersionWeights was left out" in main
    assert "# ProvisionedConcurrencyConfig was left out" in main


def test_conditional_and_computed_block_device_mappings():
    main, notes = convert(
        {
            "Vm": {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "ImageId": "ami-1",
                    "BlockDeviceMappings": [
                        optional({"DeviceName": "/dev/sdf", "Ebs": {"VolumeSize": 10}}),
                        {"Fn::If": ["On", {"DeviceName": "/dev/sdg"}, {"DeviceName": "/dev/sdh"}]},
                    ],
                },
            },
            "Whole": {
                "Type": "AWS::EC2::Instance",
                "Properties": {"ImageId": "ami-1", "BlockDeviceMappings": optional([])},
            },
        },
        CONDITION,
    )
    assert 'dynamic "ebs_block_device" {\n    for_each = local.on ? [1] : []' in main
    assert 'device_name = "/dev/sdf"' in main
    assert "# BlockDeviceMappings was left out" in main
    assert "ebs_block_device {\n    device_name = null" not in main
    assert any("'Whole.BlockDeviceMappings' comes from a function" in n for n in notes)


def test_computed_launch_template_tag_specifications_do_not_crash():
    main, notes = convert(
        {
            "Whole": {
                "Type": "AWS::EC2::LaunchTemplate",
                "Properties": {
                    "TagSpecifications": optional([{"ResourceType": "launch-template"}])
                },
            },
            "Items": {
                "Type": "AWS::EC2::LaunchTemplate",
                "Properties": {
                    "TagSpecifications": [
                        optional({"ResourceType": "launch-template"}),
                        {"ResourceType": "launch-template", "Tags": {"Ref": "Tags"}},
                    ]
                },
            },
        },
        {
            "Parameters": {"Env": {"Type": "String"}, "Tags": {"Type": "String"}},
            "Conditions": CONDITION["Conditions"],
        },
    )
    assert main.count("# TagSpecifications") == 3
    assert "# TagSpecifications.Tags was left out" in main
    assert len([n for n in notes if "comes from a function" in n]) == 3


EVIL = 'x" ${file("/etc/passwd")} y'


def test_template_text_cannot_inject_hcl():
    main, _ = convert(
        {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "FunctionName": EVIL,
                    "Role": EVIL,
                    "Handler": EVIL,
                    "Runtime": "python3.12",
                    "Code": {"ZipFile": EVIL},
                },
            },
            "Vm": {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "ImageId": EVIL,
                    "BlockDeviceMappings": [{"DeviceName": EVIL, "Ebs": {"VolumeSize": 1}}],
                    "NetworkInterfaces": [
                        {"DeviceIndex": 0, "SubnetId": EVIL, "DeleteOnTermination": EVIL}
                    ],
                },
            },
            "Task": {
                "Type": "AWS::ECS::TaskDefinition",
                "Properties": {
                    "Family": EVIL,
                    "ContainerDefinitions": [
                        {"Name": EVIL, "Environment": [{"Name": EVIL, "Value": EVIL}]}
                    ],
                    "Volumes": [{"Name": EVIL, "Host": {"SourcePath": EVIL}}],
                },
            },
            "Lt": {
                "Type": "AWS::EC2::LaunchTemplate",
                "Properties": {
                    "LaunchTemplateData": {
                        "BlockDeviceMappings": [{"DeviceName": EVIL, "NoDevice": EVIL}]
                    }
                },
            },
            "Svc": with_properties(
                SERVICE,
                {
                    "SchedulingStrategy": EVIL,
                    "NetworkConfiguration": {
                        "AwsvpcConfiguration": {"Subnets": [EVIL], "AssignPublicIp": EVIL}
                    },
                },
            ),
            "Cl": {
                "Type": "AWS::ECS::Cluster",
                "Properties": {"ClusterName": EVIL, "ServiceConnectDefaults": {"Namespace": EVIL}},
            },
        }
    )
    assert_no_injection(main)


def assert_no_injection(main: str) -> None:
    """EVIL only ever appears escaped outside comments (which are one line of JSON text)."""
    code = "\n".join(line for line in main.splitlines() if not line.lstrip().startswith("#"))
    assert 'file("' not in code
    assert not re.search(r"(?<!\$)\$\{file", code)


def test_file_systems_launch_templates_credits_and_placement():
    main, notes = convert(
        {
            "Fn": with_properties(
                FUNCTION,
                {"FileSystemConfigs": [{"Arn": "arn:ap", "LocalMountPath": "/mnt/data"}]},
            ),
            "Vm": {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "LaunchTemplate": {"LaunchTemplateName": "lt", "Version": "1"},
                },
            },
            "Lt": {
                "Type": "AWS::EC2::LaunchTemplate",
                "Properties": {
                    "LaunchTemplateData": {
                        "CreditSpecification": {"CpuCredits": "unlimited"},
                        "Placement": {"AvailabilityZone": "eu-west-1a", "Tenancy": "dedicated"},
                    }
                },
            },
        }
    )
    assert 'local_mount_path = "/mnt/data"' in main
    assert 'launch_template {\n    name    = "lt"\n    version = "1"\n  }' in main
    assert 'cpu_credits = "unlimited"' in main
    assert 'availability_zone = "eu-west-1a"' in main and 'tenancy           = "dedicated"' in main
    assert not [n for n in notes if "was left out" in n]
