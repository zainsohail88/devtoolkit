"""VPC networking, load balancers, Route 53, ACM and CloudFront."""

from __future__ import annotations

from collections.abc import Callable
from operator import attrgetter
from typing import Any, ClassVar

from converters.cfn import hcl
from converters.cfn.builder import Builder, Props, snippet
from converters.cfn.context import Context
from converters.cfn.expressions import intrinsic_name, wrap_operand
from converters.cfn.models import BLANK, Attr, Block, Comment, Raw, ResourceInfo
from converters.cfn.resources.base import ResourceConverter

# --- helpers ----------------------------------------------------------------------------

NO_VALUE = {"Ref": "AWS::NoValue"}


def _typed(p: Props, value: Any, kind: str) -> Any:
    """A value converted as a "bool", a "number" or (any other kind) an expression."""
    if kind == "bool":
        return p.as_bool(value)
    if kind == "number":
        return p.as_number(value)
    return p.ctx.expr(value)


def _as_list(ctx: Context, value: Any) -> Any:
    """A single CloudFormation value where Terraform wants a list."""
    converted = ctx.expr(value)
    if isinstance(value, list) or isinstance(converted, list):
        return converted
    if isinstance(value, dict) and intrinsic_name(value) in ("Fn::GetAZs", "Fn::Split"):
        return converted
    return [converted]


def _list_attr(p: Props, tf_name: str, cfn_name: str) -> None:
    """A property as a list attribute, with a single value put in a list (see _as_list)."""
    value = p.take(cfn_name)
    if value is not None:
        p.set(tf_name, _as_list(p.ctx, value))


class OptionalList:
    """The condition of `!If [Cond, [items], !Ref AWS::NoValue]` (either way round), so each
    item can carry it on its own."""

    def __init__(self, condition: Any, when_true: bool) -> None:
        """`condition` is the condition name; `when_true` says whether the items exist when
        it holds (the NoValue branch is the second one)."""
        self.condition = condition
        self.when_true = when_true

    def present(self, item: Any) -> dict[str, Any]:
        """`item` as it holds only when the list is present."""
        if self.when_true:
            return {"Fn::If": [self.condition, item, NO_VALUE]}
        return {"Fn::If": [self.condition, NO_VALUE, item]}

    def absent(self, item: Any) -> dict[str, Any]:
        """`item` as it holds only when the list is absent."""
        if self.when_true:
            return {"Fn::If": [self.condition, NO_VALUE, item]}
        return {"Fn::If": [self.condition, item, NO_VALUE]}


def _split_if(ctx: Context, items: Any) -> tuple[Any, OptionalList | None]:
    """`!If [Cond, [items], !Ref AWS::NoValue]` (either way round) as one `!If` per item, so
    each becomes a conditional block or resource. Also returns the condition, to wrap an item
    that holds only when the property is absent. Anything else is returned as it is (and
    None)."""
    if not (isinstance(items, dict) and intrinsic_name(items) == "Fn::If"):
        return items, None
    args = items["Fn::If"]
    if not isinstance(args, list) or len(args) != 3:
        return items, None
    name, yes, no = args
    if ctx.is_no_value(no) and not ctx.is_no_value(yes):
        optional = OptionalList(name, when_true=True)
        present = yes if isinstance(yes, list) else [yes]
    elif ctx.is_no_value(yes) and not ctx.is_no_value(no):
        optional = OptionalList(name, when_true=False)
        present = no if isinstance(no, list) else [no]
    else:
        return items, None
    return [optional.present(item) for item in present], optional


def _blocks(p: Props, tf_name: str, cfn_name: str, fill: Callable[[Props], None]) -> None:
    """A list of structures as blocks, like Props.block, where `!If [Cond, [...],
    !Ref AWS::NoValue]` around the whole list gives one conditional block per item."""
    value = p.take(cfn_name)
    if value is None:
        return
    items, _ = _split_if(p.ctx, value)
    p.add_blocks(tf_name, items, fill, f"{p.where}.{cfn_name}")


def _sub(p: Props, value: Any, name: str) -> Props:
    """The properties of the structure `name` inside `p`."""
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


def _ipv6_blocks(info: ResourceInfo) -> None:
    """CloudFormation returns a list of IPv6 blocks; the provider holds the one block as a
    string."""
    info.attrs["Ipv6CidrBlocks"] = "ipv6_cidr_blocks"
    info.overrides["ipv6_cidr_blocks"] = f"[{info.expression('ipv6_cidr_block')}]"


def _nested(b: Builder, name: str, fill: Callable[[Props], None]) -> None:
    """A resource whose properties all sit in one configuration structure."""
    inner = Props(b.ctx, b.props.take(name), f"{b.logical_id}.{name}", b.props.owner)
    fill(inner)
    inner.finish()
    b.props.items.extend(inner.items)


# --- VPC basics -------------------------------------------------------------------------


