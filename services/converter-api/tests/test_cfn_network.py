"""Network mappings: VPC, security groups, load balancers, Route 53, ACM, CloudFront."""

from __future__ import annotations

from pathlib import Path

import yaml

from converters.cfn import convert_template

FIXTURE = Path(__file__).parent / "fixtures" / "cfn" / "network.yaml"


def convert(resources: dict, **extra) -> tuple[str, list[str]]:
    template = {"Resources": resources, **extra}
    result = convert_template(
        yaml.safe_dump(template, sort_keys=False), layout="single", stack_name="demo"
    )
    # Whitespace-insensitive: alignment depends on the neighbouring attributes.
    return " ".join(result["files"][0]["content"].split()), result["notes"]


def test_fixture_converts_without_notes():
    result = convert_template(FIXTURE.read_text(), stack_name="demo")
    assert result["notes"] == []
    assert result["summary"]["aws"] == result["summary"]["resources"]
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    assert 'resource "aws_acm_certificate_validation" "certificate"' in main
    assert "cloudfront_default_certificate = true" in main


# --- VPC basics ---------------------------------------------------------------------------


def test_vpc_subnet_and_ipv6_lists():
    hcl, notes = convert(
        {
            "Vpc": {"Type": "AWS::EC2::VPC", "Properties": {"CidrBlock": "10.0.0.0/16"}},
            "Subnet": {
                "Type": "AWS::EC2::Subnet",
                "Properties": {
                    "VpcId": {"Ref": "Vpc"},
                    "CidrBlock": "10.0.0.0/24",
                    "PrivateDnsNameOptionsOnLaunch": {
                        "HostnameType": "resource-name",
                        "EnableResourceNameDnsARecord": "true",
                        "EnableResourceNameDnsAAAARecord": False,
                        "Other": 1,
                    },
                },
            },
        },
        Outputs={
            "VpcV6": {"Value": {"Fn::Select": [0, {"Fn::GetAtt": ["Vpc", "Ipv6CidrBlocks"]}]}},
            "SubnetV6": {
                "Value": {"Fn::Select": [0, {"Fn::GetAtt": ["Subnet", "Ipv6CidrBlocks"]}]}
            },
        },
    )
    assert "element([aws_vpc.vpc.ipv6_cidr_block], 0)" in hcl
    assert "element([aws_subnet.subnet.ipv6_cidr_block], 0)" in hcl
    assert 'private_dns_hostname_type_on_launch = "resource-name"' in hcl
    assert "enable_resource_name_dns_a_record_on_launch = true" in hcl
    assert "enable_resource_name_dns_aaaa_record_on_launch = false" in hcl
    assert any("PrivateDnsNameOptionsOnLaunch.Other" in n for n in notes)


def test_gateways_routes_and_eip_association():
    hcl, notes = convert(
        {
            "Egress": {
                "Type": "AWS::EC2::EgressOnlyInternetGateway",
                "Properties": {"VpcId": "vpc-1"},
            },
            "Vpn": {
                "Type": "AWS::EC2::VPCGatewayAttachment",
                "Properties": {"VpcId": "vpc-1", "VpnGatewayId": "vgw-1"},
            },
            "ToInstance": {
                "Type": "AWS::EC2::Route",
                "Properties": {"RouteTableId": "rtb-1", "InstanceId": "i-1"},
            },
            "Assoc": {
                "Type": "AWS::EC2::EIPAssociation",
                "Properties": {"AllocationId": "eipalloc-1", "InstanceId": "i-1"},
            },
        }
    )
    assert 'resource "aws_egress_only_internet_gateway" "egress"' in hcl
    assert 'resource "aws_vpn_gateway_attachment" "vpn"' in hcl
    assert 'vpn_gateway_id = "vgw-1"' in hcl
    assert 'resource "aws_eip_association" "assoc"' in hcl
    assert any("routes to an instance" in n for n in notes)
    assert any("ToInstance.InstanceId" in n for n in notes)


# --- security groups ----------------------------------------------------------------------


