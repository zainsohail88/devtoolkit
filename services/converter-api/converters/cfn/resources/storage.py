"""S3, DynamoDB and ECR."""

from __future__ import annotations

import itertools
from typing import Any

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props
from converters.cfn.context import ResourceInfo, _wrap, intrinsic_name, json_value
from converters.cfn.hcl import BLANK, Attr, Block, Raw
from converters.cfn.resources import resource
from converters.cfn.resources.compute import optional_block

# --- S3 ---------------------------------------------------------------------------------

_CANNED_ACL = {
    "Private": "private",
    "PublicRead": "public-read",
    "PublicReadWrite": "public-read-write",
    "AuthenticatedRead": "authenticated-read",
    "LogDeliveryWrite": "log-delivery-write",
    "BucketOwnerRead": "bucket-owner-read",
    "BucketOwnerFullControl": "bucket-owner-full-control",
    "AwsExecRead": "aws-exec-read",
}


def _website_url(info: ResourceInfo) -> None:
    """WebsiteURL lives on the separate website configuration resource."""
    index = "[0]" if info.conditional else ""
    website = f"aws_s3_bucket_website_configuration.{info.name}_website{index}"
    info.attrs["WebsiteURL"] = "website_url"
    info.overrides["website_url"] = f'"http://${{{website}.website_endpoint}}"'


def _bucket_items(b: Builder) -> list[Any]:
    return [Attr("bucket", b.self_ref("id"))]


