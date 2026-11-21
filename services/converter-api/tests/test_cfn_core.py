"""The CloudFormation converter's core: parsing, expressions, HCL, variables, outputs, files."""

from __future__ import annotations

import datetime
import json
import re

import pytest
import yaml

from converters.cfn import hcl
from converters.cfn.awscc import awscc_type
from converters.cfn.builder import Builder, Props
from converters.cfn.constants import MAX_TEMPLATE_CHARS
from converters.cfn.context import Context
from converters.cfn.convert import Converter
from converters.cfn.errors import CfnError
from converters.cfn.models import BLANK, Attr, Block, Call, Comment, Expression, Raw, ResourceInfo
from converters.cfn.naming import UniqueNames, identifier, snake
from converters.cfn.parse import detect_source, load_template
from converters.cfn.pipeline import TemplateConverter, convert_template

BUCKET = {"Bucket": {"Type": "AWS::S3::Bucket"}}


def run(template: dict | str, options: dict | None = None) -> dict:
    """Convert a template with options given as a dict (provider, layout, stack_name,
    include_imports)."""
    text = template if isinstance(template, str) else yaml.safe_dump(template, sort_keys=False)
    options = options or {}
    converter = TemplateConverter(
        options.get("provider", "aws"),
        options.get("layout", "files"),
        options.get("stack_name"),
        options.get("include_imports", False),
    )
    return converter.convert(text)


def files(template: dict | str, options: dict | None = None) -> dict[str, str]:
    return {f["path"]: f["content"] for f in run(template, options)["files"]}


def squeeze(text: str) -> str:
    """Alignment padding collapsed, so tests don't depend on neighbouring attribute names."""
    return re.sub(r" +", " ", text)


def notes(template: dict | str, options: dict | None = None) -> list[str]:
    return run(template, options)["notes"]


def expression(value, template: dict | None = None) -> str:
    """One CloudFormation value, rendered, as the value of an output."""
    template = template or {}
    template.setdefault("Resources", dict(BUCKET))
    template["Outputs"] = {"Out": {"Value": value}}
    out = files(template)["outputs.tf"]
    return out.split("value = ", 1)[1].rsplit("\n}", 1)[0]


# --- parsing ---------------------------------------------------------------------------


def test_yaml_short_forms_become_long_forms():
    template = load_template(
        "Resources:\n"
        "  B:\n"
        "    Type: AWS::S3::Bucket\n"
        "    Properties:\n"
        "      BucketName: !Sub '${AWS::StackName}-x'\n"
        "      Tags:\n"
        "        - Key: a\n"
        "          Value: !GetAtt Other.Arn.Deep\n"
        "        - Key: b\n"
        "          Value: !Ref Thing\n"
        "        - Key: c\n"
        "          Value: !Join [',', [a, b]]\n"
        "        - Key: d\n"
        "          Value: !If [Cond, {x: 1}, !Ref AWS::NoValue]\n"
        "        - Key: e\n"
        "          Value: !Condition C\n"
    )
    props = template["Resources"]["B"]["Properties"]
    assert props["BucketName"] == {"Fn::Sub": "${AWS::StackName}-x"}
    assert props["Tags"][0]["Value"] == {"Fn::GetAtt": ["Other", "Arn.Deep"]}
    assert props["Tags"][1]["Value"] == {"Ref": "Thing"}
    assert props["Tags"][2]["Value"] == {"Fn::Join": [",", ["a", "b"]]}
    assert props["Tags"][3]["Value"] == {"Fn::If": ["Cond", {"x": 1}, {"Ref": "AWS::NoValue"}]}
    assert props["Tags"][4]["Value"] == {"Condition": "C"}


def test_yaml_scalars_follow_yaml_1_2():
    template = load_template(
        "AWSTemplateFormatVersion: 2010-09-09\n"
        "Resources:\n"
        "  B:\n"
        "    Type: AWS::S3::Bucket\n"
        "    Properties: {A: on, B: 0755, C: 12, D: 1.5, E: true, F: ~, G: FALSE, H: '1'}\n"
    )
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert template["Resources"]["B"]["Properties"] == {
        "A": "on",
        "B": "0755",
        "C": 12,
        "D": 1.5,
        "E": True,
        "F": None,
        "G": False,
        "H": "1",
    }


def test_json_templates_and_bom():
    template = load_template("\ufeff" + json.dumps({"Resources": BUCKET}))
    assert template["Resources"]["Bucket"]["Type"] == "AWS::S3::Bucket"


@pytest.mark.parametrize(
    ("text", "message", "line"),
    [
        ("", "The template is empty", None),
        ("   ", "The template is empty", None),
        ('{"Resources": ', "The JSON has a syntax error", 1),
        ("\n\n{\n  bad", "The JSON has a syntax error", 4),
        ("Resources: [1\n", "The YAML has a syntax error", 2),
        ("Resources:\n  B: !Unknown x\n", "The YAML tag '!Unknown' is unknown", 2),
        ("- 1\n- 2\n", "top level of the template must be a mapping", None),
        ("Description: x\n", "needs a 'Resources' section", None),
        ("Resources: [1]\n", "'Resources' section must be a mapping", None),
        ("Resources:\n  B: 1\n", "resource 'B' must be a mapping", None),
        ("Resources:\n  B: {Properties: {}}\n", "resource 'B' needs a 'Type'", None),
        ("Resources:\n  B: {Type: X, Properties: [1]}\n", "'Properties' of 'B' must be", None),
        (
            "Resources: {B: {Type: X}}\nParameters: [1]\n",
            "'Parameters' section must be a mapping",
            None,
        ),
        ("Resources: {B: {Type: X}}\nParameters: {P: {Default: 1}}\n", "needs a 'Type'", None),
        ("Resources: {B: {Type: X}}\nOutputs: {O: {Description: x}}\n", "needs a 'Value'", None),
    ],
)
def test_invalid_templates(text, message, line):
    with pytest.raises(CfnError) as error:
        load_template(text)
    assert message in error.value.message
    assert error.value.line == line