def test_inline_rules_ports_protocols_and_default_egress():
    hcl, notes = convert(
        {
            "Sg": {
                "Type": "AWS::EC2::SecurityGroup",
                "Properties": {
                    "GroupDescription": "x",
                    "SecurityGroupIngress": [
                        {"IpProtocol": "icmp", "CidrIp": "10.0.0.0/8"},
                        {"IpProtocol": "tcp", "FromPort": 22, "SourceSecurityGroupId": "sg-1"},
                        {"IpProtocol": "udp", "SourcePrefixListId": "pl-1"},
                        {"IpProtocol": {"Ref": "Proto"}, "FromPort": 1, "ToPort": 2},
                    ],
                },
            }
        },
        Parameters={"Proto": {"Type": "String"}},
    )
    assert 'from_port = -1 to_port = -1 protocol = "icmp"' in hcl
    assert 'from_port = 22 to_port = 22 protocol = "tcp"' in hcl
    assert 'security_groups = ["sg-1"]' in hcl
    assert "to_port = 65535" in hcl and 'prefix_list_ids = ["pl-1"]' in hcl
    assert "protocol = var.proto" in hcl
    assert "CloudFormation adds this allow-all egress rule" in hcl
    assert notes == []


def test_inline_egress_and_conditional_rule():
    hcl, _ = convert(
        {
            "Sg": {
                "Type": "AWS::EC2::SecurityGroup",
                "Properties": {
                    "GroupDescription": "x",
                    "SecurityGroupIngress": [
                        {
                            "Fn::If": [
                                "Open",
                                {
                                    "IpProtocol": "tcp",
                                    "FromPort": 80,
                                    "ToPort": 80,
                                    "CidrIp": "0.0.0.0/0",
                                },
                                {"Ref": "AWS::NoValue"},
                            ]
                        }
                    ],
                    "SecurityGroupEgress": [
                        {"IpProtocol": "-1", "DestinationSecurityGroupId": "sg-2"},
                        {
                            "IpProtocol": 6,
                            "FromPort": 443,
                            "ToPort": 443,
                            "DestinationPrefixListId": "pl-2",
                        },
                    ],
                },
            }
        },
        Conditions={"Open": {"Fn::Equals": ["a", "a"]}},
    )
    assert 'dynamic "ingress"' in hcl and "for_each = local.open ? [1] : []" in hcl
    assert 'security_groups = ["sg-2"]' in hcl
    assert 'protocol = "6"' in hcl and 'prefix_list_ids = ["pl-2"]' in hcl
    assert "allow-all egress" not in hcl


def test_group_with_standalone_rules_gets_rule_resources():
    hcl, notes = convert(
        {
            "Sg": {
                "Type": "AWS::EC2::SecurityGroup",
                "Properties": {
                    "GroupDescription": "x",
                    "SecurityGroupIngress": [
                        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIpv6": "::/0"}
                    ],
                },
            },
            "Other": {
                "Type": "AWS::EC2::SecurityGroup",
                "Properties": {
                    "GroupDescription": "y",
                    "SecurityGroupIngress": {"Fn::If": ["C", [], []]},
                    "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}],
                },
            },
            "In": {
                "Type": "AWS::EC2::SecurityGroupIngress",
                "Properties": {
                    "GroupId": {"Fn::GetAtt": ["Sg", "GroupId"]},
                    "IpProtocol": {"Ref": "Proto"},
                    "SourceSecurityGroupId": {"Ref": "Other"},
                },
            },
            "Out": {
                "Type": "AWS::EC2::SecurityGroupEgress",
                "Properties": {
                    "GroupId": {"Fn::GetAtt": "Other.GroupId"},
                    "IpProtocol": "tcp",
                    "FromPort": 5432,
                    "ToPort": 5432,
                    "DestinationPrefixListId": "pl-1",
                    "GroupName": "default",
                },
            },
            "In2": {
                "Type": "AWS::EC2::SecurityGroupIngress",
                "Properties": {
                    "GroupId": {"Ref": "Other"},
                    "IpProtocol": "-1",
                    "CidrIp": "10.0.0.0/8",
                },
            },
            "Out2": {
                "Type": "AWS::EC2::SecurityGroupEgress",
                "Properties": {"GroupId": {"Fn::GetAtt": "Other.GroupId"}, "IpProtocol": "-1"},
            },
        },
        Parameters={"Proto": {"Type": "String"}},
        Conditions={"C": {"Fn::Equals": ["a", "b"]}},
    )
    assert 'resource "aws_vpc_security_group_ingress_rule" "sg_ingress_1"' in hcl
    assert 'resource "aws_vpc_security_group_egress_rule" "other_egress_1"' in hcl
    assert 'resource "aws_vpc_security_group_egress_rule" "sg_egress_1"' in hcl
    assert 'cidr_ipv6 = "::/0"' in hcl
    assert "ingress {" not in hcl
    assert any("Other: the ingress rules are computed" in n for n in notes)
    assert "ip_protocol = var.proto" in hcl
    assert 'prefix_list_id = "pl-1"' in hcl
    assert any("Out.GroupName" in n for n in notes)


