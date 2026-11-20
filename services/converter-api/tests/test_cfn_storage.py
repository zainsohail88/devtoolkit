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
    assert "kms_key_arn = data.aws_kms_key.events_sse_key.arn" in main
    assert "stream_enabled = local.with_website" in main
    assert 'dynamic "attribute"' in main and 'dynamic "transition"' in main
    assert "eventbridge = local.with_website ? true : false" in main
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
    assert 'local_secondary_index {\n name = "l"\n # Projection was not converted: "x"\n }' in main
    assert '# KeySchema was not converted: "computed"' in main
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


# --- regression tests for the review findings -------------------------------------------

NO = {"Ref": "AWS::NoValue"}
CONDITION = {
    "Parameters": {"Env": {"Type": "String"}, "List": {"Type": "String"}},
    "Conditions": {"On": {"Fn::Equals": [{"Ref": "Env"}, "on"]}},
}
EVIL = 'x" ${file("/etc/passwd")} y'
TABLE = {
    "TableName": "t",
    "BillingMode": "PAY_PER_REQUEST",
    "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
    "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
}


def optional(value):
    return {"Fn::If": ["On", value, NO]}


def table(**properties) -> dict:
    return {"Type": "AWS::DynamoDB::Table", "Properties": {**TABLE, **properties}}


def bucket_rule(**rule) -> dict:
    return {
        "Type": "AWS::S3::Bucket",
        "Properties": {"LifecycleConfiguration": {"Rules": [{"Status": "Enabled", **rule}]}},
    }


def assert_no_injection(main: str) -> None:
    code = "\n".join(line for line in main.splitlines() if not line.lstrip().startswith("#"))
    assert 'file("' not in code
    assert not re.search(r"(?<!\$)\$\{file", code)


def test_sse_key_that_is_not_an_arn_is_looked_up():
    main, _ = convert(
        {
            "Alias": table(SSESpecification={"SSEEnabled": True, "KMSMasterKeyId": "alias/k"}),
            "Arn": table(
                TableName="a",
                SSESpecification={"SSEEnabled": True, "KMSMasterKeyId": "arn:aws:kms:k"},
            ),
            "Key": {"Type": "AWS::KMS::Key", "Properties": {}},
            "Ref": table(TableName="r", SSESpecification={"KMSMasterKeyId": {"Ref": "Key"}}),
            "Att": table(
                TableName="g", SSESpecification={"KMSMasterKeyId": {"Fn::GetAtt": ["Key", "Arn"]}}
            ),
            "Param": table(TableName="p", SSESpecification={"KMSMasterKeyId": {"Ref": "Env"}}),
        },
        **CONDITION,
    )
    assert "kms_key_arn = data.aws_kms_key.alias_sse_key.arn" in main
    assert 'data "aws_kms_key" "alias_sse_key" {\n key_id = "alias/k"\n}' in main
    assert 'kms_key_arn = "arn:aws:kms:k"' in main
    assert main.count("kms_key_arn = aws_kms_key.key.arn") == 2
    assert 'data "aws_kms_key" "param_sse_key" {\n key_id = var.env\n}' in main


def test_sse_key_lookup_follows_the_table_condition():
    main, _ = convert(
        {
            "T": {
                **table(SSESpecification={"SSEEnabled": True, "KMSMasterKeyId": "k"}),
                "Condition": "On",
            }
        },
        **CONDITION,
    )
    assert "kms_key_arn = data.aws_kms_key.t_sse_key[0].arn" in main
    assert 'data "aws_kms_key" "t_sse_key" {\n count = local.on ? 1 : 0' in main


def test_conditional_stream_and_stream_policy_is_reported():
    main, notes = convert(
        {
            "Cond": table(StreamSpecification=optional({"StreamViewType": "KEYS_ONLY"})),
            "Policy": table(
                TableName="p",
                StreamSpecification={"StreamViewType": "NEW_IMAGE", "ResourcePolicy": {"P": 1}},
            ),
            "Computed": table(
                TableName="c",
                StreamSpecification={
                    "Fn::If": ["On", {"StreamViewType": "A"}, {"StreamViewType": "B"}]
                },
            ),
        },
        **CONDITION,
    )
    assert "stream_enabled = local.on" in main
    assert 'stream_view_type = local.on ? "KEYS_ONLY" : null' in main
    assert "stream_view_type = null" not in main
    assert '# ResourcePolicy was not converted: {"P": 1}' in main
    assert any("'Policy.StreamSpecification.ResourcePolicy' was not converted" in n for n in notes)
    assert "# StreamSpecification was not converted" in main


