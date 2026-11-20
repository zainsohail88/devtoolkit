"""RDS, KMS, Secrets Manager and SSM Parameter Store."""

from __future__ import annotations

from typing import Any

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props
from converters.cfn.context import ResourceInfo, intrinsic_name
from converters.cfn.hcl import Attr, Block, Raw
from converters.cfn.naming import identifier
from converters.cfn.resources import resource

# --- RDS --------------------------------------------------------------------------------


def _lowercase(b: Builder, tf_name: str, cfn_name: str) -> None:
    """A name RDS stores in lowercase. CloudFormation lowercases it; the provider rejects
    uppercase letters, so a literal is lowercased and an expression goes through lower().
    References to resources of the template are already lowercase."""
    value = b.props.take(cfn_name)
    if value is None:
        return
    if isinstance(value, str):
        b.props.set(tf_name, value.lower())
        return
    if isinstance(value, dict) and ("Fn::GetAtt" in value or value.get("Ref") in b.ctx.resources):
        b.props.set(tf_name, b.ctx.expr(value))
        return
    b.props.set(tf_name, Raw(f"lower({hcl.inline(b.ctx.expr(value))})"))


def _apply_immediately(b: Builder) -> None:
    """CloudFormation applies DB instance changes at once (ApplyImmediately defaults to
    true); Terraform waits for the maintenance window unless told otherwise."""
    if not b.props.boolean("apply_immediately", "ApplyImmediately"):
        b.props.set("apply_immediately", True)


def _final_snapshot(b: Builder, identifier_prop: str) -> list[Attr]:
    """CloudFormation snapshots DB instances and clusters on delete unless DeletionPolicy is
    Delete. Terraform's default is the opposite, so spell it out. Call before the identifier
    property is taken; add the returned attributes at the end."""
    if b.resource.get("DeletionPolicy") == "Delete":
        return [Attr("skip_final_snapshot", True)]
    name = b.props.peek(identifier_prop)
    base = name.lower() if isinstance(name, str) else identifier(b.logical_id).replace("_", "-")
    if b.resource.get("DeletionPolicy") is None:
        b.ctx.note(
            f"{b.logical_id}: CloudFormation snapshots this database on delete by default; "
            f"Terraform does the same here, naming the snapshot '{base}-final-snapshot'"
        )
    return [
        Attr("skip_final_snapshot", False),
        Attr("final_snapshot_identifier", f"{base}-final-snapshot"),
    ]


def _password(b: Builder, tf_name: str) -> None:
    if b.props.attr(tf_name, "MasterUserPassword"):
        b.ctx.note(
            f"{b.logical_id}: the master password ends up in the Terraform state; prefer "
            "manage_master_user_password = true (a Secrets Manager secret) or a write-only "
            f"{tf_name}_wo argument"
        )


def _master_secret(b: Builder) -> None:
    secret = b.props.take("MasterUserSecret")
    if isinstance(secret, dict) and secret.get("KmsKeyId") is not None:
        b.props.set("master_user_secret_kms_key_id", b.ctx.expr(secret["KmsKeyId"]))


def _prepare_instance(info: ResourceInfo, resource_: dict[str, Any]) -> None:
    """A DB instance of an Aurora (or Multi-AZ) cluster is an aws_rds_cluster_instance."""
    if (resource_.get("Properties") or {}).get("DBClusterIdentifier") is not None:
        info.tf_type = "aws_rds_cluster_instance"
        info.attrs.update(
            {
                "Endpoint.Address": "endpoint",
                "DbiResourceId": "dbi_resource_id",
            }
        )
        info.attrs.pop("Endpoint.HostedZoneId")
        info.attrs.pop("MasterUserSecret.SecretArn")