# --- network ACLs and flow logs -----------------------------------------------------------


def test_network_acl_entry_and_flow_log():
    hcl, notes = convert(
        {
            "Entry": {
                "Type": "AWS::EC2::NetworkAclEntry",
                "Properties": {
                    "NetworkAclId": "acl-1",
                    "RuleNumber": "200",
                    "Protocol": {"Ref": "Proto"},
                    "Egress": "true",
                    "RuleAction": "deny",
                    "Ipv6CidrBlock": "::/0",
                    "Icmp": {"Type": -1, "Code": -1},
                },
            },
            "Logs": {
                "Type": "AWS::EC2::FlowLog",
                "Properties": {
                    "ResourceType": "VPC",
                    "ResourceId": "vpc-1",
                    "TrafficType": "ALL",
                    "LogGroupName": "flow-logs",
                    "DeliverLogsPermissionArn": "arn:role",
                    "MaxAggregationInterval": 60,
                    "DestinationOptions": {"FileFormat": "parquet", "PerHourPartition": True},
                },
            },
        },
        Parameters={"Proto": {"Type": "String"}},
    )
    assert "protocol = var.proto" in hcl and "icmp_type = -1" in hcl
    assert "egress = true" in hcl
    assert 'vpc_id = "vpc-1"' in hcl
    assert (
        "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:"
        "${data.aws_caller_identity.current.account_id}:log-group:flow-logs" in hcl
    )
    assert 'file_format = "parquet"' in hcl
    assert notes == []


# --- load balancers -----------------------------------------------------------------------


def test_load_balancer_attributes_and_logs():
    hcl, notes = convert(
        {
            "Lb": {
                "Type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
                "Properties": {
                    "Scheme": {"Ref": "Scheme"},
                    "Type": "network",
                    "SubnetMappings": [{"SubnetId": "subnet-1", "AllocationId": "eipalloc-1"}],
                    "LoadBalancerAttributes": [
                        {"Key": "access_logs.s3.enabled", "Value": "true"},
                        {"Key": "connection_logs.s3.bucket", "Value": "logs"},
                        {"Key": "load_balancing.cross_zone.enabled", "Value": "true"},
                        {"Key": "routing.http.desync_mitigation_mode", "Value": "strictest"},
                        {"Key": "made.up", "Value": "1"},
                    ],
                },
            },
            "Internal": {
                "Type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
                "Properties": {
                    "Scheme": "internal",
                    "LoadBalancerAttributes": {"Fn::If": ["C", [], []]},
                },
            },
        },
        Parameters={"Scheme": {"Type": "String"}},
        Conditions={"C": {"Fn::Equals": ["a", "b"]}},
    )
    assert 'internal = var.scheme == "internal"' in hcl
    assert "internal = true" in hcl
    assert 'access_logs { bucket = "" enabled = true }' in hcl
    assert 'connection_logs { bucket = "logs" }' in hcl
    assert "enable_cross_zone_load_balancing = true" in hcl
    assert 'desync_mitigation_mode = "strictest"' in hcl
    assert "subnet_mapping {" in hcl and 'allocation_id = "eipalloc-1"' in hcl
    assert any("LoadBalancerAttributes 'made.up'" in n for n in notes)
    assert any("Internal.LoadBalancerAttributes' is computed" in n for n in notes)