@resource(
    "AWS::S3::Bucket",
    "aws_s3_bucket",
    attrs={
        "Arn": "arn",
        "DomainName": "bucket_domain_name",
        "RegionalDomainName": "bucket_regional_domain_name",
    },
    import_id="BucketName",
    prepare=lambda info, resource: _website_url(info),
)
def s3_bucket(b: Builder) -> None:
    """One aws_s3_bucket plus one resource per configuration, as provider v4+ requires."""
    p = b.props
    p.attr("bucket", "BucketName")
    p.boolean("object_lock_enabled", "ObjectLockEnabled")
    if b.hints.get("force_destroy"):
        p.set("force_destroy", True)
    p.tags()

    versioning = p.take("VersioningConfiguration")
    if versioning is not None:
        inner = _sub_props(b, versioning, "VersioningConfiguration")
        inner.attr("status", "Status")
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_versioning",
            "_versioning",
            [*_bucket_items(b), Block("versioning_configuration", [], inner.items)],
        )

    encryption = p.take("BucketEncryption")
    if encryption is not None:
        inner = _sub_props(b, encryption, "BucketEncryption")
        inner.block("rule", "ServerSideEncryptionConfiguration", _encryption_rule)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_server_side_encryption_configuration",
            "_encryption",
            [*_bucket_items(b), *inner.items],
        )

    public = p.take("PublicAccessBlockConfiguration")
    if public is not None:
        inner = _sub_props(b, public, "PublicAccessBlockConfiguration")
        for tf_name, cfn_name in (
            ("block_public_acls", "BlockPublicAcls"),
            ("block_public_policy", "BlockPublicPolicy"),
            ("ignore_public_acls", "IgnorePublicAcls"),
            ("restrict_public_buckets", "RestrictPublicBuckets"),
        ):
            inner.boolean(tf_name, cfn_name)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_public_access_block", "_public_access", [*_bucket_items(b), *inner.items]
        )

    ownership = p.take("OwnershipControls")
    ownership_address = None
    if ownership is not None:
        inner = _sub_props(b, ownership, "OwnershipControls")
        inner.block("rule", "Rules", lambda r: r.attr("object_ownership", "ObjectOwnership"))
        inner.finish()
        ownership_address = b.add_resource(
            "aws_s3_bucket_ownership_controls", "_ownership", [*_bucket_items(b), *inner.items]
        )

    acl = p.take("AccessControl")
    if acl is not None:
        value = _CANNED_ACL.get(acl, acl) if isinstance(acl, str) else b.ctx.expr(acl)
        items = [*_bucket_items(b), Attr("acl", value)]
        if ownership_address:
            # S3 rejects ACLs until the ownership setting allows them.
            items += [BLANK, Attr("depends_on", [Raw(ownership_address)])]
        else:
            b.ctx.note(
                f"{b.logical_id} sets AccessControl: S3 disables ACLs by default, so the "
                "aws_s3_bucket_acl only works once OwnershipControls is ObjectWriter or "
                "BucketOwnerPreferred"
            )
        b.add_resource("aws_s3_bucket_acl", "_acl", items)

    lifecycle = p.take("LifecycleConfiguration")
    if lifecycle is not None:
        inner = _sub_props(b, lifecycle, "LifecycleConfiguration")
        counter = itertools.count(1)
        inner.block("rule", "Rules", lambda r: _lifecycle_rule(r, next(counter)))
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_lifecycle_configuration", "_lifecycle", [*_bucket_items(b), *inner.items]
        )

    cors = p.take("CorsConfiguration")
    if cors is not None:
        inner = _sub_props(b, cors, "CorsConfiguration")
        inner.block("cors_rule", "CorsRules", _cors_rule)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_cors_configuration", "_cors", [*_bucket_items(b), *inner.items]
        )

    website = p.take("WebsiteConfiguration")
    if website is not None:
        inner = _sub_props(b, website, "WebsiteConfiguration")
        index = inner.take("IndexDocument")
        if index is not None:
            inner.items.append(Block("index_document", [], [Attr("suffix", b.ctx.expr(index))]))
        error = inner.take("ErrorDocument")
        if error is not None:
            inner.items.append(Block("error_document", [], [Attr("key", b.ctx.expr(error))]))
        inner.block(
            "redirect_all_requests_to",
            "RedirectAllRequestsTo",
            lambda r: r.attrs({"host_name": "HostName", "protocol": "Protocol"}),
        )
        inner.block("routing_rule", "RoutingRules", _routing_rule)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_website_configuration", "_website", [*_bucket_items(b), *inner.items]
        )

    logging = p.take("LoggingConfiguration")
    if logging is not None:
        inner = _sub_props(b, logging, "LoggingConfiguration")
        if not inner.attr("target_bucket", "DestinationBucketName"):
            # Without a destination, S3 writes the logs to the bucket itself.
            inner.set("target_bucket", b.self_ref("id"))
        if not inner.attr("target_prefix", "LogFilePrefix"):
            inner.set("target_prefix", "")
        inner.finish()
        b.add_resource("aws_s3_bucket_logging", "_logging", [*_bucket_items(b), *inner.items])

    accelerate = p.take("AccelerateConfiguration")
    if accelerate is not None:
        inner = _sub_props(b, accelerate, "AccelerateConfiguration")
        inner.attr("status", "AccelerationStatus")
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_accelerate_configuration",
            "_accelerate",
            [*_bucket_items(b), *inner.items],
        )

    lock = p.take("ObjectLockConfiguration")
    if lock is not None:
        inner = _sub_props(b, lock, "ObjectLockConfiguration")
        inner.drop("ObjectLockEnabled")
        inner.block("rule", "Rule", _object_lock_rule)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_object_lock_configuration",
            "_object_lock",
            [*_bucket_items(b), *inner.items],
        )

    notification = p.take("NotificationConfiguration")
    if notification is not None:
        inner = _sub_props(b, notification, "NotificationConfiguration")
        for tf_name, cfn_name, target_attr, target_key in (
            ("lambda_function", "LambdaConfigurations", "lambda_function_arn", "Function"),
            ("queue", "QueueConfigurations", "queue_arn", "Queue"),
            ("topic", "TopicConfigurations", "topic_arn", "Topic"),
        ):
            inner.block(
                tf_name,
                cfn_name,
                lambda r, a=target_attr, k=target_key: _notification(r, a, k),
            )
        when, bridge = b.ctx.optional_item(inner.take("EventBridgeConfiguration"))
        bridge = inner.literal("EventBridgeConfiguration", dict, value=bridge)
        if bridge is not None:
            enabled = b.ctx.expr(_bool(bridge.get("EventBridgeEnabled", True)))
            if when is not None:
                enabled = Raw(f"{when} ? {_wrap(hcl.inline(enabled))} : false")
            inner.set("eventbridge", enabled)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_notification", "_notification", [*_bucket_items(b), *inner.items]
        )


