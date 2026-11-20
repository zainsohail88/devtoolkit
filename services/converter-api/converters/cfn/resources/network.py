"""VPC networking, load balancers, Route 53, ACM and CloudFront."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props, snippet
from converters.cfn.context import Context, ResourceInfo, intrinsic_name
from converters.cfn.hcl import Attr, Block, Raw
from converters.cfn.resources import resource
from converters.cfn.resources.messaging import operand

# --- helpers ----------------------------------------------------------------------------


def _bool(ctx: Context, value: Any) -> Any:
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return ctx.expr(value)


def _number(ctx: Context, value: Any) -> Any:
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    return ctx.expr(value)


def _typed(ctx: Context, value: Any, kind: str) -> Any:
    if kind == "bool":
        return _bool(ctx, value)
    if kind == "number":
        return _number(ctx, value)
    return ctx.expr(value)


def _as_list(ctx: Context, value: Any) -> Any:
    """A single CloudFormation value where Terraform wants a list."""
    converted = ctx.expr(value)
    if isinstance(value, list) or isinstance(converted, list):
        return converted
    if isinstance(value, dict) and intrinsic_name(value) in ("Fn::GetAZs", "Fn::Split"):
        return converted
    return [converted]


def _blocks(p: Props, tf_name: str, cfn_name: str, fill: Callable[[Props], None]) -> None:
    """A list of structures as blocks, like Props.block, where `!If [Cond, [...],
    !Ref AWS::NoValue]` around the whole list gives one conditional block per item."""
    value = p.take(cfn_name)
    if value is None:
        return
    items, _ = _split_if(p.ctx, value)
    p.add_blocks(tf_name, items, fill, f"{p.where}.{cfn_name}")


def _sub(p: Props, value: Any, name: str) -> Props:
    return Props(p.ctx, value, f"{p.where}.{name}", p.owner)


def _refers_to(value: Any, logical_id: str) -> bool:
    """Whether a value is `!Ref logical_id` or `!GetAtt logical_id.<attr>`."""
    if not isinstance(value, dict):
        return False
    if value.get("Ref") == logical_id:
        return True
    get_att = value.get("Fn::GetAtt")
    if isinstance(get_att, str):
        return get_att.split(".", 1)[0] == logical_id
    return isinstance(get_att, list) and bool(get_att) and get_att[0] == logical_id


def _ipv6_blocks(info: ResourceInfo, resource_: dict[str, Any]) -> None:
    # CloudFormation returns a list; the provider holds the one block as a string.
    info.attrs["Ipv6CidrBlocks"] = "ipv6_cidr_blocks"
    info.overrides["ipv6_cidr_blocks"] = f"[{info.expression('ipv6_cidr_block')}]"


# --- VPC basics -------------------------------------------------------------------------


@resource(
    "AWS::EC2::VPC",
    "aws_vpc",
    attrs={
        "CidrBlock": "cidr_block",
        "DefaultNetworkAcl": "default_network_acl_id",
        "DefaultSecurityGroup": "default_security_group_id",
        "VpcId": "id",
    },
    prepare=_ipv6_blocks,
)
def vpc(b: Builder) -> None:
    p = b.props
    p.attr("cidr_block", "CidrBlock")
    p.attr("ipv4_ipam_pool_id", "Ipv4IpamPoolId")
    p.number("ipv4_netmask_length", "Ipv4NetmaskLength")
    p.boolean("enable_dns_support", "EnableDnsSupport")
    p.boolean("enable_dns_hostnames", "EnableDnsHostnames")
    p.attr("instance_tenancy", "InstanceTenancy")
    p.tags()


@resource(
    "AWS::EC2::Subnet",
    "aws_subnet",
    attrs={
        "AvailabilityZone": "availability_zone",
        "AvailabilityZoneId": "availability_zone_id",
        "CidrBlock": "cidr_block",
        "OutpostArn": "outpost_arn",
        "SubnetId": "id",
        "VpcId": "vpc_id",
    },
    prepare=_ipv6_blocks,
)
def subnet(b: Builder) -> None:
    p = b.props
    p.attr("vpc_id", "VpcId")
    p.attr("cidr_block", "CidrBlock")
    p.attr("availability_zone", "AvailabilityZone")
    p.attr("availability_zone_id", "AvailabilityZoneId")
    p.boolean("map_public_ip_on_launch", "MapPublicIpOnLaunch")
    p.attr("ipv6_cidr_block", "Ipv6CidrBlock")
    p.boolean("assign_ipv6_address_on_creation", "AssignIpv6AddressOnCreation")
    p.boolean("ipv6_native", "Ipv6Native")
    p.boolean("enable_dns64", "EnableDns64")
    p.attr("ipv4_ipam_pool_id", "Ipv4IpamPoolId")
    p.number("ipv4_netmask_length", "Ipv4NetmaskLength")
    p.attr("ipv6_ipam_pool_id", "Ipv6IpamPoolId")
    p.number("ipv6_netmask_length", "Ipv6NetmaskLength")
    p.attr("outpost_arn", "OutpostArn")
    options = p.take("PrivateDnsNameOptionsOnLaunch")
    if options is not None:
        # Flat attributes in Terraform.
        inner = _sub(p, options, "PrivateDnsNameOptionsOnLaunch")
        inner.attr("private_dns_hostname_type_on_launch", "HostnameType")
        inner.boolean("enable_resource_name_dns_a_record_on_launch", "EnableResourceNameDnsARecord")
        inner.boolean(
            "enable_resource_name_dns_aaaa_record_on_launch", "EnableResourceNameDnsAAAARecord"
        )
        inner.finish()
        p.items.extend(inner.items)
    p.tags()


@resource("AWS::EC2::InternetGateway", "aws_internet_gateway", attrs={"InternetGatewayId": "id"})
def internet_gateway(b: Builder) -> None:
    b.props.tags()


@resource("AWS::EC2::EgressOnlyInternetGateway", "aws_egress_only_internet_gateway")
def egress_only_internet_gateway(b: Builder) -> None:
    b.props.attr("vpc_id", "VpcId")
    b.props.tags()


def _attachment_prepare(info: ResourceInfo, resource_: dict[str, Any]) -> None:
    properties = resource_.get("Properties") or {}
    if properties.get("VpnGatewayId") is not None and properties.get("InternetGatewayId") is None:
        info.tf_type = "aws_vpn_gateway_attachment"


@resource(
    "AWS::EC2::VPCGatewayAttachment",
    "aws_internet_gateway_attachment",
    prepare=_attachment_prepare,
)
def vpc_gateway_attachment(b: Builder) -> None:
    p = b.props
    if b.info.tf_type == "aws_vpn_gateway_attachment":
        p.attr("vpc_id", "VpcId")
        p.attr("vpn_gateway_id", "VpnGatewayId")
        return
    p.attr("internet_gateway_id", "InternetGatewayId")
    p.attr("vpc_id", "VpcId")


@resource("AWS::EC2::RouteTable", "aws_route_table", attrs={"RouteTableId": "id"})
def route_table(b: Builder) -> None:
    b.props.attr("vpc_id", "VpcId")
    b.props.tags()


@resource("AWS::EC2::Route", "aws_route")
def route(b: Builder) -> None:
    b.props.attrs(
        {
            "route_table_id": "RouteTableId",
            "destination_cidr_block": "DestinationCidrBlock",
            "destination_ipv6_cidr_block": "DestinationIpv6CidrBlock",
            "destination_prefix_list_id": "DestinationPrefixListId",
            "gateway_id": "GatewayId",
            "nat_gateway_id": "NatGatewayId",
            "network_interface_id": "NetworkInterfaceId",
            "transit_gateway_id": "TransitGatewayId",
            "vpc_peering_connection_id": "VpcPeeringConnectionId",
            "vpc_endpoint_id": "VpcEndpointId",
            "egress_only_gateway_id": "EgressOnlyInternetGatewayId",
            "carrier_gateway_id": "CarrierGatewayId",
            "local_gateway_id": "LocalGatewayId",
            "core_network_arn": "CoreNetworkArn",
        }
    )
    if b.props.has("InstanceId"):
        b.ctx.note(
            f"{b.logical_id} routes to an instance: the aws provider routes to its network "
            "interface instead (network_interface_id = aws_instance.<name>."
            "primary_network_interface_id)"
        )


@resource("AWS::EC2::SubnetRouteTableAssociation", "aws_route_table_association")
def subnet_route_table_association(b: Builder) -> None:
    b.props.attr("subnet_id", "SubnetId")
    b.props.attr("route_table_id", "RouteTableId")


@resource(
    "AWS::EC2::EIP",
    "aws_eip",
    ref="public_ip",
    attrs={"AllocationId": "allocation_id", "PublicIp": "public_ip"},
)
def eip(b: Builder) -> None:
    p = b.props
    p.attr("domain", "Domain")
    p.attr("instance", "InstanceId")
    p.attr("network_border_group", "NetworkBorderGroup")
    p.attr("public_ipv4_pool", "PublicIpv4Pool")
    p.attr("ipam_pool_id", "IpamPoolId")
    p.attr("address", "Address")
    p.tags()


@resource("AWS::EC2::EIPAssociation", "aws_eip_association")
def eip_association(b: Builder) -> None:
    b.props.attrs(
        {
            "allocation_id": "AllocationId",
            "public_ip": "EIP",
            "instance_id": "InstanceId",
            "network_interface_id": "NetworkInterfaceId",
            "private_ip_address": "PrivateIpAddress",
        }
    )


@resource("AWS::EC2::NatGateway", "aws_nat_gateway", attrs={"NatGatewayId": "id"})
def nat_gateway(b: Builder) -> None:
    p = b.props
    p.attr("allocation_id", "AllocationId")
    p.attr("subnet_id", "SubnetId")
    p.attr("connectivity_type", "ConnectivityType")
    p.attr("private_ip", "PrivateIpAddress")
    p.attr("secondary_allocation_ids", "SecondaryAllocationIds")
    p.attr("secondary_private_ip_addresses", "SecondaryPrivateIpAddresses")
    p.number("secondary_private_ip_address_count", "SecondaryPrivateIpAddressCount")
    p.tags()


@resource(
    "AWS::EC2::VPCEndpoint",
    "aws_vpc_endpoint",
    attrs={"Id": "id", "NetworkInterfaceIds": "network_interface_ids"},
)
def vpc_endpoint(b: Builder) -> None:
    p = b.props
    p.attr("vpc_id", "VpcId")
    p.attr("service_name", "ServiceName")
    p.attr("vpc_endpoint_type", "VpcEndpointType")
    p.boolean("private_dns_enabled", "PrivateDnsEnabled")
    p.attr("ip_address_type", "IpAddressType")
    p.list_attr("route_table_ids", "RouteTableIds")
    p.list_attr("subnet_ids", "SubnetIds")
    p.list_attr("security_group_ids", "SecurityGroupIds")
    p.json("policy", "PolicyDocument")
    p.block(
        "dns_options",
        "DnsOptions",
        lambda d: (
            d.attr("dns_record_ip_type", "DnsRecordIpType"),
            d.boolean(
                "private_dns_only_for_inbound_resolver_endpoint",
                "PrivateDnsOnlyForInboundResolverEndpoint",
            ),
        ),
    )
    p.tags()


# --- security groups --------------------------------------------------------------------

_ALL_PROTOCOLS = ("-1", "all")
_ICMP = ("icmp", "icmpv6", "1", "58")
_ALLOW_ALL_EGRESS = {"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}
_EGRESS_COMMENT = (
    "CloudFormation adds this allow-all egress rule when the template has none;\n"
    "Terraform removes it, so it is written out."
)


def _protocol(value: Any) -> str | None:
    """The protocol as lowercase text when it is a literal, else None."""
    if isinstance(value, str | int) and not isinstance(value, bool):
        return str(value).lower()
    return None


def _rule_target(egress: bool) -> tuple[str, str]:
    """CloudFormation names of the other side of a rule: (security group, prefix list)."""
    if egress:
        return "DestinationSecurityGroupId", "DestinationPrefixListId"
    return "SourceSecurityGroupId", "SourcePrefixListId"


def _inline_rule(r: Props, egress: bool) -> None:
    """One SecurityGroupIngress/SecurityGroupEgress entry as an inline ingress/egress block."""
    ctx = r.ctx
    protocol = r.take("IpProtocol", "-1")
    literal = _protocol(protocol)
    from_port, to_port = r.take("FromPort"), r.take("ToPort")
    if literal in _ALL_PROTOCOLS:
        r.set("from_port", 0)
        r.set("to_port", 0)
        r.set("protocol", "-1")
    else:
        # Leaving the ports out means all of them (or every ICMP type and code).
        icmp = literal in _ICMP
        first = (-1 if icmp else 0) if from_port is None else _number(ctx, from_port)
        if to_port is not None:
            last = _number(ctx, to_port)
        elif from_port is not None:
            last = first
        else:
            last = -1 if icmp else 65535
        r.set("from_port", first)
        r.set("to_port", last)
        r.set("protocol", literal if literal is not None else ctx.expr(protocol))
    group, prefix = _rule_target(egress)
    r.attr("cidr_blocks", "CidrIp", lambda v: _as_list(ctx, v))
    r.attr("ipv6_cidr_blocks", "CidrIpv6", lambda v: _as_list(ctx, v))
    r.attr("security_groups", group, lambda v: _as_list(ctx, v))
    r.attr("prefix_list_ids", prefix, lambda v: _as_list(ctx, v))
    r.attr("description", "Description")


def _rule_items(r: Props, egress: bool) -> None:
    """One rule as an aws_vpc_security_group_ingress_rule / egress_rule body."""
    ctx = r.ctx
    protocol = r.take("IpProtocol", "-1")
    literal = _protocol(protocol)
    from_port, to_port = r.take("FromPort"), r.take("ToPort")
    if literal in _ALL_PROTOCOLS:
        # With every protocol the API takes no ports.
        r.set("ip_protocol", "-1")
    else:
        r.set("ip_protocol", literal if literal is not None else ctx.expr(protocol))
        if from_port is not None:
            r.set("from_port", _number(ctx, from_port))
        if to_port is not None:
            r.set("to_port", _number(ctx, to_port))
    group, prefix = _rule_target(egress)
    r.attr("cidr_ipv4", "CidrIp")
    r.attr("cidr_ipv6", "CidrIpv6")
    r.attr("referenced_security_group_id", group)
    r.attr("prefix_list_id", prefix)
    r.attr("description", "Description")


def _has_standalone_rules(ctx: Context, logical_id: str) -> bool:
    for other in (ctx.template.get("Resources") or {}).values():
        if other.get("Type") in ("AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress"):
            if _refers_to((other.get("Properties") or {}).get("GroupId"), logical_id):
                return True
    return False


@resource(
    "AWS::EC2::SecurityGroup",
    "aws_security_group",
    attrs={"GroupId": "id", "VpcId": "vpc_id"},
)
def security_group(b: Builder) -> None:
    p = b.props
    p.attr("name", "GroupName")
    p.attr("description", "GroupDescription")
    p.attr("vpc_id", "VpcId")
    p.tags()
    ingress, _ = _split_if(b.ctx, p.take("SecurityGroupIngress"))
    egress, absent = _split_if(b.ctx, p.take("SecurityGroupEgress"))
    # The rule CloudFormation keeps when the template sets no egress (or, for
    # `!If [Cond, [...], !Ref AWS::NoValue]`, when the condition picks NoValue).
    default: Any = None
    if egress is None:
        default = _ALLOW_ALL_EGRESS
        egress = [default]
    elif absent is not None:
        default = absent(_ALLOW_ALL_EGRESS)
        egress = [*egress, default]

    if _has_standalone_rules(b.ctx, b.logical_id):
        # Inline rules and separate rule resources on one group overwrite each other in
        # Terraform, so this group's own rules become separate resources too.
        _standalone_rules(b, ingress, egress, default)
        return
    if ingress is not None:
        p.add_blocks("ingress", ingress, lambda r: _inline_rule(r, False), f"{p.where}.Ingress")
    for rule in egress if isinstance(egress, list) else [egress]:
        if rule is default:
            p.comment(_EGRESS_COMMENT)
        p.add_blocks("egress", rule, lambda r: _inline_rule(r, True), f"{p.where}.Egress")


def _split_if(ctx: Context, rules: Any) -> tuple[Any, Callable[[Any], Any] | None]:
    """`!If [Cond, [rules], !Ref AWS::NoValue]` (either way round) as one `!If` per rule, so
    each becomes a conditional block or resource. Also returns how to wrap a rule that holds
    only when the property is absent. Anything else is returned as it is (and None)."""
    if not (isinstance(rules, dict) and intrinsic_name(rules) == "Fn::If"):
        return rules, None
    args = rules["Fn::If"]
    if not isinstance(args, list) or len(args) != 3:
        return rules, None
    name, yes, no = args
    no_value = {"Ref": "AWS::NoValue"}
    if ctx.is_no_value(no) and not ctx.is_no_value(yes):
        present = yes if isinstance(yes, list) else [yes]
        return [{"Fn::If": [name, rule, no_value]} for rule in present], (
            lambda rule: {"Fn::If": [name, no_value, rule]}
        )
    if ctx.is_no_value(yes) and not ctx.is_no_value(no):
        present = no if isinstance(no, list) else [no]
        return [{"Fn::If": [name, no_value, rule]} for rule in present], (
            lambda rule: {"Fn::If": [name, rule, no_value]}
        )
    return rules, None


def _standalone_rules(b: Builder, ingress: Any, egress: Any, default: Any) -> None:
    for kind, rules, is_egress in (("ingress", ingress, False), ("egress", egress, True)):
        if rules is None:
            continue
        if not isinstance(rules, list):
            b.ctx.note(
                f"{b.logical_id}: the {kind} rules are computed by a function and could not be "
                "turned into rule resources; add them by hand"
            )
            continue
        for number, item in enumerate(rules, start=1):
            if b.ctx.is_no_value(item):
                continue
            where = f"{b.logical_id}.{kind}[{number - 1}]"
            # `!If [Cond, {...}, !Ref AWS::NoValue]`: a rule resource counted on Cond.
            when, rule = b.ctx.optional_item(item)
            if not isinstance(rule, dict) or intrinsic_name(rule) is not None:
                b.ctx.note(
                    f"{b.props.owner}: '{where}' is computed by a function and was not "
                    f"converted; add the aws_vpc_security_group_{kind}_rule by hand"
                )
                b.props.comment(f"{kind}[{number - 1}] was not converted: {snippet(item)}")
                continue
            inner = Props(b.ctx, rule, where, b.props.owner)
            items: list[Any] = [Attr("security_group_id", b.self_ref("id"))]
            if is_egress and item is default:
                items.insert(0, hcl.Comment(_EGRESS_COMMENT))
            _rule_items(inner, is_egress)
            inner.finish()
            b.add_resource(
                f"aws_vpc_security_group_{kind}_rule",
                f"_{kind}_{number}",
                items + inner.items,
                when=when,
            )


def _standalone_rule(b: Builder, egress: bool) -> None:
    b.props.attr("security_group_id", "GroupId")
    _rule_items(b.props, egress)


@resource("AWS::EC2::SecurityGroupIngress", "aws_vpc_security_group_ingress_rule")
def security_group_ingress(b: Builder) -> None:
    _standalone_rule(b, egress=False)


@resource("AWS::EC2::SecurityGroupEgress", "aws_vpc_security_group_egress_rule")
def security_group_egress(b: Builder) -> None:
    _standalone_rule(b, egress=True)


# --- network ACLs and flow logs ---------------------------------------------------------


@resource("AWS::EC2::NetworkAcl", "aws_network_acl")
def network_acl(b: Builder) -> None:
    b.props.attr("vpc_id", "VpcId")
    b.props.tags()


@resource("AWS::EC2::NetworkAclEntry", "aws_network_acl_rule")
def network_acl_entry(b: Builder) -> None:
    p, ctx = b.props, b.ctx
    p.attr("network_acl_id", "NetworkAclId")
    p.number("rule_number", "RuleNumber")
    p.boolean("egress", "Egress")
    protocol = p.take("Protocol")
    if protocol is not None:
        literal = _protocol(protocol)
        p.set("protocol", literal if literal is not None else ctx.expr(protocol))
    p.attr("rule_action", "RuleAction")
    p.attr("cidr_block", "CidrBlock")
    p.attr("ipv6_cidr_block", "Ipv6CidrBlock")
    ports = p.take("PortRange")
    if isinstance(ports, dict):
        for key, tf_name in (("From", "from_port"), ("To", "to_port")):
            if ports.get(key) is not None:
                p.set(tf_name, _number(ctx, ports[key]))
    icmp = p.take("Icmp")
    if isinstance(icmp, dict):
        for key, tf_name in (("Type", "icmp_type"), ("Code", "icmp_code")):
            if icmp.get(key) is not None:
                p.set(tf_name, _number(ctx, icmp[key]))


@resource("AWS::EC2::SubnetNetworkAclAssociation", "aws_network_acl_association")
def subnet_network_acl_association(b: Builder) -> None:
    b.props.attr("network_acl_id", "NetworkAclId")
    b.props.attr("subnet_id", "SubnetId")


_FLOW_LOG_TARGETS = {
    "VPC": "vpc_id",
    "Subnet": "subnet_id",
    "NetworkInterface": "eni_id",
    "TransitGateway": "transit_gateway_id",
    "TransitGatewayAttachment": "transit_gateway_attachment_id",
    "RegionalNatGateway": "regional_nat_gateway_id",
}
_LOG_GROUP_ARN = "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:${Name}"


@resource("AWS::EC2::FlowLog", "aws_flow_log")
def flow_log(b: Builder) -> None:
    p, ctx = b.props, b.ctx
    kind = p.peek("ResourceType")
    if isinstance(kind, str) and kind in _FLOW_LOG_TARGETS and p.has("ResourceId"):
        p.drop("ResourceType")
        p.attr(_FLOW_LOG_TARGETS[kind], "ResourceId")
    p.attr("traffic_type", "TrafficType")
    p.attr("log_destination_type", "LogDestinationType")
    p.attr("log_destination", "LogDestination")
    group = p.take("LogGroupName")
    if group is not None:
        # The provider only takes the log group's ARN.
        p.set("log_destination", ctx.expr({"Fn::Sub": [_LOG_GROUP_ARN, {"Name": group}]}))
    p.attr("iam_role_arn", "DeliverLogsPermissionArn")
    p.attr("deliver_cross_account_role", "DeliverCrossAccountRole")
    p.attr("log_format", "LogFormat")
    p.number("max_aggregation_interval", "MaxAggregationInterval")
    p.block(
        "destination_options",
        "DestinationOptions",
        lambda d: (
            d.attr("file_format", "FileFormat"),
            d.boolean("hive_compatible_partitions", "HiveCompatiblePartitions"),
            d.boolean("per_hour_partition", "PerHourPartition"),
        ),
    )
    p.tags()


# --- load balancers ---------------------------------------------------------------------

# LoadBalancerAttributes keys -> (attribute, kind); kind is "bool", "number" or "string".
_LB_ATTRIBUTES = {
    "deletion_protection.enabled": ("enable_deletion_protection", "bool"),
    "idle_timeout.timeout_seconds": ("idle_timeout", "number"),
    "routing.http2.enabled": ("enable_http2", "bool"),
    "routing.http.drop_invalid_header_fields.enabled": ("drop_invalid_header_fields", "bool"),
    "routing.http.desync_mitigation_mode": ("desync_mitigation_mode", "string"),
    "routing.http.preserve_host_header.enabled": ("preserve_host_header", "bool"),
    "routing.http.xff_client_port.enabled": ("enable_xff_client_port", "bool"),
    "routing.http.xff_header_processing.mode": ("xff_header_processing_mode", "string"),
    "routing.http.x_amzn_tls_version_and_cipher_suite.enabled": (
        "enable_tls_version_and_cipher_suite_headers",
        "bool",
    ),
    "load_balancing.cross_zone.enabled": ("enable_cross_zone_load_balancing", "bool"),
    "waf.fail_open.enabled": ("enable_waf_fail_open", "bool"),
    "client_keep_alive.seconds": ("client_keep_alive", "number"),
    "zonal_shift.config.enabled": ("enable_zonal_shift", "bool"),
    "dns_record.client_routing_policy": ("dns_record_client_routing_policy", "string"),
}
# Attribute key prefixes that become a log block.
_LB_LOGS = {
    "access_logs.s3.": "access_logs",
    "connection_logs.s3.": "connection_logs",
    "health_check_logs.s3.": "health_check_logs",
}
_TARGET_GROUP_ATTRIBUTES = {
    "deregistration_delay.timeout_seconds": ("deregistration_delay", "string"),
    "deregistration_delay.connection_termination.enabled": ("connection_termination", "bool"),
    "slow_start.duration_seconds": ("slow_start", "number"),
    "load_balancing.algorithm.type": ("load_balancing_algorithm_type", "string"),
    "load_balancing.algorithm.anomaly_mitigation": ("load_balancing_anomaly_mitigation", "string"),
    "load_balancing.cross_zone.enabled": ("load_balancing_cross_zone_enabled", "string"),
    "lambda.multi_value_headers.enabled": ("lambda_multi_value_headers_enabled", "bool"),
    "proxy_protocol_v2.enabled": ("proxy_protocol_v2", "bool"),
    "preserve_client_ip.enabled": ("preserve_client_ip", "string"),
}
_STICKINESS = {
    "stickiness.enabled": ("enabled", "bool"),
    "stickiness.type": ("type", "string"),
    "stickiness.lb_cookie.duration_seconds": ("cookie_duration", "number"),
    "stickiness.app_cookie.duration_seconds": ("cookie_duration", "number"),
    "stickiness.app_cookie.cookie_name": ("cookie_name", "string"),
}
_HEALTH_CHECK = {
    "HealthCheckEnabled": ("enabled", "bool"),
    "HealthCheckIntervalSeconds": ("interval", "number"),
    "HealthCheckPath": ("path", "string"),
    "HealthCheckPort": ("port", "string"),
    "HealthCheckProtocol": ("protocol", "string"),
    "HealthCheckTimeoutSeconds": ("timeout", "number"),
    "HealthyThresholdCount": ("healthy_threshold", "number"),
    "UnhealthyThresholdCount": ("unhealthy_threshold", "number"),
}


def _key_values(p: Props, cfn_name: str) -> list[tuple[str, Any]]:
    """[{Key, Value}] attributes as pairs. A computed list is reported and skipped."""
    raw = p.take(cfn_name)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(
        isinstance(item, dict) and isinstance(item.get("Key"), str) for item in raw
    ):
        p.ctx.note(f"{p.owner}: '{p.where}.{cfn_name}' is computed and was not converted")
        p.comment(f"{cfn_name} was not converted (computed)")
        return []
    return [(item["Key"], item.get("Value")) for item in raw]


def _unknown_attribute(p: Props, cfn_name: str, key: str, value: Any) -> None:
    p.ctx.note(f"{p.owner}: {cfn_name} '{key}' was not converted; set it by hand")
    p.comment(f"{cfn_name} {snippet(key)} = {hcl.inline(p.ctx.expr(value))} was not converted")


@resource(
    "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "aws_lb",
    ref="arn",
    attrs={
        "CanonicalHostedZoneID": "zone_id",
        "DNSName": "dns_name",
        "LoadBalancerArn": "arn",
        "LoadBalancerFullName": "arn_suffix",
        "LoadBalancerName": "name",
        "SecurityGroups": "security_groups",
    },
)
def load_balancer(b: Builder) -> None:
    p, ctx = b.props, b.ctx
    p.attr("name", "Name")
    p.attr("load_balancer_type", "Type")
    scheme = p.take("Scheme")
    if scheme is not None:
        if isinstance(scheme, str):
            p.set("internal", scheme == "internal")
        else:
            p.set("internal", Raw(f'{operand(ctx.expr(scheme))} == "internal"'))
    p.attr("ip_address_type", "IpAddressType")
    p.list_attr("subnets", "Subnets")
    p.list_attr("security_groups", "SecurityGroups")
    p.attr(
        "enforce_security_group_inbound_rules_on_private_link_traffic",
        "EnforceSecurityGroupInboundRulesOnPrivateLinkTraffic",
    )
    logs: dict[str, list[Attr]] = {}
    for key, value in _key_values(p, "LoadBalancerAttributes"):
        if key in _LB_ATTRIBUTES:
            tf_name, kind = _LB_ATTRIBUTES[key]
            p.set(tf_name, _typed(ctx, value, kind))
            continue
        prefix = next((pre for pre in _LB_LOGS if key.startswith(pre)), None)
        field = key[len(prefix) :] if prefix else ""
        if prefix and field in ("enabled", "bucket", "prefix"):
            kind = "bool" if field == "enabled" else "string"
            logs.setdefault(_LB_LOGS[prefix], []).append(Attr(field, _typed(ctx, value, kind)))
            continue
        _unknown_attribute(p, "LoadBalancerAttributes", key, value)
    for name, items in logs.items():
        if not any(item.name == "bucket" for item in items):
            # The provider requires a bucket; with none, logging stays off as in AWS.
            items.append(Attr("bucket", ""))
        p.items.append(Block(name, [], sorted(items, key=lambda a: a.name)))
    p.block(
        "subnet_mapping",
        "SubnetMappings",
        lambda m: m.attrs(
            {
                "subnet_id": "SubnetId",
                "allocation_id": "AllocationId",
                "private_ipv4_address": "PrivateIPv4Address",
                "ipv6_address": "IPv6Address",
            }
        ),
    )
    p.tags()


@resource(
    "AWS::ElasticLoadBalancingV2::TargetGroup",
    "aws_lb_target_group",
    ref="arn",
    attrs={
        "LoadBalancerArns": "load_balancer_arns",
        "TargetGroupArn": "arn",
        "TargetGroupFullName": "arn_suffix",
        "TargetGroupName": "name",
    },
)
def target_group(b: Builder) -> None:
    p, ctx = b.props, b.ctx
    p.attr("name", "Name")
    p.number("port", "Port")
    protocol = p.peek("Protocol")
    target_type = p.peek("TargetType")
    p.attr("protocol", "Protocol")
    p.attr("protocol_version", "ProtocolVersion")
    p.attr("vpc_id", "VpcId")
    p.attr("target_type", "TargetType")
    p.attr("ip_address_type", "IpAddressType")

    attributes = _key_values(p, "TargetGroupAttributes")
    sticky_type = dict(attributes).get("stickiness.type", "lb_cookie")
    # Each cookie type has its own duration; Terraform has one cookie_duration.
    duration = (
        "stickiness.app_cookie.duration_seconds"
        if sticky_type == "app_cookie"
        else "stickiness.lb_cookie.duration_seconds"
    )
    stickiness: list[Attr] = []
    for key, value in attributes:
        if key in _TARGET_GROUP_ATTRIBUTES:
            tf_name, kind = _TARGET_GROUP_ATTRIBUTES[key]
            p.set(tf_name, _typed(ctx, value, kind))
        elif key in _STICKINESS and (not key.endswith(".duration_seconds") or key == duration):
            tf_name, kind = _STICKINESS[key]
            stickiness.append(Attr(tf_name, _typed(ctx, value, kind)))
        else:
            _unknown_attribute(p, "TargetGroupAttributes", key, value)

    health: list[Any] = []
    for cfn_name, (tf_name, kind) in _HEALTH_CHECK.items():
        value = p.take(cfn_name)
        if value is not None:
            health.append(Attr(tf_name, _typed(ctx, value, kind)))
    matcher = p.literal("Matcher", dict)
    if matcher is not None:
        code = matcher.get("HttpCode", matcher.get("GrpcCode"))
        if code is not None:
            health.append(Attr("matcher", ctx.expr(code)))
    if health:
        health.extend(_health_check_defaults(health, protocol, target_type))
        p.items.append(Block("health_check", [], health))
    if stickiness:
        # The provider turns stickiness on unless told otherwise; AWS leaves it off.
        if not any(item.name == "enabled" for item in stickiness):
            stickiness.insert(0, Attr("enabled", False))
        if not any(item.name == "type" for item in stickiness):
            stickiness.append(Attr("type", "lb_cookie"))
        p.items.append(Block("stickiness", [], stickiness))
    p.tags()

    targets = p.take("Targets")
    if isinstance(targets, list):
        for number, target in enumerate(targets, start=1):
            inner = Props(ctx, target, f"{b.logical_id}.Targets[{number - 1}]", p.owner)
            inner.set("target_group_arn", b.self_ref("arn"))
            inner.attr("target_id", "Id")
            inner.number("port", "Port")
            inner.attr("availability_zone", "AvailabilityZone")
            inner.finish()
            b.add_resource("aws_lb_target_group_attachment", f"_target_{number}", inner.items)
    elif targets is not None:
        ctx.note(f"{b.logical_id}: Targets is computed and was not converted; attach them by hand")


def _health_check_defaults(health: list[Any], protocol: Any, target_type: Any) -> list[Attr]:
    """Settings the provider defaults differently from AWS, for a health_check block that
    sets only some of them: thresholds (and interval for Lambda and GENEVE)."""
    given = {item.name for item in health}
    lambda_target = target_type == "lambda"
    defaults = {
        "healthy_threshold": 5,
        "unhealthy_threshold": 5 if lambda_target else 2,
    }
    if lambda_target:
        defaults["interval"] = 35
    elif protocol == "GENEVE":
        defaults["interval"] = 10
    return [Attr(name, value) for name, value in defaults.items() if name not in given]


def _action(r: Props) -> None:
    """A listener (rule) action as a default_action / action block."""
    r.attr("type", "Type")
    r.number("order", "Order")
    forward = r.peek("ForwardConfig")
    if isinstance(forward, dict) and forward.get("TargetGroupStickinessConfig") is not None:
        # Stickiness only exists in the forward block, and Terraform takes the group either
        # there or in target_group_arn; CloudFormation lets both name the same one.
        r.drop("TargetGroupArn")
    elif r.attr("target_group_arn", "TargetGroupArn"):
        r.drop("ForwardConfig")
    r.block("forward", "ForwardConfig", _forward)
    r.block(
        "redirect",
        "RedirectConfig",
        lambda c: c.attrs(
            {
                "host": "Host",
                "path": "Path",
                "port": "Port",
                "protocol": "Protocol",
                "query": "Query",
                "status_code": "StatusCode",
            }
        ),
    )
    r.block(
        "fixed_response",
        "FixedResponseConfig",
        lambda c: c.attrs(
            {
                "content_type": "ContentType",
                "message_body": "MessageBody",
                "status_code": "StatusCode",
            }
        ),
    )
    r.block("authenticate_cognito", "AuthenticateCognitoConfig", _cognito)
    r.block("authenticate_oidc", "AuthenticateOidcConfig", _oidc)


def _forward(c: Props) -> None:
    _blocks(
        c,
        "target_group",
        "TargetGroups",
        lambda g: (g.attr("arn", "TargetGroupArn"), g.number("weight", "Weight")),
    )
    c.block(
        "stickiness",
        "TargetGroupStickinessConfig",
        lambda s: (s.boolean("enabled", "Enabled"), s.number("duration", "DurationSeconds")),
    )


def _auth_common(c: Props) -> None:
    c.attr("authentication_request_extra_params", "AuthenticationRequestExtraParams")
    c.attr("on_unauthenticated_request", "OnUnauthenticatedRequest")
    c.attr("scope", "Scope")
    c.attr("session_cookie_name", "SessionCookieName")
    c.number("session_timeout", "SessionTimeout")


def _cognito(c: Props) -> None:
    c.attrs(
        {
            "user_pool_arn": "UserPoolArn",
            "user_pool_client_id": "UserPoolClientId",
            "user_pool_domain": "UserPoolDomain",
        }
    )
    _auth_common(c)


def _oidc(c: Props) -> None:
    c.attrs(
        {
            "authorization_endpoint": "AuthorizationEndpoint",
            "client_id": "ClientId",
            "client_secret": "ClientSecret",
            "issuer": "Issuer",
            "token_endpoint": "TokenEndpoint",
            "user_info_endpoint": "UserInfoEndpoint",
        }
    )
    c.drop("UseExistingClientSecret")  # Terraform always sends the secret it is given.
    _auth_common(c)


def _certificate_arns(b: Builder) -> list[Any]:
    certificates = b.props.take("Certificates")
    if certificates is None:
        return []
    if not isinstance(certificates, list):
        b.ctx.note(f"{b.logical_id}: Certificates is computed and was not converted")
        return []
    return [
        c["CertificateArn"] for c in certificates if isinstance(c, dict) and "CertificateArn" in c
    ]


def _extra_certificates(b: Builder, listener_arn: Any, arns: list[Any], start: int) -> None:
    for number, arn in enumerate(arns, start=start):
        b.add_resource(
            "aws_lb_listener_certificate",
            f"_certificate_{number}",
            [Attr("listener_arn", listener_arn), Attr("certificate_arn", b.ctx.expr(arn))],
        )


@resource(
    "AWS::ElasticLoadBalancingV2::Listener",
    "aws_lb_listener",
    ref="arn",
    attrs={"ListenerArn": "arn"},
)
def listener(b: Builder) -> None:
    p = b.props
    p.attr("load_balancer_arn", "LoadBalancerArn")
    p.number("port", "Port")
    p.attr("protocol", "Protocol")
    p.attr("ssl_policy", "SslPolicy")
    alpn = p.take("AlpnPolicy")
    if alpn is not None:
        # A list of one in CloudFormation, a string in Terraform.
        p.set("alpn_policy", b.ctx.expr(alpn[0] if isinstance(alpn, list) and alpn else alpn))
    arns = _certificate_arns(b)
    if arns:
        # The listener holds the default certificate; the others are attached separately.
        p.set("certificate_arn", b.ctx.expr(arns[0]))
        _extra_certificates(b, b.self_ref("arn"), arns[1:], start=2)
    p.block("default_action", "DefaultActions", _action)


@resource("AWS::ElasticLoadBalancingV2::ListenerCertificate", "aws_lb_listener_certificate")
def listener_certificate(b: Builder) -> None:
    listener_arn = b.ctx.expr(b.props.take("ListenerArn"))
    arns = _certificate_arns(b)
    b.props.set("listener_arn", listener_arn)
    if arns:
        b.props.set("certificate_arn", b.ctx.expr(arns[0]))
        _extra_certificates(b, listener_arn, arns[1:], start=2)


_CONDITION_CONFIGS = {
    "path-pattern": ("path_pattern", "PathPatternConfig"),
    "host-header": ("host_header", "HostHeaderConfig"),
    "http-header": ("http_header", "HttpHeaderConfig"),
    "http-request-method": ("http_request_method", "HttpRequestMethodConfig"),
    "query-string": ("query_string", "QueryStringConfig"),
    "source-ip": ("source_ip", "SourceIpConfig"),
}


def _rule_condition(c: Props) -> None:
    field = c.take("Field")
    legacy = c.take("Values")
    if not isinstance(field, str) or field not in _CONDITION_CONFIGS:
        c.ctx.note(f"{c.owner}: rule condition '{field}' was not converted; set it by hand")
        c.comment(f"Condition on {snippet(field)} was not converted")
        return
    tf_name, cfn_name = _CONDITION_CONFIGS[field]
    config = c.literal(cfn_name, dict) or {}
    if field == "query-string":
        pairs = c.literal("Values", list, value=config.get("Values")) or []
        c.add_blocks(
            tf_name,
            pairs,
            lambda q: (q.attr("key", "Key"), q.attr("value", "Value")),
            f"{c.where}.{cfn_name}.Values",
        )
        return
    items: list[Any] = []
    if config.get("HttpHeaderName") is not None:
        items.append(Attr("http_header_name", c.ctx.expr(config["HttpHeaderName"])))
    values = config.get("Values", legacy)
    if values is not None:
        items.append(Attr("values", _as_list(c.ctx, values)))
    if config.get("RegexValues") is not None:
        items.append(Attr("regex_values", _as_list(c.ctx, config["RegexValues"])))
    c.items.append(Block(tf_name, [], items))


@resource(
    "AWS::ElasticLoadBalancingV2::ListenerRule",
    "aws_lb_listener_rule",
    ref="arn",
    attrs={"RuleArn": "arn"},
)
def listener_rule(b: Builder) -> None:
    p = b.props
    p.attr("listener_arn", "ListenerArn")
    p.number("priority", "Priority")
    p.block("action", "Actions", _action)
    p.block("condition", "Conditions", _rule_condition)
    p.tags()


# --- Route 53 ---------------------------------------------------------------------------


@resource(
    "AWS::Route53::HostedZone",
    "aws_route53_zone",
    attrs={"Id": "zone_id", "NameServers": "name_servers"},
)
def hosted_zone(b: Builder) -> None:
    p = b.props
    p.attr("name", "Name")
    config = p.literal("HostedZoneConfig", dict) or {}
    # The provider's default comment is "Managed by Terraform"; CloudFormation sets none.
    comment = config.get("Comment")
    p.set("comment", "" if comment is None else b.ctx.expr(comment))
    p.block("vpc", "VPCs", lambda v: v.attrs({"vpc_id": "VPCId", "vpc_region": "VPCRegion"}))
    p.tags("HostedZoneTags")


def _zone_name(value: Any) -> str | None:
    return value.rstrip(".").lower() if isinstance(value, str) else None


def _zone_id(b: Builder, p: Props) -> None:
    """zone_id from HostedZoneId, or from HostedZoneName: a hosted zone of the same template
    when one has that name, a data source lookup otherwise."""
    if p.attr("zone_id", "HostedZoneId"):
        p.drop("HostedZoneName")
        return
    name = p.take("HostedZoneName")
    if name is None:
        return
    wanted = _zone_name(name)
    for logical_id, other in (b.ctx.template.get("Resources") or {}).items():
        if other.get("Type") != "AWS::Route53::HostedZone" or wanted is None:
            continue
        if _zone_name((other.get("Properties") or {}).get("Name")) == wanted:
            p.set("zone_id", b.ctx.ref(logical_id))
            return
    data_name = f"{b.info.name}_zone"
    if not any(block.type == "data" and block.labels[1] == data_name for block in b.extra):
        b.add_data("aws_route53_zone", "_zone", [Attr("name", b.ctx.expr(name))])
    index = "[0]" if b.resource.get("Condition") else ""
    p.set("zone_id", Raw(f"data.aws_route53_zone.{data_name}{index}.zone_id"))


def _record(b: Builder, p: Props) -> None:
    ctx = b.ctx
    _zone_id(b, p)
    p.attr("name", "Name")
    p.attr("type", "Type")
    p.number("ttl", "TTL")
    p.list_attr("records", "ResourceRecords")
    p.attr("set_identifier", "SetIdentifier")
    p.attr("health_check_id", "HealthCheckId")
    p.boolean("multivalue_answer_routing_policy", "MultiValueAnswer")
    p.drop("Comment")  # A Route 53 change comment; Terraform has no counterpart.
    alias = p.take("AliasTarget")
    if isinstance(alias, dict):
        evaluate = _bool(ctx, alias.get("EvaluateTargetHealth", False))
        p.items.append(
            Block(
                "alias",
                [],
                [
                    Attr("name", ctx.expr(alias.get("DNSName"))),
                    Attr("zone_id", ctx.expr(alias.get("HostedZoneId"))),
                    Attr("evaluate_target_health", evaluate),
                ],
            )
        )
    for cfn_name, block, tf_name, number in (
        ("Weight", "weighted_routing_policy", "weight", True),
        ("Region", "latency_routing_policy", "region", False),
        ("Failover", "failover_routing_policy", "type", False),
    ):
        value = p.take(cfn_name)
        if value is not None:
            converted = _number(ctx, value) if number else ctx.expr(value)
            p.items.append(Block(block, [], [Attr(tf_name, converted)]))
    p.block(
        "geolocation_routing_policy",
        "GeoLocation",
        lambda g: g.attrs(
            {
                "continent": "ContinentCode",
                "country": "CountryCode",
                "subdivision": "SubdivisionCode",
            }
        ),
    )


@resource("AWS::Route53::RecordSet", "aws_route53_record", ref="fqdn")
def record_set(b: Builder) -> None:
    _record(b, b.props)


@resource("AWS::Route53::RecordSetGroup", "aws_route53_record", ref="fqdn")
def record_set_group(b: Builder) -> None:
    """The first record is the main resource; each other record is a resource of its own."""
    p = b.props
    zone = {k: p.take(k) for k in ("HostedZoneId", "HostedZoneName")}
    zone = {k: v for k, v in zone.items() if v is not None}
    p.drop("Comment")
    records = p.take("RecordSets")
    if not isinstance(records, list) or not records:
        b.ctx.note(f"{b.logical_id}: RecordSets is empty or computed and was not converted")
        return
    for number, record in enumerate(records, start=1):
        values = {**zone, **record} if isinstance(record, dict) else {}
        inner = Props(b.ctx, values, f"{b.logical_id}.RecordSets[{number - 1}]", p.owner)
        _record(b, inner)
        inner.finish()
        if number == 1:
            p.items.extend(inner.items)
        else:
            b.add_resource("aws_route53_record", f"_{number}", inner.items)


# --- ACM --------------------------------------------------------------------------------


def _dns_validated(resource_: dict[str, Any]) -> bool:
    """CloudFormation creates the validation records itself when a zone is given."""
    properties = resource_.get("Properties") or {}
    options = properties.get("DomainValidationOptions")
    return (
        properties.get("ValidationMethod") == "DNS"
        and isinstance(options, list)
        and any(isinstance(o, dict) and o.get("HostedZoneId") is not None for o in options)
    )


def _certificate_prepare(info: ResourceInfo, resource_: dict[str, Any]) -> None:
    if _dns_validated(resource_):
        # CloudFormation finishes the certificate only once it is validated; references
        # wait for the same thing through the validation resource.
        index = "[0]" if info.conditional else ""
        info.overrides["arn"] = f"aws_acm_certificate_validation.{info.name}{index}.certificate_arn"


@resource(
    "AWS::CertificateManager::Certificate",
    "aws_acm_certificate",
    ref="arn",
    prepare=_certificate_prepare,
)
def certificate(b: Builder) -> None:
    p, ctx = b.props, b.ctx
    p.attr("domain_name", "DomainName")
    p.list_attr("subject_alternative_names", "SubjectAlternativeNames")
    method = p.peek("ValidationMethod")
    p.attr("validation_method", "ValidationMethod")
    p.attr("key_algorithm", "KeyAlgorithm")
    p.attr("certificate_authority_arn", "CertificateAuthorityArn")
    logging = p.take("CertificateTransparencyLoggingPreference")
    if logging is not None:
        preference = Attr("certificate_transparency_logging_preference", ctx.expr(logging))
        p.items.append(Block("options", [], [preference]))
    p.tags()
    dns = _dns_validated(b.resource)
    options = p.take("DomainValidationOptions")
    zones: dict[str, Any] = {}
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        domain = option.get("DomainName")
        if dns and option.get("HostedZoneId") is not None:
            zones[str(domain)] = ctx.expr(option["HostedZoneId"])
        elif option.get("ValidationDomain") is not None and method != "DNS":
            p.items.append(
                Block(
                    "validation_option",
                    [],
                    [
                        Attr("domain_name", ctx.expr(domain)),
                        Attr("validation_domain", ctx.expr(option["ValidationDomain"])),
                    ],
                )
            )
    if dns:
        _validation_records(b, zones)
    elif method == "DNS":
        ctx.note(
            f"{b.logical_id} is validated by DNS without a hosted zone: add the records from "
            "domain_validation_options and an aws_acm_certificate_validation resource"
        )


def _validation_records(b: Builder, zones: dict[str, Any]) -> None:
    """The DNS records and the wait that CloudFormation handles inside the certificate."""
    own = f"{b.info.address}[0]" if b.info.conditional else b.info.address
    options = (
        f"flatten({b.info.address}[*].domain_validation_options)"
        if b.info.conditional
        else f"{b.info.address}.domain_validation_options"
    )
    first = next(iter(zones.values()))
    if len({hcl.inline(z) for z in zones.values()}) == 1:
        zone: Any = first
    else:
        zone = Raw(f"lookup({hcl.inline(zones)}, each.key, {hcl.inline(first)})")
    records = Raw(
        f"{{ for option in {options} : option.domain_name => {{ name = "
        "option.resource_record_name, record = option.resource_record_value, type = "
        "option.resource_record_type } }"
    )
    validation = f"aws_route53_record.{b.info.name}_validation"
    b.add_resource(
        "aws_route53_record",
        "_validation",
        [
            Attr("for_each", records),
            hcl.BLANK,
            Attr("allow_overwrite", True),
            Attr("name", Raw("each.value.name")),
            Attr("records", [Raw("each.value.record")]),
            Attr("ttl", 60),
            Attr("type", Raw("each.value.type")),
            Attr("zone_id", zone),
        ],
        counted=False,
    )
    b.add_resource(
        "aws_acm_certificate_validation",
        "",
        [
            Attr("certificate_arn", Raw(f"{own}.arn")),
            Attr("validation_record_fqdns", Raw(f"[for record in {validation} : record.fqdn]")),
        ],
    )


# --- CloudFront -------------------------------------------------------------------------

_METHODS = ["GET", "HEAD"]


@resource(
    "AWS::CloudFront::Distribution",
    "aws_cloudfront_distribution",
    attrs={"DomainName": "domain_name", "Id": "id"},
)
def distribution(b: Builder) -> None:
    p = b.props
    p.tags()
    config = p.take("DistributionConfig")
    if not isinstance(config, dict) or intrinsic_name(config) is not None:
        b.ctx.note(f"{b.logical_id}: DistributionConfig is missing or computed; set it by hand")
        return
    c = Props(b.ctx, config, f"{b.logical_id}.DistributionConfig", p.owner)
    c.boolean("enabled", "Enabled")
    c.list_attr("aliases", "Aliases")
    c.attr("comment", "Comment")
    c.attr("default_root_object", "DefaultRootObject")
    if not c.attr("http_version", "HttpVersion"):
        c.set("http_version", "http1.1")  # CloudFormation's default; the provider's is http2
    c.boolean("is_ipv6_enabled", "IPV6Enabled")
    c.attr("price_class", "PriceClass")
    c.attr("web_acl_id", "WebACLId")
    c.attr("continuous_deployment_policy_id", "ContinuousDeploymentPolicyId")
    c.boolean("staging", "Staging")
    c.block("origin", "Origins", _origin)
    groups = c.take("OriginGroups")
    if isinstance(groups, dict):
        where = f"{c.where}.OriginGroups"
        c.add_blocks("origin_group", groups.get("Items") or [], _origin_group, where)
    c.block("default_cache_behavior", "DefaultCacheBehavior", lambda r: _behavior(r, False))
    c.block("ordered_cache_behavior", "CacheBehaviors", lambda r: _behavior(r, True))
    c.block(
        "custom_error_response",
        "CustomErrorResponses",
        lambda r: (
            r.number("error_code", "ErrorCode"),
            r.number("response_code", "ResponseCode"),
            r.attr("response_page_path", "ResponsePagePath"),
            r.number("error_caching_min_ttl", "ErrorCachingMinTTL"),
        ),
    )
    c.block(
        "logging_config",
        "Logging",
        lambda r: (
            r.attr("bucket", "Bucket"),
            r.boolean("include_cookies", "IncludeCookies"),
            r.attr("prefix", "Prefix"),
        ),
    )
    # Terraform requires both blocks; these are CloudFormation's defaults when left out.
    if not c.block("restrictions", "Restrictions", _restrictions):
        geo = Block("geo_restriction", [], [Attr("restriction_type", "none")])
        c.items.append(Block("restrictions", [], [geo]))
    if not c.block("viewer_certificate", "ViewerCertificate", _viewer_certificate):
        default = Attr("cloudfront_default_certificate", True)
        c.items.append(Block("viewer_certificate", [], [default]))
    c.finish()
    p.items.extend(c.items)


def _origin(r: Props) -> None:
    ctx = r.ctx
    r.attr("domain_name", "DomainName")
    r.attr("origin_id", "Id")
    r.attr("origin_path", "OriginPath")
    r.number("connection_attempts", "ConnectionAttempts")
    r.number("connection_timeout", "ConnectionTimeout")
    r.number("response_completion_timeout", "ResponseCompletionTimeout")
    r.attr("origin_access_control_id", "OriginAccessControlId")
    _blocks(
        r,
        "custom_header",
        "OriginCustomHeaders",
        lambda h: (h.attr("name", "HeaderName"), h.attr("value", "HeaderValue")),
    )
    s3 = r.literal("S3OriginConfig", dict)
    if s3 is not None:
        identity = s3.get("OriginAccessIdentity")
        # An empty identity is how CloudFormation says "use the origin access control".
        if identity not in (None, ""):
            access = Attr("origin_access_identity", ctx.expr(identity))
            r.items.append(Block("s3_origin_config", [], [access]))
        if s3.get("OriginReadTimeout") is not None:
            ctx.note(
                f"{r.owner}: '{r.where}.S3OriginConfig.OriginReadTimeout' has no Terraform "
                "counterpart for S3 origins and was not converted"
            )
    r.block("custom_origin_config", "CustomOriginConfig", _custom_origin)
    r.block(
        "origin_shield",
        "OriginShield",
        lambda s: (
            s.boolean("enabled", "Enabled"),
            s.attr("origin_shield_region", "OriginShieldRegion"),
        ),
    )
    r.block(
        "vpc_origin_config",
        "VpcOriginConfig",
        lambda v: (
            v.attr("vpc_origin_id", "VpcOriginId"),
            v.number("origin_keepalive_timeout", "OriginKeepaliveTimeout"),
            v.number("origin_read_timeout", "OriginReadTimeout"),
            v.attr("owner_account_id", "OwnerAccountId"),
        ),
    )


def _custom_origin(c: Props) -> None:
    # Ports and protocols are optional in CloudFormation but required by the provider.
    if not c.number("http_port", "HTTPPort"):
        c.set("http_port", 80)
    if not c.number("https_port", "HTTPSPort"):
        c.set("https_port", 443)
    c.attr("origin_protocol_policy", "OriginProtocolPolicy")
    if not c.list_attr("origin_ssl_protocols", "OriginSSLProtocols"):
        c.set("origin_ssl_protocols", ["TLSv1.2"])
        c.ctx.note(
            f"{c.owner}: '{c.where}' had no OriginSSLProtocols; Terraform requires them, so "
            "TLSv1.2 was set"
        )
    c.number("origin_read_timeout", "OriginReadTimeout")
    c.number("origin_keepalive_timeout", "OriginKeepaliveTimeout")
    c.attr("ip_address_type", "IpAddressType")


def _origin_group(g: Props) -> None:
    ctx = g.ctx
    g.attr("origin_id", "Id")
    criteria = g.literal("FailoverCriteria", dict) or {}
    codes = g.literal("FailoverCriteria.StatusCodes", dict, value=criteria.get("StatusCodes"))
    items = g.literal("StatusCodes.Items", list, value=(codes or {}).get("Items")) or []
    status_codes = Attr("status_codes", [_number(ctx, code) for code in items])
    g.items.append(Block("failover_criteria", [], [status_codes]))
    members = g.literal("Members", dict) or {}
    member_items = g.literal("Members.Items", list, value=members.get("Items")) or []
    g.add_blocks(
        "member", member_items, lambda m: m.attr("origin_id", "OriginId"), f"{g.where}.Members"
    )


def _methods(r: Props, tf_name: str, cfn_name: str) -> None:
    if not r.list_attr(tf_name, cfn_name):
        r.set(tf_name, list(_METHODS))


def _behavior(r: Props, ordered: bool) -> None:
    if ordered:
        r.attr("path_pattern", "PathPattern")
    _methods(r, "allowed_methods", "AllowedMethods")
    _methods(r, "cached_methods", "CachedMethods")
    r.attr("target_origin_id", "TargetOriginId")
    r.attr("viewer_protocol_policy", "ViewerProtocolPolicy")
    r.attr("cache_policy_id", "CachePolicyId")
    r.attr("origin_request_policy_id", "OriginRequestPolicyId")
    r.attr("response_headers_policy_id", "ResponseHeadersPolicyId")
    r.boolean("compress", "Compress")
    r.number("default_ttl", "DefaultTTL")
    r.number("max_ttl", "MaxTTL")
    r.number("min_ttl", "MinTTL")
    r.attr("field_level_encryption_id", "FieldLevelEncryptionId")
    r.attr("realtime_log_config_arn", "RealtimeLogConfigArn")
    r.boolean("smooth_streaming", "SmoothStreaming")
    r.list_attr("trusted_key_groups", "TrustedKeyGroups")
    r.list_attr("trusted_signers", "TrustedSigners")
    _blocks(
        r,
        "function_association",
        "FunctionAssociations",
        lambda a: (a.attr("event_type", "EventType"), a.attr("function_arn", "FunctionARN")),
    )
    _blocks(
        r,
        "lambda_function_association",
        "LambdaFunctionAssociations",
        lambda a: (
            a.attr("event_type", "EventType"),
            a.attr("lambda_arn", "LambdaFunctionARN"),
            a.boolean("include_body", "IncludeBody"),
        ),
    )
    r.block("forwarded_values", "ForwardedValues", _forwarded_values)


def _forwarded_values(f: Props) -> None:
    if not f.boolean("query_string", "QueryString"):
        f.set("query_string", False)
    f.list_attr("headers", "Headers")
    f.list_attr("query_string_cache_keys", "QueryStringCacheKeys")
    cookies = f.block(
        "cookies",
        "Cookies",
        lambda c: (
            c.attr("forward", "Forward"),
            c.list_attr("whitelisted_names", "WhitelistedNames"),
        ),
    )
    if not cookies:
        # Required by the provider; "none" is CloudFormation's default.
        f.items.append(Block("cookies", [], [Attr("forward", "none")]))


def _restrictions(r: Props) -> None:
    def geo(g: Props) -> None:
        g.attr("restriction_type", "RestrictionType")
        g.list_attr("locations", "Locations")

    r.block("geo_restriction", "GeoRestriction", geo)


def _viewer_certificate(v: Props) -> None:
    v.attrs(
        {
            "acm_certificate_arn": "AcmCertificateArn",
            "iam_certificate_id": "IamCertificateId",
            "minimum_protocol_version": "MinimumProtocolVersion",
            "ssl_support_method": "SslSupportMethod",
        }
    )
    v.boolean("cloudfront_default_certificate", "CloudFrontDefaultCertificate")


def _nested(b: Builder, name: str, fill: Callable[[Props], None]) -> None:
    """A resource whose properties all sit in one configuration structure."""
    inner = Props(b.ctx, b.props.take(name), f"{b.logical_id}.{name}", b.props.owner)
    fill(inner)
    inner.finish()
    b.props.items.extend(inner.items)


@resource(
    "AWS::CloudFront::OriginAccessControl",
    "aws_cloudfront_origin_access_control",
    attrs={"Id": "id"},
)
def origin_access_control(b: Builder) -> None:
    _nested(
        b,
        "OriginAccessControlConfig",
        lambda c: c.attrs(
            {
                "name": "Name",
                "description": "Description",
                "origin_access_control_origin_type": "OriginAccessControlOriginType",
                "signing_behavior": "SigningBehavior",
                "signing_protocol": "SigningProtocol",
            }
        ),
    )


@resource(
    "AWS::CloudFront::CloudFrontOriginAccessIdentity",
    "aws_cloudfront_origin_access_identity",
    attrs={"Id": "id", "S3CanonicalUserId": "s3_canonical_user_id"},
)
def origin_access_identity(b: Builder) -> None:
    _nested(b, "CloudFrontOriginAccessIdentityConfig", lambda c: c.attr("comment", "Comment"))
