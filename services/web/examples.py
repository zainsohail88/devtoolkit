"""The example inputs behind the "Load an example" buttons."""

from __future__ import annotations

# A realistic compose file for the "Load an example" button: two services, a health check,
# a dependency and a named volume, so every kind of conversion shows up in the result.
EXAMPLE_COMPOSE = """\
name: shop
services:
  web:
    image: nginx:1.27
    ports:
      - "8080:80"
    depends_on:
      - api
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost/"]
      interval: 30s
      timeout: 3s
      retries: 3
  api:
    image: ghcr.io/example/shop-api:1.4.2
    ports:
      - "3000"
    environment:
      DATABASE_URL: postgres://shop:shop@db:5432/shop
      LOG_LEVEL: info
    depends_on:
      - db
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
  db:
    image: postgres:16
    ports:
      - "5432"
    environment:
      POSTGRES_PASSWORD: shop
    volumes:
      - dbdata:/var/lib/postgresql/data
volumes:
  dbdata: {}
"""

# Examples for the CloudFormation page: one per kind of input, each small enough to read but
# with something in it for every part of the conversion (references, conditions, notes).
EXAMPLE_CLOUDFORMATION = """\
AWSTemplateFormatVersion: "2010-09-09"
Description: Image uploads, resized by a function
Parameters:
  Environment:
    Type: String
    Default: dev
    AllowedValues: [dev, prod]
  RetentionDays:
    Type: Number
    Default: 30
Conditions:
  IsProd: !Equals [!Ref Environment, prod]
Resources:
  Uploads:
    Type: AWS::S3::Bucket
    DeletionPolicy: Retain
    Properties:
      BucketName: !Sub "${AWS::StackName}-uploads-${AWS::AccountId}"
      VersioningConfiguration:
        Status: !If [IsProd, Enabled, Suspended]
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - ServerSideEncryptionByDefault:
              SSEAlgorithm: AES256
      LifecycleConfiguration:
        Rules:
          - Id: expire-old
            Status: Enabled
            ExpirationInDays: !Ref RetentionDays
      Tags:
        - Key: environment
          Value: !Ref Environment
  Jobs:
    Type: AWS::SQS::Queue
    Properties:
      VisibilityTimeout: 120
      RedrivePolicy:
        deadLetterTargetArn: !GetAtt DeadLetters.Arn
        maxReceiveCount: 5
  DeadLetters:
    Type: AWS::SQS::Queue
    Properties:
      MessageRetentionPeriod: 1209600
  ResizerRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              Service: lambda.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
      Policies:
        - PolicyName: read-uploads
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Effect: Allow
                Action: s3:GetObject
                Resource: !Sub "${Uploads.Arn}/*"
  Resizer:
    Type: AWS::Lambda::Function
    Properties:
      Runtime: python3.13
      Handler: index.handler
      Role: !GetAtt ResizerRole.Arn
      MemorySize: !If [IsProd, 1024, 256]
      Timeout: 60
      Environment:
        Variables:
          BUCKET: !Ref Uploads
          QUEUE_URL: !Ref Jobs
      Code:
        ZipFile: |
          def handler(event, context):
              return {"resized": len(event.get("Records", []))}
  JobsTrigger:
    Type: AWS::Lambda::EventSourceMapping
    Properties:
      EventSourceArn: !GetAtt Jobs.Arn
      FunctionName: !Ref Resizer
      BatchSize: 10
  Alerts:
    Type: AWS::SNS::Topic
    Condition: IsProd
Outputs:
  BucketName:
    Value: !Ref Uploads
  QueueUrl:
    Value: !Ref Jobs
    Export:
      Name: !Sub "${AWS::StackName}-queue-url"
"""