def test_target_group_attributes_health_and_targets():
    hcl, notes = convert(
        {
            "Tg": {
                "Type": "AWS::ElasticLoadBalancingV2::TargetGroup",
                "Properties": {
                    "Port": 50051,
                    "Protocol": "HTTP",
                    "ProtocolVersion": "GRPC",
                    "Matcher": {"GrpcCode": "0-99"},
                    "TargetGroupAttributes": [
                        {"Key": "stickiness.enabled", "Value": "true"},
                        {"Key": "stickiness.type", "Value": "app_cookie"},
                        {"Key": "stickiness.app_cookie.cookie_name", "Value": "sid"},
                        {"Key": "slow_start.duration_seconds", "Value": "30"},
                        {"Key": "unknown.key", "Value": "x"},
                    ],
                    "Targets": [{"Id": "i-1", "Port": 80}, {"Id": "i-2"}],
                },
            },
            "Computed": {
                "Type": "AWS::ElasticLoadBalancingV2::TargetGroup",
                "Properties": {"Targets": {"Fn::If": ["C", [], []]}, "Matcher": {}},
            },
        },
        Conditions={"C": {"Fn::Equals": ["a", "b"]}},
    )
    assert 'matcher = "0-99"' in hcl
    assert 'type = "app_cookie"' in hcl and 'cookie_name = "sid"' in hcl
    assert "slow_start = 30" in hcl
    assert 'resource "aws_lb_target_group_attachment" "tg_target_2"' in hcl
    assert "target_group_arn = aws_lb_target_group.tg.arn" in hcl
    assert any("'unknown.key'" in n for n in notes)
    assert any("Computed: Targets is computed" in n for n in notes)
    assert "health_check" not in hcl.split('"computed"')[1]


def test_listener_actions_and_certificates():
    hcl, notes = convert(
        {
            "Listener": {
                "Type": "AWS::ElasticLoadBalancingV2::Listener",
                "Properties": {
                    "LoadBalancerArn": "arn:lb",
                    "Port": "443",
                    "Protocol": "TLS",
                    "AlpnPolicy": ["HTTP2Preferred"],
                    "Certificates": [{"CertificateArn": "arn:c1"}, {"CertificateArn": "arn:c2"}],
                    "DefaultActions": [
                        {
                            "Type": "authenticate-cognito",
                            "Order": 1,
                            "AuthenticateCognitoConfig": {
                                "UserPoolArn": "arn:pool",
                                "UserPoolClientId": "client",
                                "UserPoolDomain": "auth",
                                "SessionTimeout": "3600",
                            },
                        },
                        {
                            "Type": "authenticate-oidc",
                            "Order": 2,
                            "AuthenticateOidcConfig": {
                                "AuthorizationEndpoint": "https://a",
                                "ClientId": "c",
                                "ClientSecret": "s",
                                "Issuer": "https://i",
                                "TokenEndpoint": "https://t",
                                "UserInfoEndpoint": "https://u",
                                "UseExistingClientSecret": False,
                            },
                        },
                        {
                            "Type": "forward",
                            "Order": 3,
                            "ForwardConfig": {
                                "TargetGroups": [
                                    {"TargetGroupArn": "arn:tg1", "Weight": "80"},
                                    {"TargetGroupArn": "arn:tg2"},
                                ],
                                "TargetGroupStickinessConfig": {
                                    "Enabled": True,
                                    "DurationSeconds": 60,
                                },
                            },
                        },
                        {"Type": "forward", "TargetGroupArn": "arn:tg1", "ForwardConfig": {}},
                    ],
                },
            },
            "Computed": {
                "Type": "AWS::ElasticLoadBalancingV2::Listener",
                "Properties": {
                    "LoadBalancerArn": "arn:lb",
                    "Certificates": {"Fn::If": ["C", [], []]},
                    "AlpnPolicy": "None",
                },
            },
            "Extra": {
                "Type": "AWS::ElasticLoadBalancingV2::ListenerCertificate",
                "Properties": {
                    "ListenerArn": {"Ref": "Listener"},
                    "Certificates": [{"CertificateArn": "arn:c3"}, {"CertificateArn": "arn:c4"}],
                },
            },
        },
        Conditions={"C": {"Fn::Equals": ["a", "b"]}},
    )
    assert 'alpn_policy = "HTTP2Preferred"' in hcl
    assert 'certificate_arn = "arn:c1"' in hcl
    assert 'resource "aws_lb_listener_certificate" "listener_certificate_2"' in hcl
    assert 'certificate_arn = "arn:c2"' in hcl
    assert 'user_pool_domain = "auth"' in hcl and "session_timeout = 3600" in hcl
    assert 'user_info_endpoint = "https://u"' in hcl
    assert "weight = 80" in hcl and "duration = 60" in hcl
    assert hcl.count('target_group { arn = "arn:tg') == 2
    assert 'alpn_policy = "None"' in hcl
    assert any("Computed: Certificates is computed" in n for n in notes)
    assert 'resource "aws_lb_listener_certificate" "extra"' in hcl
    assert "listener_arn = aws_lb_listener.listener.arn" in hcl
    assert 'resource "aws_lb_listener_certificate" "extra_certificate_2"' in hcl
    assert not any("ForwardConfig" in n for n in notes)