@resource(
    "AWS::RDS::DBInstance",
    "aws_db_instance",
    ref="identifier",
    attrs={
        "Endpoint.Address": "address",
        "Endpoint.Port": "port",
        "Endpoint.HostedZoneId": "hosted_zone_id",
        "DBInstanceArn": "arn",
        "DbiResourceId": "resource_id",
        "MasterUserSecret.SecretArn": "master_user_secret[0].secret_arn",
    },
    import_id="DBInstanceIdentifier",
    prepare=_prepare_instance,
)
def db_instance(b: Builder) -> None:
    if b.info.tf_type == "aws_rds_cluster_instance":
        _cluster_instance(b)
        return
    p = b.props
    snapshot = _final_snapshot(b, "DBInstanceIdentifier")
    _lowercase(b, "identifier", "DBInstanceIdentifier")
    p.attrs(
        {
            "engine": "Engine",
            "engine_version": "EngineVersion",
            "instance_class": "DBInstanceClass",
            "storage_type": "StorageType",
            "kms_key_id": "KmsKeyId",
            "username": "MasterUsername",
            "db_name": "DBName",
            "db_subnet_group_name": "DBSubnetGroupName",
            "parameter_group_name": "DBParameterGroupName",
            "option_group_name": "OptionGroupName",
            "backup_window": "PreferredBackupWindow",
            "maintenance_window": "PreferredMaintenanceWindow",
            "performance_insights_kms_key_id": "PerformanceInsightsKMSKeyId",
            "monitoring_role_arn": "MonitoringRoleArn",
            "availability_zone": "AvailabilityZone",
            "ca_cert_identifier": "CACertificateIdentifier",
            "license_model": "LicenseModel",
            "snapshot_identifier": "DBSnapshotIdentifier",
            "replicate_source_db": "SourceDBInstanceIdentifier",
            "network_type": "NetworkType",
            "character_set_name": "CharacterSetName",
            "timezone": "Timezone",
        }
    )
    for tf_name, cfn_name in (
        ("allocated_storage", "AllocatedStorage"),
        ("max_allocated_storage", "MaxAllocatedStorage"),
        ("iops", "Iops"),
        ("storage_throughput", "StorageThroughput"),
        ("port", "Port"),
        ("backup_retention_period", "BackupRetentionPeriod"),
        ("monitoring_interval", "MonitoringInterval"),
        ("performance_insights_retention_period", "PerformanceInsightsRetentionPeriod"),
    ):
        p.number(tf_name, cfn_name)
    for tf_name, cfn_name in (
        ("storage_encrypted", "StorageEncrypted"),
        ("multi_az", "MultiAZ"),
        ("publicly_accessible", "PubliclyAccessible"),
        ("deletion_protection", "DeletionProtection"),
        ("auto_minor_version_upgrade", "AutoMinorVersionUpgrade"),
        ("allow_major_version_upgrade", "AllowMajorVersionUpgrade"),
        ("copy_tags_to_snapshot", "CopyTagsToSnapshot"),
        ("iam_database_authentication_enabled", "EnableIAMDatabaseAuthentication"),
        ("performance_insights_enabled", "EnablePerformanceInsights"),
        ("manage_master_user_password", "ManageMasterUserPassword"),
        ("delete_automated_backups", "DeleteAutomatedBackups"),
    ):
        p.boolean(tf_name, cfn_name)
    _password(b, "password")
    _master_secret(b)
    p.list_attr("vpc_security_group_ids", "VPCSecurityGroups")
    p.list_attr("enabled_cloudwatch_logs_exports", "EnableCloudwatchLogsExports")
    _apply_immediately(b)
    p.items.extend(snapshot)
    p.tags()


def _cluster_instance(b: Builder) -> None:
    """A cluster member. CloudFormation takes no snapshot of it on delete (the cluster holds
    the data), so there are no final snapshot settings."""
    p = b.props
    _lowercase(b, "identifier", "DBInstanceIdentifier")
    p.attrs(
        {
            "cluster_identifier": "DBClusterIdentifier",
            "engine": "Engine",
            "engine_version": "EngineVersion",
            "instance_class": "DBInstanceClass",
            "db_subnet_group_name": "DBSubnetGroupName",
            "db_parameter_group_name": "DBParameterGroupName",
            "availability_zone": "AvailabilityZone",
            "ca_cert_identifier": "CACertificateIdentifier",
            "monitoring_role_arn": "MonitoringRoleArn",
            "performance_insights_kms_key_id": "PerformanceInsightsKMSKeyId",
            "preferred_backup_window": "PreferredBackupWindow",
            "preferred_maintenance_window": "PreferredMaintenanceWindow",
        }
    )
    for tf_name, cfn_name in (
        ("monitoring_interval", "MonitoringInterval"),
        ("performance_insights_retention_period", "PerformanceInsightsRetentionPeriod"),
        ("promotion_tier", "PromotionTier"),
    ):
        p.number(tf_name, cfn_name)
    for tf_name, cfn_name in (
        ("publicly_accessible", "PubliclyAccessible"),
        ("auto_minor_version_upgrade", "AutoMinorVersionUpgrade"),
        ("copy_tags_to_snapshot", "CopyTagsToSnapshot"),
        ("performance_insights_enabled", "EnablePerformanceInsights"),
    ):
        p.boolean(tf_name, cfn_name)
    _apply_immediately(b)
    p.tags()


