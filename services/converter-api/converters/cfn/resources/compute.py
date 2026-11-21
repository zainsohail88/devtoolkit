"""Lambda, EC2 instances and launch templates, and ECS."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, ClassVar

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props, snippet, tags_map
from converters.cfn.expressions import intrinsic_name, wrap_operand
from converters.cfn.models import BLANK, Attr, Block, Call, Raw
from converters.cfn.resources.base import ResourceConverter


def optional_block(kind: str, items: list[Any], when: str | None) -> Block:
    """A nested block, or (for an `!If [C, {...}, !Ref AWS::NoValue]` item) a dynamic block
    present only `when` the condition holds."""
    if when is None:
        return Block(kind, [], items)
    return Block(
        "dynamic",
        [kind],
        [Attr("for_each", Raw(f"{when} ? [1] : []")), Block("content", [], items)],
    )


def both(first: str | None, second: str | None) -> str | None:
    """Two optional conditions that must both hold."""
    if first and second:
        return f"{first} && {second}"
    return first or second


def literal_bool(value: Any) -> Any:
    """A "true" or "false" text (any case) as a boolean; anything else unchanged."""
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def snippet_text(value: Any) -> str:
    """All the text in a value, intrinsic functions included, for searching."""
    return json.dumps(value, default=str)


# --- Lambda code ------------------------------------------------------------------------


class LambdaCode:
    """The code of one Lambda function or layer: a local file or folder, an S3 object, an
    image or inline source."""

    extensions: ClassVar[tuple[tuple[str, str], ...]] = (
        ("python", ".py"),
        ("nodejs", ".js"),
        ("ruby", ".rb"),
    )
    archives: ClassVar[tuple[str, ...]] = (".zip", ".jar")

    def __init__(self, b: Builder) -> None:
        """The code of the function or layer `b` builds."""
        self.b = b

    @classmethod
    def inline_filename(cls, handler: Any, runtime: Any) -> str:
        """The file inline code must be saved as: the module of the handler plus the
        extension of the runtime ('index.handler' on nodejs20.x -> 'index.js')."""
        module = "index"
        if isinstance(handler, str) and "." in handler:
            module = handler.rsplit(".", 1)[0]
        extension = ".py"
        if isinstance(runtime, str):
            for prefix, candidate in cls.extensions:
                if runtime.startswith(prefix):
                    extension = candidate
        return module + extension

    def module_path(self, path: str) -> str:
        """A path relative to the Terraform files, as an HCL template string."""
        if path.startswith("./"):
            path = path[2:]
        return '"${path.module}/' + hcl.template_text(path) + '"'

    def use_archive(self, p: Props, archive: str) -> None:
        """Point the function at the zip file the archive_file data source `archive` makes."""
        self.b.hints["archive"] = True
        p.set("filename", Raw(f"{archive}.output_path"))
        p.set("source_code_hash", Raw(f"{archive}.output_base64sha256"))

    def archive(self, items: list[Any]) -> str:
        """An archive_file data source zipping `items`. Returns its address."""
        b = self.b
        return b.add_data(
            "archive_file",
            "_code",
            [
                Attr("type", "zip"),
                Attr("output_path", Raw(f'"${{path.module}}/build/{b.info.name}.zip"')),
            ]
            + items,
        )

    def local_code(self, p: Props, path: str) -> None:
        """Code from a local path (SAM CodeUri, CDK asset): a zip or jar file used as is, or a
        directory zipped by the archive provider."""
        location = self.module_path(path)
        if path.lower().endswith(self.archives):
            p.set("filename", Raw(location))
            p.set("source_code_hash", Raw(f"filebase64sha256({location})"))
            return
        self.use_archive(p, self.archive([Attr("source_dir", Raw(location))]))

    def convert(self, p: Props, cfn_name: str) -> None:
        """Code (functions) or Content (layers) as filename, s3_* or image_uri."""
        b = self.b
        path = b.hints.get("code_path")
        if isinstance(path, str) and path:
            # SAM and CDK point at local files; the S3 location in the template is a placeholder.
            p.drop(cfn_name)
            self.local_code(p, path)
            return
        code = p.take(cfn_name)
        if code is None:
            return
        inner = Props(b.ctx, code, f"{b.logical_id}.{cfn_name}", p.owner)
        inner.attrs(
            {
                "s3_bucket": "S3Bucket",
                "s3_key": "S3Key",
                "s3_object_version": "S3ObjectVersion",
                "source_kms_key_arn": "SourceKMSKeyArn",
            }
        )
        # Read from the template: PackageType, Handler and Runtime leave p before Code.
        original = b.resource.get("Properties") or {}
        if inner.attr("image_uri", "ImageUri") and "PackageType" not in original:
            inner.set("package_type", "Image")
        source = inner.take("ZipFile")
        if source is not None:
            if re.search(r"cfn-?response", snippet_text(source)):
                b.ctx.note(
                    f"{b.logical_id}: the inline code uses the cfn-response module, which only "
                    "CloudFormation adds to ZipFile code. Add the module to the archive or send "
                    "the response yourself."
                )
            filename = self.inline_filename(original.get("Handler"), original.get("Runtime"))
            content = [Attr("content", b.ctx.expr(source)), Attr("filename", filename)]
            self.use_archive(inner, self.archive([BLANK, Block("source", [], content)]))
        inner.finish()
        p.items.extend(inner.items)


def function_id(b: Builder, value: Any) -> str | None:
    """The logical id of the Lambda function a FunctionName value refers to, if any."""
    if not isinstance(value, dict):
        return None
    if set(value) == {"Ref"}:
        target = value["Ref"]
    elif set(value) == {"Fn::GetAtt"} and isinstance(value["Fn::GetAtt"], list):
        target = value["Fn::GetAtt"][0]
    else:
        return None
    other = b.ctx.template["Resources"].get(target)
    if isinstance(other, dict) and other.get("Type") == "AWS::Lambda::Function":
        return str(target)
    return None


def has_versions(b: Builder) -> bool:
    """Whether any AWS::Lambda::Version in the template points at this function."""
    for other in b.ctx.template["Resources"].values():
        if other.get("Type") == "AWS::Lambda::Version":
            target = (other.get("Properties") or {}).get("FunctionName")
            if function_id(b, target) == b.logical_id:
                return True
    return False


def provisioned_concurrency(b: Builder, function: Any, qualifier: Any) -> None:
    """ProvisionedConcurrencyConfig of an alias or version as
    aws_lambda_provisioned_concurrency_config."""
    when, config = b.ctx.optional_item(b.props.take("ProvisionedConcurrencyConfig"))
    config = b.props.literal("ProvisionedConcurrencyConfig", dict, value=config)
    if config is None:
        return
    b.add_resource(
        "aws_lambda_provisioned_concurrency_config",
        "_concurrency",
        [
            Attr("function_name", b.ctx.expr(function)),
            Attr("qualifier", qualifier),
            Attr(
                "provisioned_concurrent_executions",
                b.ctx.expr(config.get("ProvisionedConcurrentExecutions")),
            ),
        ],
        when=when,
    )


# --- Lambda -----------------------------------------------------------------------------


class LambdaFunction(ResourceConverter):
    """AWS::Lambda::Function as aws_lambda_function."""

    cfn_type = "AWS::Lambda::Function"
    tf_type = "aws_lambda_function"
    ref = "function_name"
    attrs = {"Arn": "arn"}
    import_id = "FunctionName"

    def convert(self, b: Builder) -> None:
        """The function, its code and its settings."""
        p = b.props
        b.name_attr("function_name", "FunctionName")
        p.attrs(
            {
                "description": "Description",
                "role": "Role",
                "handler": "Handler",
                "runtime": "Runtime",
                "package_type": "PackageType",
            }
        )
        LambdaCode(b).convert(p, "Code")
        p.number("memory_size", "MemorySize")
        p.number("timeout", "Timeout")
        p.number("reserved_concurrent_executions", "ReservedConcurrentExecutions")
        p.list_attr("architectures", "Architectures")
        p.list_attr("layers", "Layers")
        p.attr("kms_key_arn", "KmsKeyArn")
        p.attr("code_signing_config_arn", "CodeSigningConfigArn")
        if has_versions(b):
            # The AWS::Lambda::Version resources of the template read the version this
            # publishes.
            p.set("publish", True)
        p.block("environment", "Environment", self.fill_environment)
        p.block("vpc_config", "VpcConfig", self.fill_vpc_config)
        p.block("tracing_config", "TracingConfig", self.fill_tracing_config)
        p.block("dead_letter_config", "DeadLetterConfig", self.fill_dead_letter_config)
        p.block("ephemeral_storage", "EphemeralStorage", self.fill_ephemeral_storage)
        p.block("logging_config", "LoggingConfig", self.fill_logging_config)
        p.block("snap_start", "SnapStart", self.fill_snap_start)
        p.block("image_config", "ImageConfig", self.fill_image_config)
        p.block("file_system_config", "FileSystemConfigs", self.fill_file_system_config)
        p.tags()

    def fill_environment(self, e: Props) -> None:
        """The environment block."""
        e.attr("variables", "Variables")

    def fill_vpc_config(self, v: Props) -> None:
        """The vpc_config block."""
        v.list_attr("security_group_ids", "SecurityGroupIds")
        v.list_attr("subnet_ids", "SubnetIds")
        v.boolean("ipv6_allowed_for_dual_stack", "Ipv6AllowedForDualStack")

    def fill_tracing_config(self, t: Props) -> None:
        """The tracing_config block."""
        t.attr("mode", "Mode")

    def fill_dead_letter_config(self, d: Props) -> None:
        """The dead_letter_config block."""
        d.attr("target_arn", "TargetArn")

    def fill_ephemeral_storage(self, e: Props) -> None:
        """The ephemeral_storage block."""
        e.number("size", "Size")

    def fill_logging_config(self, r: Props) -> None:
        """The logging_config block."""
        # Terraform requires log_format; the CloudFormation default is Text.
        if not r.attr("log_format", "LogFormat"):
            r.set("log_format", "Text")
        r.attrs(
            {
                "application_log_level": "ApplicationLogLevel",
                "system_log_level": "SystemLogLevel",
                "log_group": "LogGroup",
            }
        )

    def fill_snap_start(self, s: Props) -> None:
        """The snap_start block."""
        s.attr("apply_on", "ApplyOn")

    def fill_image_config(self, i: Props) -> None:
        """The image_config block."""
        i.list_attr("command", "Command")
        i.list_attr("entry_point", "EntryPoint")
        i.attr("working_directory", "WorkingDirectory")

    def fill_file_system_config(self, f: Props) -> None:
        """One file_system_config block."""
        f.attrs({"arn": "Arn", "local_mount_path": "LocalMountPath"})


class LambdaPermission(ResourceConverter):
    """AWS::Lambda::Permission as aws_lambda_permission."""

    cfn_type = "AWS::Lambda::Permission"
    tf_type = "aws_lambda_permission"

    def convert(self, b: Builder) -> None:
        """Who may invoke which function."""
        b.props.attrs(
            {
                "action": "Action",
                "function_name": "FunctionName",
                "principal": "Principal",
                "source_arn": "SourceArn",
                "source_account": "SourceAccount",
                "event_source_token": "EventSourceToken",
                "function_url_auth_type": "FunctionUrlAuthType",
                "principal_org_id": "PrincipalOrgID",
            }
        )


class LambdaEventSourceMapping(ResourceConverter):
    """AWS::Lambda::EventSourceMapping as aws_lambda_event_source_mapping."""

    cfn_type = "AWS::Lambda::EventSourceMapping"
    tf_type = "aws_lambda_event_source_mapping"
    ref = "uuid"
    attrs = {"Id": "uuid", "EventSourceMappingArn": "arn"}
    numbers: ClassVar[tuple[tuple[str, str], ...]] = (
        ("batch_size", "BatchSize"),
        ("maximum_batching_window_in_seconds", "MaximumBatchingWindowInSeconds"),
        ("maximum_retry_attempts", "MaximumRetryAttempts"),
        ("maximum_record_age_in_seconds", "MaximumRecordAgeInSeconds"),
        ("parallelization_factor", "ParallelizationFactor"),
        ("tumbling_window_in_seconds", "TumblingWindowInSeconds"),
    )

    def convert(self, b: Builder) -> None:
        """The mapping between an event source and a function."""
        p = b.props
        p.attrs(
            {
                "function_name": "FunctionName",
                "event_source_arn": "EventSourceArn",
                "starting_position": "StartingPosition",
                "kms_key_arn": "KmsKeyArn",
            }
        )
        p.attr("starting_position_timestamp", "StartingPositionTimestamp", self.timestamp)
        p.boolean("enabled", "Enabled")
        p.boolean("bisect_batch_on_function_error", "BisectBatchOnFunctionError")
        for tf_name, cfn_name in self.numbers:
            p.number(tf_name, cfn_name)
        p.list_attr("function_response_types", "FunctionResponseTypes")
        p.list_attr("queues", "Queues")
        p.list_attr("topics", "Topics")
        p.block("filter_criteria", "FilterCriteria", self.fill_filter_criteria)
        p.block("scaling_config", "ScalingConfig", self.fill_scaling_config)
        p.block("destination_config", "DestinationConfig", self.fill_destination_config)
        p.block("metrics_config", "MetricsConfig", self.fill_metrics_config)
        p.tags()

    def timestamp(self, value: Any) -> Any:
        """CloudFormation takes seconds since the epoch; Terraform takes RFC 3339 text."""
        if isinstance(value, int | float) and not isinstance(value, bool):
            return Raw(f'timeadd("1970-01-01T00:00:00Z", "{int(value)}s")')
        return value

    def fill_filter_criteria(self, f: Props) -> None:
        """The filter_criteria block."""
        f.block("filter", "Filters", self.fill_filter)

    def fill_filter(self, x: Props) -> None:
        """One filter of the filter criteria."""
        x.json("pattern", "Pattern")

    def fill_scaling_config(self, s: Props) -> None:
        """The scaling_config block."""
        s.number("maximum_concurrency", "MaximumConcurrency")

    def fill_destination_config(self, d: Props) -> None:
        """The destination_config block."""
        d.block("on_failure", "OnFailure", self.fill_on_failure)

    def fill_on_failure(self, o: Props) -> None:
        """Where failed records go."""
        o.attr("destination_arn", "Destination")

    def fill_metrics_config(self, m: Props) -> None:
        """The metrics_config block."""
        m.list_attr("metrics", "Metrics")


class LambdaLayerVersion(ResourceConverter):
    """AWS::Lambda::LayerVersion as aws_lambda_layer_version."""

    cfn_type = "AWS::Lambda::LayerVersion"
    tf_type = "aws_lambda_layer_version"
    ref = "arn"
    attrs = {"LayerVersionArn": "arn"}

    def convert(self, b: Builder) -> None:
        """The layer and its content."""
        p = b.props
        b.name_attr("layer_name", "LayerName")
        p.attr("description", "Description")
        p.attr("license_info", "LicenseInfo")
        p.list_attr("compatible_runtimes", "CompatibleRuntimes")
        p.list_attr("compatible_architectures", "CompatibleArchitectures")
        LambdaCode(b).convert(p, "Content")


class LambdaUrl(ResourceConverter):
    """AWS::Lambda::Url as aws_lambda_function_url."""

    cfn_type = "AWS::Lambda::Url"
    tf_type = "aws_lambda_function_url"
    ref = "function_arn"
    attrs = {"FunctionUrl": "function_url", "FunctionArn": "function_arn"}

    def convert(self, b: Builder) -> None:
        """The function URL and its CORS settings."""
        p = b.props
        p.attrs(
            {
                "function_name": "TargetFunctionArn",
                "authorization_type": "AuthType",
                "qualifier": "Qualifier",
                "invoke_mode": "InvokeMode",
            }
        )
        p.block("cors", "Cors", self.fill_cors)

    def fill_cors(self, c: Props) -> None:
        """The cors block."""
        c.boolean("allow_credentials", "AllowCredentials")
        c.list_attr("allow_headers", "AllowHeaders")
        c.list_attr("allow_methods", "AllowMethods")
        c.list_attr("allow_origins", "AllowOrigins")
        c.list_attr("expose_headers", "ExposeHeaders")
        c.number("max_age", "MaxAge")


class LambdaAlias(ResourceConverter):
    """AWS::Lambda::Alias as aws_lambda_alias."""

    cfn_type = "AWS::Lambda::Alias"
    tf_type = "aws_lambda_alias"
    ref = "arn"
    attrs = {"AliasArn": "arn"}

    def convert(self, b: Builder) -> None:
        """The alias, its routing and its provisioned concurrency."""
        p = b.props
        function = p.peek("FunctionName")
        p.attrs(
            {
                "name": "Name",
                "description": "Description",
                "function_name": "FunctionName",
                "function_version": "FunctionVersion",
            }
        )
        p.block("routing_config", "RoutingConfig", self.fill_routing_config)
        provisioned_concurrency(b, function, b.self_ref("name"))

    def fill_routing_config(self, r: Props) -> None:
        """The routing_config block: the weight of each additional version."""
        weights: dict[Any, Any] = {}
        for entry in r.literal("AdditionalVersionWeights", list) or []:
            literal_entry = r.literal("AdditionalVersionWeights", dict, value=entry)
            if literal_entry is None:
                continue
            version = r.ctx.expr(literal_entry.get("FunctionVersion"))
            weight_key = version if isinstance(version, str) else Raw(hcl.inline(version))
            weights[weight_key] = r.ctx.expr(literal_entry.get("FunctionWeight"))
        r.set("additional_version_weights", weights)


class LambdaVersion(ResourceConverter):
    """AWS::Lambda::Version as terraform_data. Terraform has no version resource: a function
    with `publish = true` publishes a new version on every code change. A terraform_data
    records that version so Ref and Fn::GetAtt on the CloudFormation version keep working."""

    cfn_type = "AWS::Lambda::Version"
    tf_type = "terraform_data"
    ref = "output.arn"
    attrs = {"Version": "output.version", "FunctionArn": "output.arn"}

    def convert(self, b: Builder) -> None:
        """The version the function publishes, and its provisioned concurrency."""
        p = b.props
        function = p.take("FunctionName")
        target = function_id(b, function)
        p.drop("Description")
        p.drop("CodeSha256")
        if target is not None:
            info = b.ctx.resources[target]
            value: dict[str, Any] = {
                "arn": Raw(info.expression("qualified_arn")),
                "version": Raw(info.expression("version")),
            }
            b.ctx.note(
                f"{b.logical_id} (AWS::Lambda::Version): Terraform publishes versions from the "
                f"function itself. The converter sets publish = true on {info.address}. "
                f"{b.info.address} records its latest version."
            )
        else:
            value = {"arn": b.ctx.expr(function), "version": "$LATEST"}
            b.ctx.note(
                f"{b.logical_id} (AWS::Lambda::Version) belongs to a function outside this "
                "template. Set publish = true on that function and use its version."
            )
        p.set("input", value)
        provisioned_concurrency(b, function, Raw(b.info.expression("output.version")))


# --- EC2 instances and launch templates -------------------------------------------------


def ebs_items(r: Props, with_snapshot: bool) -> None:
    """The settings of an EBS volume."""
    r.attrs(
        {
            "volume_size": "VolumeSize",
            "volume_type": "VolumeType",
            "iops": "Iops",
            "throughput": "Throughput",
            "kms_key_id": "KmsKeyId",
        }
    )
    if with_snapshot:
        r.attr("snapshot_id", "SnapshotId")
    r.boolean("encrypted", "Encrypted")
    r.boolean("delete_on_termination", "DeleteOnTermination")


class Ec2Instance(ResourceConverter):
    """AWS::EC2::Instance as aws_instance."""

    cfn_type = "AWS::EC2::Instance"
    tf_type = "aws_instance"
    attrs = {
        "InstanceId": "id",
        "PublicIp": "public_ip",
        "PrivateIp": "private_ip",
        "PublicDnsName": "public_dns",
        "PrivateDnsName": "private_dns",
        "AvailabilityZone": "availability_zone",
    }
    root_devices: ClassVar[tuple[str, ...]] = ("/dev/xvda", "/dev/sda1", "/dev/sda")
    booleans: ClassVar[tuple[tuple[str, str], ...]] = (
        ("monitoring", "Monitoring"),
        ("ebs_optimized", "EbsOptimized"),
        ("disable_api_termination", "DisableApiTermination"),
        ("source_dest_check", "SourceDestCheck"),
    )

    def convert(self, b: Builder) -> None:
        """The instance, its network interface, user data and block devices."""
        p = b.props
        p.attrs(
            {
                "ami": "ImageId",
                "instance_type": "InstanceType",
                "key_name": "KeyName",
                "subnet_id": "SubnetId",
                "availability_zone": "AvailabilityZone",
                "iam_instance_profile": "IamInstanceProfile",
                "private_ip": "PrivateIpAddress",
                "tenancy": "Tenancy",
                "placement_group": "PlacementGroupName",
                "instance_initiated_shutdown_behavior": "InstanceInitiatedShutdownBehavior",
                "host_id": "HostId",
            }
        )
        p.list_attr("vpc_security_group_ids", "SecurityGroupIds")
        p.list_attr("security_groups", "SecurityGroups")
        p.number("ipv6_address_count", "Ipv6AddressCount")
        for tf_name, cfn_name in self.booleans:
            p.boolean(tf_name, cfn_name)
        self.primary_interface(b, p)
        user_data = p.take("UserData")
        if user_data is not None:
            # CloudFormation wants UserData base64-encoded, nearly always with Fn::Base64.
            if isinstance(user_data, dict) and intrinsic_name(user_data) == "Fn::Base64":
                p.set("user_data", b.ctx.expr(user_data["Fn::Base64"]))
            else:
                p.set("user_data_base64", b.ctx.expr(user_data))
        p.block("credit_specification", "CreditSpecification", self.fill_credit_specification)
        p.block("launch_template", "LaunchTemplate", self.fill_launch_template)
        self.devices(b, p)
        p.tags()

    def fill_credit_specification(self, c: Props) -> None:
        """The credit_specification block."""
        c.attr("cpu_credits", "CPUCredits")

    def fill_launch_template(self, t: Props) -> None:
        """The launch_template block."""
        t.attrs({"id": "LaunchTemplateId", "name": "LaunchTemplateName", "version": "Version"})

    def devices(self, b: Builder, p: Props) -> None:
        """BlockDeviceMappings as root, EBS and instance-store (ephemeral) device blocks."""
        for item in p.literal("BlockDeviceMappings", list) or []:
            when, mapping = b.ctx.optional_item(item)
            mapping = p.literal("BlockDeviceMappings", dict, value=mapping)
            if mapping is None:
                continue
            where = f"{b.logical_id}.BlockDeviceMappings"
            inner = Props(b.ctx, mapping, where, p.owner)
            device = inner.take("DeviceName")
            ebs = inner.take("Ebs")
            virtual = inner.take("VirtualName")
            no_device = inner.take("NoDevice")
            if virtual is not None or no_device is not None:
                kind = "ephemeral_block_device"
                inner.set("device_name", b.ctx.expr(device))
                if virtual is not None:
                    inner.set("virtual_name", b.ctx.expr(virtual))
                else:
                    inner.set("no_device", True)
            else:
                disk = Props(b.ctx, ebs, f"{where}.Ebs", p.owner)
                if device in self.root_devices:
                    kind = "root_block_device"
                    ebs_items(disk, with_snapshot=False)
                    b.ctx.note(
                        f"{b.logical_id}: the converter treats block device '{device}' as "
                        "root_block_device. Check that it is the root device of the AMI."
                    )
                else:
                    kind = "ebs_block_device"
                    disk.set("device_name", b.ctx.expr(device))
                    ebs_items(disk, with_snapshot=True)
                disk.finish()
                inner.items.extend(disk.items)
            inner.finish()
            p.items.append(optional_block(kind, inner.items, when))

    def primary_interface(self, b: Builder, p: Props) -> None:
        """A single NetworkInterfaces entry for device 0 only says where the own subnet,
        groups and public IP of the instance are. Anything more complex stays a note."""
        interfaces = p.peek("NetworkInterfaces")
        if not (isinstance(interfaces, list) and len(interfaces) == 1):
            return
        interface = interfaces[0]
        if not isinstance(interface, dict) or str(interface.get("DeviceIndex", "0")) != "0":
            return
        if interface.get("NetworkInterfaceId"):
            return
        p.drop("NetworkInterfaces")
        inner = Props(b.ctx, interface, f"{b.logical_id}.NetworkInterfaces[0]", p.owner)
        inner.drop("DeviceIndex")
        keep = inner.take("DeleteOnTermination")
        if keep is not None and not (keep is True or str(keep).lower() == "true"):
            b.ctx.note(
                f"{b.logical_id}: the primary network interface has DeleteOnTermination "
                f"{snippet(keep)}, but Terraform always deletes the interface it creates with the "
                "instance. To keep the interface, create an aws_network_interface and attach it."
            )
            inner.comment(
                f"DeleteOnTermination was left out. CloudFormation value: {snippet(keep)}"
            )
        inner.boolean("associate_public_ip_address", "AssociatePublicIpAddress")
        inner.attr("subnet_id", "SubnetId")
        inner.list_attr("vpc_security_group_ids", "GroupSet")
        inner.attr("private_ip", "PrivateIpAddress")
        inner.number("ipv6_address_count", "Ipv6AddressCount")
        inner.finish()
        p.items.extend(inner.items)


class Ec2LaunchTemplate(ResourceConverter):
    """AWS::EC2::LaunchTemplate as aws_launch_template."""

    cfn_type = "AWS::EC2::LaunchTemplate"
    tf_type = "aws_launch_template"
    attrs = {
        "LaunchTemplateId": "id",
        "LatestVersionNumber": "latest_version",
        "DefaultVersionNumber": "default_version",
    }

    def convert(self, b: Builder) -> None:
        """The launch template, its own tags and its LaunchTemplateData."""
        p = b.props
        p.attr("name", "LaunchTemplateName")
        p.attr("description", "VersionDescription")
        own_tags: list[Any] = []
        for spec in p.literal("TagSpecifications", list) or []:
            # The own tags of the launch template (ResourceType: launch-template).
            literal_spec = p.literal("TagSpecifications", dict, value=spec)
            if literal_spec is not None:
                tags = p.literal("TagSpecifications.Tags", list, value=literal_spec.get("Tags"))
                own_tags.extend(tags or [])
        if own_tags:
            p.set("tags", tags_map(b.ctx, own_tags))
        data = p.take("LaunchTemplateData")
        if data is None:
            return
        d = Props(b.ctx, data, f"{b.logical_id}.LaunchTemplateData", p.owner)
        self.fill_data(d)
        d.finish()
        p.items.extend(d.items)

    def fill_data(self, d: Props) -> None:
        """The arguments LaunchTemplateData holds."""
        d.attrs(
            {
                "image_id": "ImageId",
                "instance_type": "InstanceType",
                "key_name": "KeyName",
                "instance_initiated_shutdown_behavior": "InstanceInitiatedShutdownBehavior",
                "kernel_id": "KernelId",
                "ram_disk_id": "RamDiskId",
                # Launch templates take UserData base64-encoded, as CloudFormation does.
                "user_data": "UserData",
            }
        )
        d.list_attr("vpc_security_group_ids", "SecurityGroupIds")
        d.list_attr("security_group_names", "SecurityGroups")
        d.boolean("ebs_optimized", "EbsOptimized")
        d.boolean("disable_api_termination", "DisableApiTermination")
        d.boolean("disable_api_stop", "DisableApiStop")
        d.block("iam_instance_profile", "IamInstanceProfile", self.fill_iam_instance_profile)
        d.block("block_device_mappings", "BlockDeviceMappings", self.fill_device)
        d.block("metadata_options", "MetadataOptions", self.fill_metadata_options)
        d.block("monitoring", "Monitoring", self.fill_monitoring)
        d.block("credit_specification", "CreditSpecification", self.fill_credit_specification)
        d.block("placement", "Placement", self.fill_placement)
        d.block("network_interfaces", "NetworkInterfaces", self.fill_interface)
        d.block("tag_specifications", "TagSpecifications", self.fill_tag_specification)

    def fill_iam_instance_profile(self, i: Props) -> None:
        """The iam_instance_profile block."""
        i.attrs({"arn": "Arn", "name": "Name"})

    def fill_device(self, r: Props) -> None:
        """One block_device_mappings block."""
        r.attrs({"device_name": "DeviceName", "virtual_name": "VirtualName"})
        # CloudFormation omits the device whenever NoDevice is present, usually as "". The
        # provider ignores an empty no_device, so any value becomes a non-empty one.
        no_device = r.take("NoDevice")
        if no_device is not None:
            r.set("no_device", self.no_device(r, no_device))
        r.block("ebs", "Ebs", self.fill_ebs)

    def no_device(self, r: Props, value: Any) -> Any:
        """NoDevice as a value the provider acts on: never empty."""
        if isinstance(value, str):
            return value or "true"
        return Raw(f'coalesce({hcl.inline(r.ctx.expr(value))}, "true")')

    def fill_ebs(self, e: Props) -> None:
        """The ebs block of a device."""
        ebs_items(e, with_snapshot=True)

    def fill_metadata_options(self, m: Props) -> None:
        """The metadata_options block."""
        m.attrs(
            {
                "http_tokens": "HttpTokens",
                "http_endpoint": "HttpEndpoint",
                "instance_metadata_tags": "InstanceMetadataTags",
                "http_protocol_ipv6": "HttpProtocolIpv6",
            }
        )
        m.number("http_put_response_hop_limit", "HttpPutResponseHopLimit")

    def fill_monitoring(self, m: Props) -> None:
        """The monitoring block."""
        m.boolean("enabled", "Enabled")

    def fill_credit_specification(self, c: Props) -> None:
        """The credit_specification block."""
        c.attr("cpu_credits", "CpuCredits")

    def fill_placement(self, c: Props) -> None:
        """The placement block."""
        c.attrs(
            {
                "availability_zone": "AvailabilityZone",
                "group_name": "GroupName",
                "tenancy": "Tenancy",
            }
        )

    def fill_interface(self, r: Props) -> None:
        """One network_interfaces block."""
        r.boolean("associate_public_ip_address", "AssociatePublicIpAddress")
        r.boolean("delete_on_termination", "DeleteOnTermination")
        r.number("device_index", "DeviceIndex")
        r.attrs(
            {
                "subnet_id": "SubnetId",
                "description": "Description",
                "private_ip_address": "PrivateIpAddress",
                "network_interface_id": "NetworkInterfaceId",
                "interface_type": "InterfaceType",
            }
        )
        r.list_attr("security_groups", "Groups")

    def fill_tag_specification(self, t: Props) -> None:
        """One tag_specifications block."""
        t.attr("resource_type", "ResourceType")
        t.tags()


# --- ECS --------------------------------------------------------------------------------


def fill_strategy(r: Props) -> None:
    """One capacity provider strategy item, of a cluster or a service."""
    r.attr("capacity_provider", "CapacityProvider")
    r.number("weight", "Weight")
    r.number("base", "Base")


def fill_placement_constraint(c: Props) -> None:
    """One placement_constraints block, of a task definition or a service."""
    c.attrs({"type": "Type", "expression": "Expression"})


class ServiceConnectDefaults:
    """Fills the service_connect_defaults block of one cluster."""

    def __init__(self, b: Builder) -> None:
        """The defaults of the cluster `b` builds."""
        self.b = b

    def fill(self, s: Props) -> None:
        """The service_connect_defaults block."""
        s.attr("namespace", "Namespace", self.namespace)

    def namespace(self, value: Any) -> Any:
        """The namespace, which Terraform takes as an ARN."""
        b = self.b
        if isinstance(value, str) and not value.startswith("arn:"):
            b.ctx.note(
                f"{b.logical_id}: ServiceConnectDefaults.Namespace '{value}' is a namespace name, "
                "which CloudFormation looks up or creates. Terraform needs the namespace ARN, "
                "for example from an aws_service_discovery_http_namespace."
            )
        return b.ctx.expr(value)


class EcsCluster(ResourceConverter):
    """AWS::ECS::Cluster as aws_ecs_cluster, with its capacity providers as a separate
    resource."""

    cfn_type = "AWS::ECS::Cluster"
    tf_type = "aws_ecs_cluster"
    ref = "name"
    attrs = {"Arn": "arn"}
    import_id = "ClusterName"

    def convert(self, b: Builder) -> None:
        """The cluster, then its capacity providers."""
        p = b.props
        b.name_attr("name", "ClusterName")
        p.block("setting", "ClusterSettings", self.fill_setting)
        p.block(
            "service_connect_defaults", "ServiceConnectDefaults", ServiceConnectDefaults(b).fill
        )
        p.block("configuration", "Configuration", self.fill_configuration)
        p.tags()
        providers = p.take("CapacityProviders")
        strategy = p.take("DefaultCapacityProviderStrategy")
        if providers is None and strategy is None:
            return
        # Capacity providers are a separate resource in Terraform.
        holder = Props(b.ctx, {}, b.logical_id, p.owner)
        holder.set("cluster_name", b.self_ref("name"))
        if providers is not None:
            holder.set("capacity_providers", b.ctx.expr(providers))
        holder.add_blocks(
            "default_capacity_provider_strategy",
            strategy or [],
            fill_strategy,
            f"{b.logical_id}.DefaultCapacityProviderStrategy",
        )
        b.add_resource("aws_ecs_cluster_capacity_providers", "_capacity_providers", holder.items)

    def fill_setting(self, s: Props) -> None:
        """One setting block."""
        s.attrs({"name": "Name", "value": "Value"})

    def fill_configuration(self, r: Props) -> None:
        """The configuration block."""
        r.block(
            "execute_command_configuration",
            "ExecuteCommandConfiguration",
            self.fill_execute_command,
        )
        r.block(
            "managed_storage_configuration",
            "ManagedStorageConfiguration",
            self.fill_managed_storage,
        )

    def fill_execute_command(self, e: Props) -> None:
        """The execute_command_configuration block."""
        e.attrs({"kms_key_id": "KmsKeyId", "logging": "Logging"})
        e.block("log_configuration", "LogConfiguration", self.fill_exec_log_configuration)

    def fill_exec_log_configuration(self, r: Props) -> None:
        """Where the execute command logs go."""
        r.boolean("cloud_watch_encryption_enabled", "CloudWatchEncryptionEnabled")
        r.attr("cloud_watch_log_group_name", "CloudWatchLogGroupName")
        r.boolean("s3_bucket_encryption_enabled", "S3EncryptionEnabled")
        r.attr("s3_bucket_name", "S3BucketName")
        r.attr("s3_key_prefix", "S3KeyPrefix")

    def fill_managed_storage(self, m: Props) -> None:
        """The managed_storage_configuration block."""
        m.attrs(
            {
                "kms_key_id": "KmsKeyId",
                "fargate_ephemeral_storage_kms_key_id": "FargateEphemeralStorageKmsKeyId",
            }
        )


def if_branches(value: Any, convert: Callable[[Any], Any]) -> Any:
    """`convert(value)`, or for `!If [C, a, b]` the same with a and b converted. Other
    intrinsic functions are left as written."""
    if isinstance(value, dict) and intrinsic_name(value) is not None:
        args = value[intrinsic_name(value)]
        if intrinsic_name(value) == "Fn::If" and isinstance(args, list) and len(args) == 3:
            name, yes, no = args
            return {"Fn::If": [name, if_branches(yes, convert), if_branches(no, convert)]}
        return value
    return convert(value)


def string_value(value: Any) -> Any:
    """A literal as the text CloudFormation would pass: true -> "true", 8080 -> "8080"."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return value