def test_listener_rule_conditions():
    hcl, notes = convert(
        {
            "Rule": {
                "Type": "AWS::ElasticLoadBalancingV2::ListenerRule",
                "Properties": {
                    "ListenerArn": "arn:l",
                    "Priority": 5,
                    "Actions": [{"Type": "forward", "TargetGroupArn": "arn:tg"}],
                    "Conditions": [
                        {"Field": "host-header", "Values": ["a.example.com"]},
                        {
                            "Field": "http-header",
                            "HttpHeaderConfig": {
                                "HttpHeaderName": "X-Env",
                                "Values": ["blue"],
                            },
                        },
                        {
                            "Field": "query-string",
                            "QueryStringConfig": {
                                "Values": [{"Key": "v", "Value": "2"}, {"Value": "beta"}],
                            },
                        },
                        {"Field": "source-ip", "SourceIpConfig": {"Values": "10.0.0.0/8"}},
                        {"Field": "path-pattern", "PathPatternConfig": {"RegexValues": ["^/a"]}},
                        {"Field": "made-up"},
                    ],
                },
            }
        }
    )
    assert 'host_header { values = ["a.example.com"]' in hcl
    assert 'http_header_name = "X-Env"' in hcl
    assert 'key = "v"' in hcl and 'value = "beta"' in hcl
    assert 'values = ["10.0.0.0/8"]' in hcl
    assert 'regex_values = ["^/a"]' in hcl
    assert any("rule condition 'made-up'" in n for n in notes)


# --- Route 53 -----------------------------------------------------------------------------


