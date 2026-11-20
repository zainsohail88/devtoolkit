"""Lambda, EC2 instances and launch templates, and ECS."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props, snippet, tags_map
from converters.cfn.context import _wrap, intrinsic_name
from converters.cfn.hcl import Attr, Block, Raw
from converters.cfn.resources import resource


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


# --- Lambda code ------------------------------------------------------------------------

_EXTENSIONS = (("python", ".py"), ("nodejs", ".js"), ("ruby", ".rb"))
_ARCHIVES = (".zip", ".jar")


def _inline_filename(handler: Any, runtime: Any) -> str:
    """The file inline code must be saved as: the handler's module plus the runtime's
    extension ('index.handler' on nodejs20.x -> 'index.js')."""
    module = "index"
    if isinstance(handler, str) and "." in handler:
        module = handler.rsplit(".", 1)[0]
    extension = ".py"
    if isinstance(runtime, str):
        for prefix, candidate in _EXTENSIONS:
            if runtime.startswith(prefix):
                extension = candidate
    return module + extension


def _module_path(path: str) -> str:
    """A path relative to the Terraform files, as an HCL template string."""
    if path.startswith("./"):
        path = path[2:]
    return '"${path.module}/' + hcl.template_text(path) + '"'


def _use_archive(b: Builder, p: Props, archive: str) -> None:
    b.hints["archive"] = True
    p.set("filename", Raw(f"{archive}.output_path"))
    p.set("source_code_hash", Raw(f"{archive}.output_base64sha256"))


def _archive(b: Builder, items: list[Any]) -> str:
    return b.add_data(
        "archive_file",
        "_code",
        [
            Attr("type", "zip"),
            Attr("output_path", Raw(f'"${{path.module}}/build/{b.info.name}.zip"')),
            *items,
        ],
    )


def _local_code(b: Builder, p: Props, path: str) -> None:
    """Code from a local path (SAM CodeUri, CDK asset): a zip or jar file used as is, or a
    directory zipped by the archive provider."""
    location = _module_path(path)
    if path.lower().endswith(_ARCHIVES):
        p.set("filename", Raw(location))
        p.set("source_code_hash", Raw(f"filebase64sha256({location})"))
        return
    _use_archive(b, p, _archive(b, [Attr("source_dir", Raw(location))]))


def _code(b: Builder, p: Props, cfn_name: str) -> None:
    """Code (functions) or Content (layers) as filename, s3_* or image_uri."""
    path = b.hints.get("code_path")
    if isinstance(path, str) and path:
        # SAM and CDK point at local files; the S3 location in the template is a placeholder.
        p.drop(cfn_name)
        _local_code(b, p, path)
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
                f"{b.logical_id}: the inline code uses the cfn-response module, which "
                "CloudFormation adds to ZipFile code; Terraform does not, so add the module "
                "to the archive (or send the response yourself)"
            )
        filename = _inline_filename(original.get("Handler"), original.get("Runtime"))
        content = [Attr("content", b.ctx.expr(source)), Attr("filename", filename)]
        _use_archive(b, inner, _archive(b, [hcl.BLANK, Block("source", [], content)]))
    inner.finish()
    p.items.extend(inner.items)


def snippet_text(value: Any) -> str:
    """All the text in a value, intrinsic functions included, for searching."""
    return json.dumps(value, default=str)


def _function_id(b: Builder, value: Any) -> str | None:
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


def _has_versions(b: Builder) -> bool:
    """Whether any AWS::Lambda::Version in the template points at this function."""
    for other in b.ctx.template["Resources"].values():
        if other.get("Type") == "AWS::Lambda::Version":
            target = (other.get("Properties") or {}).get("FunctionName")
            if _function_id(b, target) == b.logical_id:
                return True
    return False


# --- Lambda -----------------------------------------------------------------------------


@resource(
    "AWS::Lambda::Function",
    "aws_lambda_function",
    ref="function_name",
    attrs={"Arn": "arn"},
    import_id="FunctionName",
)
def lambda_function(b: Builder) -> None:
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
    _code(b, p, "Code")
    p.number("memory_size", "MemorySize")
    p.number("timeout", "Timeout")
    p.number("reserved_concurrent_executions", "ReservedConcurrentExecutions")
    p.list_attr("architectures", "Architectures")
    p.list_attr("layers", "Layers")
    p.attr("kms_key_arn", "KmsKeyArn")
    p.attr("code_signing_config_arn", "CodeSigningConfigArn")
    if _has_versions(b):
        # The template's AWS::Lambda::Version resources read the version this publishes.
        p.set("publish", True)
    p.block("environment", "Environment", lambda e: e.attr("variables", "Variables"))
    p.block(
        "vpc_config",
        "VpcConfig",
        lambda v: (
            v.list_attr("security_group_ids", "SecurityGroupIds"),
            v.list_attr("subnet_ids", "SubnetIds"),
            v.boolean("ipv6_allowed_for_dual_stack", "Ipv6AllowedForDualStack"),
        ),
    )
    p.block("tracing_config", "TracingConfig", lambda t: t.attr("mode", "Mode"))
    p.block("dead_letter_config", "DeadLetterConfig", lambda d: d.attr("target_arn", "TargetArn"))
    p.block("ephemeral_storage", "EphemeralStorage", lambda e: e.number("size", "Size"))
    p.block("logging_config", "LoggingConfig", _logging_config)
    p.block("snap_start", "SnapStart", lambda s: s.attr("apply_on", "ApplyOn"))
    p.block(
        "image_config",
        "ImageConfig",
        lambda i: (
            i.list_attr("command", "Command"),
            i.list_attr("entry_point", "EntryPoint"),
            i.attr("working_directory", "WorkingDirectory"),
        ),
    )
    p.block(
        "file_system_config",
        "FileSystemConfigs",
        lambda f: f.attrs({"arn": "Arn", "local_mount_path": "LocalMountPath"}),
    )
    p.tags()


def _logging_config(r: Props) -> None:
    # Terraform requires log_format; CloudFormation's default is Text.
    if not r.attr("log_format", "LogFormat"):
        r.set("log_format", "Text")
    r.attrs(
        {
            "application_log_level": "ApplicationLogLevel",
            "system_log_level": "SystemLogLevel",
            "log_group": "LogGroup",
        }
    )


@resource("AWS::Lambda::Permission", "aws_lambda_permission")
def lambda_permission(b: Builder) -> None:
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


@resource(
    "AWS::Lambda::EventSourceMapping",
    "aws_lambda_event_source_mapping",
    ref="uuid",
    attrs={"Id": "uuid", "EventSourceMappingArn": "arn"},
)
def lambda_event_source_mapping(b: Builder) -> None:
    p = b.props
    p.attrs(
        {
            "function_name": "FunctionName",
            "event_source_arn": "EventSourceArn",
            "starting_position": "StartingPosition",
            "kms_key_arn": "KmsKeyArn",
        }
    )
    p.attr("starting_position_timestamp", "StartingPositionTimestamp", _timestamp)
    p.boolean("enabled", "Enabled")
    p.boolean("bisect_batch_on_function_error", "BisectBatchOnFunctionError")
    for tf_name, cfn_name in (
        ("batch_size", "BatchSize"),
        ("maximum_batching_window_in_seconds", "MaximumBatchingWindowInSeconds"),
        ("maximum_retry_attempts", "MaximumRetryAttempts"),
        ("maximum_record_age_in_seconds", "MaximumRecordAgeInSeconds"),
        ("parallelization_factor", "ParallelizationFactor"),
        ("tumbling_window_in_seconds", "TumblingWindowInSeconds"),
    ):
        p.number(tf_name, cfn_name)
    p.list_attr("function_response_types", "FunctionResponseTypes")
    p.list_attr("queues", "Queues")
    p.list_attr("topics", "Topics")
    p.block(
        "filter_criteria",
        "FilterCriteria",
        lambda f: f.block("filter", "Filters", lambda x: x.json("pattern", "Pattern")),
    )
    p.block(
        "scaling_config",
        "ScalingConfig",
        lambda s: s.number("maximum_concurrency", "MaximumConcurrency"),
    )
    p.block(
        "destination_config",
        "DestinationConfig",
        lambda d: d.block(
            "on_failure", "OnFailure", lambda o: o.attr("destination_arn", "Destination")
        ),
    )
    p.block("metrics_config", "MetricsConfig", lambda m: m.list_attr("metrics", "Metrics"))
    p.tags()


def _timestamp(value: Any) -> Any:
    # CloudFormation takes seconds since the epoch; Terraform takes RFC 3339 text.
    if isinstance(value, int | float) and not isinstance(value, bool):
        return Raw(f'timeadd("1970-01-01T00:00:00Z", "{int(value)}s")')
    return value


@resource(
    "AWS::Lambda::LayerVersion",
    "aws_lambda_layer_version",
    ref="arn",
    attrs={"LayerVersionArn": "arn"},
)
def lambda_layer_version(b: Builder) -> None:
    p = b.props
    b.name_attr("layer_name", "LayerName")
    p.attr("description", "Description")
    p.attr("license_info", "LicenseInfo")
    p.list_attr("compatible_runtimes", "CompatibleRuntimes")
    p.list_attr("compatible_architectures", "CompatibleArchitectures")
    _code(b, p, "Content")


@resource(
    "AWS::Lambda::Url",
    "aws_lambda_function_url",
    ref="function_arn",
    attrs={"FunctionUrl": "function_url", "FunctionArn": "function_arn"},
)
def lambda_url(b: Builder) -> None:
    p = b.props
    p.attrs(
        {
            "function_name": "TargetFunctionArn",
            "authorization_type": "AuthType",
            "qualifier": "Qualifier",
            "invoke_mode": "InvokeMode",
        }
    )
    p.block("cors", "Cors", _function_url_cors)


def _function_url_cors(c: Props) -> None:
    c.boolean("allow_credentials", "AllowCredentials")
    c.list_attr("allow_headers", "AllowHeaders")
    c.list_attr("allow_methods", "AllowMethods")
    c.list_attr("allow_origins", "AllowOrigins")
    c.list_attr("expose_headers", "ExposeHeaders")
    c.number("max_age", "MaxAge")


@resource("AWS::Lambda::Alias", "aws_lambda_alias", ref="arn", attrs={"AliasArn": "arn"})
def lambda_alias(b: Builder) -> None:
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
    p.block("routing_config", "RoutingConfig", _routing_config)
    _provisioned_concurrency(b, function, b.self_ref("name"))


def _routing_config(r: Props) -> None:
    weights: dict[Any, Any] = {}
    for entry in r.literal("AdditionalVersionWeights", list) or []:
        entry = r.literal("AdditionalVersionWeights", dict, value=entry)
        if entry is None:
            continue
        version = r.ctx.expr(entry.get("FunctionVersion"))
        weight_key = version if isinstance(version, str) else Raw(hcl.inline(version))
        weights[weight_key] = r.ctx.expr(entry.get("FunctionWeight"))
    r.set("additional_version_weights", weights)


def _provisioned_concurrency(b: Builder, function: Any, qualifier: Any) -> None:
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


@resource(
    "AWS::Lambda::Version",
    "terraform_data",
    ref="output.arn",
    attrs={"Version": "output.version", "FunctionArn": "output.arn"},
)
def lambda_version(b: Builder) -> None:
    """Terraform has no version resource: a function with `publish = true` publishes a new
    version on every code change. A terraform_data records that version so Ref and
    Fn::GetAtt on the CloudFormation version keep working."""
    p = b.props
    function = p.take("FunctionName")
    target = _function_id(b, function)
    p.drop("Description", "CodeSha256")
    if target is not None:
        info = b.ctx.resources[target]
        value: dict[str, Any] = {
            "arn": Raw(info.expression("qualified_arn")),
            "version": Raw(info.expression("version")),
        }
        b.ctx.note(
            f"{b.logical_id} (AWS::Lambda::Version): Terraform publishes versions from the "
            f"function itself, so {info.address} has publish = true and {b.info.address} "
            "records its latest version"
        )
    else:
        value = {"arn": b.ctx.expr(function), "version": "$LATEST"}
        b.ctx.note(
            f"{b.logical_id} (AWS::Lambda::Version) belongs to a function outside this "
            "template: set publish = true on that function and use its version"
        )
    p.set("input", value)
    _provisioned_concurrency(b, function, Raw(b.info.expression("output.version")))


# --- EC2 instances and launch templates -------------------------------------------------

_ROOT_DEVICES = ("/dev/xvda", "/dev/sda1", "/dev/sda")
_EBS = {
    "volume_size": "VolumeSize",
    "volume_type": "VolumeType",
    "iops": "Iops",
    "throughput": "Throughput",
    "kms_key_id": "KmsKeyId",
}


def _ebs_items(r: Props, with_snapshot: bool) -> None:
    r.attrs(_EBS)
    if with_snapshot:
        r.attr("snapshot_id", "SnapshotId")
    r.boolean("encrypted", "Encrypted")
    r.boolean("delete_on_termination", "DeleteOnTermination")


def _instance_devices(b: Builder, p: Props) -> None:
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
            if device in _ROOT_DEVICES:
                kind = "root_block_device"
                _ebs_items(disk, with_snapshot=False)
                b.ctx.note(
                    f"{b.logical_id}: block device '{device}' became root_block_device; check "
                    "that it is the AMI's root device"
                )
            else:
                kind = "ebs_block_device"
                disk.set("device_name", b.ctx.expr(device))
                _ebs_items(disk, with_snapshot=True)
            disk.finish()
            inner.items.extend(disk.items)
        inner.finish()
        p.items.append(optional_block(kind, inner.items, when))


def _primary_interface(b: Builder, p: Props) -> None:
    """A single NetworkInterfaces entry for device 0 only says where the instance's own
    subnet, groups and public IP are. Anything more complex stays a note."""
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
            f"{snippet(keep)}, but the interface Terraform creates with the instance is always "
            "deleted with it; to keep it, create an aws_network_interface and attach it"
        )
        inner.comment(f"DeleteOnTermination was not converted: {snippet(keep)}")
    inner.boolean("associate_public_ip_address", "AssociatePublicIpAddress")
    inner.attr("subnet_id", "SubnetId")
    inner.list_attr("vpc_security_group_ids", "GroupSet")
    inner.attr("private_ip", "PrivateIpAddress")
    inner.number("ipv6_address_count", "Ipv6AddressCount")
    inner.finish()
    p.items.extend(inner.items)


@resource(
    "AWS::EC2::Instance",
    "aws_instance",
    attrs={
        "InstanceId": "id",
        "PublicIp": "public_ip",
        "PrivateIp": "private_ip",
        "PublicDnsName": "public_dns",
        "PrivateDnsName": "private_dns",
        "AvailabilityZone": "availability_zone",
    },
)
def ec2_instance(b: Builder) -> None:
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
    for tf_name, cfn_name in (
        ("monitoring", "Monitoring"),
        ("ebs_optimized", "EbsOptimized"),
        ("disable_api_termination", "DisableApiTermination"),
        ("source_dest_check", "SourceDestCheck"),
    ):
        p.boolean(tf_name, cfn_name)
    _primary_interface(b, p)
    user_data = p.take("UserData")
    if user_data is not None:
        # CloudFormation wants UserData base64-encoded, nearly always with Fn::Base64.
        if isinstance(user_data, dict) and intrinsic_name(user_data) == "Fn::Base64":
            p.set("user_data", b.ctx.expr(user_data["Fn::Base64"]))
        else:
            p.set("user_data_base64", b.ctx.expr(user_data))
    p.block(
        "credit_specification",
        "CreditSpecification",
        lambda c: c.attr("cpu_credits", "CPUCredits"),
    )
    p.block(
        "launch_template",
        "LaunchTemplate",
        lambda t: t.attrs(
            {"id": "LaunchTemplateId", "name": "LaunchTemplateName", "version": "Version"}
        ),
    )
    _instance_devices(b, p)
    p.tags()


@resource(
    "AWS::EC2::LaunchTemplate",
    "aws_launch_template",
    attrs={
        "LaunchTemplateId": "id",
        "LatestVersionNumber": "latest_version",
        "DefaultVersionNumber": "default_version",
    },
)
def ec2_launch_template(b: Builder) -> None:
    p = b.props
    p.attr("name", "LaunchTemplateName")
    p.attr("description", "VersionDescription")
    own_tags: list[Any] = []
    for spec in p.literal("TagSpecifications", list) or []:
        # The launch template's own tags (ResourceType: launch-template).
        spec = p.literal("TagSpecifications", dict, value=spec)
        if spec is not None:
            own_tags.extend(p.literal("TagSpecifications.Tags", list, value=spec.get("Tags")) or [])
    if own_tags:
        p.set("tags", tags_map(b.ctx, own_tags))
    data = p.take("LaunchTemplateData")
    if data is None:
        return
    d = Props(b.ctx, data, f"{b.logical_id}.LaunchTemplateData", p.owner)
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
    d.block(
        "iam_instance_profile",
        "IamInstanceProfile",
        lambda i: i.attrs({"arn": "Arn", "name": "Name"}),
    )
    d.block("block_device_mappings", "BlockDeviceMappings", _template_device)
    d.block("metadata_options", "MetadataOptions", _metadata_options)
    d.block("monitoring", "Monitoring", lambda m: m.boolean("enabled", "Enabled"))
    d.block(
        "credit_specification",
        "CreditSpecification",
        lambda c: c.attr("cpu_credits", "CpuCredits"),
    )
    d.block(
        "placement",
        "Placement",
        lambda c: c.attrs(
            {
                "availability_zone": "AvailabilityZone",
                "group_name": "GroupName",
                "tenancy": "Tenancy",
            }
        ),
    )
    d.block("network_interfaces", "NetworkInterfaces", _template_interface)
    d.block(
        "tag_specifications",
        "TagSpecifications",
        lambda t: (t.attr("resource_type", "ResourceType"), t.tags()),
    )
    d.finish()
    p.items.extend(d.items)


def _metadata_options(m: Props) -> None:
    m.attrs(
        {
            "http_tokens": "HttpTokens",
            "http_endpoint": "HttpEndpoint",
            "instance_metadata_tags": "InstanceMetadataTags",
            "http_protocol_ipv6": "HttpProtocolIpv6",
        }
    )
    m.number("http_put_response_hop_limit", "HttpPutResponseHopLimit")


def _template_device(r: Props) -> None:
    r.attrs({"device_name": "DeviceName", "virtual_name": "VirtualName"})
    # CloudFormation omits the device whenever NoDevice is present, usually as "". The
    # provider ignores an empty no_device, so any value becomes a non-empty one.
    r.attr("no_device", "NoDevice", lambda v: _no_device(r, v))
    r.block("ebs", "Ebs", lambda e: _ebs_items(e, with_snapshot=True))


def _no_device(r: Props, value: Any) -> Any:
    if isinstance(value, str):
        return value or "true"
    return Raw(f'coalesce({hcl.inline(r.ctx.expr(value))}, "true")')


def _template_interface(r: Props) -> None:
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


# --- ECS --------------------------------------------------------------------------------


@resource(
    "AWS::ECS::Cluster",
    "aws_ecs_cluster",
    ref="name",
    attrs={"Arn": "arn"},
    import_id="ClusterName",
)
def ecs_cluster(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "ClusterName")
    p.block("setting", "ClusterSettings", lambda s: s.attrs({"name": "Name", "value": "Value"}))
    p.block(
        "service_connect_defaults",
        "ServiceConnectDefaults",
        lambda s: s.attr("namespace", "Namespace", lambda v: _namespace(b, v)),
    )
    p.block("configuration", "Configuration", _cluster_configuration)
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
        _strategy,
        f"{b.logical_id}.DefaultCapacityProviderStrategy",
    )
    b.add_resource("aws_ecs_cluster_capacity_providers", "_capacity_providers", holder.items)


def _namespace(b: Builder, value: Any) -> Any:
    if isinstance(value, str) and not value.startswith("arn:"):
        b.ctx.note(
            f"{b.logical_id}: ServiceConnectDefaults.Namespace '{value}' is a namespace name, "
            "which CloudFormation looks up (or creates); Terraform needs the namespace ARN, "
            "e.g. from an aws_service_discovery_http_namespace"
        )
    return b.ctx.expr(value)


def _strategy(r: Props) -> None:
    r.attr("capacity_provider", "CapacityProvider")
    r.number("weight", "Weight")
    r.number("base", "Base")


def _cluster_configuration(r: Props) -> None:
    r.block(
        "execute_command_configuration",
        "ExecuteCommandConfiguration",
        lambda e: (
            e.attrs({"kms_key_id": "KmsKeyId", "logging": "Logging"}),
            e.block("log_configuration", "LogConfiguration", _exec_log_configuration),
        ),
    )
    r.block(
        "managed_storage_configuration",
        "ManagedStorageConfiguration",
        lambda m: m.attrs(
            {
                "kms_key_id": "KmsKeyId",
                "fargate_ephemeral_storage_kms_key_id": "FargateEphemeralStorageKmsKeyId",
            }
        ),
    )


def _exec_log_configuration(r: Props) -> None:
    r.boolean("cloud_watch_encryption_enabled", "CloudWatchEncryptionEnabled")
    r.attr("cloud_watch_log_group_name", "CloudWatchLogGroupName")
    r.boolean("s3_bucket_encryption_enabled", "S3EncryptionEnabled")
    r.attr("s3_bucket_name", "S3BucketName")
    r.attr("s3_key_prefix", "S3KeyPrefix")


# Container definition keys holding user maps: their own keys stay as written.
_ECS_MAPS = {"DockerLabels", "Options", "DriverOpts", "Labels"}
_ECS_NUMBERS = {
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
_ECS_BOOLEANS = {
    "essential",
    "readOnly",
    "privileged",
    "readonlyRootFilesystem",
    "disableNetworking",
    "interactive",
    "pseudoTerminal",
    "initProcessEnabled",
}
_DIGITS = re.compile(r"^[0-9]+$")


def ecs_json(value: Any) -> Any:
    """CloudFormation container definitions (PascalCase) as the JSON the ECS API takes
    (camelCase). Intrinsic functions and user maps are left as written, but both branches of
    an Fn::If are converted too. Numbers and booleans written as strings become real ones,
    as ECS rejects "256" for cpu, and the string fields CloudFormation coerces (environment
    values, labels, log options) become strings, as ECS rejects 8080 for them."""
    if isinstance(value, list):
        return [ecs_json(item) for item in value]
    if not isinstance(value, dict):
        return value
    if intrinsic_name(value) is not None:
        return _branches(value, ecs_json)
    result: dict[str, Any] = {}
    for name, item in value.items():
        key = str(name)
        camel = key[:1].lower() + key[1:]
        if key in _ECS_MAPS:
            result[camel] = _branches(item, _string_values)
            continue
        item = ecs_json(item)
        if camel == "environment":
            item = _branches(item, _environment)
        result[camel] = _branches(item, lambda v, c=camel: _typed(c, v))
    return result


def _branches(value: Any, convert: Callable[[Any], Any]) -> Any:
    """`convert(value)`, or for `!If [C, a, b]` the same with a and b converted. Other
    intrinsic functions are left as written."""
    if isinstance(value, dict) and intrinsic_name(value) is not None:
        args = value[intrinsic_name(value)]
        if intrinsic_name(value) == "Fn::If" and isinstance(args, list) and len(args) == 3:
            name, yes, no = args
            return {"Fn::If": [name, _branches(yes, convert), _branches(no, convert)]}
        return value
    return convert(value)


def _typed(camel: str, item: Any) -> Any:
    if camel in _ECS_NUMBERS and isinstance(item, str) and _DIGITS.match(item):
        return int(item)
    if camel in _ECS_BOOLEANS and isinstance(item, str) and item.lower() in ("true", "false"):
        return item.lower() == "true"
    return item


def _string(value: Any) -> Any:
    """A literal as the text CloudFormation would pass: true -> "true", 8080 -> "8080"."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return value