class ContainerField:
    """Turns the text value of one container definition field into the number or boolean
    the field takes."""

    numbers: ClassVar[frozenset[str]] = frozenset(
        {
            "cpu",
            "memory",
            "memoryReservation",
            "containerPort",
            "hostPort",
            "startTimeout",
            "stopTimeout",
            "softLimit",
            "hardLimit",
            "interval",
            "timeout",
            "retries",
            "startPeriod",
            "sharedMemorySize",
            "maxSwap",
            "swappiness",
            "size",
        }
    )
    booleans: ClassVar[frozenset[str]] = frozenset(
        {
            "essential",
            "readOnly",
            "privileged",
            "readonlyRootFilesystem",
            "disableNetworking",
            "interactive",
            "pseudoTerminal",
            "initProcessEnabled",
        }
    )
    digits: ClassVar[re.Pattern[str]] = re.compile(r"^[0-9]+$")

    def __init__(self, camel: str) -> None:
        """The field named `camel` ("containerPort")."""
        self.camel = camel

    def typed(self, item: Any) -> Any:
        """`item`, with digits as a number and "true"/"false" as a boolean where the field
        takes one."""
        if self.camel in self.numbers and isinstance(item, str) and self.digits.match(item):
            return int(item)
        is_text = isinstance(item, str)
        if self.camel in self.booleans and is_text and item.lower() in ("true", "false"):
            return item.lower() == "true"
        return item