class Vpc(ResourceConverter):
    """AWS::EC2::VPC as aws_vpc."""

    cfn_type = "AWS::EC2::VPC"
    tf_type = "aws_vpc"
    attrs: ClassVar[dict[str, str]] = {
        "CidrBlock": "cidr_block",
        "DefaultNetworkAcl": "default_network_acl_id",
        "DefaultSecurityGroup": "default_security_group_id",
        "VpcId": "id",
    }

    def prepare(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """Ipv6CidrBlocks reads the one IPv6 block as a list."""
        _ipv6_blocks(info)

    def convert(self, b: Builder) -> None:
        """The CIDR block, DNS settings and tags."""
        p = b.props
        p.attr("cidr_block", "CidrBlock")
        p.attr("ipv4_ipam_pool_id", "Ipv4IpamPoolId")
        p.number("ipv4_netmask_length", "Ipv4NetmaskLength")
        p.boolean("enable_dns_support", "EnableDnsSupport")
        p.boolean("enable_dns_hostnames", "EnableDnsHostnames")
        p.attr("instance_tenancy", "InstanceTenancy")
        p.tags()


class Subnet(ResourceConverter):
    """AWS::EC2::Subnet as aws_subnet."""

    cfn_type = "AWS::EC2::Subnet"
    tf_type = "aws_subnet"
    attrs: ClassVar[dict[str, str]] = {
        "AvailabilityZone": "availability_zone",
        "AvailabilityZoneId": "availability_zone_id",
        "CidrBlock": "cidr_block",
        "OutpostArn": "outpost_arn",
        "SubnetId": "id",
        "VpcId": "vpc_id",
    }

    def prepare(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """Ipv6CidrBlocks reads the one IPv6 block as a list."""
        _ipv6_blocks(info)

    def convert(self, b: Builder) -> None:
        """The addresses, the zone and the launch options, which are flat attributes in
        Terraform."""
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
            inner = _sub(p, options, "PrivateDnsNameOptionsOnLaunch")
            inner.attr("private_dns_hostname_type_on_launch", "HostnameType")
            inner.boolean(
                "enable_resource_name_dns_a_record_on_launch", "EnableResourceNameDnsARecord"
            )
            inner.boolean(
                "enable_resource_name_dns_aaaa_record_on_launch", "EnableResourceNameDnsAAAARecord"
            )
            inner.finish()
            p.items.extend(inner.items)
        p.tags()


class InternetGateway(ResourceConverter):
    """AWS::EC2::InternetGateway as aws_internet_gateway."""

    cfn_type = "AWS::EC2::InternetGateway"
    tf_type = "aws_internet_gateway"
    attrs: ClassVar[dict[str, str]] = {"InternetGatewayId": "id"}

    def convert(self, b: Builder) -> None:
        """Only tags."""
        b.props.tags()


class EgressOnlyInternetGateway(ResourceConverter):
    """AWS::EC2::EgressOnlyInternetGateway as aws_egress_only_internet_gateway."""

    cfn_type = "AWS::EC2::EgressOnlyInternetGateway"
    tf_type = "aws_egress_only_internet_gateway"

    def convert(self, b: Builder) -> None:
        """The VPC and tags."""
        b.props.attr("vpc_id", "VpcId")
        b.props.tags()


class VpcGatewayAttachment(ResourceConverter):
    """AWS::EC2::VPCGatewayAttachment as aws_internet_gateway_attachment, or as
    aws_vpn_gateway_attachment for a VPN gateway."""

    cfn_type = "AWS::EC2::VPCGatewayAttachment"
    tf_type = "aws_internet_gateway_attachment"

    def prepare(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """A VPN gateway attachment has a Terraform type of its own."""
        properties = resource.get("Properties") or {}
        if (
            properties.get("VpnGatewayId") is not None
            and properties.get("InternetGatewayId") is None
        ):
            info.tf_type = "aws_vpn_gateway_attachment"

    def convert(self, b: Builder) -> None:
        """The VPC and the gateway."""
        p = b.props
        if b.info.tf_type == "aws_vpn_gateway_attachment":
            p.attr("vpc_id", "VpcId")
            p.attr("vpn_gateway_id", "VpnGatewayId")
            return
        p.attr("internet_gateway_id", "InternetGatewayId")
        p.attr("vpc_id", "VpcId")


class RouteTable(ResourceConverter):
    """AWS::EC2::RouteTable as aws_route_table."""

    cfn_type = "AWS::EC2::RouteTable"
    tf_type = "aws_route_table"
    attrs: ClassVar[dict[str, str]] = {"RouteTableId": "id"}

    def convert(self, b: Builder) -> None:
        """The VPC and tags."""
        b.props.attr("vpc_id", "VpcId")
        b.props.tags()


class Route(ResourceConverter):
    """AWS::EC2::Route as aws_route."""

    cfn_type = "AWS::EC2::Route"
    tf_type = "aws_route"

    def convert(self, b: Builder) -> None:
        """The destination and the target. An instance target is reported."""
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
                f"{b.logical_id} routes to an instance, but the aws provider routes to its "
                "network interface. Set network_interface_id = "
                "aws_instance.<name>.primary_network_interface_id instead."
            )


class SubnetRouteTableAssociation(ResourceConverter):
    """AWS::EC2::SubnetRouteTableAssociation as aws_route_table_association."""

    cfn_type = "AWS::EC2::SubnetRouteTableAssociation"
    tf_type = "aws_route_table_association"

    def convert(self, b: Builder) -> None:
        """The subnet and the route table."""
        b.props.attr("subnet_id", "SubnetId")
        b.props.attr("route_table_id", "RouteTableId")


class Eip(ResourceConverter):
    """AWS::EC2::EIP as aws_eip."""

    cfn_type = "AWS::EC2::EIP"
    tf_type = "aws_eip"
    ref = "public_ip"
    attrs: ClassVar[dict[str, str]] = {"AllocationId": "allocation_id", "PublicIp": "public_ip"}

    def convert(self, b: Builder) -> None:
        """The address settings and tags."""
        p = b.props
        p.attr("domain", "Domain")
        p.attr("instance", "InstanceId")
        p.attr("network_border_group", "NetworkBorderGroup")
        p.attr("public_ipv4_pool", "PublicIpv4Pool")
        p.attr("ipam_pool_id", "IpamPoolId")
        p.attr("address", "Address")
        p.tags()


class EipAssociation(ResourceConverter):
    """AWS::EC2::EIPAssociation as aws_eip_association."""

    cfn_type = "AWS::EC2::EIPAssociation"
    tf_type = "aws_eip_association"

    def convert(self, b: Builder) -> None:
        """The address and what it is associated with."""
        b.props.attrs(
            {
                "allocation_id": "AllocationId",
                "public_ip": "EIP",
                "instance_id": "InstanceId",
                "network_interface_id": "NetworkInterfaceId",
                "private_ip_address": "PrivateIpAddress",
            }
        )


class NatGateway(ResourceConverter):
    """AWS::EC2::NatGateway as aws_nat_gateway."""

    cfn_type = "AWS::EC2::NatGateway"
    tf_type = "aws_nat_gateway"
    attrs: ClassVar[dict[str, str]] = {"NatGatewayId": "id"}

    def convert(self, b: Builder) -> None:
        """The subnet, the addresses and tags."""
        p = b.props
        p.attr("allocation_id", "AllocationId")
        p.attr("subnet_id", "SubnetId")
        p.attr("connectivity_type", "ConnectivityType")
        p.attr("private_ip", "PrivateIpAddress")
        p.attr("secondary_allocation_ids", "SecondaryAllocationIds")
        p.attr("secondary_private_ip_addresses", "SecondaryPrivateIpAddresses")
        p.number("secondary_private_ip_address_count", "SecondaryPrivateIpAddressCount")
        p.tags()


class VpcEndpoint(ResourceConverter):
    """AWS::EC2::VPCEndpoint as aws_vpc_endpoint."""

    cfn_type = "AWS::EC2::VPCEndpoint"
    tf_type = "aws_vpc_endpoint"
    attrs: ClassVar[dict[str, str]] = {"Id": "id", "NetworkInterfaceIds": "network_interface_ids"}

    def convert(self, b: Builder) -> None:
        """The service, the network placement, the policy and DNS options."""
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
        p.block("dns_options", "DnsOptions", self.fill_dns_options)
        p.tags()

    def fill_dns_options(self, d: Props) -> None:
        """The dns_options block."""
        d.attr("dns_record_ip_type", "DnsRecordIpType")
        d.boolean(
            "private_dns_only_for_inbound_resolver_endpoint",
            "PrivateDnsOnlyForInboundResolverEndpoint",
        )


# --- security groups --------------------------------------------------------------------

ALL_PROTOCOLS = ("-1", "all")
ICMP_PROTOCOLS = ("icmp", "icmpv6", "1", "58")
ALLOW_ALL_EGRESS = {"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}
EGRESS_COMMENT = (
    "CloudFormation adds this allow-all egress rule to a group without egress rules.\n"
    "Terraform removes that rule, so the converter writes it out here."
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
    protocol = r.take("IpProtocol", "-1")
    literal = _protocol(protocol)
    from_port, to_port = r.take("FromPort"), r.take("ToPort")
    if literal in ALL_PROTOCOLS:
        r.set("from_port", 0)
        r.set("to_port", 0)
        r.set("protocol", "-1")
    else:
        # Leaving the ports out means all of them (or every ICMP type and code).
        icmp = literal in ICMP_PROTOCOLS
        first = (-1 if icmp else 0) if from_port is None else r.as_number(from_port)
        if to_port is not None:
            last = r.as_number(to_port)
        elif from_port is not None:
            last = first
        else:
            last = -1 if icmp else 65535
        r.set("from_port", first)
        r.set("to_port", last)
        r.set("protocol", literal if literal is not None else r.ctx.expr(protocol))
    group, prefix = _rule_target(egress)
    _list_attr(r, "cidr_blocks", "CidrIp")
    _list_attr(r, "ipv6_cidr_blocks", "CidrIpv6")
    _list_attr(r, "security_groups", group)
    _list_attr(r, "prefix_list_ids", prefix)
    r.attr("description", "Description")


def _inline_ingress(r: Props) -> None:
    """An inline ingress block."""
    _inline_rule(r, egress=False)


def _inline_egress(r: Props) -> None:
    """An inline egress block."""
    _inline_rule(r, egress=True)


def _rule_items(r: Props, egress: bool) -> None:
    """One rule as an aws_vpc_security_group_ingress_rule / egress_rule body."""
    protocol = r.take("IpProtocol", "-1")
    literal = _protocol(protocol)
    from_port, to_port = r.take("FromPort"), r.take("ToPort")
    if literal in ALL_PROTOCOLS:
        # With every protocol the API takes no ports.
        r.set("ip_protocol", "-1")
    else:
        r.set("ip_protocol", literal if literal is not None else r.ctx.expr(protocol))
        if from_port is not None:
            r.set("from_port", r.as_number(from_port))
        if to_port is not None:
            r.set("to_port", r.as_number(to_port))
    group, prefix = _rule_target(egress)
    r.attr("cidr_ipv4", "CidrIp")
    r.attr("cidr_ipv6", "CidrIpv6")
    r.attr("referenced_security_group_id", group)
    r.attr("prefix_list_id", prefix)
    r.attr("description", "Description")


class SecurityGroup(ResourceConverter):
    """AWS::EC2::SecurityGroup as aws_security_group, with its rules inline, or as rule
    resources when other rule resources point at the group."""

    cfn_type = "AWS::EC2::SecurityGroup"
    tf_type = "aws_security_group"
    attrs: ClassVar[dict[str, str]] = {"GroupId": "id", "VpcId": "vpc_id"}

    def convert(self, b: Builder) -> None:
        """The group and its rules, with the default egress rule CloudFormation adds."""
        p = b.props
        p.attr("name", "GroupName")
        p.attr("description", "GroupDescription")
        p.attr("vpc_id", "VpcId")
        p.tags()
        ingress, _ = _split_if(b.ctx, p.take("SecurityGroupIngress"))
        egress, optional = _split_if(b.ctx, p.take("SecurityGroupEgress"))
        # The rule CloudFormation keeps when the template sets no egress (or, for
        # `!If [Cond, [...], !Ref AWS::NoValue]`, when the condition picks NoValue).
        default: Any = None
        if egress is None:
            default = ALLOW_ALL_EGRESS
            egress = [default]
        elif optional is not None:
            default = optional.absent(ALLOW_ALL_EGRESS)
            egress = egress + [default]

        if self.has_standalone_rules(b.ctx, b.logical_id):
            # Inline rules and separate rule resources on one group overwrite each other in
            # Terraform, so this group's own rules become separate resources too.
            self.standalone_rules(b, "ingress", ingress, None)
            self.standalone_rules(b, "egress", egress, default)
            return
        if ingress is not None:
            p.add_blocks("ingress", ingress, _inline_ingress, f"{p.where}.Ingress")
        for rule in egress if isinstance(egress, list) else [egress]:
            if rule is default:
                p.comment(EGRESS_COMMENT)
            p.add_blocks("egress", rule, _inline_egress, f"{p.where}.Egress")

    def has_standalone_rules(self, ctx: Context, logical_id: str) -> bool:
        """Whether a rule resource of the template points at this group."""
        for other in (ctx.template.get("Resources") or {}).values():
            if other.get("Type") in (
                "AWS::EC2::SecurityGroupIngress",
                "AWS::EC2::SecurityGroupEgress",
            ):
                if _refers_to((other.get("Properties") or {}).get("GroupId"), logical_id):
                    return True
        return False

    def standalone_rules(self, b: Builder, kind: str, rules: Any, default: Any) -> None:
        """The group's own `kind` ("ingress" or "egress") rules as rule resources."""
        if rules is None:
            return
        if not isinstance(rules, list):
            b.ctx.note(
                f"{b.logical_id}: the {kind} rules come from a function, so the converter cannot "
                "turn them into rule resources. Add them by hand."
            )
            return
        is_egress = kind == "egress"
        for number, item in enumerate(rules, start=1):
            if b.ctx.is_no_value(item):
                continue
            where = f"{b.logical_id}.{kind}[{number - 1}]"
            # `!If [Cond, {...}, !Ref AWS::NoValue]`: a rule resource counted on Cond.
            when, rule = b.ctx.optional_item(item)
            if not isinstance(rule, dict) or intrinsic_name(rule) is not None:
                b.ctx.note(
                    f"{b.props.owner}: '{where}' comes from a function, so the converter left it "
                    f"out. Add the aws_vpc_security_group_{kind}_rule by hand."
                )
                b.props.comment(
                    f"{kind}[{number - 1}] was left out. CloudFormation value: {snippet(item)}"
                )
                continue
            inner = Props(b.ctx, rule, where, b.props.owner)
            items: list[Any] = [Attr("security_group_id", b.self_ref("id"))]
            if is_egress and item is default:
                items.insert(0, Comment(EGRESS_COMMENT))
            _rule_items(inner, is_egress)
            inner.finish()
            b.add_resource(
                f"aws_vpc_security_group_{kind}_rule",
                f"_{kind}_{number}",
                items + inner.items,
                when=when,
            )


class SecurityGroupIngress(ResourceConverter):
    """AWS::EC2::SecurityGroupIngress as aws_vpc_security_group_ingress_rule."""

    cfn_type = "AWS::EC2::SecurityGroupIngress"
    tf_type = "aws_vpc_security_group_ingress_rule"
    egress: ClassVar[bool] = False

    def convert(self, b: Builder) -> None:
        """The group and the rule."""
        b.props.attr("security_group_id", "GroupId")
        _rule_items(b.props, self.egress)


class SecurityGroupEgress(SecurityGroupIngress):
    """AWS::EC2::SecurityGroupEgress as aws_vpc_security_group_egress_rule."""

    cfn_type = "AWS::EC2::SecurityGroupEgress"
    tf_type = "aws_vpc_security_group_egress_rule"
    egress: ClassVar[bool] = True


# --- network ACLs and flow logs ---------------------------------------------------------


class NetworkAcl(ResourceConverter):
    """AWS::EC2::NetworkAcl as aws_network_acl."""

    cfn_type = "AWS::EC2::NetworkAcl"
    tf_type = "aws_network_acl"

    def convert(self, b: Builder) -> None:
        """The VPC and tags."""
        b.props.attr("vpc_id", "VpcId")
        b.props.tags()


class NetworkAclEntry(ResourceConverter):
    """AWS::EC2::NetworkAclEntry as aws_network_acl_rule."""

    cfn_type = "AWS::EC2::NetworkAclEntry"
    tf_type = "aws_network_acl_rule"

    def convert(self, b: Builder) -> None:
        """The rule, with the port range and ICMP settings as flat attributes."""
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
                    p.set(tf_name, p.as_number(ports[key]))
        icmp = p.take("Icmp")
        if isinstance(icmp, dict):
            for key, tf_name in (("Type", "icmp_type"), ("Code", "icmp_code")):
                if icmp.get(key) is not None:
                    p.set(tf_name, p.as_number(icmp[key]))


class SubnetNetworkAclAssociation(ResourceConverter):
    """AWS::EC2::SubnetNetworkAclAssociation as aws_network_acl_association."""

    cfn_type = "AWS::EC2::SubnetNetworkAclAssociation"
    tf_type = "aws_network_acl_association"

    def convert(self, b: Builder) -> None:
        """The network ACL and the subnet."""
        b.props.attr("network_acl_id", "NetworkAclId")
        b.props.attr("subnet_id", "SubnetId")


class FlowLog(ResourceConverter):
    """AWS::EC2::FlowLog as aws_flow_log."""

    cfn_type = "AWS::EC2::FlowLog"
    tf_type = "aws_flow_log"
    # ResourceType -> the attribute that takes ResourceId.
    targets: ClassVar[dict[str, str]] = {
        "VPC": "vpc_id",
        "Subnet": "subnet_id",
        "NetworkInterface": "eni_id",
        "TransitGateway": "transit_gateway_id",
        "TransitGatewayAttachment": "transit_gateway_attachment_id",
        "RegionalNatGateway": "regional_nat_gateway_id",
    }
    log_group_arn = "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:${Name}"

    def convert(self, b: Builder) -> None:
        """What is logged, where to and in what format."""
        p, ctx = b.props, b.ctx
        kind = p.peek("ResourceType")
        if isinstance(kind, str) and kind in self.targets and p.has("ResourceId"):
            p.drop("ResourceType")
            p.attr(self.targets[kind], "ResourceId")
        p.attr("traffic_type", "TrafficType")
        p.attr("log_destination_type", "LogDestinationType")
        p.attr("log_destination", "LogDestination")
        group = p.take("LogGroupName")
        if group is not None:
            # The provider only takes the ARN of the log group.
            p.set("log_destination", ctx.expr({"Fn::Sub": [self.log_group_arn, {"Name": group}]}))
        p.attr("iam_role_arn", "DeliverLogsPermissionArn")
        p.attr("deliver_cross_account_role", "DeliverCrossAccountRole")
        p.attr("log_format", "LogFormat")
        p.number("max_aggregation_interval", "MaxAggregationInterval")
        p.block("destination_options", "DestinationOptions", self.fill_destination_options)
        p.tags()

    def fill_destination_options(self, d: Props) -> None:
        """The destination_options block."""
        d.attr("file_format", "FileFormat")
        d.boolean("hive_compatible_partitions", "HiveCompatiblePartitions")
        d.boolean("per_hour_partition", "PerHourPartition")


# --- load balancers ---------------------------------------------------------------------


def _key_values(p: Props, cfn_name: str) -> list[tuple[str, Any]]:
    """[{Key, Value}] attributes as pairs. A computed list is reported and skipped."""
    raw = p.take(cfn_name)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(
        isinstance(item, dict) and isinstance(item.get("Key"), str) for item in raw
    ):
        p.ctx.note(
            f"{p.owner}: '{p.where}.{cfn_name}' is computed, so the converter left it out. Set "
            "the attributes by hand."
        )
        p.comment(f"{cfn_name} was left out because its value is computed.")
        return []
    return [(item["Key"], item.get("Value")) for item in raw]


def _unknown_attribute(p: Props, cfn_name: str, key: str, value: Any) -> None:
    """Report an attribute key the converter does not know, and keep it as a comment."""
    p.ctx.note(
        f"{p.owner}: {cfn_name} '{key}' is unknown to the converter, so it was left out. Set it "
        "by hand."
    )
    p.comment(f"{cfn_name} {snippet(key)} = {hcl.inline(p.ctx.expr(value))} was left out")


class LoadBalancer(ResourceConverter):
    """AWS::ElasticLoadBalancingV2::LoadBalancer as aws_lb."""

    cfn_type = "AWS::ElasticLoadBalancingV2::LoadBalancer"
    tf_type = "aws_lb"
    ref = "arn"
    attrs: ClassVar[dict[str, str]] = {
        "CanonicalHostedZoneID": "zone_id",
        "DNSName": "dns_name",
        "LoadBalancerArn": "arn",
        "LoadBalancerFullName": "arn_suffix",
        "LoadBalancerName": "name",
        "SecurityGroups": "security_groups",
    }
    # LoadBalancerAttributes keys -> (attribute, kind); kind is "bool", "number" or "string".
    attributes: ClassVar[dict[str, tuple[str, str]]] = {
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
    logs: ClassVar[dict[str, str]] = {
        "access_logs.s3.": "access_logs",
        "connection_logs.s3.": "connection_logs",
        "health_check_logs.s3.": "health_check_logs",
    }

    def convert(self, b: Builder) -> None:
        """The scheme, the network placement, the attributes and the log blocks."""
        p, ctx = b.props, b.ctx
        p.attr("name", "Name")
        p.attr("load_balancer_type", "Type")
        scheme = p.take("Scheme")
        if scheme is not None:
            if isinstance(scheme, str):
                p.set("internal", scheme == "internal")
            else:
                operand = wrap_operand(hcl.inline(ctx.expr(scheme)))
                p.set("internal", Raw(f'{operand} == "internal"'))
        p.attr("ip_address_type", "IpAddressType")
        p.list_attr("subnets", "Subnets")
        p.list_attr("security_groups", "SecurityGroups")
        p.attr(
            "enforce_security_group_inbound_rules_on_private_link_traffic",
            "EnforceSecurityGroupInboundRulesOnPrivateLinkTraffic",
        )
        self.add_attributes(p)
        p.block("subnet_mapping", "SubnetMappings", self.fill_subnet_mapping)
        p.tags()

    def add_attributes(self, p: Props) -> None:
        """LoadBalancerAttributes as attributes and log blocks."""
        logs: dict[str, list[Attr]] = {}
        for key, value in _key_values(p, "LoadBalancerAttributes"):
            if key in self.attributes:
                tf_name, kind = self.attributes[key]
                p.set(tf_name, _typed(p, value, kind))
                continue
            prefix = next((pre for pre in self.logs if key.startswith(pre)), None)
            field = key[len(prefix) :] if prefix else ""
            if prefix and field in ("enabled", "bucket", "prefix"):
                kind = "bool" if field == "enabled" else "string"
                logs.setdefault(self.logs[prefix], []).append(Attr(field, _typed(p, value, kind)))
                continue
            _unknown_attribute(p, "LoadBalancerAttributes", key, value)
        for name, items in logs.items():
            if not any(item.name == "bucket" for item in items):
                # The provider requires a bucket; with none, logging stays off as in AWS.
                items.append(Attr("bucket", ""))
            p.items.append(Block(name, [], sorted(items, key=attrgetter("name"))))

    def fill_subnet_mapping(self, m: Props) -> None:
        """A subnet_mapping block."""
        m.attrs(
            {
                "subnet_id": "SubnetId",
                "allocation_id": "AllocationId",
                "private_ipv4_address": "PrivateIPv4Address",
                "ipv6_address": "IPv6Address",
            }
        )


class TargetGroup(ResourceConverter):
    """AWS::ElasticLoadBalancingV2::TargetGroup as aws_lb_target_group, with
    aws_lb_target_group_attachment resources for its targets."""

    cfn_type = "AWS::ElasticLoadBalancingV2::TargetGroup"
    tf_type = "aws_lb_target_group"
    ref = "arn"
    attrs: ClassVar[dict[str, str]] = {
        "LoadBalancerArns": "load_balancer_arns",
        "TargetGroupArn": "arn",
        "TargetGroupFullName": "arn_suffix",
        "TargetGroupName": "name",
    }
    # TargetGroupAttributes keys -> (attribute, kind), as in LoadBalancer.attributes.
    attributes: ClassVar[dict[str, tuple[str, str]]] = {
        "deregistration_delay.timeout_seconds": ("deregistration_delay", "string"),
        "deregistration_delay.connection_termination.enabled": ("connection_termination", "bool"),
        "slow_start.duration_seconds": ("slow_start", "number"),
        "load_balancing.algorithm.type": ("load_balancing_algorithm_type", "string"),
        "load_balancing.algorithm.anomaly_mitigation": (
            "load_balancing_anomaly_mitigation",
            "string",
        ),
        "load_balancing.cross_zone.enabled": ("load_balancing_cross_zone_enabled", "string"),
        "lambda.multi_value_headers.enabled": ("lambda_multi_value_headers_enabled", "bool"),
        "proxy_protocol_v2.enabled": ("proxy_protocol_v2", "bool"),
        "preserve_client_ip.enabled": ("preserve_client_ip", "string"),
    }
    # Attribute keys that go to the stickiness block.
    stickiness: ClassVar[dict[str, tuple[str, str]]] = {
        "stickiness.enabled": ("enabled", "bool"),
        "stickiness.type": ("type", "string"),
        "stickiness.lb_cookie.duration_seconds": ("cookie_duration", "number"),
        "stickiness.app_cookie.duration_seconds": ("cookie_duration", "number"),
        "stickiness.app_cookie.cookie_name": ("cookie_name", "string"),
    }
    # Properties that go to the health_check block.
    health_check: ClassVar[dict[str, tuple[str, str]]] = {
        "HealthCheckEnabled": ("enabled", "bool"),
        "HealthCheckIntervalSeconds": ("interval", "number"),
        "HealthCheckPath": ("path", "string"),
        "HealthCheckPort": ("port", "string"),
        "HealthCheckProtocol": ("protocol", "string"),
        "HealthCheckTimeoutSeconds": ("timeout", "number"),
        "HealthyThresholdCount": ("healthy_threshold", "number"),
        "UnhealthyThresholdCount": ("unhealthy_threshold", "number"),
    }

    def convert(self, b: Builder) -> None:
        """The group, its attributes, health check and stickiness, and the targets."""
        p = b.props
        p.attr("name", "Name")
        p.number("port", "Port")
        protocol = p.peek("Protocol")
        target_type = p.peek("TargetType")
        p.attr("protocol", "Protocol")
        p.attr("protocol_version", "ProtocolVersion")
        p.attr("vpc_id", "VpcId")
        p.attr("target_type", "TargetType")
        p.attr("ip_address_type", "IpAddressType")
        stickiness = self.add_attributes(p)
        health = self.health_items(p)
        if health:
            health.extend(self.health_check_defaults(health, protocol, target_type))
            p.items.append(Block("health_check", [], health))
        if stickiness:
            # The provider turns stickiness on unless told otherwise; AWS leaves it off.
            if not any(item.name == "enabled" for item in stickiness):
                stickiness.insert(0, Attr("enabled", False))
            if not any(item.name == "type" for item in stickiness):
                stickiness.append(Attr("type", "lb_cookie"))
            p.items.append(Block("stickiness", [], stickiness))
        p.tags()
        self.add_targets(b)

    def add_attributes(self, p: Props) -> list[Attr]:
        """TargetGroupAttributes as attributes. Returns the items of the stickiness block."""
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
            if key in self.attributes:
                tf_name, kind = self.attributes[key]
                p.set(tf_name, _typed(p, value, kind))
            elif key in self.stickiness and (
                not key.endswith(".duration_seconds") or key == duration
            ):
                tf_name, kind = self.stickiness[key]
                stickiness.append(Attr(tf_name, _typed(p, value, kind)))
            else:
                _unknown_attribute(p, "TargetGroupAttributes", key, value)
        return stickiness

    def health_items(self, p: Props) -> list[Any]:
        """The items of the health_check block, from the HealthCheck* properties and
        Matcher."""
        health: list[Any] = []
        for cfn_name, (tf_name, kind) in self.health_check.items():
            value = p.take(cfn_name)
            if value is not None:
                health.append(Attr(tf_name, _typed(p, value, kind)))
        matcher = p.literal("Matcher", dict)
        if matcher is not None:
            code = matcher.get("HttpCode", matcher.get("GrpcCode"))
            if code is not None:
                health.append(Attr("matcher", p.ctx.expr(code)))
        return health

    def health_check_defaults(
        self, health: list[Any], protocol: Any, target_type: Any
    ) -> list[Attr]:
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

    def add_targets(self, b: Builder) -> None:
        """Targets as aws_lb_target_group_attachment resources."""
        p, ctx = b.props, b.ctx
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
            ctx.note(
                f"{b.logical_id}: Targets is computed, so the converter left it out. Attach the "
                "targets by hand."
            )


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
    r.block("redirect", "RedirectConfig", _redirect)
    r.block("fixed_response", "FixedResponseConfig", _fixed_response)
    r.block("authenticate_cognito", "AuthenticateCognitoConfig", _cognito)
    r.block("authenticate_oidc", "AuthenticateOidcConfig", _oidc)


def _forward(c: Props) -> None:
    """The forward block of an action."""
    _blocks(c, "target_group", "TargetGroups", _forward_target_group)
    c.block("stickiness", "TargetGroupStickinessConfig", _forward_stickiness)


def _forward_target_group(g: Props) -> None:
    """A target_group block of a forward block."""
    g.attr("arn", "TargetGroupArn")
    g.number("weight", "Weight")


def _forward_stickiness(s: Props) -> None:
    """The stickiness block of a forward block."""
    s.boolean("enabled", "Enabled")
    s.number("duration", "DurationSeconds")


def _redirect(c: Props) -> None:
    """The redirect block of an action."""
    c.attrs(
        {
            "host": "Host",
            "path": "Path",
            "port": "Port",
            "protocol": "Protocol",
            "query": "Query",
            "status_code": "StatusCode",
        }
    )


def _fixed_response(c: Props) -> None:
    """The fixed_response block of an action."""
    c.attrs(
        {
            "content_type": "ContentType",
            "message_body": "MessageBody",
            "status_code": "StatusCode",
        }
    )


def _auth_common(c: Props) -> None:
    """The settings Cognito and OIDC authentication share."""
    c.attr("authentication_request_extra_params", "AuthenticationRequestExtraParams")
    c.attr("on_unauthenticated_request", "OnUnauthenticatedRequest")
    c.attr("scope", "Scope")
    c.attr("session_cookie_name", "SessionCookieName")
    c.number("session_timeout", "SessionTimeout")


def _cognito(c: Props) -> None:
    """The authenticate_cognito block of an action."""
    c.attrs(
        {
            "user_pool_arn": "UserPoolArn",
            "user_pool_client_id": "UserPoolClientId",
            "user_pool_domain": "UserPoolDomain",
        }
    )
    _auth_common(c)


def _oidc(c: Props) -> None:
    """The authenticate_oidc block of an action."""
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
    """The ARNs in Certificates. A computed list is reported and skipped."""
    certificates = b.props.take("Certificates")
    if certificates is None:
        return []
    if not isinstance(certificates, list):
        b.ctx.note(
            f"{b.logical_id}: Certificates is computed, so the converter left it out. Set the "
            "certificates by hand."
        )
        return []
    return [
        c["CertificateArn"] for c in certificates if isinstance(c, dict) and "CertificateArn" in c
    ]


def _extra_certificates(b: Builder, listener_arn: Any, arns: list[Any], start: int) -> None:
    """aws_lb_listener_certificate resources for certificates after the first."""
    for number, arn in enumerate(arns, start=start):
        b.add_resource(
            "aws_lb_listener_certificate",
            f"_certificate_{number}",
            [Attr("listener_arn", listener_arn), Attr("certificate_arn", b.ctx.expr(arn))],
        )


class Listener(ResourceConverter):
    """AWS::ElasticLoadBalancingV2::Listener as aws_lb_listener."""

    cfn_type = "AWS::ElasticLoadBalancingV2::Listener"
    tf_type = "aws_lb_listener"
    ref = "arn"
    attrs: ClassVar[dict[str, str]] = {"ListenerArn": "arn"}

    def convert(self, b: Builder) -> None:
        """The port, TLS settings, certificates and default actions."""
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


class ListenerCertificate(ResourceConverter):
    """AWS::ElasticLoadBalancingV2::ListenerCertificate as aws_lb_listener_certificate, one
    resource per certificate."""

    cfn_type = "AWS::ElasticLoadBalancingV2::ListenerCertificate"
    tf_type = "aws_lb_listener_certificate"

    def convert(self, b: Builder) -> None:
        """The listener and the first certificate; the others become resources of their
        own."""
        listener_arn = b.ctx.expr(b.props.take("ListenerArn"))
        arns = _certificate_arns(b)
        b.props.set("listener_arn", listener_arn)
        if arns:
            b.props.set("certificate_arn", b.ctx.expr(arns[0]))
            _extra_certificates(b, listener_arn, arns[1:], start=2)


class ListenerRule(ResourceConverter):
    """AWS::ElasticLoadBalancingV2::ListenerRule as aws_lb_listener_rule."""

    cfn_type = "AWS::ElasticLoadBalancingV2::ListenerRule"
    tf_type = "aws_lb_listener_rule"
    ref = "arn"
    attrs: ClassVar[dict[str, str]] = {"RuleArn": "arn"}
    # Condition Field -> (Terraform block, CloudFormation configuration property).
    condition_configs: ClassVar[dict[str, tuple[str, str]]] = {
        "path-pattern": ("path_pattern", "PathPatternConfig"),
        "host-header": ("host_header", "HostHeaderConfig"),
        "http-header": ("http_header", "HttpHeaderConfig"),
        "http-request-method": ("http_request_method", "HttpRequestMethodConfig"),
        "query-string": ("query_string", "QueryStringConfig"),
        "source-ip": ("source_ip", "SourceIpConfig"),
    }

    def convert(self, b: Builder) -> None:
        """The listener, the priority, actions, conditions and tags."""
        p = b.props
        p.attr("listener_arn", "ListenerArn")
        p.number("priority", "Priority")
        p.block("action", "Actions", _action)
        p.block("condition", "Conditions", self.fill_condition)
        p.tags()

    def fill_condition(self, c: Props) -> None:
        """A condition block."""
        field = c.take("Field")
        legacy = c.take("Values")
        if not isinstance(field, str) or field not in self.condition_configs:
            c.ctx.note(f"{c.owner}: the rule condition on '{field}' was left out. Set it by hand.")
            c.comment(f"The condition on {snippet(field)} was left out")
            return
        tf_name, cfn_name = self.condition_configs[field]
        config = c.literal(cfn_name, dict) or {}
        if field == "query-string":
            pairs = c.literal("Values", list, value=config.get("Values")) or []
            c.add_blocks(tf_name, pairs, self.fill_query_string, f"{c.where}.{cfn_name}.Values")
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

    def fill_query_string(self, q: Props) -> None:
        """A query_string block of a condition."""
        q.attr("key", "Key")
        q.attr("value", "Value")


# --- Route 53 ---------------------------------------------------------------------------


class HostedZone(ResourceConverter):
    """AWS::Route53::HostedZone as aws_route53_zone."""

    cfn_type = "AWS::Route53::HostedZone"
    tf_type = "aws_route53_zone"
    attrs: ClassVar[dict[str, str]] = {"Id": "zone_id", "NameServers": "name_servers"}

    def convert(self, b: Builder) -> None:
        """The name, the comment, the VPCs of a private zone and tags."""
        p = b.props
        p.attr("name", "Name")
        config = p.literal("HostedZoneConfig", dict) or {}
        # The default comment of the provider is "Managed by Terraform"; CloudFormation sets
        # none.
        comment = config.get("Comment")
        p.set("comment", "" if comment is None else b.ctx.expr(comment))
        p.block("vpc", "VPCs", self.fill_vpc)
        p.tags("HostedZoneTags")

    def fill_vpc(self, v: Props) -> None:
        """A vpc block."""
        v.attrs({"vpc_id": "VPCId", "vpc_region": "VPCRegion"})


def _zone_name(value: Any) -> str | None:
    """A literal zone name in the form names are compared in, else None."""
    return value.rstrip(".").lower() if isinstance(value, str) else None


class RecordSet(ResourceConverter):
    """AWS::Route53::RecordSet as aws_route53_record."""

    cfn_type = "AWS::Route53::RecordSet"
    tf_type = "aws_route53_record"
    ref = "fqdn"

    def convert(self, b: Builder) -> None:
        """The one record."""
        self.record(b, b.props)

    def record(self, b: Builder, p: Props) -> None:
        """One record set's properties `p` as aws_route53_record items."""
        ctx = b.ctx
        self.zone_id(b, p)
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
            evaluate = p.as_bool(alias.get("EvaluateTargetHealth", False))
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
                converted = p.as_number(value) if number else ctx.expr(value)
                p.items.append(Block(block, [], [Attr(tf_name, converted)]))
        p.block("geolocation_routing_policy", "GeoLocation", self.fill_geolocation)

    def fill_geolocation(self, g: Props) -> None:
        """The geolocation_routing_policy block."""
        g.attrs(
            {
                "continent": "ContinentCode",
                "country": "CountryCode",
                "subdivision": "SubdivisionCode",
            }
        )

    def zone_id(self, b: Builder, p: Props) -> None:
        """zone_id from HostedZoneId, or from HostedZoneName: a hosted zone of the same
        template when one has that name, a data source lookup otherwise."""
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


class RecordSetGroup(RecordSet):
    """AWS::Route53::RecordSetGroup as aws_route53_record resources."""

    cfn_type = "AWS::Route53::RecordSetGroup"

    def convert(self, b: Builder) -> None:
        """The first record is the main resource; each other record is a resource of its
        own."""
        p = b.props
        zone: dict[str, Any] = {}
        for name in ("HostedZoneId", "HostedZoneName"):
            value = p.take(name)
            if value is not None:
                zone[name] = value
        p.drop("Comment")
        records = p.take("RecordSets")
        if not isinstance(records, list) or not records:
            b.ctx.note(
                f"{b.logical_id}: RecordSets is empty or computed, so the converter left it out. "
                "Add the records by hand."
            )
            return
        for number, record in enumerate(records, start=1):
            values: dict[str, Any] = {}
            if isinstance(record, dict):
                values = dict(zone)
                values.update(record)
            inner = Props(b.ctx, values, f"{b.logical_id}.RecordSets[{number - 1}]", p.owner)
            self.record(b, inner)
            inner.finish()
            if number == 1:
                p.items.extend(inner.items)
            else:
                b.add_resource("aws_route53_record", f"_{number}", inner.items)


# --- ACM --------------------------------------------------------------------------------


def _dns_validated(resource: dict[str, Any]) -> bool:
    """Whether CloudFormation creates the validation records itself: DNS validation with a
    zone given."""
    properties = resource.get("Properties") or {}
    options = properties.get("DomainValidationOptions")
    return (
        properties.get("ValidationMethod") == "DNS"
        and isinstance(options, list)
        and any(isinstance(o, dict) and o.get("HostedZoneId") is not None for o in options)
    )


class Certificate(ResourceConverter):
    """AWS::CertificateManager::Certificate as aws_acm_certificate, with the DNS validation
    records and the wait for validation when CloudFormation handles those."""

    cfn_type = "AWS::CertificateManager::Certificate"
    tf_type = "aws_acm_certificate"
    ref = "arn"

    def prepare(self, info: ResourceInfo, resource: dict[str, Any]) -> None:
        """CloudFormation finishes the certificate only once it is validated; references
        wait for the same thing through the validation resource."""
        if _dns_validated(resource):
            index = "[0]" if info.conditional else ""
            info.overrides["arn"] = (
                f"aws_acm_certificate_validation.{info.name}{index}.certificate_arn"
            )

    def convert(self, b: Builder) -> None:
        """The names, the validation and the options."""
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
            self.validation_records(b, zones)
        elif method == "DNS":
            ctx.note(
                f"{b.logical_id} is validated by DNS, but the template names no hosted zone. Add "
                "the records from domain_validation_options and an "
                "aws_acm_certificate_validation resource."
            )

    def validation_records(self, b: Builder, zones: dict[str, Any]) -> None:
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
                BLANK,
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


class Distribution(ResourceConverter):
    """AWS::CloudFront::Distribution as aws_cloudfront_distribution."""

    cfn_type = "AWS::CloudFront::Distribution"
    tf_type = "aws_cloudfront_distribution"
    attrs: ClassVar[dict[str, str]] = {"DomainName": "domain_name", "Id": "id"}
    # The methods a cache behavior allows and caches when the template names none.
    default_methods: ClassVar[list[str]] = ["GET", "HEAD"]

    def convert(self, b: Builder) -> None:
        """DistributionConfig, flattened into the resource, and tags."""
        p = b.props
        p.tags()
        config = p.take("DistributionConfig")
        if not isinstance(config, dict) or intrinsic_name(config) is not None:
            b.ctx.note(
                f"{b.logical_id}: DistributionConfig is missing or computed, so the converter "
                "left it out. Set it by hand."
            )
            return
        c = Props(b.ctx, config, f"{b.logical_id}.DistributionConfig", p.owner)
        c.boolean("enabled", "Enabled")
        c.list_attr("aliases", "Aliases")
        c.attr("comment", "Comment")
        c.attr("default_root_object", "DefaultRootObject")
        if not c.attr("http_version", "HttpVersion"):
            c.set("http_version", "http1.1")  # The CloudFormation default; the provider has http2
        c.boolean("is_ipv6_enabled", "IPV6Enabled")
        c.attr("price_class", "PriceClass")
        c.attr("web_acl_id", "WebACLId")
        c.attr("continuous_deployment_policy_id", "ContinuousDeploymentPolicyId")
        c.boolean("staging", "Staging")
        c.block("origin", "Origins", self.fill_origin)
        groups = c.take("OriginGroups")
        if isinstance(groups, dict):
            where = f"{c.where}.OriginGroups"
            c.add_blocks("origin_group", groups.get("Items") or [], self.fill_origin_group, where)
        c.block("default_cache_behavior", "DefaultCacheBehavior", self.fill_default_behavior)
        c.block("ordered_cache_behavior", "CacheBehaviors", self.fill_ordered_behavior)
        c.block("custom_error_response", "CustomErrorResponses", self.fill_error_response)
        c.block("logging_config", "Logging", self.fill_logging)
        # Terraform requires both blocks; these are the CloudFormation defaults when left out.
        if not c.block("restrictions", "Restrictions", self.fill_restrictions):
            geo = Block("geo_restriction", [], [Attr("restriction_type", "none")])
            c.items.append(Block("restrictions", [], [geo]))
        if not c.block("viewer_certificate", "ViewerCertificate", self.fill_viewer_certificate):
            default = Attr("cloudfront_default_certificate", True)
            c.items.append(Block("viewer_certificate", [], [default]))
        c.finish()
        p.items.extend(c.items)

    def fill_error_response(self, r: Props) -> None:
        """A custom_error_response block."""
        r.number("error_code", "ErrorCode")
        r.number("response_code", "ResponseCode")
        r.attr("response_page_path", "ResponsePagePath")
        r.number("error_caching_min_ttl", "ErrorCachingMinTTL")

    def fill_logging(self, r: Props) -> None:
        """The logging_config block."""
        r.attr("bucket", "Bucket")
        r.boolean("include_cookies", "IncludeCookies")
        r.attr("prefix", "Prefix")

    def fill_origin(self, r: Props) -> None:
        """An origin block."""
        ctx = r.ctx
        r.attr("domain_name", "DomainName")
        r.attr("origin_id", "Id")
        r.attr("origin_path", "OriginPath")
        r.number("connection_attempts", "ConnectionAttempts")
        r.number("connection_timeout", "ConnectionTimeout")
        r.number("response_completion_timeout", "ResponseCompletionTimeout")
        r.attr("origin_access_control_id", "OriginAccessControlId")
        _blocks(r, "custom_header", "OriginCustomHeaders", self.fill_custom_header)
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
                    "equivalent for S3 origins, so it was left out."
                )
        r.block("custom_origin_config", "CustomOriginConfig", self.fill_custom_origin)
        r.block("origin_shield", "OriginShield", self.fill_origin_shield)
        r.block("vpc_origin_config", "VpcOriginConfig", self.fill_vpc_origin)

    def fill_custom_header(self, h: Props) -> None:
        """A custom_header block of an origin."""
        h.attr("name", "HeaderName")
        h.attr("value", "HeaderValue")

    def fill_origin_shield(self, s: Props) -> None:
        """The origin_shield block of an origin."""
        s.boolean("enabled", "Enabled")
        s.attr("origin_shield_region", "OriginShieldRegion")

    def fill_vpc_origin(self, v: Props) -> None:
        """The vpc_origin_config block of an origin."""
        v.attr("vpc_origin_id", "VpcOriginId")
        v.number("origin_keepalive_timeout", "OriginKeepaliveTimeout")
        v.number("origin_read_timeout", "OriginReadTimeout")
        v.attr("owner_account_id", "OwnerAccountId")

    def fill_custom_origin(self, c: Props) -> None:
        """The custom_origin_config block of an origin."""
        # Ports and protocols are optional in CloudFormation but required by the provider.
        if not c.number("http_port", "HTTPPort"):
            c.set("http_port", 80)
        if not c.number("https_port", "HTTPSPort"):
            c.set("https_port", 443)
        c.attr("origin_protocol_policy", "OriginProtocolPolicy")
        if not c.list_attr("origin_ssl_protocols", "OriginSSLProtocols"):
            c.set("origin_ssl_protocols", ["TLSv1.2"])
            c.ctx.note(
                f"{c.owner}: '{c.where}' leaves out OriginSSLProtocols. Terraform requires them, "
                "so the converter uses TLSv1.2."
            )
        c.number("origin_read_timeout", "OriginReadTimeout")
        c.number("origin_keepalive_timeout", "OriginKeepaliveTimeout")
        c.attr("ip_address_type", "IpAddressType")

    def fill_origin_group(self, g: Props) -> None:
        """An origin_group block."""
        g.attr("origin_id", "Id")
        criteria = g.literal("FailoverCriteria", dict) or {}
        codes = g.literal("FailoverCriteria.StatusCodes", dict, value=criteria.get("StatusCodes"))
        items = g.literal("StatusCodes.Items", list, value=(codes or {}).get("Items")) or []
        status_codes = Attr("status_codes", [g.as_number(code) for code in items])
        g.items.append(Block("failover_criteria", [], [status_codes]))
        members = g.literal("Members", dict) or {}
        member_items = g.literal("Members.Items", list, value=members.get("Items")) or []
        g.add_blocks("member", member_items, self.fill_member, f"{g.where}.Members")

    def fill_member(self, m: Props) -> None:
        """A member block of an origin group."""
        m.attr("origin_id", "OriginId")

    def methods(self, r: Props, tf_name: str, cfn_name: str) -> None:
        """A list of methods, GET and HEAD when the template names none."""
        if not r.list_attr(tf_name, cfn_name):
            r.set(tf_name, list(self.default_methods))

    def fill_default_behavior(self, r: Props) -> None:
        """The default_cache_behavior block."""
        self.behavior(r, ordered=False)

    def fill_ordered_behavior(self, r: Props) -> None:
        """An ordered_cache_behavior block."""
        self.behavior(r, ordered=True)

    def behavior(self, r: Props, ordered: bool) -> None:
        """A cache behavior block; an `ordered` one has a path pattern."""
        if ordered:
            r.attr("path_pattern", "PathPattern")
        self.methods(r, "allowed_methods", "AllowedMethods")
        self.methods(r, "cached_methods", "CachedMethods")
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
        _blocks(r, "function_association", "FunctionAssociations", self.fill_function)
        _blocks(r, "lambda_function_association", "LambdaFunctionAssociations", self.fill_lambda)
        r.block("forwarded_values", "ForwardedValues", self.fill_forwarded_values)

    def fill_function(self, a: Props) -> None:
        """A function_association block."""
        a.attr("event_type", "EventType")
        a.attr("function_arn", "FunctionARN")

    def fill_lambda(self, a: Props) -> None:
        """A lambda_function_association block."""
        a.attr("event_type", "EventType")
        a.attr("lambda_arn", "LambdaFunctionARN")
        a.boolean("include_body", "IncludeBody")

    def fill_forwarded_values(self, f: Props) -> None:
        """The forwarded_values block, with the query_string and cookies the provider
        requires."""
        if not f.boolean("query_string", "QueryString"):
            f.set("query_string", False)
        f.list_attr("headers", "Headers")
        f.list_attr("query_string_cache_keys", "QueryStringCacheKeys")
        if not f.block("cookies", "Cookies", self.fill_cookies):
            # Required by the provider; "none" is the CloudFormation default.
            f.items.append(Block("cookies", [], [Attr("forward", "none")]))

    def fill_cookies(self, c: Props) -> None:
        """The cookies block of forwarded_values."""
        c.attr("forward", "Forward")
        c.list_attr("whitelisted_names", "WhitelistedNames")

    def fill_restrictions(self, r: Props) -> None:
        """The restrictions block."""
        r.block("geo_restriction", "GeoRestriction", self.fill_geo_restriction)

    def fill_geo_restriction(self, g: Props) -> None:
        """The geo_restriction block of restrictions."""
        g.attr("restriction_type", "RestrictionType")
        g.list_attr("locations", "Locations")

    def fill_viewer_certificate(self, v: Props) -> None:
        """The viewer_certificate block."""
        v.attrs(
            {
                "acm_certificate_arn": "AcmCertificateArn",
                "iam_certificate_id": "IamCertificateId",
                "minimum_protocol_version": "MinimumProtocolVersion",
                "ssl_support_method": "SslSupportMethod",
            }
        )
        v.boolean("cloudfront_default_certificate", "CloudFrontDefaultCertificate")


class OriginAccessControl(ResourceConverter):
    """AWS::CloudFront::OriginAccessControl as aws_cloudfront_origin_access_control."""

    cfn_type = "AWS::CloudFront::OriginAccessControl"
    tf_type = "aws_cloudfront_origin_access_control"
    attrs: ClassVar[dict[str, str]] = {"Id": "id"}

    def convert(self, b: Builder) -> None:
        """OriginAccessControlConfig, flattened into the resource."""
        _nested(b, "OriginAccessControlConfig", self.fill_config)

    def fill_config(self, c: Props) -> None:
        """The attributes from OriginAccessControlConfig."""
        c.attrs(
            {
                "name": "Name",
                "description": "Description",
                "origin_access_control_origin_type": "OriginAccessControlOriginType",
                "signing_behavior": "SigningBehavior",
                "signing_protocol": "SigningProtocol",
            }
        )


class OriginAccessIdentity(ResourceConverter):
    """AWS::CloudFront::CloudFrontOriginAccessIdentity as
    aws_cloudfront_origin_access_identity."""

    cfn_type = "AWS::CloudFront::CloudFrontOriginAccessIdentity"
    tf_type = "aws_cloudfront_origin_access_identity"
    attrs: ClassVar[dict[str, str]] = {"Id": "id", "S3CanonicalUserId": "s3_canonical_user_id"}

    def convert(self, b: Builder) -> None:
        """CloudFrontOriginAccessIdentityConfig, flattened into the resource."""
        _nested(b, "CloudFrontOriginAccessIdentityConfig", self.fill_config)

    def fill_config(self, c: Props) -> None:
        """The comment from CloudFrontOriginAccessIdentityConfig."""
        c.attr("comment", "Comment")


CONVERTERS: tuple[type[ResourceConverter], ...] = (
    Vpc,
    Subnet,
    InternetGateway,
    EgressOnlyInternetGateway,
    VpcGatewayAttachment,
    RouteTable,
    Route,
    SubnetRouteTableAssociation,
    Eip,
    EipAssociation,
    NatGateway,
    VpcEndpoint,
    SecurityGroup,
    SecurityGroupIngress,
    SecurityGroupEgress,
    NetworkAcl,
    NetworkAclEntry,
    SubnetNetworkAclAssociation,
    FlowLog,
    LoadBalancer,
    TargetGroup,
    Listener,
    ListenerCertificate,
    ListenerRule,
    HostedZone,
    RecordSet,
    RecordSetGroup,
    Certificate,
    Distribution,
    OriginAccessControl,
    OriginAccessIdentity,
)