def test_contributor_insights_of_an_index_is_reported():
    main, notes = convert(
        {
            "T": table(
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "g",
                        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                        "ContributorInsightsSpecification": {"Enabled": True},
                    }
                ]
            )
        }
    )
    assert "# ContributorInsightsSpecification was not converted" in main
    assert any("GlobalSecondaryIndexes[0].ContributorInsightsSpecification" in n for n in notes)


def test_computed_table_structures_are_reported_not_guessed():
    main, notes = convert(
        {
            "T": table(
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"},
                    optional({"AttributeName": "gk", "AttributeType": "S"}),
                    {"Fn::If": ["On", {"AttributeName": "a"}, {"AttributeName": "b"}]},
                ],
                KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"Ref": "List"}],
                ProvisionedThroughput={"Ref": "List"},
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "g",
                        "KeySchema": {"Ref": "List"},
                        "Projection": {"Fn::If": ["On", {"ProjectionType": "ALL"}, {}]},
                    }
                ],
            ),
            "Whole": table(
                TableName="w", AttributeDefinitions=optional([]), KeySchema={"Ref": "List"}
            ),
        },
        **CONDITION,
    )
    assert 'dynamic "attribute" {\n for_each = local.on ? [1] : []' in main
    assert 'name = "gk"' in main
    assert main.count("# AttributeDefinitions was not converted") == 2
    assert main.count("# KeySchema was not converted") == 3
    assert "# ProvisionedThroughput was not converted" in main
    assert "# Projection was not converted" in main
    assert 'projection_type = "ALL"' not in main


def test_lifecycle_rule_with_conditional_and_computed_parts():
    main, notes = convert(
        {
            "Cond": bucket_rule(
                Transitions=[
                    optional({"StorageClass": "GLACIER", "TransitionInDays": 30}),
                    {"Ref": "List"},
                ],
                NoncurrentVersionTransitions=[
                    optional({"StorageClass": "GLACIER", "TransitionInDays": 5})
                ],
                NoncurrentVersionExpiration=optional({"NoncurrentDays": 5}),
                AbortIncompleteMultipartUpload=optional({"DaysAfterInitiation": 1}),
                TagFilters=[{"Key": "a", "Value": "b"}, {"Ref": "List"}],
            ),
            "Computed": bucket_rule(
                Transitions={"Ref": "List"},
                TagFilters=optional([{"Key": "a", "Value": "b"}]),
                NoncurrentVersionExpiration={"Ref": "List"},
                AbortIncompleteMultipartUpload={"Ref": "List"},
            ),
        },
        **CONDITION,
    )
    for kind in (
        "transition",
        "noncurrent_version_transition",
        "noncurrent_version_expiration",
        "abort_incomplete_multipart_upload",
    ):
        assert f'dynamic "{kind}" {{\n for_each = local.on ? [1] : []' in main
    assert "noncurrent_days = null" not in main
    assert 'tag {\n key = "a"' in main
    for name in ("Transitions", "TagFilters"):
        assert main.count(f"# {name} was not converted") == 2
    assert "# NoncurrentVersionExpiration was not converted" in main
    assert "# AbortIncompleteMultipartUpload was not converted" in main


def test_more_than_a_thousand_lifecycle_rules():
    rules = [{"Status": "Enabled", "ExpirationInDays": 1}] * 1001
    main, _ = convert(
        {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"LifecycleConfiguration": {"Rules": rules}},
            }
        }
    )
    assert 'id = "rule-1001"' in main