def test_size_and_depth_limits():
    with pytest.raises(CfnError, match="longer than"):
        load_template("a" * (MAX_TEMPLATE_CHARS + 1))
    with pytest.raises(CfnError, match="too deeply"):
        load_template("Resources: " + "[" * 150 + "]" * 150)
    with pytest.raises(CfnError, match="too deeply"):
        load_template('{"Resources": ' + "[" * 150 + "]" * 150 + "}")
    with pytest.raises(CfnError, match="too deeply"):
        load_template('{"Resources": ' + "[" * 100_000 + "]" * 100_000 + "}")
    values = ",".join("0" * 100_001)
    with pytest.raises(CfnError, match="The template has more than"):
        load_template('{"Resources": {"B": {"Type": "X", "Properties": {"V": [' + values + "]}}}}")
    with pytest.raises(CfnError, match="more than"):
        load_template("Resources: {B: {Type: X, Properties: {V: [" + values + "]}}}")


def test_alias_bombs_are_rejected():
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
    for level in range(1, 8):
        previous = "a" if level == 1 else f"a{level - 1}"
        bomb += f"a{level}: &a{level} [*{previous}, *{previous}, *{previous}, *{previous}]\n"
    with pytest.raises(CfnError, match="The template has more than"):
        load_template(bomb + "Resources: {B: {Type: X, Properties: {V: *a7}}}\n")


def test_detect_source():
    assert detect_source({"Resources": BUCKET}) == "cloudformation"
    assert (
        detect_source({"Transform": ["AWS::Serverless-2016-10-31"], "Resources": BUCKET}) == "sam"
    )
    assert detect_source({"Resources": {"M": {"Type": "AWS::CDK::Metadata"}}}) == "cdk"
    cdk = {"Resources": {"B": {"Type": "X", "Metadata": {"aws:cdk:path": "S/B/Resource"}}}}
    assert detect_source(cdk) == "cdk"


# --- naming ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("BucketName", "bucket_name"),
        ("VPCId", "vpc_id"),
        ("DBInstanceClass", "db_instance_class"),
        ("Ipv6CidrBlock", "ipv_6_cidr_block"),
        ("S3Bucket", "s3_bucket"),
        ("CertificateS3BucketName", "certificate_s3_bucket_name"),
        ("EC2InstanceType", "ec2_instance_type"),
        ("CloudWatchRoleArn", "cloudwatch_role_arn"),
        ("BasePathMappingV2", "base_path_mapping_v2"),
        ("FSxLustreConfig", "fsx_lustre_config"),
    ],
)
def test_snake_matches_awscc(name, expected):
    assert snake(name) == expected


def test_identifier_and_unique_names():
    assert identifier("MyBucket") == "my_bucket"
    assert identifier("1stQueue") == "r_1st_queue"
    assert identifier("S3Endpoint") == "s3_endpoint"
    assert identifier("PublicSubnet1A") == "public_subnet1_a"
    assert identifier("MyVPC") == "my_vpc"
    assert identifier("DBInstance") == "db_instance"
    assert identifier("Count") == "count_"
    assert identifier("count") == "count_"
    assert identifier("Version") == "version_"
    assert identifier("é") == "this"
    assert identifier("--", fallback="x") == "x"
    assert len(identifier("A" * 200)) <= 64
    names = UniqueNames()
    assert [names.take("a"), names.take("a"), names.take("a")] == ["a", "a_2", "a_3"]
    names.reserve("b")
    assert names.take("b") == "b_2"


def test_awscc_type_names():
    assert awscc_type("AWS::S3::Bucket") == "awscc_s3_bucket"
    assert awscc_type("AWS::EC2::VPCEndpoint") == "awscc_ec2_vpc_endpoint"
    assert (
        awscc_type("AWS::CloudFront::CloudFrontOriginAccessIdentity")
        == "awscc_cloudfront_cloudfront_origin_access_identity"
    )
    assert awscc_type("AWS::Not::AThing") is None


# --- HCL rendering ----------------------------------------------------------------------


def test_quote_escapes_interpolation_and_control_characters():
    assert hcl.quote('a "b" \\ ${x} %{y}\n\t\r') == '"a \\"b\\" \\\\ $${x} %%{y}\\n\\t\\r"'


def test_render_values():
    assert hcl.render(None) == "null"
    assert hcl.render(True) == "true"
    assert hcl.render(3.0) == "3"
    assert hcl.render(2.5) == "2.5"
    assert hcl.render(float("inf")) == '"inf"'
    assert hcl.render(1e20) == "1e+20"
    assert hcl.render([]) == "[]"
    assert hcl.render({}) == "{}"
    assert hcl.render(["a", 1]) == '["a", 1]'
    assert hcl.render([{"a": 1}]) == "[\n  {\n    a = 1\n  },\n]"
    assert hcl.render(["x" * 90]) == '[\n  "' + "x" * 90 + '",\n]'
    assert hcl.render({"aws:Key": 1, "true": 2, Raw("var.k"): 3}) == (
        '{\n  "aws:Key" = 1\n  "true"    = 2\n  (var.k)   = 3\n}'
    )

    assert hcl.render(datetime.date(2024, 1, 2)) == '"2024-01-02"'


def test_inline_rendering():
    value = Call("f", [{"a": 1, "b": Comment("x")}, {}, "s"])
    assert hcl.inline(value) == 'f([{ a = 1 }, {}, "s"])'