@resource(
    "AWS::RDS::DBSubnetGroup", "aws_db_subnet_group", ref="name", import_id="DBSubnetGroupName"
)
def db_subnet_group(b: Builder) -> None:
    p = b.props
    _lowercase(b, "name", "DBSubnetGroupName")
    p.attr("description", "DBSubnetGroupDescription")
    p.list_attr("subnet_ids", "SubnetIds")
    p.tags()


@resource(
    "AWS::RDS::DBParameterGroup",
    "aws_db_parameter_group",
    ref="name",
    import_id="DBParameterGroupName",
)
def db_parameter_group(b: Builder) -> None:
    p = b.props
    _lowercase(b, "name", "DBParameterGroupName")
    p.attr("description", "Description")
    p.attr("family", "Family")
    parameters = p.peek("Parameters")
    if isinstance(parameters, dict) and intrinsic_name(parameters) is None:
        p.drop("Parameters")
        for name, value in parameters.items():
            # No apply_method: like CloudFormation, the provider applies each parameter the
            # default way, immediately ("pending-reboot" would hold dynamic ones back).
            items = [Attr("name", str(name)), Attr("value", _parameter_value(b, value))]
            p.items.append(Block("parameter", [], items))
        if parameters:
            b.ctx.note(
                f"{b.logical_id}: the provider applies parameters immediately; a static "
                'parameter fails that way and needs apply_method = "pending-reboot" (it '
                "takes effect at the next reboot, as with CloudFormation)"
            )
    p.tags()