def test_hosted_zone_and_records():
    hcl, notes = convert(
        {
            "Zone": {
                "Type": "AWS::Route53::HostedZone",
                "Properties": {
                    "Name": "Example.com",
                    "VPCs": [{"VPCId": "vpc-1", "VPCRegion": "eu-west-1"}],
                    "HostedZoneTags": [{"Key": "team", "Value": "web"}],
                },
            },
            "Local": {
                "Type": "AWS::Route53::RecordSet",
                "Properties": {
                    "HostedZoneName": "example.com.",
                    "Name": "a.example.com",
                    "Type": "A",
                    "TTL": 60,
                    "ResourceRecords": ["1.2.3.4"],
                    "SetIdentifier": "eu",
                    "Weight": "10",
                    "Comment": "ignored",
                },
            },
            "Remote": {
                "Type": "AWS::Route53::RecordSet",
                "Condition": "C",
                "Properties": {
                    "HostedZoneName": "other.com.",
                    "Name": "b.other.com",
                    "Type": "CNAME",
                    "TTL": "60",
                    "ResourceRecords": ["x.other.com"],
                    "SetIdentifier": "x",
                    "Region": "eu-west-1",
                    "Failover": "PRIMARY",
                    "GeoLocation": {"ContinentCode": "EU"},
                    "HealthCheckId": "hc-1",
                },
            },
            "NoZone": {
                "Type": "AWS::Route53::RecordSet",
                "Properties": {"Name": "d", "Type": "A", "ResourceRecords": ["1.1.1.1"]},
            },
            "Computed": {
                "Type": "AWS::Route53::RecordSet",
                "Properties": {"HostedZoneName": {"Ref": "ZoneName"}, "Name": "c", "Type": "A"},
            },
        },
        Parameters={"ZoneName": {"Type": "String"}},
        Conditions={"C": {"Fn::Equals": ["a", "b"]}},
    )
    assert 'vpc { vpc_id = "vpc-1"' in hcl
    assert "team = " in hcl
    assert "zone_id = aws_route53_zone.zone.id" in hcl
    assert "weighted_routing_policy { weight = 10" in hcl
    assert 'data "aws_route53_zone" "remote_zone"' in hcl
    assert "zone_id = data.aws_route53_zone.remote_zone[0].zone_id" in hcl
    assert 'latency_routing_policy { region = "eu-west-1"' in hcl
    assert 'failover_routing_policy { type = "PRIMARY"' in hcl
    assert 'continent = "EU"' in hcl
    assert "name = var.zone_name" in hcl
    assert "data.aws_route53_zone.computed_zone.zone_id" in hcl
    assert notes == []


def test_record_set_group():
    hcl, notes = convert(
        {
            "Group": {
                "Type": "AWS::Route53::RecordSetGroup",
                "Properties": {
                    "HostedZoneName": "example.com.",
                    "Comment": "x",
                    "RecordSets": [
                        {
                            "Name": "a.example.com",
                            "Type": "A",
                            "TTL": 60,
                            "ResourceRecords": ["1.1.1.1"],
                        },
                        {
                            "Name": "b.example.com",
                            "Type": "A",
                            "AliasTarget": {
                                "DNSName": "d.cloudfront.net",
                                "HostedZoneId": "Z2FDTNDATAQYW2",
                            },
                        },
                    ],
                },
            },
            "Empty": {"Type": "AWS::Route53::RecordSetGroup", "Properties": {"RecordSets": []}},
        }
    )
    assert hcl.count('data "aws_route53_zone" "group_zone"') == 1
    assert 'resource "aws_route53_record" "group_2"' in hcl
    assert "evaluate_target_health = false" in hcl
    assert any("Empty: RecordSets is empty" in n for n in notes)


# --- ACM ----------------------------------------------------------------------------------


def test_certificate_validation_variants():
    hcl, notes = convert(
        {
            "Email": {
                "Type": "AWS::CertificateManager::Certificate",
                "Properties": {
                    "DomainName": "a.com",
                    "ValidationMethod": "EMAIL",
                    "CertificateTransparencyLoggingPreference": "DISABLED",
                    "DomainValidationOptions": [
                        {"DomainName": "a.com", "ValidationDomain": "a.com"},
                        "not-a-mapping",
                    ],
                },
            },
            "NoZone": {
                "Type": "AWS::CertificateManager::Certificate",
                "Properties": {"DomainName": "b.com", "ValidationMethod": "DNS"},
            },
            "TwoZones": {
                "Type": "AWS::CertificateManager::Certificate",
                "Condition": "C",
                "Properties": {
                    "DomainName": "c.com",
                    "SubjectAlternativeNames": ["d.com"],
                    "ValidationMethod": "DNS",
                    "DomainValidationOptions": [
                        {"DomainName": "c.com", "HostedZoneId": "Z1"},
                        {"DomainName": "d.com", "HostedZoneId": "Z2"},
                    ],
                },
            },
        },
        Conditions={"C": {"Fn::Equals": ["a", "b"]}},
        Outputs={"Arn": {"Value": {"Ref": "TwoZones"}, "Condition": "C"}},
    )
    assert 'certificate_transparency_logging_preference = "DISABLED"' in hcl
    assert 'validation_option { domain_name = "a.com"' in hcl
    assert any("NoZone is validated by DNS without a hosted zone" in n for n in notes)
    assert "flatten(aws_acm_certificate.two_zones[*].domain_validation_options)" in hcl
    assert 'lookup({ "c.com" = "Z1", "d.com" = "Z2" }, each.key, "Z1")' in hcl
    assert "certificate_arn = aws_acm_certificate.two_zones[0].arn" in hcl
    assert "aws_acm_certificate_validation.two_zones[0].certificate_arn" in hcl