def test_blocks_align_like_terraform_fmt():
    block = Block(
        "resource",
        ["t", "n"],
        [
            Attr("a", 1),
            Attr("bbbb", 2),
            Attr("obj", {"k": 1, "kkk": 2}),
            Attr("c", 3),
            Comment("note\nsecond line"),
            Attr("dd", 4),
            BLANK,
            BLANK,
            Block("inner", [], []),
            BLANK,
        ],
    )
    assert hcl.render_block(block) == (
        'resource "t" "n" {\n'
        "  a    = 1\n"
        "  bbbb = 2\n"
        "  obj = {\n"
        "    k   = 1\n"
        "    kkk = 2\n"
        "  }\n"
        "  c = 3\n"
        "  # note\n"
        "  # second line\n"
        "  dd = 4\n"
        "\n"
        "  inner {}\n"
        "}"
    )


def test_render_file_keeps_comments_above_blocks():
    text = hcl.render_file([Block("a", [], []), Comment("about b"), Block("b", [], [])])
    assert text == "a {}\n\n# about b\nb {}\n"


# --- expressions ------------------------------------------------------------------------


def test_ref_and_pseudo_parameters():
    template = {"Parameters": {"Env": {"Type": "String"}}}
    assert expression({"Ref": "Env"}, template) == "var.env"
    assert expression({"Ref": "AWS::Region"}) == "data.aws_region.current.region"
    assert expression({"Ref": "AWS::AccountId"}) == "data.aws_caller_identity.current.account_id"
    assert expression({"Ref": "AWS::Partition"}) == "data.aws_partition.current.partition"
    assert expression({"Ref": "AWS::URLSuffix"}) == "data.aws_partition.current.dns_suffix"
    assert expression({"Ref": "AWS::StackName"}) == "var.stack_name"
    assert expression({"Ref": "AWS::NoValue"}) == "null"
    assert expression({"Ref": "AWS::NotificationARNs"}) == "[]"
    assert expression({"Ref": "AWS::StackId"}) == "var.stack_name"
    assert expression({"Ref": "Bucket"}) == "aws_s3_bucket.bucket.id"


def test_pseudo_parameters_add_data_sources():
    main = files({"Resources": BUCKET, "Outputs": {"R": {"Value": {"Ref": "AWS::Region"}}}})[
        "main.tf"
    ]
    assert main.startswith('data "aws_region" "current" {}')


def test_get_att():
    assert expression({"Fn::GetAtt": ["Bucket", "Arn"]}) == "aws_s3_bucket.bucket.arn"
    assert expression({"Fn::GetAtt": "Bucket.Arn"}) == "aws_s3_bucket.bucket.arn"
    queue = {"Q": {"Type": "AWS::SQS::Queue"}}
    unknown = {"Resources": {"Q": {"Type": "AWS::Batch::JobQueue"}}}
    assert expression({"Fn::GetAtt": ["Q", "JobQueueArn"]}, unknown) == (
        "awscc_batch_job_queue.q.job_queue_arn"
    )
    assert "queue" in expression({"Fn::GetAtt": ["Q", "Arn"]}, {"Resources": queue})


def test_get_att_unknown_attribute_on_a_mapped_type_is_noted():
    template = {
        "Resources": BUCKET,
        "Outputs": {"O": {"Value": {"Fn::GetAtt": ["Bucket", "DualStackDomainName"]}}},
    }
    out = files(template)["outputs.tf"]
    assert "aws_s3_bucket.bucket.dual_stack_domain_name" in out
    assert any(
        "Check in the Terraform docs that this attribute exists" in n for n in notes(template)
    )


def test_get_att_computed_attribute_name():
    template = {
        "Parameters": {"A": {"Type": "String"}},
        "Resources": BUCKET,
        "Outputs": {"O": {"Value": {"Fn::GetAtt": ["Bucket", {"Ref": "A"}]}}},
    }
    assert "value = null" in files(template)["outputs.tf"]
    assert any("builds its attribute name at deploy time" in n for n in notes(template))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"Ref": "Nope"}, "the template has no parameter or resource with that name"),
        ({"Ref": ["x"]}, "needs the name of a parameter or resource"),
        ({"Fn::GetAtt": ["Nope", "Arn"]}, "points to 'Nope', but the template has no resource"),
        ({"Fn::GetAtt": ["Bucket"]}, "needs a resource and an attribute"),
        ({"Fn::Sub": 1}, "needs a string"),
        ({"Fn::Sub": ["a", "b"]}, "needs a string or a [string, {variables}] pair"),
        ({"Fn::Join": ["a"]}, "'Fn::Join' needs a list of 2"),
        ({"Fn::Select": [5, ["a"]]}, "past the end of the list"),
        ({"Fn::If": ["Nope", 1, 2]}, "uses the condition 'Nope', but Conditions has no entry"),
        (
            {"Fn::FindInMap": ["Nope", "a", "b"]},
            "points to the mapping 'Nope', but the template has no mapping",
        ),
        ({"Fn::And": [{"Condition": "X"}]}, "2 to 10 conditions"),
        ({"Fn::Cidr": ["not-a-cidr", 2, 8]}, "cannot split"),
    ],
)
def test_invalid_intrinsics(value, message):
    with pytest.raises(CfnError) as error:
        expression(value)
    assert message in error.value.message