class ContainerDefinitions:
    """CloudFormation container definitions (PascalCase) as the JSON the ECS API takes
    (camelCase). Intrinsic functions and user maps are left as written, but both branches of
    an Fn::If are converted too. Numbers and booleans written as strings become real ones,
    as ECS rejects "256" for cpu, and the string fields CloudFormation coerces (environment
    values, labels, log options) become strings, as ECS rejects 8080 for them."""

    # Container definition keys holding user maps: their own keys stay as written.
    maps: ClassVar[frozenset[str]] = frozenset({"DockerLabels", "Options", "DriverOpts", "Labels"})

    def convert(self, value: Any) -> Any:
        """The ECS API form of `value`, a container definition or part of one."""
        if isinstance(value, list):
            return [self.convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        if intrinsic_name(value) is not None:
            return if_branches(value, self.convert)
        result: dict[str, Any] = {}
        for name, item in value.items():
            key = str(name)
            camel = key[:1].lower() + key[1:]
            if key in self.maps:
                result[camel] = if_branches(item, self.string_values)
                continue
            converted = self.convert(item)
            if camel == "environment":
                converted = if_branches(converted, self.environment)
            result[camel] = if_branches(converted, ContainerField(camel).typed)
        return result

    def string_values(self, value: Any) -> Any:
        """A user map with every value as text."""
        if not isinstance(value, dict):
            return value
        return {k: if_branches(v, string_value) for k, v in value.items()}

    def environment(self, value: Any) -> Any:
        """The environment list with every value as text."""
        if not isinstance(value, list):
            return value
        return [if_branches(entry, self.environment_entry) for entry in value]

    def environment_entry(self, entry: Any) -> Any:
        """One environment entry with its value as text."""
        if not isinstance(entry, dict) or "value" not in entry:
            return entry
        return dict(entry, value=if_branches(entry["value"], string_value))


class EcsTaskDefinition(ResourceConverter):
    """AWS::ECS::TaskDefinition as aws_ecs_task_definition."""

    cfn_type = "AWS::ECS::TaskDefinition"
    tf_type = "aws_ecs_task_definition"
    ref = "arn"
    attrs = {"TaskDefinitionArn": "arn"}

    def convert(self, b: Builder) -> None:
        """The task definition, its containers and volumes."""
        p = b.props
        b.name_attr("family", "Family")
        definitions = p.take("ContainerDefinitions")
        if definitions is not None:
            container_json = ContainerDefinitions().convert(definitions)
            p.set("container_definitions", Call("jsonencode", b.ctx.expr(container_json)))
        p.attrs(
            {
                "cpu": "Cpu",
                "memory": "Memory",
                "network_mode": "NetworkMode",
                "execution_role_arn": "ExecutionRoleArn",
                "task_role_arn": "TaskRoleArn",
                "pid_mode": "PidMode",
                "ipc_mode": "IpcMode",
            }
        )
        p.list_attr("requires_compatibilities", "RequiresCompatibilities")
        p.boolean("enable_fault_injection", "EnableFaultInjection")
        p.block("runtime_platform", "RuntimePlatform", self.fill_runtime_platform)
        p.block("ephemeral_storage", "EphemeralStorage", self.fill_ephemeral_storage)
        p.block("placement_constraints", "PlacementConstraints", fill_placement_constraint)
        p.block("volume", "Volumes", self.fill_volume)
        p.tags()

    def fill_runtime_platform(self, r: Props) -> None:
        """The runtime_platform block."""
        r.attrs(
            {
                "cpu_architecture": "CpuArchitecture",
                "operating_system_family": "OperatingSystemFamily",
            }
        )

    def fill_ephemeral_storage(self, e: Props) -> None:
        """The ephemeral_storage block."""
        e.number("size_in_gib", "SizeInGiB")

    def fill_volume(self, r: Props) -> None:
        """One volume block."""
        r.attr("name", "Name")
        r.boolean("configure_at_launch", "ConfigureAtLaunch")
        when, host = r.ctx.optional_item(r.take("Host"))
        host = r.literal("Host", dict, value=host)
        if host is not None and host.get("SourcePath") is not None:
            path = r.ctx.expr(host["SourcePath"])
            if when is not None:
                path = Raw(f"{when} ? {wrap_operand(hcl.inline(path))} : null")
            r.set("host_path", path)
        r.block("efs_volume_configuration", "EFSVolumeConfiguration", self.fill_efs_volume)
        r.block(
            "docker_volume_configuration",
            "DockerVolumeConfiguration",
            self.fill_docker_volume,
        )

    def fill_efs_volume(self, e: Props) -> None:
        """The efs_volume_configuration block."""
        e.attrs(
            {
                "file_system_id": "FilesystemId",
                "root_directory": "RootDirectory",
                "transit_encryption": "TransitEncryption",
            }
        )
        e.number("transit_encryption_port", "TransitEncryptionPort")
        e.block("authorization_config", "AuthorizationConfig", self.fill_efs_authorization)

    def fill_efs_authorization(self, a: Props) -> None:
        """The authorization_config block of an EFS volume."""
        a.attrs({"access_point_id": "AccessPointId", "iam": "IAM"})

    def fill_docker_volume(self, d: Props) -> None:
        """The docker_volume_configuration block."""
        d.boolean("autoprovision", "Autoprovision")
        d.attrs(
            {
                "driver": "Driver",
                "scope": "Scope",
                "driver_opts": "DriverOpts",
                "labels": "Labels",
            }
        )


class EcsService(ResourceConverter):
    """AWS::ECS::Service as aws_ecs_service."""

    cfn_type = "AWS::ECS::Service"
    tf_type = "aws_ecs_service"
    attrs = {"Name": "name", "ServiceArn": "arn"}

    def convert(self, b: Builder) -> None:
        """The service, its deployment, network and load balancer settings."""
        p = b.props
        b.name_attr("name", "ServiceName")
        p.attrs(
            {
                "cluster": "Cluster",
                "task_definition": "TaskDefinition",
                "launch_type": "LaunchType",
                "platform_version": "PlatformVersion",
                "scheduling_strategy": "SchedulingStrategy",
                "propagate_tags": "PropagateTags",
                "iam_role": "Role",
            }
        )
        if not p.number("desired_count", "DesiredCount"):
            self.default_desired_count(b, p)
        p.number("health_check_grace_period_seconds", "HealthCheckGracePeriodSeconds")
        p.boolean("enable_execute_command", "EnableExecuteCommand")
        p.boolean("enable_ecs_managed_tags", "EnableECSManagedTags")
        self.deployment(b, p)
        network = p.take("NetworkConfiguration")
        if network is not None:
            inner = Props(b.ctx, network, f"{b.logical_id}.NetworkConfiguration", p.owner)
            inner.block("network_configuration", "AwsvpcConfiguration", self.fill_awsvpc)
            inner.finish()
            p.items.extend(inner.items)
        p.block("load_balancer", "LoadBalancers", self.fill_load_balancer)
        p.block("deployment_controller", "DeploymentController", self.fill_deployment_controller)
        p.block("capacity_provider_strategy", "CapacityProviderStrategy", fill_strategy)
        p.block("placement_constraints", "PlacementConstraints", fill_placement_constraint)
        p.block("ordered_placement_strategy", "PlacementStrategies", self.fill_placement_strategy)
        p.block("service_registries", "ServiceRegistries", self.fill_service_registry)
        p.tags()

    def default_desired_count(self, b: Builder, p: Props) -> None:
        """CloudFormation starts one task when DesiredCount is left out (except for DAEMON
        services, which take none); the Terraform default is 0."""
        strategy = (b.resource.get("Properties") or {}).get("SchedulingStrategy")
        if strategy is None or b.ctx.is_no_value(strategy):
            p.set("desired_count", 1)
        elif isinstance(strategy, str):
            if strategy.upper() != "DAEMON":
                p.set("desired_count", 1)
        else:
            strategy_text = wrap_operand(hcl.inline(b.ctx.expr(strategy)))
            p.set("desired_count", Raw(f'{strategy_text} == "DAEMON" ? null : 1'))

    def deployment(self, b: Builder, p: Props) -> None:
        """DeploymentConfiguration, whose parts are arguments and blocks of the service in
        Terraform."""
        deployment = p.take("DeploymentConfiguration")
        if deployment is None:
            return
        inner = Props(b.ctx, deployment, f"{b.logical_id}.DeploymentConfiguration", p.owner)
        inner.number("deployment_maximum_percent", "MaximumPercent")
        inner.number("deployment_minimum_healthy_percent", "MinimumHealthyPercent")
        inner.block(
            "deployment_circuit_breaker", "DeploymentCircuitBreaker", self.fill_circuit_breaker
        )
        inner.block("alarms", "Alarms", self.fill_alarms)
        inner.finish()
        p.items.extend(inner.items)

    def fill_circuit_breaker(self, c: Props) -> None:
        """The deployment_circuit_breaker block."""
        c.boolean("enable", "Enable")
        c.boolean("rollback", "Rollback")

    def fill_alarms(self, a: Props) -> None:
        """The alarms block."""
        a.list_attr("alarm_names", "AlarmNames")
        a.boolean("enable", "Enable")
        a.boolean("rollback", "Rollback")

    def fill_awsvpc(self, r: Props) -> None:
        """The network_configuration block."""
        r.list_attr("subnets", "Subnets")
        r.list_attr("security_groups", "SecurityGroups")
        value = r.take("AssignPublicIp")
        if value is None:
            return
        if isinstance(value, str) and value.upper() in ("ENABLED", "DISABLED"):
            r.set("assign_public_ip", value.upper() == "ENABLED")
        else:
            operand = wrap_operand(hcl.inline(r.ctx.expr(value)))
            r.set("assign_public_ip", Raw(f'{operand} == "ENABLED"'))

    def fill_load_balancer(self, lb: Props) -> None:
        """One load_balancer block."""
        lb.attrs(
            {
                "container_name": "ContainerName",
                "target_group_arn": "TargetGroupArn",
                "elb_name": "LoadBalancerName",
            }
        )
        lb.number("container_port", "ContainerPort")

    def fill_deployment_controller(self, d: Props) -> None:
        """The deployment_controller block."""
        d.attr("type", "Type")

    def fill_placement_strategy(self, s: Props) -> None:
        """One ordered_placement_strategy block."""
        s.attrs({"type": "Type", "field": "Field"})

    def fill_service_registry(self, s: Props) -> None:
        """The service_registries block."""
        s.attrs({"registry_arn": "RegistryArn", "container_name": "ContainerName"})
        s.number("port", "Port")
        s.number("container_port", "ContainerPort")


CONVERTERS: tuple[type[ResourceConverter], ...] = (
    LambdaFunction,
    LambdaPermission,
    LambdaEventSourceMapping,
    LambdaLayerVersion,
    LambdaUrl,
    LambdaAlias,
    LambdaVersion,
    Ec2Instance,
    Ec2LaunchTemplate,
    EcsCluster,
    EcsTaskDefinition,
    EcsService,
)