def _string_values(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {k: _branches(v, _string) for k, v in value.items()}


def _environment(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [_branches(entry, _environment_entry) for entry in value]


def _environment_entry(entry: Any) -> Any:
    if not isinstance(entry, dict) or "value" not in entry:
        return entry
    return {**entry, "value": _branches(entry["value"], _string)}


@resource(
    "AWS::ECS::TaskDefinition",
    "aws_ecs_task_definition",
    ref="arn",
    attrs={"TaskDefinitionArn": "arn"},
)
def ecs_task_definition(b: Builder) -> None:
    p = b.props
    b.name_attr("family", "Family")
    definitions = p.take("ContainerDefinitions")
    if definitions is not None:
        p.set("container_definitions", hcl.Call("jsonencode", b.ctx.expr(ecs_json(definitions))))
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
    p.block(
        "runtime_platform",
        "RuntimePlatform",
        lambda r: r.attrs(
            {
                "cpu_architecture": "CpuArchitecture",
                "operating_system_family": "OperatingSystemFamily",
            }
        ),
    )
    p.block(
        "ephemeral_storage",
        "EphemeralStorage",
        lambda e: e.number("size_in_gib", "SizeInGiB"),
    )
    p.block(
        "placement_constraints",
        "PlacementConstraints",
        lambda c: c.attrs({"type": "Type", "expression": "Expression"}),
    )
    p.block("volume", "Volumes", _volume)
    p.tags()


def _volume(r: Props) -> None:
    r.attr("name", "Name")
    r.boolean("configure_at_launch", "ConfigureAtLaunch")
    when, host = r.ctx.optional_item(r.take("Host"))
    host = r.literal("Host", dict, value=host)
    if host is not None and host.get("SourcePath") is not None:
        path = r.ctx.expr(host["SourcePath"])
        if when is not None:
            path = Raw(f"{when} ? {_wrap(hcl.inline(path))} : null")
        r.set("host_path", path)
    r.block("efs_volume_configuration", "EFSVolumeConfiguration", _efs_volume)
    r.block(
        "docker_volume_configuration",
        "DockerVolumeConfiguration",
        lambda d: (
            d.boolean("autoprovision", "Autoprovision"),
            d.attrs(
                {
                    "driver": "Driver",
                    "scope": "Scope",
                    "driver_opts": "DriverOpts",
                    "labels": "Labels",
                }
            ),
        ),
    )


def _efs_volume(e: Props) -> None:
    e.attrs(
        {
            "file_system_id": "FilesystemId",
            "root_directory": "RootDirectory",
            "transit_encryption": "TransitEncryption",
        }
    )
    e.number("transit_encryption_port", "TransitEncryptionPort")
    e.block(
        "authorization_config",
        "AuthorizationConfig",
        lambda a: a.attrs({"access_point_id": "AccessPointId", "iam": "IAM"}),
    )


@resource("AWS::ECS::Service", "aws_ecs_service", attrs={"Name": "name", "ServiceArn": "arn"})
def ecs_service(b: Builder) -> None:
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
        _default_desired_count(b, p)
    p.number("health_check_grace_period_seconds", "HealthCheckGracePeriodSeconds")
    p.boolean("enable_execute_command", "EnableExecuteCommand")
    p.boolean("enable_ecs_managed_tags", "EnableECSManagedTags")
    deployment = p.take("DeploymentConfiguration")
    if deployment is not None:
        # In Terraform these are top-level arguments and blocks of the service.
        inner = Props(b.ctx, deployment, f"{b.logical_id}.DeploymentConfiguration", p.owner)
        inner.number("deployment_maximum_percent", "MaximumPercent")
        inner.number("deployment_minimum_healthy_percent", "MinimumHealthyPercent")
        inner.block(
            "deployment_circuit_breaker",
            "DeploymentCircuitBreaker",
            lambda c: (c.boolean("enable", "Enable"), c.boolean("rollback", "Rollback")),
        )
        inner.block(
            "alarms",
            "Alarms",
            lambda a: (
                a.list_attr("alarm_names", "AlarmNames"),
                a.boolean("enable", "Enable"),
                a.boolean("rollback", "Rollback"),
            ),
        )
        inner.finish()
        p.items.extend(inner.items)
    network = p.take("NetworkConfiguration")
    if network is not None:
        inner = Props(b.ctx, network, f"{b.logical_id}.NetworkConfiguration", p.owner)
        inner.block("network_configuration", "AwsvpcConfiguration", _awsvpc)
        inner.finish()
        p.items.extend(inner.items)
    p.block(
        "load_balancer",
        "LoadBalancers",
        lambda lb: (
            lb.attrs(
                {
                    "container_name": "ContainerName",
                    "target_group_arn": "TargetGroupArn",
                    "elb_name": "LoadBalancerName",
                }
            ),
            lb.number("container_port", "ContainerPort"),
        ),
    )
    p.block("deployment_controller", "DeploymentController", lambda d: d.attr("type", "Type"))
    p.block("capacity_provider_strategy", "CapacityProviderStrategy", _strategy)
    p.block(
        "placement_constraints",
        "PlacementConstraints",
        lambda c: c.attrs({"type": "Type", "expression": "Expression"}),
    )
    p.block(
        "ordered_placement_strategy",
        "PlacementStrategies",
        lambda s: s.attrs({"type": "Type", "field": "Field"}),
    )
    p.block(
        "service_registries",
        "ServiceRegistries",
        lambda s: (
            s.attrs({"registry_arn": "RegistryArn", "container_name": "ContainerName"}),
            s.number("port", "Port"),
            s.number("container_port", "ContainerPort"),
        ),
    )
    p.tags()


def _default_desired_count(b: Builder, p: Props) -> None:
    """CloudFormation starts one task when DesiredCount is left out (except for DAEMON
    services, which take none); Terraform's default is 0."""
    strategy = (b.resource.get("Properties") or {}).get("SchedulingStrategy")
    if strategy is None or b.ctx.is_no_value(strategy):
        p.set("desired_count", 1)
    elif isinstance(strategy, str):
        if strategy.upper() != "DAEMON":
            p.set("desired_count", 1)
    else:
        strategy_text = _wrap(hcl.inline(b.ctx.expr(strategy)))
        p.set("desired_count", Raw(f'{strategy_text} == "DAEMON" ? null : 1'))


def _awsvpc(r: Props) -> None:
    r.list_attr("subnets", "Subnets")
    r.list_attr("security_groups", "SecurityGroups")
    value = r.take("AssignPublicIp")
    if value is None:
        return
    if isinstance(value, str) and value.upper() in ("ENABLED", "DISABLED"):
        r.set("assign_public_ip", value.upper() == "ENABLED")
    else:
        r.set("assign_public_ip", Raw(f'{_wrap(hcl.inline(r.ctx.expr(value)))} == "ENABLED"'))