def test_sub():
    template = {"Parameters": {"Env": {"Type": "String"}}}
    assert expression({"Fn::Sub": "plain"}) == '"plain"'
    assert expression({"Fn::Sub": "${Env}"}, template) == "var.env"
    assert expression({"Fn::Sub": "${Env}-${Bucket.Arn}"}, template) == (
        '"${var.env}-${aws_s3_bucket.bucket.arn}"'
    )
    assert expression({"Fn::Sub": "a-${!Literal}-b"}) == '"a-$${Literal}-b"'
    assert expression({"Fn::Sub": ["${X}/${Y}", {"X": "lit", "Y": {"Ref": "Env"}}]}, template) == (
        '"lit/${var.env}"'
    )
    assert expression({"Fn::Sub": ["${X}", {"X": "lit"}]}) == '"lit"'
    assert expression({"Fn::Sub": "arn:${AWS::Partition}:s3:::x"}) == (
        '"arn:${data.aws_partition.current.partition}:s3:::x"'
    )


def test_join_select_split():
    template = {"Parameters": {"L": {"Type": "CommaDelimitedList"}, "E": {"Type": "String"}}}
    assert expression({"Fn::Join": ["-", ["a", 1, 2.5]]}) == '"a-1-2.5"'
    assert expression({"Fn::Join": ["-", ["a", {"Ref": "E"}, True]]}, template) == (
        '"a-${var.e}-true"'
    )
    assert expression({"Fn::Join": [",", {"Ref": "L"}]}, template) == 'join(",", var.l)'
    assert expression({"Fn::Join": [",", [{"Fn::Split": [",", {"Ref": "E"}]}]]}, template) == (
        'join(",", [split(",", var.e)])'
    )
    assert expression({"Fn::Join": ["", [{"Fn::Sub": "x"}, "y"]]}) == '"xy"'
    assert expression({"Fn::Select": ["1", ["a", "b"]]}) == '"b"'
    assert expression({"Fn::Select": [0, {"Ref": "L"}]}, template) == "element(var.l, 0)"
    assert expression({"Fn::Split": [",", "a,b"]}) == '["a", "b"]'
    assert expression({"Fn::Split": [",", {"Ref": "E"}]}, template) == 'split(",", var.e)'


def test_azs_base64_cidr():
    assert expression({"Fn::GetAZs": ""}) == "data.aws_availability_zones.available.names"
    assert expression({"Fn::GetAZs": {"Ref": "AWS::Region"}}).endswith(".names")
    template = {"Resources": dict(BUCKET), "Outputs": {"O": {"Value": {"Fn::GetAZs": "eu-west-1"}}}}
    assert any("another region" in n for n in notes(template))
    assert expression({"Fn::Base64": "x"}) == 'base64encode("x")'
    assert expression({"Fn::Cidr": ["10.0.0.0/16", 3, 8]}) == (
        '["10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24"]'
    )
    template = {"Parameters": {"C": {"Type": "String"}}}
    assert expression({"Fn::Cidr": [{"Ref": "C"}, 2, 8]}, template) == (
        '[for i in range(2) : cidrsubnet(var.c, (strcontains(var.c, ":") ? 128 : 32) - 8 '
        '- tonumber(split("/", var.c)[1]), i)]'
    )


def test_find_in_map_and_conditions():
    template = {
        "Parameters": {"Env": {"Type": "String"}, "Count": {"Type": "Number"}},
        "Mappings": {"Sizes": {"dev": {"Mem": 128}}},
        "Conditions": {
            "IsDev": {"Fn::Equals": [{"Ref": "Env"}, "dev"]},
            "IsTwo": {"Fn::Equals": [{"Ref": "Count"}, "2"]},
            "IsNum": {"Fn::Equals": [{"Ref": "Env"}, 3]},
            "Both": {"Fn::And": [{"Condition": "IsDev"}, {"Fn::Not": [{"Condition": "IsTwo"}]}]},
            "Either": {"Fn::Or": [{"Condition": "IsDev"}, {"Fn::Equals": ["a", "b"]}]},
        },
    }
    assert expression({"Fn::FindInMap": ["Sizes", {"Ref": "Env"}, "Mem"]}, dict(template)) == (
        'local.sizes[var.env]["Mem"]'
    )
    assert expression(
        {"Fn::FindInMap": ["Sizes", "x", "Mem", {"DefaultValue": 64}]}, dict(template)
    ) == ('lookup(lookup(local.sizes, "x", {}), "Mem", 64)')
    assert expression({"Fn::If": ["IsDev", 1, {"Ref": "AWS::NoValue"}]}, dict(template)) == (
        "local.is_dev ? 1 : null"
    )
    locals_tf = squeeze(files(dict(template, Resources=BUCKET))["locals.tf"])
    assert 'is_dev = var.env == "dev"' in locals_tf
    assert 'is_two = tostring(var.count_) == "2"' in locals_tf
    assert 'is_num = var.env == "3"' in locals_tf
    assert "both = local.is_dev && !local.is_two" in locals_tf
    assert 'either = local.is_dev || ("a" == "b")' in locals_tf
    assert "Mem = 128" in locals_tf


def test_import_value_becomes_a_variable():
    template = {
        "Resources": dict(BUCKET),
        "Outputs": {"O": {"Value": {"Fn::ImportValue": "net-VpcId"}}},
    }
    out = files(template)
    assert "value = var.import_net_vpc_id" in out["outputs.tf"]
    assert 'variable "import_net_vpc_id"' in out["variables.tf"]
    assert any("Terraform has no exports of its own" in n for n in notes(template))
    computed = {"Fn::ImportValue": {"Fn::Sub": "${AWS::StackName}-Vpc"}}
    assert expression(computed).startswith("var.import_")


