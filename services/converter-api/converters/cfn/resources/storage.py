"""S3, DynamoDB and ECR."""

from __future__ import annotations

from typing import Any, ClassVar

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props
from converters.cfn.expressions import intrinsic_name, wrap_operand
from converters.cfn.models import BLANK, Attr, Block, Raw, ResourceInfo
from converters.cfn.resources.base import ResourceConverter
from converters.cfn.resources.compute import literal_bool, optional_block


def sub_props(b: Builder, value: Any, name: str) -> Props:
    """The properties of the nested structure `name` of the resource."""
    return Props(b.ctx, value, f"{b.logical_id}.{name}", b.props.owner)


def bucket_items(b: Builder) -> list[Any]:
    """The `bucket = ...` line every bucket configuration resource starts with."""
    return [Attr("bucket", b.self_ref("id"))]


def optional_structure(r: Props, name: str, value: Any) -> tuple[str | None, Any]:
    """A structure, or `!If [C, {...}, !Ref AWS::NoValue]` as (condition, structure). Other
    functions are noted and give (None, None)."""
    when, value = r.ctx.optional_item(value)
    return when, r.literal(name, dict, value=value)


def structure_entries(r: Props, many: str, one: str) -> list[tuple[str | None, Any]]:
    """The structures of a list property plus its single-item form (Transitions and the
    older Transition), each with the condition it exists under."""
    values = list(r.literal(many, list) or [])
    single = r.take(one)
    if single is not None:
        values.append(single)
    entries = [optional_structure(r, many, value) for value in values]
    return [(when, value) for when, value in entries if value is not None]


def fill_on_demand_throughput(o: Props) -> None:
    """The on_demand_throughput block of a table or a global secondary index."""
    o.number("max_read_request_units", "MaxReadRequestUnits")
    o.number("max_write_request_units", "MaxWriteRequestUnits")


# --- S3 ---------------------------------------------------------------------------------


class LifecycleRules:
    """Fills the rule blocks of one lifecycle configuration, numbering them for the rules
    that have no Id."""

    def __init__(self) -> None:
        """No rule filled yet."""
        self.number = 0

    def fill(self, r: Props) -> None:
        """One lifecycle rule."""
        self.number += 1
        if not r.attr("id", "Id"):
            r.set("id", f"rule-{self.number}")
        r.attr("status", "Status")
        self.filter(r)
        self.abort_incomplete_upload(r)
        self.expiration(r)
        self.transitions(r)
        self.noncurrent_expiration(r)
        self.noncurrent_transitions(r)

    def filter(self, r: Props) -> None:
        """The filter block, built from the prefix, size and tag conditions."""
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

    def abort_incomplete_upload(self, r: Props) -> None:
        """The abort_incomplete_multipart_upload block."""
        name = "AbortIncompleteMultipartUpload"
        when, abort = optional_structure(r, name, r.take(name))
        if abort is None:
            return
        r.items.append(
            optional_block(
                "abort_incomplete_multipart_upload",
                [Attr("days_after_initiation", r.ctx.expr(abort.get("DaysAfterInitiation")))],
                when,
            )
        )

    def expiration(self, r: Props) -> None:
        """The expiration block, from the three expiration properties."""
        expiration: list[Any] = []
        days = r.take("ExpirationInDays")
        if days is not None:
            expiration.append(Attr("days", r.ctx.expr(days)))
        date = r.take("ExpirationDate")
        if date is not None:
            expiration.append(Attr("date", r.ctx.expr(date)))
        marker = r.take("ExpiredObjectDeleteMarker")
        if marker is not None:
            expiration.append(Attr("expired_object_delete_marker", literal_bool(marker)))
        if expiration:
            r.items.append(Block("expiration", [], expiration))

    def transitions(self, r: Props) -> None:
        """One transition block per transition."""
        for when, transition in structure_entries(r, "Transitions", "Transition"):
            items = [Attr("storage_class", r.ctx.expr(transition.get("StorageClass")))]
            if "TransitionInDays" in transition:
                items.insert(0, Attr("days", r.ctx.expr(transition["TransitionInDays"])))
            if "TransitionDate" in transition:
                items.insert(0, Attr("date", r.ctx.expr(transition["TransitionDate"])))
            r.items.append(optional_block("transition", items, when))

    def noncurrent_expiration(self, r: Props) -> None:
        """The noncurrent_version_expiration block, from the old and the new property."""
        noncurrent_days = r.take("NoncurrentVersionExpirationInDays")
        name = "NoncurrentVersionExpiration"
        when, noncurrent = optional_structure(r, name, r.take(name))
        if noncurrent_days is None and noncurrent is None:
            return
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

    def noncurrent_transitions(self, r: Props) -> None:
        """One noncurrent_version_transition block per noncurrent transition."""
        for when, transition in structure_entries(
            r, "NoncurrentVersionTransitions", "NoncurrentVersionTransition"
        ):
            items = [
                Attr("noncurrent_days", r.ctx.expr(transition.get("TransitionInDays"))),
                Attr("storage_class", r.ctx.expr(transition.get("StorageClass"))),
            ]
            if "NewerNoncurrentVersions" in transition:
                items.append(
                    Attr(
                        "newer_noncurrent_versions",
                        r.ctx.expr(transition["NewerNoncurrentVersions"]),
                    )
                )
            r.items.append(optional_block("noncurrent_version_transition", items, when))