def _parameter_value(b: Builder, value: Any) -> Any:
    """Parameter values are text; CloudFormation writes booleans as "true" and "false"."""
    if isinstance(value, dict):
        return b.ctx.expr(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _scaling_v2(s: Props) -> None:
    s.number("min_capacity", "MinCapacity")
    s.number("max_capacity", "MaxCapacity")
    s.number("seconds_until_auto_pause", "SecondsUntilAutoPause")


@resource(
    "AWS::RDS::DBCluster",
    "aws_rds_cluster",
    ref="cluster_identifier",
    attrs={
        "Endpoint.Address": "endpoint",
        "Endpoint.Port": "port",
        "ReadEndpoint.Address": "reader_endpoint",
        "DBClusterArn": "arn",
        "DBClusterResourceId": "cluster_resource_id",
        "MasterUserSecret.SecretArn": "master_user_secret[0].secret_arn",
    },
    import_id="DBClusterIdentifier",
)
def db_cluster(b: Builder) -> None:
    p = b.props
    snapshot = _final_snapshot(b, "DBClusterIdentifier")
    _lowercase(b, "cluster_identifier", "DBClusterIdentifier")
    p.attrs(
        {
            "engine": "Engine",
            "engine_version": "EngineVersion",
            "engine_mode": "EngineMode",
            "database_name": "DatabaseName",
            "master_username": "MasterUsername",
            "db_subnet_group_name": "DBSubnetGroupName",
            "db_cluster_parameter_group_name": "DBClusterParameterGroupName",
            "db_cluster_instance_class": "DBClusterInstanceClass",
            "preferred_backup_window": "PreferredBackupWindow",
            "preferred_maintenance_window": "PreferredMaintenanceWindow",
            "kms_key_id": "KmsKeyId",
            "storage_type": "StorageType",
            "network_type": "NetworkType",
            "snapshot_identifier": "SnapshotIdentifier",
        }
    )
    for tf_name, cfn_name in (
        ("port", "Port"),
        ("backup_retention_period", "BackupRetentionPeriod"),
        ("allocated_storage", "AllocatedStorage"),
        ("iops", "Iops"),
    ):
        p.number(tf_name, cfn_name)
    for tf_name, cfn_name in (
        ("storage_encrypted", "StorageEncrypted"),
        ("deletion_protection", "DeletionProtection"),
        ("iam_database_authentication_enabled", "EnableIAMDatabaseAuthentication"),
        ("enable_http_endpoint", "EnableHttpEndpoint"),
        ("copy_tags_to_snapshot", "CopyTagsToSnapshot"),
        ("manage_master_user_password", "ManageMasterUserPassword"),
    ):
        p.boolean(tf_name, cfn_name)
    _password(b, "master_password")
    _master_secret(b)
    p.list_attr("vpc_security_group_ids", "VpcSecurityGroupIds")
    p.list_attr("enabled_cloudwatch_logs_exports", "EnableCloudwatchLogsExports")
    p.block("serverlessv2_scaling_configuration", "ServerlessV2ScalingConfiguration", _scaling_v2)
    p.items.extend(snapshot)
    p.tags()


# --- KMS --------------------------------------------------------------------------------


@resource("AWS::KMS::Key", "aws_kms_key", ref="key_id", attrs={"Arn": "arn", "KeyId": "key_id"})
def kms_key(b: Builder) -> None:
    p = b.props
    p.attr("description", "Description")
    p.json("policy", "KeyPolicy")
    p.boolean("enable_key_rotation", "EnableKeyRotation")
    p.boolean("is_enabled", "Enabled")
    p.attr("customer_master_key_spec", "KeySpec")
    p.attr("key_usage", "KeyUsage")
    p.boolean("multi_region", "MultiRegion")
    p.number("deletion_window_in_days", "PendingWindowInDays")
    p.number("rotation_period_in_days", "RotationPeriodInDays")
    p.boolean("bypass_policy_lockout_safety_check", "BypassPolicyLockoutSafetyCheck")
    p.tags()


@resource("AWS::KMS::Alias", "aws_kms_alias", ref="name", import_id="AliasName")
def kms_alias(b: Builder) -> None:
    b.props.attr("name", "AliasName")
    b.props.attr("target_key_id", "TargetKeyId")


# --- Secrets Manager --------------------------------------------------------------------


@resource("AWS::SecretsManager::Secret", "aws_secretsmanager_secret", ref="arn", attrs={"Id": "id"})
def secretsmanager_secret(b: Builder) -> None:
    p = b.props
    p.attr("name", "Name")
    p.attr("description", "Description")
    p.attr("kms_key_id", "KmsKeyId")
    p.block(
        "replica",
        "ReplicaRegions",
        lambda r: r.attrs({"region": "Region", "kms_key_id": "KmsKeyId"}),
    )
    p.tags()
    value = p.take("SecretString")
    if value is not None:
        b.add_resource(
            "aws_secretsmanager_secret_version",
            "_value",
            [Attr("secret_id", b.self_ref("id")), Attr("secret_string", b.ctx.expr(value))],
        )
        b.ctx.note(
            f"{b.logical_id}: the secret value ends up in the Terraform state; consider the "
            "write-only secret_string_wo argument"
        )
    if p.has("GenerateSecretString"):
        b.ctx.note(
            f"{b.logical_id}: GenerateSecretString has no aws provider equivalent; generate the "
            "value with an ephemeral random_password and secret_string_wo, or convert this "
            "secret with the awscc provider"
        )


# --- SSM Parameter Store ----------------------------------------------------------------


@resource(
    "AWS::SSM::Parameter",
    "aws_ssm_parameter",
    ref="name",
    attrs={"Type": "type", "Value": "value"},
    import_id="Name",
)
def ssm_parameter(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "Name")
    p.attr("type", "Type")
    p.attr("value", "Value")
    p.attr("description", "Description")
    p.attr("allowed_pattern", "AllowedPattern")
    p.attr("tier", "Tier")
    p.attr("data_type", "DataType")
    p.tags()