def _bool(value: Any) -> Any:
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _sub_props(b: Builder, value: Any, name: str) -> Props:
    return Props(b.ctx, value, f"{b.logical_id}.{name}", b.props.owner)


def _encryption_rule(r: Props) -> None:
    r.boolean("bucket_key_enabled", "BucketKeyEnabled")
    r.block(
        "apply_server_side_encryption_by_default",
        "ServerSideEncryptionByDefault",
        lambda d: d.attrs({"sse_algorithm": "SSEAlgorithm", "kms_master_key_id": "KMSMasterKeyID"}),
    )


def _lifecycle_rule(r: Props, number: int) -> None:
    if not r.attr("id", "Id"):
        r.set("id", f"rule-{number}")
    r.attr("status", "Status")

    prefix = r.take("Prefix")
    tags = [
        tag
        for tag in (r.literal("TagFilters", list) or [])
        if r.literal("TagFilters", dict, value=tag) is not None
    ]
    greater = r.take("ObjectSizeGreaterThan")
    less = r.take("ObjectSizeLessThan")
    conditions = [x for x in (prefix, greater, less) if x is not None] + (tags or [])
    filter_items: list[Any] = []
    if len(conditions) > 1:
        inner: list[Any] = []
        if prefix is not None:
            inner.append(Attr("prefix", r.ctx.expr(prefix)))
        if greater is not None:
            inner.append(Attr("object_size_greater_than", r.ctx.expr(greater)))
        if less is not None:
            inner.append(Attr("object_size_less_than", r.ctx.expr(less)))
        if tags:
            inner.append(
                Attr("tags", {str(t.get("Key")): r.ctx.expr(t.get("Value")) for t in tags})
            )
        filter_items.append(Block("and", [], inner))
    elif prefix is not None:
        filter_items.append(Attr("prefix", r.ctx.expr(prefix)))
    elif greater is not None:
        filter_items.append(Attr("object_size_greater_than", r.ctx.expr(greater)))
    elif less is not None:
        filter_items.append(Attr("object_size_less_than", r.ctx.expr(less)))
    elif tags:
        tag = tags[0]
        filter_items.append(
            Block(
                "tag",
                [],
                [
                    Attr("key", r.ctx.expr(tag.get("Key"))),
                    Attr("value", r.ctx.expr(tag.get("Value"))),
                ],
            )
        )
    # An empty filter means the whole bucket; the provider wants it spelled out.
    r.items.append(Block("filter", [], filter_items))

    when, abort = _optional(
        r, "AbortIncompleteMultipartUpload", r.take("AbortIncompleteMultipartUpload")
    )
    if abort is not None:
        r.items.append(
            optional_block(
                "abort_incomplete_multipart_upload",
                [Attr("days_after_initiation", r.ctx.expr(abort.get("DaysAfterInitiation")))],
                when,
            )
        )

    expiration: list[Any] = []
    days = r.take("ExpirationInDays")
    if days is not None:
        expiration.append(Attr("days", r.ctx.expr(days)))
    date = r.take("ExpirationDate")
    if date is not None:
        expiration.append(Attr("date", r.ctx.expr(date)))
    marker = r.take("ExpiredObjectDeleteMarker")
    if marker is not None:
        expiration.append(Attr("expired_object_delete_marker", _bool(marker)))
    if expiration:
        r.items.append(Block("expiration", [], expiration))

    for when, transition in _entries(r, "Transitions", "Transition"):
        items = [Attr("storage_class", r.ctx.expr(transition.get("StorageClass")))]
        if "TransitionInDays" in transition:
            items.insert(0, Attr("days", r.ctx.expr(transition["TransitionInDays"])))
        if "TransitionDate" in transition:
            items.insert(0, Attr("date", r.ctx.expr(transition["TransitionDate"])))
        r.items.append(optional_block("transition", items, when))

    noncurrent_days = r.take("NoncurrentVersionExpirationInDays")
    when, noncurrent = _optional(
        r, "NoncurrentVersionExpiration", r.take("NoncurrentVersionExpiration")
    )
    if noncurrent_days is not None or noncurrent is not None:
        items = []
        if noncurrent is not None:
            noncurrent_days = noncurrent.get("NoncurrentDays", noncurrent_days)
            if "NewerNoncurrentVersions" in noncurrent:
                items.append(
                    Attr(
                        "newer_noncurrent_versions",
                        r.ctx.expr(noncurrent["NewerNoncurrentVersions"]),
                    )
                )
        items.insert(0, Attr("noncurrent_days", r.ctx.expr(noncurrent_days)))
        r.items.append(optional_block("noncurrent_version_expiration", items, when))

    for when, transition in _entries(
        r, "NoncurrentVersionTransitions", "NoncurrentVersionTransition"
    ):
        items = [
            Attr("noncurrent_days", r.ctx.expr(transition.get("TransitionInDays"))),
            Attr("storage_class", r.ctx.expr(transition.get("StorageClass"))),
        ]
        if "NewerNoncurrentVersions" in transition:
            items.append(
                Attr("newer_noncurrent_versions", r.ctx.expr(transition["NewerNoncurrentVersions"]))
            )
        r.items.append(optional_block("noncurrent_version_transition", items, when))