class NotificationTarget:
    """Fills the notification blocks of one kind of target (Lambda function, queue or topic)."""

    def __init__(self, target_attr: str, target_key: str) -> None:
        """Targets read from `target_key` ("Queue") into `target_attr` ("queue_arn")."""
        self.target_attr = target_attr
        self.target_key = target_key

    def fill(self, r: Props) -> None:
        """One notification: its events, target and key filters."""
        r.list_attr("events", "Event")
        r.attr(self.target_attr, self.target_key)
        filters = r.literal("Filter", dict) or {}
        key = r.literal("Filter.S3Key", dict, value=filters.get("S3Key")) or {}
        for rule in r.literal("Filter.S3Key.Rules", list, value=key.get("Rules")) or []:
            literal_rule = r.literal("Filter.S3Key.Rules", dict, value=rule)
            if literal_rule is None:
                continue
            name = str(literal_rule.get("Name", "")).lower()
            if name in ("prefix", "suffix"):
                r.set(f"filter_{name}", r.ctx.expr(literal_rule.get("Value")))


class S3Bucket(ResourceConverter):
    """AWS::S3::Bucket as one aws_s3_bucket plus one resource per configuration, as provider
    v4+ requires."""

    cfn_type = "AWS::S3::Bucket"
    tf_type = "aws_s3_bucket"
    attrs = {
        "Arn": "arn",
        "DomainName": "bucket_domain_name",
        "RegionalDomainName": "bucket_regional_domain_name",
    }
    import_id = "BucketName"
    canned_acl: ClassVar[dict[str, str]] = {
        "Private": "private",
        "PublicRead": "public-read",
        "PublicReadWrite": "public-read-write",
        "AuthenticatedRead": "authenticated-read",
        "LogDeliveryWrite": "log-delivery-write",
        "BucketOwnerRead": "bucket-owner-read",
        "BucketOwnerFullControl": "bucket-owner-full-control",
        "AwsExecRead": "aws-exec-read",
    }
    public_access: ClassVar[tuple[tuple[str, str], ...]] = (
        ("block_public_acls", "BlockPublicAcls"),
        ("block_public_policy", "BlockPublicPolicy"),
        ("ignore_public_acls", "IgnorePublicAcls"),
        ("restrict_public_buckets", "RestrictPublicBuckets"),
    )
    notification_targets: ClassVar[tuple[tuple[str, str, str, str], ...]] = (
        ("lambda_function", "LambdaConfigurations", "lambda_function_arn", "Function"),
        ("queue", "QueueConfigurations", "queue_arn", "Queue"),
        ("topic", "TopicConfigurations", "topic_arn", "Topic"),
    )

    def prepare(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """WebsiteURL lives on the separate website configuration resource."""
        index = "[0]" if info.conditional else ""
        website = f"aws_s3_bucket_website_configuration.{info.name}_website{index}"
        info.attrs["WebsiteURL"] = "website_url"
        info.overrides["website_url"] = f'"http://${{{website}.website_endpoint}}"'

    def convert(self, b: Builder) -> None:
        """The bucket, then its configuration resources."""
        p = b.props
        p.attr("bucket", "BucketName")
        p.boolean("object_lock_enabled", "ObjectLockEnabled")
        if b.hints.get("force_destroy"):
            p.set("force_destroy", True)
        p.tags()
        self.versioning(b)
        self.encryption(b)
        self.public_access_block(b)
        self.access_control(b, self.ownership_controls(b))
        self.lifecycle(b)
        self.cors(b)
        self.website(b)
        self.logging(b)
        self.accelerate(b)
        self.object_lock(b)
        self.notification(b)

    def versioning(self, b: Builder) -> None:
        """VersioningConfiguration as aws_s3_bucket_versioning."""
        versioning = b.props.take("VersioningConfiguration")
        if versioning is None:
            return
        inner = sub_props(b, versioning, "VersioningConfiguration")
        inner.attr("status", "Status")
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_versioning",
            "_versioning",
            bucket_items(b) + [Block("versioning_configuration", [], inner.items)],
        )

    def encryption(self, b: Builder) -> None:
        """BucketEncryption as aws_s3_bucket_server_side_encryption_configuration."""
        encryption = b.props.take("BucketEncryption")
        if encryption is None:
            return
        inner = sub_props(b, encryption, "BucketEncryption")
        inner.block("rule", "ServerSideEncryptionConfiguration", self.fill_encryption_rule)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_server_side_encryption_configuration",
            "_encryption",
            bucket_items(b) + inner.items,
        )

    def fill_encryption_rule(self, r: Props) -> None:
        """One server side encryption rule."""
        r.boolean("bucket_key_enabled", "BucketKeyEnabled")
        r.block(
            "apply_server_side_encryption_by_default",
            "ServerSideEncryptionByDefault",
            self.fill_encryption_default,
        )

    def fill_encryption_default(self, d: Props) -> None:
        """The default encryption of a rule."""
        d.attrs({"sse_algorithm": "SSEAlgorithm", "kms_master_key_id": "KMSMasterKeyID"})

    def public_access_block(self, b: Builder) -> None:
        """PublicAccessBlockConfiguration as aws_s3_bucket_public_access_block."""
        public = b.props.take("PublicAccessBlockConfiguration")
        if public is None:
            return
        inner = sub_props(b, public, "PublicAccessBlockConfiguration")
        for tf_name, cfn_name in self.public_access:
            inner.boolean(tf_name, cfn_name)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_public_access_block", "_public_access", bucket_items(b) + inner.items
        )

    def ownership_controls(self, b: Builder) -> str | None:
        """OwnershipControls as aws_s3_bucket_ownership_controls. Returns its address."""
        ownership = b.props.take("OwnershipControls")
        if ownership is None:
            return None
        inner = sub_props(b, ownership, "OwnershipControls")
        inner.block("rule", "Rules", self.fill_ownership_rule)
        inner.finish()
        return b.add_resource(
            "aws_s3_bucket_ownership_controls", "_ownership", bucket_items(b) + inner.items
        )

    def fill_ownership_rule(self, r: Props) -> None:
        """One ownership rule."""
        r.attr("object_ownership", "ObjectOwnership")

    def access_control(self, b: Builder, ownership_address: str | None) -> None:
        """AccessControl (a canned ACL) as aws_s3_bucket_acl, after the ownership controls
        at `ownership_address` when there are any."""
        acl = b.props.take("AccessControl")
        if acl is None:
            return
        value = self.canned_acl.get(acl, acl) if isinstance(acl, str) else b.ctx.expr(acl)
        items = bucket_items(b) + [Attr("acl", value)]
        if ownership_address:
            # S3 rejects ACLs until the ownership setting allows them.
            items += [BLANK, Attr("depends_on", [Raw(ownership_address)])]
        else:
            b.ctx.note(
                f"{b.logical_id} sets AccessControl. S3 disables ACLs by default, so "
                "aws_s3_bucket_acl only works once OwnershipControls is ObjectWriter or "
                "BucketOwnerPreferred."
            )
        b.add_resource("aws_s3_bucket_acl", "_acl", items)

    def lifecycle(self, b: Builder) -> None:
        """LifecycleConfiguration as aws_s3_bucket_lifecycle_configuration."""
        lifecycle = b.props.take("LifecycleConfiguration")
        if lifecycle is None:
            return
        inner = sub_props(b, lifecycle, "LifecycleConfiguration")
        inner.block("rule", "Rules", LifecycleRules().fill)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_lifecycle_configuration", "_lifecycle", bucket_items(b) + inner.items
        )

    def cors(self, b: Builder) -> None:
        """CorsConfiguration as aws_s3_bucket_cors_configuration."""
        cors = b.props.take("CorsConfiguration")
        if cors is None:
            return
        inner = sub_props(b, cors, "CorsConfiguration")
        inner.block("cors_rule", "CorsRules", self.fill_cors_rule)
        inner.finish()
        b.add_resource("aws_s3_bucket_cors_configuration", "_cors", bucket_items(b) + inner.items)

    def fill_cors_rule(self, r: Props) -> None:
        """One CORS rule."""
        r.attr("id", "Id")
        r.list_attr("allowed_headers", "AllowedHeaders")
        r.list_attr("allowed_methods", "AllowedMethods")
        r.list_attr("allowed_origins", "AllowedOrigins")
        r.list_attr("expose_headers", "ExposedHeaders")
        r.number("max_age_seconds", "MaxAge")

    def website(self, b: Builder) -> None:
        """WebsiteConfiguration as aws_s3_bucket_website_configuration."""
        website = b.props.take("WebsiteConfiguration")
        if website is None:
            return
        inner = sub_props(b, website, "WebsiteConfiguration")
        index = inner.take("IndexDocument")
        if index is not None:
            inner.items.append(Block("index_document", [], [Attr("suffix", b.ctx.expr(index))]))
        error = inner.take("ErrorDocument")
        if error is not None:
            inner.items.append(Block("error_document", [], [Attr("key", b.ctx.expr(error))]))
        inner.block("redirect_all_requests_to", "RedirectAllRequestsTo", self.fill_redirect_all)
        inner.block("routing_rule", "RoutingRules", self.fill_routing_rule)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_website_configuration", "_website", bucket_items(b) + inner.items
        )

    def fill_redirect_all(self, r: Props) -> None:
        """Where a website bucket sends every request."""
        r.attrs({"host_name": "HostName", "protocol": "Protocol"})

    def fill_routing_rule(self, r: Props) -> None:
        """One website routing rule."""
        r.block("condition", "RoutingRuleCondition", self.fill_routing_condition)
        r.block("redirect", "RedirectRule", self.fill_routing_redirect)

    def fill_routing_condition(self, c: Props) -> None:
        """When a routing rule applies."""
        c.attrs(
            {
                "http_error_code_returned_equals": "HttpErrorCodeReturnedEquals",
                "key_prefix_equals": "KeyPrefixEquals",
            }
        )

    def fill_routing_redirect(self, c: Props) -> None:
        """Where a routing rule redirects to."""
        c.attrs(
            {
                "host_name": "HostName",
                "http_redirect_code": "HttpRedirectCode",
                "protocol": "Protocol",
                "replace_key_prefix_with": "ReplaceKeyPrefixWith",
                "replace_key_with": "ReplaceKeyWith",
            }
        )

    def logging(self, b: Builder) -> None:
        """LoggingConfiguration as aws_s3_bucket_logging."""
        logging = b.props.take("LoggingConfiguration")
        if logging is None:
            return
        inner = sub_props(b, logging, "LoggingConfiguration")
        if not inner.attr("target_bucket", "DestinationBucketName"):
            # Without a destination, S3 writes the logs to the bucket itself.
            inner.set("target_bucket", b.self_ref("id"))
        if not inner.attr("target_prefix", "LogFilePrefix"):
            inner.set("target_prefix", "")
        inner.finish()
        b.add_resource("aws_s3_bucket_logging", "_logging", bucket_items(b) + inner.items)

    def accelerate(self, b: Builder) -> None:
        """AccelerateConfiguration as aws_s3_bucket_accelerate_configuration."""
        accelerate = b.props.take("AccelerateConfiguration")
        if accelerate is None:
            return
        inner = sub_props(b, accelerate, "AccelerateConfiguration")
        inner.attr("status", "AccelerationStatus")
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_accelerate_configuration", "_accelerate", bucket_items(b) + inner.items
        )

    def object_lock(self, b: Builder) -> None:
        """ObjectLockConfiguration as aws_s3_bucket_object_lock_configuration."""
        lock = b.props.take("ObjectLockConfiguration")
        if lock is None:
            return
        inner = sub_props(b, lock, "ObjectLockConfiguration")
        inner.drop("ObjectLockEnabled")
        inner.block("rule", "Rule", self.fill_object_lock_rule)
        inner.finish()
        b.add_resource(
            "aws_s3_bucket_object_lock_configuration",
            "_object_lock",
            bucket_items(b) + inner.items,
        )

    def fill_object_lock_rule(self, r: Props) -> None:
        """The object lock rule."""
        r.block("default_retention", "DefaultRetention", self.fill_default_retention)

    def fill_default_retention(self, d: Props) -> None:
        """The default retention of the object lock rule."""
        d.attr("mode", "Mode")
        d.number("days", "Days")
        d.number("years", "Years")

    def notification(self, b: Builder) -> None:
        """NotificationConfiguration as aws_s3_bucket_notification."""
        notification = b.props.take("NotificationConfiguration")
        if notification is None:
            return
        inner = sub_props(b, notification, "NotificationConfiguration")
        for tf_name, cfn_name, target_attr, target_key in self.notification_targets:
            inner.block(tf_name, cfn_name, NotificationTarget(target_attr, target_key).fill)
        when, bridge = b.ctx.optional_item(inner.take("EventBridgeConfiguration"))
        bridge = inner.literal("EventBridgeConfiguration", dict, value=bridge)
        if bridge is not None:
            enabled = b.ctx.expr(literal_bool(bridge.get("EventBridgeEnabled", True)))
            if when is not None:
                enabled = Raw(f"{when} ? {wrap_operand(hcl.inline(enabled))} : false")
            inner.set("eventbridge", enabled)
        inner.finish()
        b.add_resource("aws_s3_bucket_notification", "_notification", bucket_items(b) + inner.items)


