"""RDS, KMS, Secrets Manager and SSM Parameter Store."""

from __future__ import annotations

from typing import Any

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props
from converters.cfn.expressions import intrinsic_name
from converters.cfn.models import Attr, Block, Raw, ResourceInfo
from converters.cfn.naming import identifier
from converters.cfn.resources.base import ResourceConverter

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
    Delete. The Terraform default is the opposite, so spell it out. Call before the identifier
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
    """MasterUserPassword as `tf_name`, with a note on where the password ends up."""
    if b.props.attr(tf_name, "MasterUserPassword"):
        b.ctx.note(
            f"{b.logical_id}: the master password ends up in the Terraform state; prefer "
            "manage_master_user_password = true (a Secrets Manager secret) or a write-only "
            f"{tf_name}_wo argument"
        )


def _master_secret(b: Builder) -> None:
    """The KMS key of MasterUserSecret as master_user_secret_kms_key_id."""
    secret = b.props.take("MasterUserSecret")
    if isinstance(secret, dict) and secret.get("KmsKeyId") is not None:
        b.props.set("master_user_secret_kms_key_id", b.ctx.expr(secret["KmsKeyId"]))


def _parameter_value(b: Builder, value: Any) -> Any:
    """Parameter values are text; CloudFormation writes booleans as "true" and "false"."""
    if isinstance(value, dict):
        return b.ctx.expr(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class DbInstance(ResourceConverter):
    """AWS::RDS::DBInstance as aws_db_instance, or as aws_rds_cluster_instance for a member
    of a cluster."""

    cfn_type = "AWS::RDS::DBInstance"
    tf_type = "aws_db_instance"
    ref = "identifier"
    attrs = {
        "Endpoint.Address": "address",
        "Endpoint.Port": "port",
        "Endpoint.HostedZoneId": "hosted_zone_id",
        "DBInstanceArn": "arn",
        "DbiResourceId": "resource_id",
        "MasterUserSecret.SecretArn": "master_user_secret[0].secret_arn",
    }
    import_id = "DBInstanceIdentifier"

    def prepare(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """A DB instance of an Aurora (or Multi-AZ) cluster is an aws_rds_cluster_instance."""
        if (resource.get("Properties") or {}).get("DBClusterIdentifier") is not None:
            info.tf_type = "aws_rds_cluster_instance"
            info.attrs.update(
                {
                    "Endpoint.Address": "endpoint",
                    "DbiResourceId": "dbi_resource_id",
                }
            )
            info.attrs.pop("Endpoint.HostedZoneId")
            info.attrs.pop("MasterUserSecret.SecretArn")

    def convert(self, b: Builder) -> None:
        """A standalone instance with the snapshot and apply settings of CloudFormation. A
        cluster member goes to convert_cluster_instance."""
        if b.info.tf_type == "aws_rds_cluster_instance":
            self.convert_cluster_instance(b)
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

    def convert_cluster_instance(self, b: Builder) -> None:
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


class DbSubnetGroup(ResourceConverter):
    """AWS::RDS::DBSubnetGroup as aws_db_subnet_group."""

    cfn_type = "AWS::RDS::DBSubnetGroup"
    tf_type = "aws_db_subnet_group"
    ref = "name"
    import_id = "DBSubnetGroupName"

    def convert(self, b: Builder) -> None:
        """The subnet group settings."""
        p = b.props
        _lowercase(b, "name", "DBSubnetGroupName")
        p.attr("description", "DBSubnetGroupDescription")
        p.list_attr("subnet_ids", "SubnetIds")
        p.tags()


class DbParameterGroup(ResourceConverter):
    """AWS::RDS::DBParameterGroup as aws_db_parameter_group."""

    cfn_type = "AWS::RDS::DBParameterGroup"
    tf_type = "aws_db_parameter_group"
    ref = "name"
    import_id = "DBParameterGroupName"

    def convert(self, b: Builder) -> None:
        """The parameter group, with one parameter block per parameter."""
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


class DbCluster(ResourceConverter):
    """AWS::RDS::DBCluster as aws_rds_cluster."""

    cfn_type = "AWS::RDS::DBCluster"
    tf_type = "aws_rds_cluster"
    ref = "cluster_identifier"
    attrs = {
        "Endpoint.Address": "endpoint",
        "Endpoint.Port": "port",
        "ReadEndpoint.Address": "reader_endpoint",
        "DBClusterArn": "arn",
        "DBClusterResourceId": "cluster_resource_id",
        "MasterUserSecret.SecretArn": "master_user_secret[0].secret_arn",
    }
    import_id = "DBClusterIdentifier"

    def convert(self, b: Builder) -> None:
        """The cluster, with the snapshot settings of CloudFormation."""
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
        p.block(
            "serverlessv2_scaling_configuration",
            "ServerlessV2ScalingConfiguration",
            self.fill_scaling_v2,
        )
        p.items.extend(snapshot)
        p.tags()

    def fill_scaling_v2(self, s: Props) -> None:
        """The serverlessv2_scaling_configuration block."""
        s.number("min_capacity", "MinCapacity")
        s.number("max_capacity", "MaxCapacity")
        s.number("seconds_until_auto_pause", "SecondsUntilAutoPause")


# --- KMS --------------------------------------------------------------------------------


class KmsKey(ResourceConverter):
    """AWS::KMS::Key as aws_kms_key."""

    cfn_type = "AWS::KMS::Key"
    tf_type = "aws_kms_key"
    ref = "key_id"
    attrs = {"Arn": "arn", "KeyId": "key_id"}

    def convert(self, b: Builder) -> None:
        """The key settings and its key policy."""
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


class KmsAlias(ResourceConverter):
    """AWS::KMS::Alias as aws_kms_alias."""

    cfn_type = "AWS::KMS::Alias"
    tf_type = "aws_kms_alias"
    ref = "name"
    import_id = "AliasName"

    def convert(self, b: Builder) -> None:
        """The alias name and its key."""
        b.props.attr("name", "AliasName")
        b.props.attr("target_key_id", "TargetKeyId")


# --- Secrets Manager --------------------------------------------------------------------


class SecretsManagerSecret(ResourceConverter):
    """AWS::SecretsManager::Secret as aws_secretsmanager_secret, with its value as a
    separate aws_secretsmanager_secret_version."""

    cfn_type = "AWS::SecretsManager::Secret"
    tf_type = "aws_secretsmanager_secret"
    ref = "arn"
    attrs = {"Id": "id"}

    def convert(self, b: Builder) -> None:
        """The secret, its replicas and its value."""
        p = b.props
        p.attr("name", "Name")
        p.attr("description", "Description")
        p.attr("kms_key_id", "KmsKeyId")
        p.block("replica", "ReplicaRegions", self.fill_replica)
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
                f"{b.logical_id}: GenerateSecretString has no aws provider equivalent; generate "
                "the value with an ephemeral random_password and secret_string_wo or convert "
                "this secret with the awscc provider"
            )

    def fill_replica(self, r: Props) -> None:
        """One replica block."""
        r.attrs({"region": "Region", "kms_key_id": "KmsKeyId"})


# --- SSM Parameter Store ----------------------------------------------------------------


class SsmParameter(ResourceConverter):
    """AWS::SSM::Parameter as aws_ssm_parameter."""

    cfn_type = "AWS::SSM::Parameter"
    tf_type = "aws_ssm_parameter"
    ref = "name"
    attrs = {"Type": "type", "Value": "value"}
    import_id = "Name"

    def convert(self, b: Builder) -> None:
        """The parameter settings."""
        p = b.props
        b.name_attr("name", "Name")
        p.attr("type", "Type")
        p.attr("value", "Value")
        p.attr("description", "Description")
        p.attr("allowed_pattern", "AllowedPattern")
        p.attr("tier", "Tier")
        p.attr("data_type", "DataType")
        p.tags()


CONVERTERS: tuple[type[ResourceConverter], ...] = (
    DbInstance,
    DbSubnetGroup,
    DbParameterGroup,
    DbCluster,
    KmsKey,
    KmsAlias,
    SecretsManagerSecret,
    SsmParameter,
)
