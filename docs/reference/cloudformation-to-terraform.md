# CloudFormation to Terraform

How a CloudFormation, AWS SAM or AWS CDK template becomes Terraform. The [API reference](api.md#cloudformation-to-terraform) describes the endpoint.

The rule is the same as for the other converters: whatever converts only in part is kept in the output as a comment and listed in `notes`.

## Inputs

| Input | How it is detected | What happens first |
|-------|--------------------|--------------------|
| CloudFormation, JSON or YAML | The default | Nothing |
| AWS SAM | `Transform: AWS::Serverless-2016-10-31` | `AWS::Serverless::*` resources expand into the resources SAM itself creates (see [SAM](#aws-sam)) |
| AWS CDK | An `AWS::CDK::Metadata` resource or `aws:cdk:path` metadata | CDK bookkeeping is removed and names come from construct paths (see [CDK](#aws-cdk)) |

For a CDK app, run `cdk synth` and convert the template it writes to `cdk.out/<Stack>.template.json`.

Logical IDs and the names of parameters, conditions, mappings and outputs must be letters and digits, as CloudFormation requires. This also keeps every name safe to use in Terraform. `!Cidr` makes at most 256 blocks, as in CloudFormation.

YAML short forms (`!Ref`, `!GetAtt A.B`, `!Sub`, `!If`...) are read as their long forms. Scalars are read the way CloudFormation reads them: `2010-09-09` stays a string and `0755` stays `"0755"`.

## Output files

| File | Holds |
|------|-------|
| `versions.tf` | `required_version` (Terraform 1.5 or later), the providers used (`aws ~> 6.40`, `awscc ~> 1.0`, `archive ~> 2.0`), empty provider blocks |
| `variables.tf` | One variable per parameter, plus `stack_name` and any placeholder variables |
| `locals.tf` | Mappings and conditions |
| `main.tf` | Data sources and resources, in template order |
| `outputs.tf` | One output per template output |
| `imports.tf` | With `include_imports`: an `import` block per resource |

`layout: single` puts everything in `main.tf`. The output is formatted the way `terraform fmt` formats it.

The providers take their region and credentials from the environment (`AWS_REGION`, `AWS_PROFILE`) like the AWS CLI, the same way a stack gets its region when it is deployed.

## Providers

With `provider: aws` (the default), the [85 types below](#hand-written-mappings) use hand-written mappings to the `aws` provider, which give the Terraform an experienced user writes: the versioning of an S3 bucket becomes `aws_s3_bucket_versioning`, IAM inline policies become `aws_iam_role_policy` and so on.

Every other type uses the [`awscc` provider](https://registry.terraform.io/providers/hashicorp/awscc/latest/docs). awscc is generated from the CloudFormation resource schemas, so its resources match CloudFormation one to one: `AWS::Batch::JobQueue` becomes `awscc_batch_job_queue` and property `ComputeEnvironmentOrder` becomes `compute_environment_order`. DevToolkit ships an index of the 1,713 types awscc supports, built from the provider schema. The index gives the exact resource and attribute names, the properties that are maps (their keys stay as written) and the properties that are JSON documents (written with `jsonencode`). A property missing from the installed awscc version is listed in the notes. Each awscc type in use gets a note.

With `provider: awscc`, every resource uses awscc.

A type outside both (custom resources, `AWS::CloudFormation::*`, the few types awscc lacks) is written as a comment with its properties and listed in the notes.

## Parameters

| CloudFormation | Terraform |
|----------------|-----------|
| `Type: String`, `AWS::EC2::VPC::Id` and other AWS-specific types | `type = string` |
| `Type: Number` | `type = number`, default converted to a number |
| `List<Number>` | `list(number)` |
| `CommaDelimitedList`, `List<AWS::EC2::Subnet::Id>` | `list(string)`, default `"a,b"` becomes `["a", "b"]` |
| `AWS::SSM::Parameter::Value<...>` | A variable holding the parameter name and a `data "aws_ssm_parameter"`; references read its value |
| `Description` (or the label from `AWS::CloudFormation::Interface`) | `description` |
| `NoEcho: true` | `sensitive = true`; outputs that use it are sensitive too |
| `AllowedValues` | `validation { condition = contains([...], var.x) }` |
| `AllowedPattern` | `validation { condition = can(regex("^(?:pattern)$", var.x)) }` |
| `MinLength` / `MaxLength`, `MinValue` / `MaxValue` | `validation` with `length()` or a comparison |
| `ConstraintDescription` | `error_message` |

A parameter with a name Terraform reserves (`count`, `version`, `source`...) gets a trailing `_`.

## References and functions

| CloudFormation | Terraform |
|----------------|-----------|
| `!Ref Parameter` | `var.parameter` |
| `!Ref Resource` | The attribute the mapping says `Ref` returns: `aws_s3_bucket.x.id`, `aws_sqs_queue.x.url`, `aws_sns_topic.x.arn`... For awscc, `id` |
| `!GetAtt Resource.Attribute` | The mapped attribute (`Arn` → `arn`, `RepositoryUri` → `repository_url`); for other attributes the name in snake_case, with a note for aws types |
| `AWS::Region`, `AWS::AccountId`, `AWS::Partition`, `AWS::URLSuffix` | `data.aws_region.current.region`, `data.aws_caller_identity.current.account_id`, `data.aws_partition.current.partition` / `.dns_suffix` |
| `AWS::StackName` | `var.stack_name` (default: the `stack_name` option) |
| `AWS::NoValue` | `null` or the attribute is left out |
| `!Sub "a-${X}-${Y.Arn}"` | `"a-${var.x}-${aws_y.y.arn}"`, with `${!Literal}` kept as `$${Literal}` |
| `!Join`, `!Select`, `!Split` | A template string where it reads better, else `join()`, `element()`, `split()`; literal values are worked out in place |
| `!If [Cond, A, B]` | `local.cond ? A : B` |
| `!Equals`, `!And`, `!Or`, `!Not` | `==`, `&&`, `\|\|`, `!`. CloudFormation compares text, so literals become strings and a `Number` parameter goes through `tostring()` |
| `!FindInMap [Map, A, B]` | `local.map[A][B]`; with a `DefaultValue`, nested `lookup()` |
| `!GetAZs` | `data.aws_availability_zones.available.names` |
| `!Base64 x` | `base64encode(x)` |
| `!Cidr [block, count, bits]` | The list itself for a literal block, else a `for` expression over `cidrsubnet()` |
| `!ImportValue name` | A variable `import_<name>`, with a note: stack exports exist in CloudFormation only |
| `Fn::ToJsonString`, `Fn::Length` (`AWS::LanguageExtensions`) | `jsonencode()`, `length()` |
| Policy documents, event patterns, state machine definitions | `jsonencode({...})` with the original keys |

A reference to a resource that was left as a comment becomes a placeholder variable describing what it stands for, so the configuration still validates.

`AWS::NoValue` is removed the way CloudFormation removes it: from a list (`[for item in [...] : item if item != null]`), from a mapping (`merge({...}, cond ? {key = value} : {})`) and from a `!Join`. A list item such as a policy ARN, a queue or a security group rule written as `!If [Cond, x, !Ref AWS::NoValue]` becomes a resource with `count = local.cond ? 1 : 0`. Inside JSON documents, an `!If` with branches of different shapes (a string or a list) is encoded per branch.

### Known limitations

- An `!If` that picks between two whole lists of nested blocks (for example two different `BlockDeviceMappings` lists) stays a comment and is listed in the notes.
- In ECS container definitions, a `!Ref` to a `Number` parameter used as an environment value stays a number; ECS expects a string there.

## Resource attributes

| CloudFormation | Terraform |
|----------------|-----------|
| `Condition` | `count = local.cond ? 1 : 0`; references to it use `[0]` |
| `DependsOn` | `depends_on` |
| `DeletionPolicy: Retain` | `lifecycle { prevent_destroy = true }`, with a note: Terraform then refuses to destroy the resource, where CloudFormation leaves it behind |
| `DeletionPolicy: Snapshot` | For RDS, `skip_final_snapshot = false` and a `final_snapshot_identifier` (also the default for DB instances, as in CloudFormation); for other types a note |
| `UpdateReplacePolicy`, `CreationPolicy`, `UpdatePolicy`, `AWS::CloudFormation::Init` | Notes |

Where CloudFormation generates a name Terraform requires (a DynamoDB table, a Lambda function, an ECR repository...), the name becomes `${var.stack_name}-<LogicalId>`, with a note. To adopt an existing resource, use its current name.

## Hand-written mappings

| Area | CloudFormation types |
|------|----------------------|
| S3 | `Bucket` (versioning, encryption, public access block, ownership controls, ACL, lifecycle, CORS, website, logging, acceleration, object lock and notifications each become their own resource), `BucketPolicy` |
| DynamoDB, ECR | `Table`, `Repository` (lifecycle and repository policies become their own resources) |
| IAM | `Role`, `Policy`, `ManagedPolicy`, `InstanceProfile`, `User`, `Group`. Inline policies and managed policy attachments become one resource each |
| Lambda | `Function`, `Permission`, `EventSourceMapping`, `LayerVersion`, `Url`, `Alias`, `Version` |
| EC2 compute, ECS | `Instance`, `LaunchTemplate`; `Cluster`, `TaskDefinition` (container definitions as ECS JSON), `Service` |
| API Gateway | `RestApi`, `Deployment`, `Stage`, `Resource`, `Method`; v2 `Api`, `Integration`, `Route`, `Stage`, `Authorizer` |
| Messaging | SQS `Queue`, `QueuePolicy`; SNS `Topic`, `Subscription`, `TopicPolicy`; EventBridge `Rule` (one target resource per target), `EventBus`; Kinesis `Stream`; Step Functions `StateMachine` |
| Networking | `VPC`, `Subnet`, `InternetGateway`, `EgressOnlyInternetGateway`, `VPCGatewayAttachment`, `RouteTable`, `Route`, `SubnetRouteTableAssociation`, `EIP`, `EIPAssociation`, `NatGateway`, `VPCEndpoint`, `SecurityGroup`, `SecurityGroupIngress`, `SecurityGroupEgress`, `NetworkAcl`, `NetworkAclEntry`, `SubnetNetworkAclAssociation`, `FlowLog` |
| Load balancing, DNS, certificates, CDN | ELBv2 `LoadBalancer`, `TargetGroup`, `Listener`, `ListenerCertificate`, `ListenerRule`; Route 53 `HostedZone`, `RecordSet`, `RecordSetGroup`; ACM `Certificate` (DNS validation records and `aws_acm_certificate_validation` when the zone is known); CloudFront `Distribution`, `OriginAccessControl`, `CloudFrontOriginAccessIdentity` |
| Databases, secrets, keys | RDS `DBInstance`, `DBCluster`, `DBSubnetGroup`, `DBParameterGroup`; KMS `Key`, `Alias`; Secrets Manager `Secret`; SSM `Parameter` |
| Observability | Logs `LogGroup`, `MetricFilter`, `SubscriptionFilter`; CloudWatch `Alarm`, `Dashboard` |

These differences between CloudFormation and Terraform are handled for you:

- A security group without egress rules gets the implicit allow-all egress rule of CloudFormation written out, because `aws_security_group` removes it.
- Lambda `ZipFile` code becomes an `archive_file` data source, named after the handler and runtime (`index.py`, `index.js`).
- `AWS::Lambda::Version` becomes `publish = true` on the function plus a `terraform_data` that records the version; in the aws provider, versions belong to the function.
- An API Gateway deployment is redeployed when the API body changes and keeps `create_before_destroy`.

A property a mapping leaves out is written as a comment in the resource and listed in the notes.

## AWS SAM

SAM resources expand with the logical IDs SAM uses, so references in the template (`!GetAtt MyFunctionRole.Arn`, `!Ref ServerlessRestApi`, `!Ref MyFunction.Alias`) keep working.

| SAM | Becomes |
|-----|---------|
| `Globals` | Merged into each resource the way SAM merges them: the value on the resource wins, maps merge, lists add up |
| `AWS::Serverless::Function` | A function and its role `<Function>Role` with `AWSLambdaBasicExecutionRole` (plus X-Ray for `Tracing: Active` and VPC access for `VpcConfig`) |
| `CodeUri: ./src` | An `archive_file` that zips the folder; `s3://` URIs and `{Bucket, Key}` become S3 code |
| `InlineCode`, `ImageUri` | `ZipFile` code, an image function |
| `Policies` | Managed policy names, policy documents and the common [policy templates](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-policy-template-list.html) (DynamoDB, S3, SQS, SNS, Lambda, Step Functions, EventBridge, SSM, Secrets Manager, KMS, Kinesis, CloudWatch, VPC access...). Other templates are listed in the notes |
| Events: `Api`, `HttpApi` | Paths in the generated OpenAPI body of `ServerlessRestApi` / `ServerlessHttpApi` (or your `AWS::Serverless::Api`) and a Lambda permission |
| Events: `SQS`, `Kinesis`, `DynamoDB` | An event source mapping and the matching managed policy |
| Events: `Schedule`, `EventBridgeRule`, `CloudWatchEvent`, `ScheduleV2` | An EventBridge rule (or an EventBridge Scheduler schedule and its role) and a permission |
| Events: `SNS`, `S3`, `CloudWatchLogs` | A subscription, a notification on the bucket or a subscription filter, plus a permission |
| `AutoPublishAlias`, `FunctionUrlConfig`, `DeadLetterQueue` | A version and an alias that the events point at, a function URL, a dead-letter target and its permission |
| `AWS::Serverless::Api`, `HttpApi` | API, deployment and stage. `DefinitionUri: api.yaml` becomes `yamldecode(file("api.yaml"))` |
| `AWS::Serverless::SimpleTable`, `LayerVersion`, `StateMachine` | A table (on-demand unless throughput is given), a layer version, a state machine with its role, events and `DefinitionSubstitutions` applied |
| `AWS::Serverless::Connector` and `Connectors` | IAM policies on the role of the source; Lambda permissions for API, SNS, S3 and EventBridge sources |

`DeploymentPreference`, `Auth`, `Cors` (on REST APIs), `Domain`, `AWS::Serverless::Application` and other settings without an equivalent are listed in the notes.

## AWS CDK

- `CDKMetadata`, the `BootstrapVersion` parameter and the `CheckBootstrapVersion` rule are left out.
- Terraform names come from construct paths: `MyStack/Api/Handler/Resource` becomes `api_handler`.
- Lambda and layer code from assets is read from `cdk.out/asset.<hash>` and zipped with `archive_file`. Keep `cdk.out` next to the Terraform files.
- `autoDeleteObjects` becomes `force_destroy = true` on the bucket; the helper function of the CDK, its role and its bucket policy statement are left out.
- `logRetention` becomes an `aws_cloudwatch_log_group` with that retention.
- `restrictDefaultSecurityGroup` becomes a note to add an `aws_default_security_group`.

## Moving a stack to Terraform

With `include_imports`, `imports.tf` has an [`import` block](https://developer.hashicorp.com/terraform/language/import) per resource, so Terraform adopts what the stack created. Where the template gives the name of a resource as a literal, the import ID is that name; elsewhere it is `TODO`, to be replaced with the physical ID from `aws cloudformation describe-stack-resources --stack-name <stack>`. Then:

1. Set `DeletionPolicy: Retain` on every resource and update the stack.
2. Run `terraform plan` until it shows no changes.
3. Delete the stack. The resources stay, now managed by Terraform.

## Checking the output

`make validate-terraform` converts each CloudFormation, SAM and CDK test template in `services/converter-api/tests/fixtures/cfn/` twice, once for each provider. It runs `terraform fmt -check` and `terraform validate` on every result and fails on errors and warnings. CI runs it on every pull request.