# --- CloudFront ---------------------------------------------------------------------------


def test_distribution_full_config():
    hcl, notes = convert(
        {
            "Oai": {
                "Type": "AWS::CloudFront::CloudFrontOriginAccessIdentity",
                "Properties": {"CloudFrontOriginAccessIdentityConfig": {"Comment": "site"}},
            },
            "Cdn": {
                "Type": "AWS::CloudFront::Distribution",
                "Properties": {
                    "Tags": [{"Key": "app", "Value": "site"}],
                    "DistributionConfig": {
                        "Enabled": True,
                        "Aliases": ["www.example.com"],
                        "Logging": {"Bucket": "logs.s3.amazonaws.com", "IncludeCookies": False},
                        "Origins": [
                            {
                                "Id": "s3",
                                "DomainName": "b.s3.amazonaws.com",
                                "OriginCustomHeaders": [{"HeaderName": "X-A", "HeaderValue": "1"}],
                                "S3OriginConfig": {
                                    "OriginAccessIdentity": {
                                        "Fn::Sub": "origin-access-identity/cloudfront/${Oai}"
                                    },
                                    "OriginReadTimeout": 30,
                                },
                                "OriginShield": {
                                    "Enabled": True,
                                    "OriginShieldRegion": "us-east-1",
                                },
                            },
                            {
                                "Id": "web",
                                "DomainName": "example.org",
                                "CustomOriginConfig": {"OriginProtocolPolicy": "match-viewer"},
                            },
                            {
                                "Id": "vpc",
                                "DomainName": "internal",
                                "VpcOriginConfig": {"VpcOriginId": "vo-1"},
                            },
                        ],
                        "OriginGroups": {
                            "Quantity": 1,
                            "Items": [
                                {
                                    "Id": "group",
                                    "FailoverCriteria": {"StatusCodes": {"Items": [500, "502"]}},
                                    "Members": {"Items": [{"OriginId": "s3"}, {"OriginId": "web"}]},
                                }
                            ],
                        },
                        "DefaultCacheBehavior": {
                            "TargetOriginId": "group",
                            "ViewerProtocolPolicy": "allow-all",
                            "DefaultTTL": "60",
                            "FunctionAssociations": [
                                {"EventType": "viewer-request", "FunctionARN": "arn:fn"}
                            ],
                            "LambdaFunctionAssociations": [
                                {
                                    "EventType": "origin-request",
                                    "LambdaFunctionARN": "arn:l",
                                    "IncludeBody": "true",
                                },
                                {"EventType": "origin-response", "LambdaFunctionARN": "arn:l2"},
                            ],
                            "ForwardedValues": {
                                "Cookies": {"Forward": "whitelist", "WhitelistedNames": ["s"]}
                            },
                        },
                        "CacheBehaviors": [
                            {
                                "PathPattern": "/x",
                                "TargetOriginId": "web",
                                "ViewerProtocolPolicy": "allow-all",
                                "ForwardedValues": {"QueryString": "false"},
                            }
                        ],
                        "Restrictions": {
                            "GeoRestriction": {"RestrictionType": "whitelist", "Locations": ["DE"]}
                        },
                        "ViewerCertificate": {
                            "AcmCertificateArn": "arn:cert",
                            "SslSupportMethod": "sni-only",
                            "MinimumProtocolVersion": "TLSv1.2_2021",
                        },
                    },
                },
            },
            "Broken": {
                "Type": "AWS::CloudFront::Distribution",
                "Properties": {"DistributionConfig": {"Fn::If": ["C", {}, {}]}},
            },
        },
        Conditions={"C": {"Fn::Equals": ["a", "b"]}},
    )
    assert 'resource "aws_cloudfront_origin_access_identity" "oai"' in hcl
    assert 'comment = "site"' in hcl
    assert (
        'origin_access_identity = "origin-access-identity/cloudfront/'
        '${aws_cloudfront_origin_access_identity.oai.id}"' in hcl
    )
    assert any("OriginReadTimeout' has no Terraform counterpart" in n for n in notes)
    assert 'custom_header { name = "X-A"' in hcl
    assert "origin_shield {" in hcl and 'vpc_origin_id = "vo-1"' in hcl
    assert 'origin_ssl_protocols = ["TLSv1.2"]' in hcl
    assert any("had no OriginSSLProtocols" in n for n in notes)
    assert "status_codes = [500, 502]" in hcl and hcl.count("member {") == 2
    assert "default_ttl = 60" in hcl
    assert 'function_arn = "arn:fn"' in hcl and "include_body = true" in hcl
    assert "query_string = false" in hcl
    assert 'forward = "whitelist"' in hcl
    assert 'forward = "none"' in hcl
    assert 'locations = ["DE"]' in hcl
    assert 'ssl_support_method = "sni-only"' in hcl
    assert 'bucket = "logs.s3.amazonaws.com"' in hcl
    assert any("Broken: DistributionConfig is missing or computed" in n for n in notes)