EXAMPLE_SAM = """\
AWSTemplateFormatVersion: "2010-09-09"
Transform: AWS::Serverless-2016-10-31
Description: A small orders API
Globals:
  Function:
    Runtime: python3.13
    Timeout: 10
    Environment:
      Variables:
        TABLE_NAME: !Ref Orders
Resources:
  Orders:
    Type: AWS::Serverless::SimpleTable
    Properties:
      PrimaryKey:
        Name: orderId
        Type: String
  OrdersFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/orders/
      Handler: app.handler
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref Orders
      Events:
        ListOrders:
          Type: Api
          Properties:
            Path: /orders
            Method: get
        GetOrder:
          Type: Api
          Properties:
            Path: /orders/{id}
            Method: get
        Nightly:
          Type: Schedule
          Properties:
            Schedule: cron(0 3 * * ? *)
Outputs:
  ApiUrl:
    Value: !Sub "https://${ServerlessRestApi}.execute-api.${AWS::Region}.amazonaws.com/Prod/orders"
"""

EXAMPLE_CDK = """\
{
  "Resources": {
    "NotesTable7B1C1A4B": {
      "Type": "AWS::DynamoDB::Table",
      "Properties": {
        "AttributeDefinitions": [{"AttributeName": "id", "AttributeType": "S"}],
        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
        "BillingMode": "PAY_PER_REQUEST"
      },
      "UpdateReplacePolicy": "Retain",
      "DeletionPolicy": "Retain",
      "Metadata": {"aws:cdk:path": "NotesStack/NotesTable/Resource"}
    },
    "HandlerServiceRoleFCDC14AE": {
      "Type": "AWS::IAM::Role",
      "Properties": {
        "AssumeRolePolicyDocument": {
          "Statement": [{
            "Action": "sts:AssumeRole",
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"}
          }],
          "Version": "2012-10-17"
        },
        "ManagedPolicyArns": [{
          "Fn::Join": ["", [
            "arn:", {"Ref": "AWS::Partition"},
            ":iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
          ]]
        }]
      },
      "Metadata": {"aws:cdk:path": "NotesStack/Handler/ServiceRole/Resource"}
    },
    "Handler886CB40B": {
      "Type": "AWS::Lambda::Function",
      "Properties": {
        "Code": {
          "S3Bucket": {"Fn::Sub": "cdk-hnb659fds-assets-${AWS::AccountId}-${AWS::Region}"},
          "S3Key": "6c3d4a1f0e2b.zip"
        },
        "Handler": "index.handler",
        "Role": {"Fn::GetAtt": ["HandlerServiceRoleFCDC14AE", "Arn"]},
        "Runtime": "nodejs22.x",
        "Environment": {"Variables": {"TABLE": {"Ref": "NotesTable7B1C1A4B"}}}
      },
      "DependsOn": ["HandlerServiceRoleFCDC14AE"],
      "Metadata": {
        "aws:cdk:path": "NotesStack/Handler/Resource",
        "aws:asset:path": "asset.6c3d4a1f0e2b",
        "aws:asset:property": "Code"
      }
    },
    "CDKMetadata": {
      "Type": "AWS::CDK::Metadata",
      "Properties": {"Analytics": "v2:deflate64:H4sIAAAAAAAA"},
      "Metadata": {"aws:cdk:path": "NotesStack/CDKMetadata/Default"}
    }
  },
  "Parameters": {
    "BootstrapVersion": {
      "Type": "AWS::SSM::Parameter::Value<String>",
      "Default": "/cdk-bootstrap/hnb659fds/version"
    }
  },
  "Rules": {
    "CheckBootstrapVersion": {
      "Assertions": [{
        "Assert": {"Fn::Not": [{"Fn::Contains": [
          ["1", "2", "3", "4", "5"], {"Ref": "BootstrapVersion"}
        ]}]},
        "AssertDescription": "CDK bootstrap stack version 6 required."
      }]
    }
  }
}
"""

EXAMPLES = {"cloudformation": EXAMPLE_CLOUDFORMATION, "sam": EXAMPLE_SAM, "cdk": EXAMPLE_CDK}