class S3BucketPolicy(ResourceConverter):
    """AWS::S3::BucketPolicy as aws_s3_bucket_policy."""

    cfn_type = "AWS::S3::BucketPolicy"
    tf_type = "aws_s3_bucket_policy"

    def convert(self, b: Builder) -> None:
        """The bucket and its policy document."""
        b.props.attr("bucket", "Bucket")
        b.props.json("policy", "PolicyDocument")


# --- DynamoDB ---------------------------------------------------------------------------


class TableEncryption:
    """Fills the server_side_encryption block of one table, which may look up its KMS key."""

    def __init__(self, b: Builder) -> None:
        """The encryption of the table `b` builds."""
        self.b = b

    def fill(self, s: Props) -> None:
        """The server_side_encryption block."""
        s.boolean("enabled", "SSEEnabled")
        s.attr("kms_key_arn", "KMSMasterKeyId", self.kms_key_arn)
        s.drop("SSEType")

    def kms_key_arn(self, value: Any) -> Any:
        """KMSMasterKeyId takes a key ID, ARN, alias name or alias ARN (and Ref of a KMS key
        gives its ID); kms_key_arn in Terraform takes only an ARN."""
        b = self.b
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


class DynamoDbTable(ResourceConverter):
    """AWS::DynamoDB::Table as aws_dynamodb_table."""

    cfn_type = "AWS::DynamoDB::Table"
    tf_type = "aws_dynamodb_table"
    ref = "name"
    attrs = {"Arn": "arn", "StreamArn": "stream_arn"}
    import_id = "TableName"

    def convert(self, b: Builder) -> None:
        """The table, its keys, attributes, indexes and settings."""
        p = b.props
        b.name_attr("name", "TableName")
        p.attr("billing_mode", "BillingMode")
        hash_key, range_key = self.key_names(p)
        if hash_key is not None:
            p.set("hash_key", b.ctx.expr(hash_key))
        if range_key is not None:
            p.set("range_key", b.ctx.expr(range_key))
        self.throughput(p)
        p.attr("table_class", "TableClass")
        p.boolean("deletion_protection_enabled", "DeletionProtectionEnabled")
        self.stream(b)
        self.attribute_definitions(b)
        p.block("global_secondary_index", "GlobalSecondaryIndexes", self.fill_global_index)
        p.block("local_secondary_index", "LocalSecondaryIndexes", self.fill_local_index)
        p.block("ttl", "TimeToLiveSpecification", self.fill_ttl)
        p.block("server_side_encryption", "SSESpecification", TableEncryption(b).fill)
        p.block(
            "point_in_time_recovery",
            "PointInTimeRecoverySpecification",
            self.fill_point_in_time_recovery,
        )
        p.block("on_demand_throughput", "OnDemandThroughput", fill_on_demand_throughput)
        p.tags()

    def stream(self, b: Builder) -> None:
        """StreamSpecification as stream_enabled and stream_view_type."""
        p = b.props
        when, stream = optional_structure(p, "StreamSpecification", p.take("StreamSpecification"))
        if stream is None:
            return
        inner = sub_props(b, stream, "StreamSpecification")
        view = b.ctx.expr(inner.take("StreamViewType"))
        if when is None:
            p.set("stream_enabled", True)
            p.set("stream_view_type", view)
        else:
            p.set("stream_enabled", Raw(when))
            p.set("stream_view_type", Raw(f"{when} ? {wrap_operand(hcl.inline(view))} : null"))
        # ResourcePolicy and Tags (for the stream) have no counterpart on the table.
        inner.finish()
        p.items.extend(inner.items)

    def attribute_definitions(self, b: Builder) -> None:
        """One attribute block per attribute definition."""
        p = b.props
        for item in p.literal("AttributeDefinitions", list) or []:
            when, attribute = optional_structure(p, "AttributeDefinitions", item)
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

    def key_schema(self, r: Props) -> list[dict[str, Any]]:
        """KeySchema entries that are literal mappings (the others are noted)."""
        keys = [
            r.literal("KeySchema", dict, value=key) for key in r.literal("KeySchema", list) or []
        ]
        return [key for key in keys if key is not None]

    def key_names(self, r: Props) -> tuple[Any, Any]:
        """The hash and range key attribute names (None when absent)."""
        hash_key = range_key = None
        for key in self.key_schema(r):
            if key.get("KeyType") == "HASH":
                hash_key = key.get("AttributeName")
            elif key.get("KeyType") == "RANGE":
                range_key = key.get("AttributeName")
        return hash_key, range_key

    def throughput(self, r: Props) -> None:
        """ProvisionedThroughput as read_capacity and write_capacity."""
        throughput = r.literal("ProvisionedThroughput", dict)
        if throughput is not None:
            r.set("read_capacity", r.ctx.expr(throughput.get("ReadCapacityUnits")))
            r.set("write_capacity", r.ctx.expr(throughput.get("WriteCapacityUnits")))

    def projection(self, r: Props) -> None:
        """The Projection of an index as projection_type and non_key_attributes."""
        projection = r.literal("Projection", dict)
        if projection is None:
            return
        if projection.get("ProjectionType"):
            r.set("projection_type", r.ctx.expr(projection["ProjectionType"]))
        else:
            r.set("projection_type", "ALL")
        if projection.get("NonKeyAttributes"):
            r.set("non_key_attributes", r.ctx.expr(projection["NonKeyAttributes"]))

    def fill_global_index(self, r: Props) -> None:
        """One global secondary index."""
        r.attr("name", "IndexName")
        for key in self.key_schema(r):
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
        self.projection(r)
        self.throughput(r)
        r.block("on_demand_throughput", "OnDemandThroughput", fill_on_demand_throughput)

    def fill_local_index(self, r: Props) -> None:
        """One local secondary index."""
        r.attr("name", "IndexName")
        _, range_key = self.key_names(r)
        if range_key is not None:
            r.set("range_key", r.ctx.expr(range_key))
        self.projection(r)

    def fill_ttl(self, t: Props) -> None:
        """The ttl block."""
        t.attr("attribute_name", "AttributeName")
        t.boolean("enabled", "Enabled")

    def fill_point_in_time_recovery(self, s: Props) -> None:
        """The point_in_time_recovery block."""
        s.boolean("enabled", "PointInTimeRecoveryEnabled")
        s.number("recovery_period_in_days", "RecoveryPeriodInDays")