def test_language_extensions_and_unsupported_functions():
    assert expression({"Fn::ToJsonString": {"a": 1}}) == "jsonencode({\n    a = 1\n  })"
    assert expression({"Fn::Length": [1, 2]}) == "2"
    template = {"Parameters": {"L": {"Type": "CommaDelimitedList"}}}
    assert expression({"Fn::Length": {"Ref": "L"}}, template) == "length(var.l)"
    assert expression({"Fn::Transform": {"Name": "AWS::Include"}}) == "null"
    assert expression({"Fn::Nope": 1}) == "null"
    unknown = {"Resources": dict(BUCKET), "Outputs": {"O": {"Value": {"Fn::Nope": 1}}}}
    assert any("'Fn::Nope' has no Terraform conversion" in n for n in notes(unknown))


def test_plain_mappings_and_lists_keep_their_keys():
    assert expression({"Ref": "Bucket", "Other": 1}) == (
        '{\n    Ref   = "Bucket"\n    Other = 1\n  }'
    )


def test_expressions_nested_too_deeply():
    value: dict = {"Fn::Base64": "x"}
    for _ in range(60):
        value = {"Fn::Base64": value}
    with pytest.raises(CfnError, match="too deeply"):
        expression(value)


def test_expression_objects_from_the_sam_step():
    ctx = Context({"Resources": {}})
    assert ctx.expr(Expression('file("x")')) == Raw('file("x")')


# --- parameters -------------------------------------------------------------------------


def test_parameters_become_variables_with_validation():
    template = {
        "Metadata": {
            "AWS::CloudFormation::Interface": {
                "ParameterLabels": {"Size": {"default": "Size of it"}}
            }
        },
        "Parameters": {
            "Env": {
                "Type": "String",
                "Description": "Where it runs",
                "Default": "dev",
                "AllowedValues": ["dev", "prod"],
                "ConstraintDescription": "pick dev or prod",
            },
            "Name": {
                "Type": "String",
                "AllowedPattern": "[a-z]+",
                "MinLength": 1,
                "MaxLength": "8",
            },
            "Size": {"Type": "Number", "Default": "10", "MinValue": 1, "MaxValue": 100},
            "Ratio": {"Type": "Number", "Default": "0.5"},
            "Bad": {"Type": "Number", "Default": "lots"},
            "Ports": {"Type": "List<Number>", "Default": "80, 443"},
            "Subnets": {"Type": "List<AWS::EC2::Subnet::Id>", "Default": ["a", "b"]},
            "Zones": {"Type": "CommaDelimitedList", "Default": "a,,b", "AllowedValues": ["a"]},
            "Secret": {"Type": "String", "NoEcho": "true"},
            "Flag": {"Type": "String", "Default": True},
            "Ami": {
                "Type": "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
                "Default": "/aws/service/ami",
                "Description": "Image",
            },
            "Ids": {"Type": "AWS::SSM::Parameter::Value<List<String>>"},
            "Count": {"Type": "String"},
        },
        "Resources": BUCKET,
        "Outputs": {
            "A": {"Value": {"Ref": "Ami"}},
            "B": {"Value": {"Ref": "Ids"}},
            "S": {"Value": {"Ref": "Secret"}},
        },
    }
    out = {k: squeeze(v) for k, v in files(template).items()}
    variables = out["variables.tf"]
    assert 'description = "Where it runs"' in variables
    assert 'condition = contains(["dev", "prod"], var.env)' in variables
    assert 'error_message = "Pick dev or prod."' in variables
    assert 'can(regex("^(?:[a-z]+)$", var.name))' in variables
    assert "length(var.name) >= 1" in variables and "length(var.name) <= 8" in variables
    assert 'description = "Size of it"' in variables
    assert "default = 10" in variables and "var.size >= 1" in variables
    assert "default = 0.5" in variables
    assert 'default = "lots"' in variables
    assert "type = list(number)\n default = [80, 443]" in variables
    assert 'default = ["a", "b"]' in variables
    assert "sensitive = true" in variables
    assert 'default = "true"' in variables
    assert 'description = "Image (the name of the SSM parameter that holds the value)"' in variables
    assert 'variable "count_"' in variables
    main = out["main.tf"]
    assert 'data "aws_ssm_parameter" "ami" {\n name = var.ami\n}' in main
    outputs = out["outputs.tf"]
    assert "value = data.aws_ssm_parameter.ami.insecure_value" in outputs
    assert 'value = split(",", data.aws_ssm_parameter.ids.insecure_value)' in outputs
    assert "value = var.secret\n sensitive = true" in outputs


def test_stack_name_variable_and_default():
    template = {
        "Resources": {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"BucketName": {"Ref": "AWS::StackName"}},
            }
        }
    }
    assert 'default     = "shop"' in files(template, {"stack_name": "shop"})["variables.tf"]
    assert "default" not in files(template)["variables.tf"]
    clash = dict(template, Parameters={"StackName": {"Type": "String"}})
    assert 'variable "stack_name_2"' in files(clash)["variables.tf"]


# --- resources and their attributes ----------------------------------------------------


def test_resource_attributes():
    template = {
        "Conditions": {"On": {"Fn::Equals": ["a", "a"]}},
        "Resources": {
            "A": {"Type": "AWS::S3::Bucket", "Condition": "On", "DeletionPolicy": "Retain"},
            "B": {
                "Type": "AWS::S3::Bucket",
                "DependsOn": ["A", "C"],
                "DeletionPolicy": "Snapshot",
                "UpdateReplacePolicy": "Retain",
                "CreationPolicy": {"x": 1},
                "Metadata": {"AWS::CloudFormation::Init": {}},
            },
            "C": {"Type": "Custom::Thing", "Properties": {"ServiceToken": "arn"}},
        },
        "Outputs": {"O": {"Value": {"Fn::GetAtt": ["C", "Result"]}}, "R": {"Value": {"Ref": "C"}}},
    }
    out = files(template)
    main = out["main.tf"]
    assert "count = local.on ? 1 : 0" in main
    assert "lifecycle {\n    prevent_destroy = true\n  }" in main
    assert "depends_on = [aws_s3_bucket.a]" in main
    assert "# C (Custom::Thing) was left out" in main
    assert 'variable "c_result"' in out["variables.tf"]
    assert "value = var.c\n" in out["outputs.tf"]
    messages = "\n".join(notes(template))
    for text in (
        "prevent_destroy",
        "Terraform takes no snapshot",
        "UpdateReplacePolicy Retain",
        "CreationPolicy only works in CloudFormation",
        "cfn-init",
        "Custom resources call a Lambda function",
    ):
        assert text in messages