def _optional(r: Props, name: str, value: Any) -> tuple[str | None, Any]:
    """A structure, or `!If [C, {...}, !Ref AWS::NoValue]` as (condition, structure). Other
    functions are noted and give (None, None)."""
    when, value = r.ctx.optional_item(value)
    return when, r.literal(name, dict, value=value)


def _entries(r: Props, many: str, one: str) -> list[tuple[str | None, Any]]:
    """The structures of a list property plus its single-item form (Transitions and the
    older Transition), each with the condition it exists under."""
    values = list(r.literal(many, list) or [])
    single = r.take(one)
    if single is not None:
        values.append(single)
    entries = [_optional(r, many, value) for value in values]
    return [(when, value) for when, value in entries if value is not None]


def _cors_rule(r: Props) -> None:
    r.attr("id", "Id")
    r.list_attr("allowed_headers", "AllowedHeaders")
    r.list_attr("allowed_methods", "AllowedMethods")
    r.list_attr("allowed_origins", "AllowedOrigins")
    r.list_attr("expose_headers", "ExposedHeaders")
    r.number("max_age_seconds", "MaxAge")


def _routing_rule(r: Props) -> None:
    r.block(
        "condition",
        "RoutingRuleCondition",
        lambda c: c.attrs(
            {
                "http_error_code_returned_equals": "HttpErrorCodeReturnedEquals",
                "key_prefix_equals": "KeyPrefixEquals",
            }
        ),
    )
    r.block(
        "redirect",
        "RedirectRule",
        lambda c: c.attrs(
            {
                "host_name": "HostName",
                "http_redirect_code": "HttpRedirectCode",
                "protocol": "Protocol",
                "replace_key_prefix_with": "ReplaceKeyPrefixWith",
                "replace_key_with": "ReplaceKeyWith",
            }
        ),
    )


def _object_lock_rule(r: Props) -> None:
    r.block(
        "default_retention",
        "DefaultRetention",
        lambda d: (d.attr("mode", "Mode"), d.number("days", "Days"), d.number("years", "Years")),
    )


def _notification(r: Props, target_attr: str, target_key: str) -> None:
    r.list_attr("events", "Event")
    r.attr(target_attr, target_key)
    filters = r.literal("Filter", dict) or {}
    key = r.literal("Filter.S3Key", dict, value=filters.get("S3Key")) or {}
    for rule in r.literal("Filter.S3Key.Rules", list, value=key.get("Rules")) or []:
        rule = r.literal("Filter.S3Key.Rules", dict, value=rule)
        if rule is None:
            continue
        name = str(rule.get("Name", "")).lower()
        if name in ("prefix", "suffix"):
            r.set(f"filter_{name}", r.ctx.expr(rule.get("Value")))