def test_conditional_and_computed_notification_parts():
    main, notes = convert(
        {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "NotificationConfiguration": {
                        "QueueConfigurations": [
                            {
                                "Event": "e",
                                "Queue": "arn:q1",
                                "Filter": {
                                    "S3Key": {"Rules": optional([{"Name": "prefix", "Value": "a"}])}
                                },
                            },
                            {
                                "Event": "e",
                                "Queue": "arn:q2",
                                "Filter": {
                                    "S3Key": {
                                        "Rules": [
                                            {"Ref": "List"},
                                            {"Name": "suffix", "Value": ".x"},
                                        ]
                                    }
                                },
                            },
                            {"Event": "e", "Queue": "arn:q3", "Filter": {"S3Key": {"Ref": "List"}}},
                        ],
                        "EventBridgeConfiguration": optional({"EventBridgeEnabled": True}),
                    }
                },
            },
            "Computed": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "NotificationConfiguration": {
                        "EventBridgeConfiguration": {
                            "Fn::If": ["On", {}, {"EventBridgeEnabled": False}]
                        }
                    }
                },
            },
            "Plain": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"NotificationConfiguration": {"EventBridgeConfiguration": {}}},
            },
        },
        **CONDITION,
    )
    assert "# Filter.S3Key.Rules was not converted" in main
    assert 'filter_suffix = ".x"' in main
    assert "# Filter.S3Key was not converted" in main
    assert "eventbridge = local.on ? true : false" in main
    assert "eventbridge = true" in main
    assert "# EventBridgeConfiguration was not converted" in main


def test_conditional_and_computed_ecr_lifecycle_policy():
    main, notes = convert(
        {
            "Cond": {
                "Type": "AWS::ECR::Repository",
                "Properties": {
                    "RepositoryName": "c",
                    "LifecyclePolicy": optional({"LifecyclePolicyText": '{"rules": []}'}),
                },
            },
            "Registry": {
                "Type": "AWS::ECR::Repository",
                "Properties": {
                    "RepositoryName": "r",
                    "LifecyclePolicy": {"LifecyclePolicyText": '{"rules": []}', "RegistryId": "1"},
                },
            },
            "Computed": {
                "Type": "AWS::ECR::Repository",
                "Properties": {"RepositoryName": "x", "LifecyclePolicy": {"Ref": "List"}},
            },
            "OnlyRegistry": {
                "Type": "AWS::ECR::Repository",
                "Properties": {"RepositoryName": "o", "LifecyclePolicy": {"RegistryId": "1"}},
            },
        },
        **CONDITION,
    )
    lifecycle = main.split('resource "aws_ecr_lifecycle_policy" "cond_lifecycle"')[1]
    assert lifecycle.startswith(" {\n count = local.on ? 1 : 0")
    assert main.count('# RegistryId was not converted: "1"') == 2
    assert any("'Registry.LifecyclePolicy.RegistryId' was not converted" in n for n in notes)
    assert "# LifecyclePolicy was not converted" in main


def test_storage_template_text_cannot_inject_hcl():
    main, _ = convert(
        {
            "B": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "BucketName": EVIL,
                    "LifecycleConfiguration": {
                        "Rules": [
                            {
                                "Id": EVIL,
                                "Status": EVIL,
                                "Prefix": EVIL,
                                "TagFilters": [{"Key": EVIL, "Value": EVIL}],
                                "Transitions": [{"StorageClass": EVIL, "TransitionInDays": EVIL}],
                            }
                        ]
                    },
                    "NotificationConfiguration": {
                        "QueueConfigurations": [
                            {
                                "Event": EVIL,
                                "Queue": EVIL,
                                "Filter": {"S3Key": {"Rules": [{"Name": "prefix", "Value": EVIL}]}},
                            }
                        ],
                        "EventBridgeConfiguration": {"EventBridgeEnabled": EVIL},
                    },
                },
            },
            "T": table(
                TableName=EVIL,
                AttributeDefinitions=[{"AttributeName": EVIL, "AttributeType": EVIL}],
                KeySchema=[{"AttributeName": EVIL, "KeyType": "HASH"}],
                StreamSpecification={"StreamViewType": EVIL},
                SSESpecification={"SSEEnabled": True, "KMSMasterKeyId": EVIL},
            ),
            "R": {
                "Type": "AWS::ECR::Repository",
                "Properties": {
                    "RepositoryName": EVIL,
                    "LifecyclePolicy": {"LifecyclePolicyText": EVIL},
                },
            },
        }
    )
    assert_no_injection(main)