def test_unknown_depends_on_is_an_error():
    template = {"Resources": {"A": {"Type": "AWS::S3::Bucket", "DependsOn": "Nope"}}}
    with pytest.raises(CfnError, match="has DependsOn 'Nope', but the template has no resource"):
        files(template)


def test_unsupported_types():
    template = {
        "Resources": {
            "S": {"Type": "AWS::CloudFormation::WaitConditionHandle"},
            "T": {"Type": "AWS::Made::Up"},
        }
    }
    result = convert_template(yaml.safe_dump(template))
    assert result["summary"] == {"resources": 2, "aws": 0, "awscc": 0, "unsupported": 2}
    messages = "\n".join(result["notes"])
    assert "This type only exists inside CloudFormation" in messages
    assert "Neither the aws mappings nor the awscc provider covers this type" in messages
    assert [r["address"] for r in result["resources"]] == [None, None]


def test_leftover_properties_are_kept_as_comments_and_noted():
    template = {
        "Resources": {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "Unknown": {"a": "b" * 200},
                    "Gone": None,
                    "No": {"Ref": "AWS::NoValue"},
                },
            }
        }
    }
    main = files(template)["main.tf"]
    assert '# Unknown was left out. CloudFormation value: {"a": "bbb' in main and "bbb...\n" in main
    assert "Gone" not in main and "No was" not in main
    assert any("no mapping for 'B.Unknown' yet" in n for n in notes(template))


def test_generated_names_use_the_stack_name():
    template = {"Resources": {"T": {"Type": "AWS::DynamoDB::Table", "Properties": {}}}}
    out = files(template, {"stack_name": "shop"})
    assert 'name = "${var.stack_name}-T"' in out["main.tf"]
    assert any("the template leaves out TableName" in n for n in notes(template))


# --- awscc ------------------------------------------------------------------------------


def test_awscc_conversion_follows_the_schema_index():
    template = {
        "Conditions": {"On": {"Fn::Equals": ["a", "a"]}},
        "Resources": {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Environment": {"Variables": {"MY_VAR": "1", "Other": {"Ref": "AWS::Region"}}},
                    "Code": {"S3Bucket": "b", "S3Key": "k"},
                    "VpcConfig": {"Fn::If": ["On", {"SubnetIds": ["s"]}, {"Ref": "AWS::NoValue"}]},
                    "Tags": [{"Key": "k", "Value": "v"}],
                    "Skipped": None,
                    "TracingConfig": {"Mode": "Active", "Drop": {"Ref": "AWS::NoValue"}},
                },
            },
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {"AssumeRolePolicyDocument": {"Statement": []}},
            },
            "Param": {
                "Type": "AWS::SSM::Parameter",
                "Properties": {
                    "Type": "String",
                    "Value": "x",
                    "Tags": [{"Key": "a", "Value": "b"}],
                },
            },
            "Env": {
                "Type": "AWS::Cloud9::EnvironmentEC2",
                "Properties": {"InstanceType": "t3.micro"},
            },
        },
    }
    result = run(template, {"provider": "awscc"})
    main = result["files"][2]["content"] if len(result["files"]) > 2 else ""
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    assert 'resource "awscc_lambda_function" "fn"' in main
    assert 'MY_VAR = "1"' in main and "Other  = data.aws_region.current.region" in main
    assert 's3_bucket = "b"' in main
    assert 'vpc_config = local.on ? { subnet_ids = ["s"] } : null' in main
    assert 'key   = "k"' in main
    assert "assume_role_policy_document = jsonencode({" in main
    assert 'tags = {\n    a = "b"\n  }' in main
    assert 'resource "awscc_cloud9_environment_ec2" "env"' in main
    assert "drop" not in main
    versions = result["files"][0]["content"]
    assert "hashicorp/awscc" in versions and 'hashicorp/aws"' in versions
    assert result["summary"]["awscc"] == 4
    assert any("because you chose the awscc provider" in n for n in result["notes"])


def test_awscc_only_template_has_no_aws_provider():
    template = {"Resources": {"Q": {"Type": "AWS::Batch::JobQueue", "Properties": {"Priority": 1}}}}
    versions = files(template)["versions.tf"]
    assert "hashicorp/awscc" in versions and 'hashicorp/aws"' not in versions
    assert 'provider "awscc" {}' in versions
    assert "AWS_REGION" in versions


# --- files, layouts, imports, outputs -------------------------------------------------


def test_file_layouts():
    template = {
        "Parameters": {"P": {"Type": "String"}},
        "Conditions": {"C": {"Fn::Equals": [{"Ref": "P"}, "x"]}},
        "Resources": BUCKET,
        "Outputs": {"O": {"Value": {"Ref": "Bucket"}, "Description": "The bucket"}},
    }
    split = files(template)
    assert list(split) == ["versions.tf", "variables.tf", "locals.tf", "main.tf", "outputs.tf"]
    assert 'description = "The bucket"' in split["outputs.tf"]
    single = files(template, {"layout": "single"})
    assert list(single) == ["main.tf"]
    text = single["main.tf"]
    assert text.index("terraform {") < text.index('variable "p"') < text.index("locals {")
    assert text.index("locals {") < text.index('resource "aws_s3_bucket"') < text.index("output")


