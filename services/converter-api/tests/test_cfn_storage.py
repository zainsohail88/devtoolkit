"""S3, DynamoDB and ECR mappings (converters/cfn/resources/storage.py)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from converters.cfn import convert_template

FIXTURE = Path(__file__).parent / "fixtures" / "cfn" / "storage.yaml"


def squeeze(text: str) -> str:
    return re.sub(r" +", " ", text)


def convert(resources: dict, **extra) -> tuple[str, list[str]]:
    result = convert_template(yaml.safe_dump({"Resources": resources, **extra}), stack_name="demo")
    main = next(f["content"] for f in result["files"] if f["path"] == "main.tf")
    return squeeze(main), result["notes"]


def test_fixture_converts_everything():
    result = convert_template(FIXTURE.read_text(), stack_name="demo")
    assert result["summary"]["unsupported"] == 0
    assert not [n for n in result["notes"] if "was not converted" in n]
    files = {f["path"]: squeeze(f["content"]) for f in result["files"]}
    main = files["main.tf"]
    for kind in (
        "aws_s3_bucket_versioning",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_s3_bucket_public_access_block",
        "aws_s3_bucket_ownership_controls",
        "aws_s3_bucket_acl",
        "aws_s3_bucket_lifecycle_configuration",
        "aws_s3_bucket_cors_configuration",
        "aws_s3_bucket_website_configuration",
        "aws_s3_bucket_logging",
        "aws_s3_bucket_accelerate_configuration",
        "aws_s3_bucket_object_lock_configuration",
        "aws_s3_bucket_notification",
        "aws_s3_bucket_policy",
        "aws_dynamodb_table",
        "aws_ecr_repository",
        "aws_ecr_lifecycle_policy",
        "aws_ecr_repository_policy",
    ):
        assert f'resource "{kind}"' in main, kind
    assert 'acl = "log-delivery-write"' in main
    assert "depends_on = [aws_s3_bucket_ownership_controls.logs_ownership]" in main
    assert "target_bucket = aws_s3_bucket.logs.id" in main
    assert 'target_prefix = "assets/"' in main
    assert 'target_bucket = aws_s3_bucket.redirect.id\n target_prefix = ""' in main
    assert 'filter_prefix = "uploads/"' in main and 'filter_suffix = ".jpg"' in main
    assert "eventbridge = true" in main
    assert "max_age_seconds = 3600" in main
    assert 'replace_key_prefix_with = "documents/"' in main
    assert 'range_key = "sk"' in main and "read_capacity = 5" in main
    assert 'non_key_attributes = ["total"]' in main
    assert 'projection_type = "ALL"' in main  # an empty Projection means ALL
    assert "stream_enabled = true" in main
    assert "max_read_request_units = 100" in main
    assert 'name = "shop/images"' in main and "force_delete = true" in main
    assert 'name = "${var.stack_name}-cache"' in main
    outputs = files["outputs.tf"]
    website = "aws_s3_bucket_website_configuration.site_website[0].website_endpoint"
    assert f'value = local.with_website ? "http://${{{website}}}" : null' in outputs
    assert "value = aws_s3_bucket.assets.bucket_regional_domain_name" in outputs
    assert "value = aws_ecr_repository.images.repository_url" in outputs


def test_access_control_without_ownership_controls_is_noted():
    main, notes = convert(
        {"B": {"Type": "AWS::S3::Bucket", "Properties": {"AccessControl": "Private"}}}
    )
    assert 'acl = "private"' in main and "depends_on" not in main
    assert any("S3 disables ACLs by default" in n for n in notes)


def test_computed_access_control():
    main, _ = convert(
        {"B": {"Type": "AWS::S3::Bucket", "Properties": {"AccessControl": {"Fn::Sub": "x"}}}}
    )
    assert 'acl = "x"' in main


def test_lifecycle_without_id_or_filter():
    main, _ = convert(
        {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "LifecycleConfiguration": {
                        "Rules": [{"Status": "Enabled", "ExpirationInDays": 1}]
                    }
                },
            }
        }
    )
    assert 'id = "rule-1"' in main and "filter {}" in main


def test_lifecycle_tag_and_size_filters_combine_into_and():
    main, _ = convert(
        {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "LifecycleConfiguration": {
                        "Rules": [
                            {
                                "Status": "Enabled",
                                "ObjectSizeLessThan": 5,
                                "TagFilters": [{"Key": "a", "Value": "b"}],
                                "ExpirationInDays": 1,
                            },
                            {
                                "Status": "Enabled",
                                "ObjectSizeGreaterThan": 5,
                                "ExpirationInDays": 1,
                            },
                        ]
                    }
                },
            }
        }
    )
    assert 'and {\n object_size_less_than = 5\n tags = {\n a = "b"\n }\n }' in main
    assert "filter {\n object_size_greater_than = 5\n }" in main


def test_conditional_bucket_sub_resources_share_the_condition():
    main, _ = convert(
        {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Condition": "On",
                "Properties": {"VersioningConfiguration": {"Status": "Enabled"}},
            }
        },
        Conditions={"On": {"Fn::Equals": ["a", "a"]}},
    )
    assert main.count("count = local.on ? 1 : 0") == 2
    assert "bucket = aws_s3_bucket.b[0].id" in main


def test_cdk_auto_delete_hint_sets_force_destroy():
    from converters.cfn.convert import Converter
    from converters.cfn.parse import load_template

    template = load_template(yaml.safe_dump({"Resources": {"B": {"Type": "AWS::S3::Bucket"}}}))
    result = Converter(template, hints={"B": {"force_destroy": True}}).run("single")
    assert "force_destroy = true" in result["files"][0]["content"]


def test_dynamic_versioning_and_notification_with_odd_filters():
    main, _ = convert(
        {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "NotificationConfiguration": {
                        "QueueConfigurations": [
                            {"Event": "s3:ObjectCreated:*", "Queue": "arn:q", "Filter": "odd"},
                            {
                                "Event": "s3:ObjectCreated:*",
                                "Queue": "arn:q2",
                                "Filter": {"S3Key": {"Rules": [{"Name": "other", "Value": "x"}]}},
                            },
                        ],
                        "EventBridgeConfiguration": "yes",
                    }
                },
            }
        }
    )
    assert 'queue_arn = "arn:q"' in main and "filter_" not in main
    assert "eventbridge" not in main


def test_dynamodb_table_with_only_a_hash_key_and_no_stream():
    main, notes = convert(
        {
            "T": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {
                    "TableName": "t",
                    "KeySchema": "computed",
                    "LocalSecondaryIndexes": [
                        {"IndexName": "l", "KeySchema": [], "Projection": "x"}
                    ],
                },
            }
        }
    )
    assert 'name = "t"' in main and "hash_key" not in main
    assert 'local_secondary_index {\n name = "l"\n }' in main
    assert not any("TableName" in n for n in notes)


def test_ecr_lifecycle_policy_without_text_is_ignored():
    main, _ = convert(
        {
            "R": {
                "Type": "AWS::ECR::Repository",
                "Properties": {"RepositoryName": "r", "LifecyclePolicy": {}},
            }
        }
    )
    assert "aws_ecr_lifecycle_policy" not in main


def test_lifecycle_dates_single_noncurrent_transition_and_string_booleans():
    main, _ = convert(
        {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "LifecycleConfiguration": {
                        "Rules": [
                            {
                                "Status": "Enabled",
                                "ExpiredObjectDeleteMarker": "true",
                                "Transition": {
                                    "StorageClass": "GLACIER",
                                    "TransitionDate": "2030-01-01T00:00:00Z",
                                },
                                "NoncurrentVersionTransition": {
                                    "StorageClass": "GLACIER",
                                    "TransitionInDays": 5,
                                },
                            }
                        ]
                    }
                },
            }
        }
    )
    assert "expired_object_delete_marker = true" in main
    assert 'date = "2030-01-01T00:00:00Z"' in main
    assert "noncurrent_version_transition {\n noncurrent_days = 5" in main