def test_origin_access_control_leftovers_are_noted():
    _, notes = convert(
        {
            "Oac": {
                "Type": "AWS::CloudFront::OriginAccessControl",
                "Properties": {
                    "OriginAccessControlConfig": {
                        "Name": "oac",
                        "OriginAccessControlOriginType": "s3",
                        "SigningBehavior": "always",
                        "SigningProtocol": "sigv4",
                        "Extra": True,
                    }
                },
            }
        }
    )
    assert any("OriginAccessControlConfig.Extra" in n for n in notes)


def test_value_helpers_pass_through_non_literals():
    hcl, _ = convert(
        {
            "Tg": {
                "Type": "AWS::ElasticLoadBalancingV2::TargetGroup",
                "Properties": {
                    "HealthCheckEnabled": {"Ref": "On"},
                    "HealthCheckIntervalSeconds": "often",
                    "TargetGroupAttributes": [
                        {"Key": "preserve_client_ip.enabled", "Value": "true"}
                    ],
                },
            },
            "Endpoint": {
                "Type": "AWS::EC2::VPCEndpoint",
                "Properties": {
                    "VpcId": "vpc-1",
                    "ServiceName": "s",
                    "SubnetIds": {"Fn::Split": [",", {"Ref": "Subnets"}]},
                    "DnsOptions": {"DnsRecordIpType": "ipv4"},
                },
            },
            "Rule": {
                "Type": "AWS::ElasticLoadBalancingV2::ListenerRule",
                "Properties": {
                    "ListenerArn": "arn:l",
                    "Actions": [{"Type": "forward", "TargetGroupArn": "arn:tg"}],
                    "Conditions": [
                        {"Field": "source-ip", "SourceIpConfig": {"Values": {"Fn::GetAZs": ""}}}
                    ],
                },
            },
        },
        Parameters={"On": {"Type": "String"}, "Subnets": {"Type": "String"}},
    )
    assert "enabled = var.on" in hcl and 'interval = "often"' in hcl
    assert 'preserve_client_ip = "true"' in hcl
    assert 'subnet_ids = split(",", var.subnets)' in hcl
    assert "values = data.aws_availability_zones.available.names" in hcl


def test_refers_to_reference_forms():
    from converters.cfn.resources.network import _refers_to

    assert _refers_to({"Ref": "Sg"}, "Sg")
    assert _refers_to({"Fn::GetAtt": "Sg.GroupId"}, "Sg")
    assert _refers_to({"Fn::GetAtt": ["Sg", "GroupId"]}, "Sg")
    assert not _refers_to("sg-123", "Sg")
    assert not _refers_to({"Fn::GetAtt": []}, "Sg")