@resource("AWS::S3::BucketPolicy", "aws_s3_bucket_policy")
def s3_bucket_policy(b: Builder) -> None:
    b.props.attr("bucket", "Bucket")
    b.props.json("policy", "PolicyDocument")


# --- DynamoDB ---------------------------------------------------------------------------


def _key_names(r: Props) -> tuple[Any, Any]:
    hash_key = range_key = None
    for key in _key_schema(r):
        if key.get("KeyType") == "HASH":
            hash_key = key.get("AttributeName")
        elif key.get("KeyType") == "RANGE":
            range_key = key.get("AttributeName")
    return hash_key, range_key


def _key_schema(r: Props) -> list[dict[str, Any]]:
    """KeySchema entries that are literal mappings (the others are noted)."""
    keys = [r.literal("KeySchema", dict, value=key) for key in r.literal("KeySchema", list) or []]
    return [key for key in keys if key is not None]


def _throughput(r: Props) -> None:
    throughput = r.literal("ProvisionedThroughput", dict)
    if throughput is not None:
        r.set("read_capacity", r.ctx.expr(throughput.get("ReadCapacityUnits")))
        r.set("write_capacity", r.ctx.expr(throughput.get("WriteCapacityUnits")))


def _projection(r: Props) -> None:
    projection = r.literal("Projection", dict)
    if projection is not None:
        if projection.get("ProjectionType"):
            r.set("projection_type", r.ctx.expr(projection["ProjectionType"]))
        else:
            r.set("projection_type", "ALL")
        if projection.get("NonKeyAttributes"):
            r.set("non_key_attributes", r.ctx.expr(projection["NonKeyAttributes"]))


def _global_index(r: Props) -> None:
    r.attr("name", "IndexName")
    for key in _key_schema(r):
        r.items.append(
            Block(
                "key_schema",
                [],
                [
                    Attr("attribute_name", r.ctx.expr(key.get("AttributeName"))),
                    Attr("key_type", r.ctx.expr(key.get("KeyType"))),
                ],
            )
        )
    _projection(r)
    _throughput(r)
    r.block(
        "on_demand_throughput",
        "OnDemandThroughput",
        lambda o: (
            o.number("max_read_request_units", "MaxReadRequestUnits"),
            o.number("max_write_request_units", "MaxWriteRequestUnits"),
        ),
    )


def _local_index(r: Props) -> None:
    r.attr("name", "IndexName")
    _, range_key = _key_names(r)
    if range_key is not None:
        r.set("range_key", r.ctx.expr(range_key))
    _projection(r)


@resource(
    "AWS::DynamoDB::Table",
    "aws_dynamodb_table",
    ref="name",
    attrs={"Arn": "arn", "StreamArn": "stream_arn"},
    import_id="TableName",
)
def dynamodb_table(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "TableName")
    p.attr("billing_mode", "BillingMode")
    hash_key, range_key = _key_names(p)
    if hash_key is not None:
        p.set("hash_key", b.ctx.expr(hash_key))
    if range_key is not None:
        p.set("range_key", b.ctx.expr(range_key))
    _throughput(p)
    p.attr("table_class", "TableClass")
    p.boolean("deletion_protection_enabled", "DeletionProtectionEnabled")

    when, stream = _optional(p, "StreamSpecification", p.take("StreamSpecification"))
    if stream is not None:
        inner = _sub_props(b, stream, "StreamSpecification")
        view = b.ctx.expr(inner.take("StreamViewType"))
        if when is None:
            p.set("stream_enabled", True)
            p.set("stream_view_type", view)
        else:
            p.set("stream_enabled", Raw(when))
            p.set("stream_view_type", Raw(f"{when} ? {_wrap(hcl.inline(view))} : null"))
        # ResourcePolicy and Tags (for the stream) have no counterpart on the table.
        inner.finish()
        p.items.extend(inner.items)

    for item in p.literal("AttributeDefinitions", list) or []:
        when, attribute = _optional(p, "AttributeDefinitions", item)
        if attribute is None:
            continue
        p.items.append(
            optional_block(
                "attribute",
                [
                    Attr("name", b.ctx.expr(attribute.get("AttributeName"))),
                    Attr("type", b.ctx.expr(attribute.get("AttributeType"))),
                ],
                when,
            )
        )
    p.block("global_secondary_index", "GlobalSecondaryIndexes", _global_index)
    p.block("local_secondary_index", "LocalSecondaryIndexes", _local_index)
    p.block(
        "ttl",
        "TimeToLiveSpecification",
        lambda t: (t.attr("attribute_name", "AttributeName"), t.boolean("enabled", "Enabled")),
    )
    p.block(
        "server_side_encryption",
        "SSESpecification",
        lambda s: (
            s.boolean("enabled", "SSEEnabled"),
            s.attr("kms_key_arn", "KMSMasterKeyId", lambda v: _kms_key_arn(b, v)),
            s.drop("SSEType"),
        ),
    )
    p.block(
        "point_in_time_recovery",
        "PointInTimeRecoverySpecification",
        lambda s: (
            s.boolean("enabled", "PointInTimeRecoveryEnabled"),
            s.number("recovery_period_in_days", "RecoveryPeriodInDays"),
        ),
    )
    p.block(
        "on_demand_throughput",
        "OnDemandThroughput",
        lambda o: (
            o.number("max_read_request_units", "MaxReadRequestUnits"),
            o.number("max_write_request_units", "MaxWriteRequestUnits"),
        ),
    )
    p.tags()


