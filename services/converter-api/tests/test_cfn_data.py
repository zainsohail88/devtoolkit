"""RDS, KMS, Secrets Manager and SSM mappings."""

from __future__ import annotations

import json

from converters.cfn import convert_template


def convert(resources: dict, **sections: object) -> tuple[str, list[str]]:
    result = convert_template(json.dumps({"Resources": resources, **sections}), stack_name="demo")
    files = {f["path"]: f["content"] for f in result["files"]}
    return files["main.tf"] + files.get("outputs.tf", ""), result["notes"]


def _db(policy: str | None = None, **props: object) -> dict:
    resource: dict = {
        "Type": "AWS::RDS::DBInstance",
        "Properties": {"Engine": "postgres", "DBInstanceClass": "db.t4g.micro", **props},
    }
    if policy:
        resource["DeletionPolicy"] = policy
    return resource


def test_db_instance_final_snapshot_follows_the_deletion_policy():
    main, notes = convert(
        {
            "Default": _db(),
            "Named": _db("Retain", DBInstanceIdentifier="named-db"),
            "Gone": _db("Delete"),
            "Snap": _db("Snapshot"),
        },
        Outputs={
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
    main, notes = convert({"Db": _db("Delete", MasterUserPassword="secret", MasterUsername="u")})
    assert 'password            = "secret"' in main
    assert any("Db: the master password ends up in the Terraform state" in n for n in notes)


def test_db_instance_master_secret_without_key():
    main, _ = convert({"Db": _db("Delete", MasterUserSecret={})})
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
        Parameters={"Value": {"Type": "String"}},
        Conditions={"On": {"Fn::Equals": [1, 1]}},
    )
    assert 'value        = "1"' in main
    assert "value        = var.value" in main
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
        Outputs={
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
        Outputs={"Alias": {"Value": {"Ref": "Alias"}}, "Key": {"Value": {"Ref": "Key"}}},
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
        Outputs={"Arn": {"Value": {"Ref": "Secret"}}},
    )
    assert "GenerateSecretString was not converted" in main
    assert any("ephemeral random_password" in n for n in notes)
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
        Outputs={
            "Name": {"Value": {"Ref": "P"}},
            "Value": {"Value": {"Fn::GetAtt": ["P", "Value"]}, "Description": "v"},
        },
    )
    assert "aws_ssm_parameter.p.name" in main
    assert "aws_ssm_parameter.p.value" in main
