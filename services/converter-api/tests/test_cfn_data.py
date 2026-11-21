"""RDS, KMS, Secrets Manager and SSM mappings."""

from __future__ import annotations

import json
import re

from converters.cfn.pipeline import convert_template


def convert(
    resources: dict,
    parameters: dict | None = None,
    conditions: dict | None = None,
    outputs: dict | None = None,
) -> tuple[str, list[str]]:
    template: dict = {"Resources": resources}
    for section, value in (
        ("Parameters", parameters),
        ("Conditions", conditions),
        ("Outputs", outputs),
    ):
        if value is not None:
            template[section] = value
    result = convert_template(json.dumps(template), stack_name="demo")
    files = {f["path"]: f["content"] for f in result["files"]}
    return files["main.tf"] + files.get("outputs.tf", ""), result["notes"]


def squash(text: str) -> str:
    """HCL without the alignment spaces before '='."""
    return re.sub(r" +=", " =", text)


def _db(policy: str | None = None, props: dict | None = None) -> dict:
    resource: dict = {
        "Type": "AWS::RDS::DBInstance",
        "Properties": {"Engine": "postgres", "DBInstanceClass": "db.t4g.micro"} | (props or {}),
    }
    if policy:
        resource["DeletionPolicy"] = policy
    return resource


def test_db_instance_final_snapshot_follows_the_deletion_policy():
    main, notes = convert(
        {
            "Default": _db(),
            "Named": _db("Retain", {"DBInstanceIdentifier": "named-db"}),
            "Gone": _db("Delete"),
            "Snap": _db("Snapshot"),
        },
        outputs={
            "Id": {"Value": {"Ref": "Default"}},
            "Arn": {"Value": {"Fn::GetAtt": ["Default", "DBInstanceArn"]}},
            "Zone": {"Value": {"Fn::GetAtt": ["Default", "Endpoint.HostedZoneId"]}},
            "Res": {"Value": {"Fn::GetAtt": ["Default", "DbiResourceId"]}},
        },
    )
    assert 'final_snapshot_identifier = "default-final-snapshot"' in main
    assert 'final_snapshot_identifier = "named-db-final-snapshot"' in main
    assert 'final_snapshot_identifier = "snap-final-snapshot"' in main
    assert main.count("skip_final_snapshot = true") == 1
    assert any("Default: CloudFormation snapshots this database" in n for n in notes)
    assert not any(n.startswith("Snap: CloudFormation snapshots") for n in notes)
    assert not any("Snap has DeletionPolicy Snapshot" in n for n in notes)
    for attribute in ("identifier", "arn", "hosted_zone_id", "resource_id"):
        assert f"aws_db_instance.default.{attribute}" in main


def test_db_instance_password_note():
    main, notes = convert(
        {"Db": _db("Delete", {"MasterUserPassword": "secret", "MasterUsername": "u"})}
    )
    assert 'password            = "secret"' in main
    assert any("Db: the master password ends up in the Terraform state" in n for n in notes)


def test_db_instance_master_secret_without_key():
    main, _ = convert({"Db": _db("Delete", {"MasterUserSecret": {}})})
    assert "master_user_secret_kms_key_id" not in main


def test_parameter_group_values():
    main, notes = convert(
        {
            "Params": {
                "Type": "AWS::RDS::DBParameterGroup",
                "Properties": {
                    "Family": "postgres16",
                    "Description": "d",
                    "Parameters": {"a": 1, "b": {"Ref": "Value"}},
                },
            },
            "Computed": {
                "Type": "AWS::RDS::DBParameterGroup",
                "Properties": {
                    "Family": "postgres16",
                    "Description": "d",
                    "Parameters": {"Fn::If": ["On", {}, {}]},
                },
            },
        },
        parameters={"Value": {"Type": "String"}},
        conditions={"On": {"Fn::Equals": [1, 1]}},
    )
    assert 'value = "1"' in main
    assert "value = var.value" in main
    assert "apply_method" not in main
    assert any("Computed.Parameters' was not converted" in n for n in notes)


def test_cluster_attributes_and_password():
    main, notes = convert(
        {
            "Cluster": {
                "Type": "AWS::RDS::DBCluster",
                "DeletionPolicy": "Delete",
                "Properties": {
                    "Engine": "aurora-mysql",
                    "MasterUsername": "u",
                    "MasterUserPassword": "p",
                    "MasterUserSecret": {"KmsKeyId": "k"},
                },
            }
        },
        outputs={
            "Id": {"Value": {"Ref": "Cluster"}},
            "Read": {"Value": {"Fn::GetAtt": ["Cluster", "ReadEndpoint.Address"]}},
            "Secret": {"Value": {"Fn::GetAtt": ["Cluster", "MasterUserSecret.SecretArn"]}},
        },
    )
    assert 'master_password               = "p"' in main
    assert 'master_user_secret_kms_key_id = "k"' in main
    assert "skip_final_snapshot           = true" in main
    assert "aws_rds_cluster.cluster.cluster_identifier" in main
    assert "aws_rds_cluster.cluster.reader_endpoint" in main
    assert "aws_rds_cluster.cluster.master_user_secret[0].secret_arn" in main
    assert any("write-only master_password_wo" in n for n in notes)


def test_kms_references():
    main, _ = convert(
        {
            "Key": {"Type": "AWS::KMS::Key", "Properties": {"Description": "k"}},
            "Alias": {
                "Type": "AWS::KMS::Alias",
                "Properties": {
                    "AliasName": "alias/k",
                    "TargetKeyId": {"Fn::GetAtt": ["Key", "KeyId"]},
                },
            },
        },
        outputs={"Alias": {"Value": {"Ref": "Alias"}}, "Key": {"Value": {"Ref": "Key"}}},
    )
    assert "target_key_id = aws_kms_key.key.key_id" in main
    assert "aws_kms_alias.alias.name" in main