def _kms_key_arn(b: Builder, value: Any) -> Any:
    """KMSMasterKeyId takes a key ID, ARN, alias name or alias ARN (and Ref of a KMS key
    gives its ID); Terraform's kms_key_arn takes only an ARN."""
    if isinstance(value, str) and value.startswith("arn:"):
        return value
    if isinstance(value, dict) and intrinsic_name(value) == "Fn::GetAtt":
        return b.ctx.expr(value)
    if isinstance(value, dict) and set(value) == {"Ref"}:
        info = b.ctx.resources.get(str(value["Ref"]))
        if info is not None and info.cfn_type == "AWS::KMS::Key" and info.kind == "aws":
            return Raw(info.expression("arn"))
    key = b.add_data("aws_kms_key", "_sse_key", [Attr("key_id", b.ctx.expr(value))])
    return Raw(f"{key}.arn")


# --- ECR --------------------------------------------------------------------------------


@resource(
    "AWS::ECR::Repository",
    "aws_ecr_repository",
    ref="name",
    attrs={"Arn": "arn", "RepositoryUri": "repository_url"},
    import_id="RepositoryName",
)
def ecr_repository(b: Builder) -> None:
    p = b.props
    b.name_attr("name", "RepositoryName", lowercase=True)
    p.attr("image_tag_mutability", "ImageTagMutability")
    p.boolean("force_delete", "EmptyOnDelete")
    p.block(
        "image_scanning_configuration",
        "ImageScanningConfiguration",
        lambda s: s.boolean("scan_on_push", "ScanOnPush"),
    )
    p.block(
        "encryption_configuration",
        "EncryptionConfiguration",
        lambda e: e.attrs({"encryption_type": "EncryptionType", "kms_key": "KmsKey"}),
    )
    p.tags()
    when, lifecycle = _optional(p, "LifecyclePolicy", p.take("LifecyclePolicy"))
    if lifecycle is not None:
        inner = _sub_props(b, lifecycle, "LifecyclePolicy")
        text = inner.take("LifecyclePolicyText")
        # RegistryId (another account's registry) has no counterpart: it is reported.
        inner.finish()
        if text is not None:
            b.add_resource(
                "aws_ecr_lifecycle_policy",
                "_lifecycle",
                [
                    Attr("repository", b.self_ref("name")),
                    Attr("policy", _json_text(b, text)),
                    *inner.items,
                ],
                when=when,
            )
        else:
            p.items.extend(inner.items)
    policy = p.take("RepositoryPolicyText")
    if policy is not None:
        b.add_resource(
            "aws_ecr_repository_policy",
            "_policy",
            [Attr("repository", b.self_ref("name")), Attr("policy", _json_text(b, policy))],
        )


def _json_text(b: Builder, value: Any) -> Any:
    return json_value(b.ctx, value)