def test_import_blocks():
    template = {
        "Conditions": {"C": {"Fn::Equals": ["a", "a"]}},
        "Resources": {
            "Named": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": "my-bucket"}},
            "Unnamed": {"Type": "AWS::S3::Bucket", "Condition": "C"},
            "Cc": {"Type": "AWS::Batch::JobQueue", "Properties": {"Priority": 1}},
        },
    }
    imports = files(template, {"include_imports": True})["imports.tf"]
    assert "# These import blocks adopt the resources the stack already created" in imports
    assert 'to = aws_s3_bucket.named\n  id = "my-bucket"' in imports
    assert 'to = aws_s3_bucket.unnamed[0]\n  id = "TODO: the physical ID of Unnamed"' in imports
    assert "to = awscc_batch_job_queue.cc" in imports
    single = files(template, {"include_imports": True, "layout": "single"})["main.tf"]
    assert single.rstrip().endswith("}") and "import {" in single


def test_outputs():
    template = {
        "Conditions": {"C": {"Fn::Equals": ["a", "a"]}},
        "Resources": BUCKET,
        "Outputs": {
            "Name": {
                "Value": {"Ref": "Bucket"},
                "Condition": "C",
                "Export": {"Name": {"Fn::Sub": "${AWS::StackName}-bucket"}},
            },
            "Plain": {"Value": "x", "Export": {"Name": "plain-export"}},
        },
    }
    out = files(template)["outputs.tf"]
    assert "value = local.c ? aws_s3_bucket.bucket.id : null" in out
    messages = "\n".join(notes(template))
    assert "exported as '${var.stack_name}-bucket'" in messages
    assert "exported as 'plain-export'" in messages


def test_template_level_notes():
    template = {
        "Transform": ["AWS::LanguageExtensions", "AWS::SecretsManager-2020-07-23", {"Name": "x"}],
        "Rules": {"R": {}},
        "Extra": 1,
        "Resources": BUCKET,
    }
    messages = "\n".join(notes(template))
    assert "The top-level section 'Extra' is unknown" in messages
    assert "The transform 'AWS::SecretsManager-2020-07-23'" in messages
    assert "The transform 'AWS::Include'" in messages
    assert "The Rules section of the template was left out" in messages
    assert "LanguageExtensions" not in messages


def test_invalid_options():
    with pytest.raises(CfnError, match="The provider 'google' is unknown"):
        convert_template("Resources: {}", "google")
    with pytest.raises(CfnError, match="The layout 'tree' is unknown"):
        convert_template("Resources: {}", "aws", "tree")

    with pytest.raises(CfnError, match="The provider 'google' is unknown"):
        Converter({"Resources": {}}, provider="google")
    converter = Converter({"Resources": BUCKET})
    with pytest.raises(CfnError, match="The layout 'tree' is unknown"):
        converter.files("tree")


# --- edge paths -------------------------------------------------------------------------


def test_awscc_renamed_types_missing_properties_and_local_code():

    template = load_template(
        yaml.safe_dump(
            {
                "Resources": {
                    "Oai": {
                        "Type": "AWS::CloudFront::CloudFrontOriginAccessIdentity",
                        "Properties": {"CloudFrontOriginAccessIdentityConfig": {"Comment": "c"}},
                    },
                    "Fn": {
                        "Type": "AWS::Lambda::Function",
                        "Properties": {"Handler": "a.b", "Role": "arn:r"},
                    },
                    "Layer": {"Type": "AWS::Lambda::LayerVersion", "Properties": {}},
                    "Topic": {
                        "Type": "AWS::SNS::Topic",
                        "Properties": {"DeliveryPolicy": {"x": 1}},
                    },
                    "Logs": {
                        "Type": "AWS::Logs::LogGroup",
                        "Properties": {"DeletionProtectionEnabled": True, "RetentionInDays": 1},
                    },
                    "Glue": {
                        "Type": "AWS::Glue::Catalog",
                        "Properties": {
                            "Name": "c",
                            "FederatedCatalog": {"ConnectionType": "x", "Identifier": "i"},
                        },
                    },
                }
            }
        )
    )
    converter = Converter(
        template,
        provider="awscc",
        hints={"Fn": {"code_path": "src"}, "Layer": {"code_path": "layer"}},
    )
    result = converter.run("single")
    main = result["files"][0]["content"]
    messages = "\n".join(result["notes"])
    assert 'resource "awscc_cloudfront_cloudfront_origin_access_identity" "oai"' in main
    assert "s3_bucket = var.fn_code_bucket" in main and "s3_key    = var.fn_code_key" in main
    assert "content = {" in main and "var.layer_code_bucket" in main
    assert "the awscc provider reads Lambda code only from S3" in messages
    assert "# DeliveryPolicy was left out" in main
    assert "# DeletionProtectionEnabled was left out" in main
    assert (
        "awscc_glue_catalog in this awscc provider version has no argument for "
        "'Glue.FederatedCatalog.ConnectionType'" in messages
    )


def fill_a(props: Props) -> None:
    """Copy property A to attribute a."""
    props.attr("a", "A")


def test_body_items_must_be_hcl_nodes():
    with pytest.raises(TypeError, match="Unexpected body item"):
        hcl.BodyWriter(0).write([object()])