def test_secret_with_generated_value_is_reported():
    main, notes = convert(
        {
            "Secret": {
                "Type": "AWS::SecretsManager::Secret",
                "Properties": {"GenerateSecretString": {"PasswordLength": 32}},
            }
        },
        outputs={"Arn": {"Value": {"Ref": "Secret"}}},
    )
    assert "GenerateSecretString was not converted" in main
    assert any("random_password and secret_string_wo or convert this secret" in n for n in notes)
    assert "aws_secretsmanager_secret_version" not in main
    assert "aws_secretsmanager_secret.secret.arn" in main


def test_ssm_parameter_references():
    main, _ = convert(
        {
            "P": {
                "Type": "AWS::SSM::Parameter",
                "Properties": {"Name": "/p", "Type": "String", "Value": "v"},
            }
        },
        outputs={
            "Name": {"Value": {"Ref": "P"}},
            "Value": {"Value": {"Fn::GetAtt": ["P", "Value"]}, "Description": "v"},
        },
    )
    assert "aws_ssm_parameter.p.name" in main
    assert "aws_ssm_parameter.p.value" in main


def test_rds_names_are_lowercased_as_cloudformation_stores_them():
    main, _ = convert(
        {
            "Db": _db(props={"DBInstanceIdentifier": "MyDatabase"}),
            "Cluster": {
                "Type": "AWS::RDS::DBCluster",
                "Properties": {
                    "Engine": "aurora-postgresql",
                    "DBClusterIdentifier": {"Fn::Sub": "${AWS::StackName}-Cluster"},
                },
            },
            "Subnets": {
                "Type": "AWS::RDS::DBSubnetGroup",
                "Properties": {
                    "DBSubnetGroupName": {"Ref": "GroupName"},
                    "DBSubnetGroupDescription": "d",
                    "SubnetIds": ["subnet-1"],
                },
            },
            "Params": {
                "Type": "AWS::RDS::DBParameterGroup",
                "Properties": {
                    "DBParameterGroupName": "MyParams",
                    "Family": "postgres16",
                    "Description": "d",
                },
            },
            "Ref": _db("Delete", {"DBInstanceIdentifier": {"Ref": "Params"}}),
        },
        parameters={"GroupName": {"Type": "String"}},
    )
    main = squash(main)
    assert 'identifier = "mydatabase"' in main
    assert 'final_snapshot_identifier = "mydatabase-final-snapshot"' in main
    assert 'cluster_identifier = lower("${var.stack_name}-Cluster")' in main
    assert "name = lower(var.group_name)" in main
    assert 'name = "myparams"' in main
    # A reference to a resource of the template is already lowercase.
    assert "identifier = aws_db_parameter_group.params.name" in main


def test_db_instance_applies_changes_immediately_like_cloudformation():
    main, notes = convert(
        {"Now": _db("Delete"), "Later": _db("Delete", {"ApplyImmediately": False})},
    )
    main = squash(main)
    assert main.count("apply_immediately = true") == 1
    assert main.count("apply_immediately = false") == 1
    assert not any("ApplyImmediately" in n for n in notes)


def test_cluster_member_instance_is_a_cluster_instance():
    main, notes = convert(
        {
            "Cluster": {
                "Type": "AWS::RDS::DBCluster",
                "DeletionPolicy": "Delete",
                "Properties": {"Engine": "aurora-postgresql"},
            },
            "Member": _db(
                props={
                    "DBClusterIdentifier": {"Ref": "Cluster"},
                    "Engine": "aurora-postgresql",
                    "DBInstanceIdentifier": "Member-1",
                    "DBInstanceClass": "db.serverless",
                    "DBParameterGroupName": "params",
                    "PromotionTier": 1,
                    "PubliclyAccessible": False,
                    "MonitoringInterval": "60",
                    "Tags": [{"Key": "a", "Value": "b"}],
                }
            ),
        },
        outputs={
            "Address": {"Value": {"Fn::GetAtt": ["Member", "Endpoint.Address"]}},
            "Port": {"Value": {"Fn::GetAtt": ["Member", "Endpoint.Port"]}},
            "Res": {"Value": {"Fn::GetAtt": ["Member", "DbiResourceId"]}},
            "Id": {"Value": {"Ref": "Member"}},
        },
    )
    main = squash(main)
    assert 'resource "aws_rds_cluster_instance" "member"' in main
    assert "cluster_identifier = aws_rds_cluster.cluster.cluster_identifier" in main
    assert 'identifier = "member-1"' in main
    assert 'db_parameter_group_name = "params"' in main
    assert "promotion_tier = 1" in main
    assert "apply_immediately = true" in main
    member = main.split('resource "aws_rds_cluster_instance" "member"')[1]
    assert "final_snapshot" not in member
    assert not any(n.startswith("Member: CloudFormation snapshots") for n in notes)
    assert not any("not converted" in n for n in notes)
    for attribute in ("endpoint", "port", "dbi_resource_id", "identifier"):
        assert f"aws_rds_cluster_instance.member.{attribute}" in main


def test_parameter_group_applies_parameters_the_default_way():
    main, notes = convert(
        {
            "Params": {
                "Type": "AWS::RDS::DBParameterGroup",
                "Properties": {
                    "Family": "postgres16",
                    "Description": "d",
                    "Parameters": {"log_connections": True, "rds.force_ssl": False},
                },
            },
        }
    )
    main = squash(main)
    assert 'value = "true"' in main
    assert 'value = "false"' in main
    assert "apply_method" not in main
    assert any('needs apply_method = "pending-reboot"' in n for n in notes)