# --- ECR --------------------------------------------------------------------------------


class EcrRepository(ResourceConverter):
    """AWS::ECR::Repository as aws_ecr_repository, with its lifecycle and repository
    policies as separate resources."""

    cfn_type = "AWS::ECR::Repository"
    tf_type = "aws_ecr_repository"
    ref = "name"
    attrs = {"Arn": "arn", "RepositoryUri": "repository_url"}
    import_id = "RepositoryName"

    def convert(self, b: Builder) -> None:
        """The repository, then its policies."""
        p = b.props
        b.name_attr("name", "RepositoryName", lowercase=True)
        p.attr("image_tag_mutability", "ImageTagMutability")
        p.boolean("force_delete", "EmptyOnDelete")
        p.block("image_scanning_configuration", "ImageScanningConfiguration", self.fill_scanning)
        p.block("encryption_configuration", "EncryptionConfiguration", self.fill_encryption)
        p.tags()
        self.lifecycle_policy(b)
        policy = p.take("RepositoryPolicyText")
        if policy is not None:
            b.add_resource(
                "aws_ecr_repository_policy",
                "_policy",
                [Attr("repository", b.self_ref("name")), Attr("policy", b.ctx.json(policy))],
            )

    def fill_scanning(self, s: Props) -> None:
        """The image_scanning_configuration block."""
        s.boolean("scan_on_push", "ScanOnPush")

    def fill_encryption(self, e: Props) -> None:
        """The encryption_configuration block."""
        e.attrs({"encryption_type": "EncryptionType", "kms_key": "KmsKey"})

    def lifecycle_policy(self, b: Builder) -> None:
        """LifecyclePolicy as aws_ecr_lifecycle_policy."""
        p = b.props
        when, lifecycle = optional_structure(p, "LifecyclePolicy", p.take("LifecyclePolicy"))
        if lifecycle is None:
            return
        inner = sub_props(b, lifecycle, "LifecyclePolicy")
        text = inner.take("LifecyclePolicyText")
        # RegistryId (the registry of another account) has no counterpart: it is reported.
        inner.finish()
        if text is None:
            p.items.extend(inner.items)
            return
        b.add_resource(
            "aws_ecr_lifecycle_policy",
            "_lifecycle",
            [Attr("repository", b.self_ref("name")), Attr("policy", b.ctx.json(text))]
            + inner.items,
            when=when,
        )


CONVERTERS: tuple[type[ResourceConverter], ...] = (
    S3Bucket,
    S3BucketPolicy,
    DynamoDbTable,
    EcrRepository,
)