def test_blocks_from_computed_values():

    ctx = Context({"Conditions": {"C": {}}, "Resources": {}})
    ctx.conditions["C"] = "c"
    props = Props(
        ctx,
        {
            "Text": "not a mapping",
            "Short": {"Fn::If": ["C", {}]},
            "Inverted": {"Fn::If": ["C", {"Ref": "AWS::NoValue"}, {"A": 1}]},
            "Both": {"Fn::If": ["C", {"A": 1}, {"A": 2}]},
        },
        "R",
        "R (X)",
    )
    for name in ("Text", "Short", "Inverted", "Both"):
        props.block(name.lower(), name, fill_a)
    rendered = hcl.render_block(Block("resource", ["x", "y"], props.items))
    assert "# Text was left out" in rendered
    assert "# Short was left out" in rendered
    assert 'dynamic "inverted" {\n    for_each = !local.c ? [1] : []' in rendered
    assert "# Both was left out" in rendered
    assert sum("comes from a function" in n for n in ctx.notes) == 3


def test_json_values_and_template_parts():

    ctx = Context({"Resources": {}})
    assert ctx.json("not json") == "not json"
    assert ctx.json('{"a": 1}') == Call("jsonencode", {"a": 1})
    assert ctx.json({"Fn::Sub": "x"}) == "x"
    assert ctx.json([1]) == Call("jsonencode", [1])
    assert (
        expression({"Fn::Join": ["", [False, {"Fn::Base64": "x"}]]})
        == '"false${base64encode("x")}"'
    )
    template = {"Parameters": {"N": {"Type": "String"}}}
    assert "range(var.n)" in expression({"Fn::Cidr": ["10.0.0.0/16", {"Ref": "N"}, 8]}, template)


def test_render_comment_inside_an_object_and_mapping_tags():
    assert hcl.render({"a": 1, "note": Comment("why")}) == "{\n  a = 1\n  # why\n}"
    template = load_template(
        "Resources:\n  B:\n    Type: X\n    Properties:\n      V: !Transform {Name: M}\n"
    )
    assert template["Resources"]["B"]["Properties"]["V"] == {"Fn::Transform": {"Name": "M"}}


def test_remaining_small_paths():
    assert (
        expression({"Fn::Join": ["-", [1, {"Ref": "Bucket"}]]}) == '"1-${aws_s3_bucket.bucket.id}"'
    )
    assert awscc_type("AWS::Redshift::QEV2IdcApplication") == "awscc_redshift_qev2_idc_application"
    template = {
        "Resources": {
            "Q": {
                "Type": "AWS::Batch::JobQueue",
                "Properties": {"Priority": 1, "State": {"Ref": "AWS::NoValue"}},
            },
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "CorsConfiguration": {
                        "CorsRules": [
                            {"AllowedMethods": ["GET"], "AllowedOrigins": ["*"], "MaxAge": "soon"}
                        ]
                    }
                },
            },
        }
    }
    main = files(template)["main.tf"]
    assert "state" not in main.split('resource "awscc_batch_job_queue"')[1].split("}")[0]
    assert 'max_age_seconds = "soon"' in main


def test_lists_drop_no_value_items():
    template = {"Conditions": {"C": {"Fn::Equals": ["a", "b"]}}}
    assert expression(["a", {"Ref": "AWS::NoValue"}], template) == '["a"]'
    assert expression(["a", {"Fn::If": ["C", "b", {"Ref": "AWS::NoValue"}]}], template) == (
        '[for item in ["a", local.c ? "b" : null] : item if item != null]'
    )
    assert expression([{"Fn::If": ["C", {"Ref": "AWS::NoValue"}, "b"]}], template) == (
        '[for item in [local.c ? null : "b"] : item if item != null]'
    )
    assert expression([{"Fn::If": ["C", "a", "b"]}], template) == '[local.c ? "a" : "b"]'


def test_optional_items_and_literal_guard():

    ctx = Context({"Resources": {}})
    ctx.conditions["C"] = "c"
    no = {"Ref": "AWS::NoValue"}
    assert ctx.optional_item({"Fn::If": ["C", "x", no]}) == ("local.c", "x")
    assert ctx.optional_item({"Fn::If": ["C", no, "x"]}) == ("!local.c", "x")
    assert ctx.optional_item({"Fn::If": ["C", "x", "y"]}) == (None, {"Fn::If": ["C", "x", "y"]})
    assert ctx.optional_item({"Fn::If": "bad"}) == (None, {"Fn::If": "bad"})
    assert ctx.optional_item("x") == (None, "x")

    props = Props(ctx, {"L": [1], "M": {"Ref": "X"}, "S": "text"}, "R", "R (T)")
    assert props.literal("L", list) == [1]
    assert props.literal("M", dict) is None and props.literal("S", list) is None
    assert props.literal("Gone", list) is None
    assert props.literal("V", dict, value={"a": 1}) == {"a": 1}
    assert sum("comes from a function" in n for n in ctx.notes) == 2

    info = ResourceInfo("R", "AWS::X::Y", "aws_x", "r")
    plain = Builder(ctx, info, {"Type": "AWS::X::Y"})
    assert plain.count_items() == []
    assert hcl.inline(plain.count_items("local.c")[0].value) == "local.c ? 1 : 0"
    counted = Builder(ctx, info, {"Type": "AWS::X::Y", "Condition": "C"})
    assert hcl.inline(counted.count_items("!local.c")[0].value) == "local.c && (!local.c) ? 1 : 0"
    counted.add_resource("aws_y", "_extra", [], when="local.c")
    assert "count = local.c && (local.c) ? 1 : 0" in hcl.render_block(counted.extra[0])


def test_awscc_if_with_wrong_arguments_is_an_error():
    template = {
        "Conditions": {"C": {"Fn::Equals": ["a", "b"]}},
        "Resources": {
            "Q": {
                "Type": "AWS::Batch::JobQueue",
                "Properties": {"Priority": {"Fn::If": ["C", 1]}},
            }
        },
    }
    with pytest.raises(CfnError, match="Fn::If"):
        run(template, {"provider": "awscc"})
