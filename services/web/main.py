"""web: DevToolkit's web UI. Renders the pages and calls converter-api server-side."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from api_client import ApiClient, ApiUnavailableError

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def resolve_log_level(value: str | None) -> tuple[str, str | None]:
    """Return (level, warning). An unknown LOG_LEVEL falls back to INFO instead of crashing."""
    level = (value or "info").strip().upper()
    if level in LOG_LEVELS:
        return level, None
    allowed = ", ".join(name.lower() for name in LOG_LEVELS)
    return "INFO", f"Unknown LOG_LEVEL {value!r}; using 'info'. Allowed: {allowed}"


logger = logging.getLogger("web")


def configure_logging(value: str | None) -> str:
    """Set up logging from a LOG_LEVEL value and return the level used."""
    level, warning = resolve_log_level(value)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if warning:
        logger.warning(warning)
    return level


LOG_LEVEL = configure_logging(os.getenv("LOG_LEVEL"))

BASE_DIR = Path(__file__).parent
# Same limits as converter-api, checked here first so users get a clear message.
MAX_INPUT_CHARS = 100_000
MAX_TEMPLATE_CHARS = 500_000
# Room for the form fields and multipart framing around the largest input.
MAX_BODY_BYTES = 4 * MAX_TEMPLATE_CHARS + 16_384
NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
STACK_NAME_RE = re.compile(r"^[A-Za-z][-A-Za-z0-9]{0,127}$")
OUTPUTS = ("manifests", "helm")
PROVIDERS = ("aws", "awscc")
LAYOUTS = ("files", "single")
UNAVAILABLE = "The converter is unavailable right now. Please try again in a minute."

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

# Applies a saved theme choice before first paint, so the page never flashes the wrong theme.
# It must be inline (a deferred script is too late), so the CSP allows exactly this script by
# hash. Change the text here and the hash follows.
THEME_SCRIPT = (
    'try{var t=localStorage.getItem("theme");'
    'if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}'
    "catch(e){}"
)
THEME_SCRIPT_HASH = base64.b64encode(hashlib.sha256(THEME_SCRIPT.encode()).digest()).decode()

# Scripts: only our own files plus the theme script by hash. Styles: the code editor positions
# its cursor and layers with inline style attributes and injects its own stylesheet, which a
# nonce cannot cover, so inline styles are allowed; every piece of user content is escaped.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        f"default-src 'self'; script-src 'self' 'sha256-{THEME_SCRIPT_HASH}'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
# Static URLs carry a content hash, so files can be cached for a year and a new build still
# shows up at once. HTML must never be cached: its URL stays the same while its content changes.
STATIC_CACHE = "public, max-age=31536000, immutable"
HTML_CACHE = "public, max-age=0, must-revalidate"

_static_versions: dict[str, str] = {}


def static_url(name: str) -> str:
    """URL for a file in static/, versioned by its content. A missing file (e.g. app.css before
    `make css`) gets version 0 rather than breaking the page."""
    if name not in _static_versions:
        try:
            digest = hashlib.sha256((BASE_DIR / "static" / name).read_bytes()).hexdigest()
            _static_versions[name] = digest[:8]
        except OSError:
            _static_versions[name] = "0"
    return f"/static/{name}?v={_static_versions[name]}"


templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["theme_script"] = THEME_SCRIPT
templates.env.globals["static_url"] = static_url


class InputError(Exception):
    """The user's input can't be sent to converter-api. The message is shown on the page."""


async def read_input(
    text: str,
    upload: UploadFile | None,
    limit: int = MAX_INPUT_CHARS,
    empty: str = "Paste some YAML or choose a file to upload.",
) -> str:
    """Return the uploaded file if one was chosen, otherwise the pasted text."""
    if upload is not None and upload.filename:
        raw = await upload.read(limit * 4 + 1)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InputError("The uploaded file isn't UTF-8 text.") from exc
    if not text.strip():
        raise InputError(empty)
    if len(text) > limit:
        raise InputError(f"The input is longer than {limit:,} characters.")
    return text


def notes_markdown(notes: list[str], source: str) -> str:
    """The conversion notes as a file for the zip, so they travel with the code."""
    lines = [
        "# Conversion notes",
        "",
        f"Converted from {SOURCE_LABELS.get(source, source)} by DevToolkit. Everything not "
        "listed here was converted; these need a decision.",
        "",
    ]
    lines += [f"- {note}" for note in notes] or ["- Nothing: every resource was converted."]
    return "\n".join(lines) + "\n"


SOURCE_LABELS = {
    "cloudformation": "CloudFormation",
    "sam": "AWS SAM",
    "cdk": "AWS CDK (synthesized)",
}


def wants_fragment(request: Request) -> bool:
    """The page's script posts forms itself and asks for just the result markup."""
    return request.headers.get("x-requested-with") == "fetch"


def create_app(api: ApiClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.api = api or ApiClient(
            os.getenv("CONVERTER_API_URL", "http://localhost:8000"),
            auth=os.getenv("CONVERTER_API_AUTH", "none"),
        )
        yield
        await app.state.api.aclose()

    app = FastAPI(
        title="DevToolkit web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.middleware("http")
    async def limits_and_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "POST":
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                return PlainTextResponse("Content-Length required", status_code=411)
            if int(length) > MAX_BODY_BYTES:
                return PlainTextResponse("Request too large", status_code=413)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = STATIC_CACHE
        elif response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = HTML_CACHE
        return response

    def page(
        request: Request, name: str, context: dict[str, Any], status_code: int = 200
    ) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def tool_page(
        request: Request, name: str, fragment: str, context: dict[str, Any], status_code: int = 200
    ) -> HTMLResponse:
        """The whole page for a normal post, or just the result markup for the page's script."""
        template = fragment if wants_fragment(request) else name
        return page(request, template, context, status_code)

    # Error pages with a way back, instead of FastAPI's JSON.
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        if exc.status_code == 404:
            message = "There is no page at this address."
        elif exc.status_code == 405:
            message = "That request method isn't allowed here."
        else:
            message = str(exc.detail)  # Starlette always sets it, at least to the status phrase
        return page(
            request, "error.html", {"status": exc.status_code, "message": message}, exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def form_error(request: Request, exc: RequestValidationError) -> HTMLResponse:
        return page(
            request,
            "error.html",
            {"status": 400, "message": "The form wasn't filled in correctly."},
            400,
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness probe: the process is up."""
        return {"status": "healthy"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        """Readiness probe. Doesn't check converter-api, so one slow service can't take the
        other out of rotation; pages show an error instead."""
        return {"status": "ready"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return page(request, "index.html", {})

    # --- Compose -> Kubernetes ------------------------------------------------------------

    def compose_context(
        compose_yaml: str, namespace: str, include_service: bool, output: str
    ) -> dict[str, Any]:
        return {
            "compose_yaml": compose_yaml,
            "namespace": namespace,
            "include_service": include_service,
            "output": output,
        }

    @app.get("/compose-to-k8s", response_class=HTMLResponse)
    def compose_form(request: Request, example: str | None = None) -> Response:
        if example:
            # The page's script asks for the text alone; a plain visit gets the filled form.
            if "text/plain" in request.headers.get("accept", ""):
                return PlainTextResponse(EXAMPLE_COMPOSE)
            return page(
                request,
                "compose_to_k8s.html",
                compose_context(EXAMPLE_COMPOSE, "shop", True, "manifests"),
            )
        return page(
            request, "compose_to_k8s.html", compose_context("", "default", True, "manifests")
        )

    async def convert(
        request: Request,
        compose_yaml: str,
        compose_file: UploadFile | None,
        namespace: str,
        include_service: bool,
        output: str,
    ) -> tuple[dict[str, Any], int]:
        """Run a conversion. Returns (template context, HTTP status)."""
        context = compose_context(compose_yaml, namespace, include_service, output)
        try:
            context["compose_yaml"] = await read_input(compose_yaml, compose_file)
            if not NAMESPACE_RE.match(namespace):
                raise InputError(
                    "Namespace must be lowercase letters, digits and '-', at most 63 characters, "
                    "starting and ending with a letter or digit."
                )
            if output not in OUTPUTS:
                raise InputError("Output must be 'manifests' or 'helm'.")
        except InputError as exc:
            return context | {"error": str(exc)}, 400

        try:
            status, body = await request.app.state.api.post(
                "/convert/compose-to-k8s/files",
                {
                    "compose_yaml": context["compose_yaml"],
                    "namespace": namespace,
                    "include_service": include_service,
                    "output": output,
                },
            )
        except ApiUnavailableError as exc:
            logger.error("converter-api unavailable: %s", exc)
            return context | {"error": UNAVAILABLE}, 502

        if status == 200 and body.get("success") and isinstance(body.get("files"), list):
            return context | {"files": body["files"], "notes": body.get("notes", [])}, 200
        error = body.get("error") or "The converter couldn't process this input."
        return context | {
            "error": error,
            "error_line": body.get("line"),
            "error_column": body.get("column"),
        }, 400

    @app.post("/compose-to-k8s", response_class=HTMLResponse)
    async def compose_convert(
        request: Request,
        compose_yaml: str = Form(""),
        compose_file: UploadFile | None = None,
        namespace: str = Form("default"),
        include_service: bool = Form(False),
        output: str = Form("manifests"),
    ) -> HTMLResponse:
        context, status = await convert(
            request, compose_yaml, compose_file, namespace, include_service, output
        )
        return tool_page(request, "compose_to_k8s.html", "_result.html", context, status)

    @app.post("/compose-to-k8s/zip")
    async def compose_zip(
        request: Request,
        compose_yaml: str = Form(""),
        compose_file: UploadFile | None = None,
        namespace: str = Form("default"),
        include_service: bool = Form(False),
        output: str = Form("manifests"),
    ) -> Response:
        """The same conversion, downloaded as a zip of the files."""
        context, status = await convert(
            request, compose_yaml, compose_file, namespace, include_service, output
        )
        if status != 200:
            return page(request, "compose_to_k8s.html", context, status)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in context["files"]:
                archive.writestr(file["path"], file["content"])
        buffer.seek(0)
        name = "helm-chart.zip" if output == "helm" else "kubernetes-manifests.zip"
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # --- CloudFormation -> Terraform -------------------------------------------------------

    def cfn_context(
        template: str, provider: str, layout: str, stack_name: str, include_imports: bool
    ) -> dict[str, Any]:
        return {
            "template": template,
            "provider": provider,
            "layout": layout,
            "stack_name": stack_name,
            "include_imports": include_imports,
            "source_labels": SOURCE_LABELS,
        }

    @app.get("/cfn-to-terraform", response_class=HTMLResponse)
    def cfn_form(request: Request, example: str | None = None) -> Response:
        if example:
            text = EXAMPLES.get(example, EXAMPLE_CLOUDFORMATION)
            if "text/plain" in request.headers.get("accept", ""):
                return PlainTextResponse(text)
            return page(
                request,
                "cfn_to_terraform.html",
                cfn_context(text, "aws", "files", "", False),
            )
        return page(request, "cfn_to_terraform.html", cfn_context("", "aws", "files", "", False))

    async def convert_cfn(
        request: Request,
        template: str,
        template_file: UploadFile | None,
        provider: str,
        layout: str,
        stack_name: str,
        include_imports: bool,
    ) -> tuple[dict[str, Any], int]:
        """Run a CloudFormation conversion. Returns (template context, HTTP status)."""
        stack_name = stack_name.strip()
        context = cfn_context(template, provider, layout, stack_name, include_imports)
        try:
            context["template"] = await read_input(
                template,
                template_file,
                MAX_TEMPLATE_CHARS,
                "Paste a template or choose a file to upload.",
            )
            if stack_name and not STACK_NAME_RE.match(stack_name):
                raise InputError(
                    "Stack name must start with a letter and contain only letters, digits and "
                    "'-', at most 128 characters."
                )
            if provider not in PROVIDERS:
                raise InputError("Provider must be 'aws' or 'awscc'.")
            if layout not in LAYOUTS:
                raise InputError("Layout must be 'files' or 'single'.")
        except InputError as exc:
            return context | {"error": str(exc)}, 400

        payload: dict[str, Any] = {
            "template": context["template"],
            "provider": provider,
            "layout": layout,
            "include_imports": include_imports,
        }
        if stack_name:
            payload["stack_name"] = stack_name
        try:
            status, body = await request.app.state.api.post("/convert/cfn-to-terraform", payload)
        except ApiUnavailableError as exc:
            logger.error("converter-api unavailable: %s", exc)
            return context | {"error": UNAVAILABLE}, 502

        if status == 200 and body.get("success") and isinstance(body.get("files"), list):
            return context | {
                "files": body["files"],
                "notes": body.get("notes", []),
                "resources": body.get("resources", []),
                "summary": body.get("summary", {}),
                "source": body.get("source", "cloudformation"),
            }, 200
        error = body.get("error") or "The converter couldn't process this input."
        return context | {
            "error": error,
            "error_line": body.get("line"),
            "error_column": body.get("column"),
        }, 400

    @app.post("/cfn-to-terraform", response_class=HTMLResponse)
    async def cfn_convert(
        request: Request,
        template: str = Form(""),
        template_file: UploadFile | None = None,
        provider: str = Form("aws"),
        layout: str = Form("files"),
        stack_name: str = Form(""),
        include_imports: bool = Form(False),
    ) -> HTMLResponse:
        context, status = await convert_cfn(
            request, template, template_file, provider, layout, stack_name, include_imports
        )
        return tool_page(
            request, "cfn_to_terraform.html", "_terraform_result.html", context, status
        )

    @app.post("/cfn-to-terraform/zip")
    async def cfn_zip(
        request: Request,
        template: str = Form(""),
        template_file: UploadFile | None = None,
        provider: str = Form("aws"),
        layout: str = Form("files"),
        stack_name: str = Form(""),
        include_imports: bool = Form(False),
    ) -> Response:
        """The same conversion, downloaded as a zip of the .tf files and the notes."""
        context, status = await convert_cfn(
            request, template, template_file, provider, layout, stack_name, include_imports
        )
        if status != 200:
            return page(request, "cfn_to_terraform.html", context, status)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in context["files"]:
                archive.writestr(file["path"], file["content"])
            archive.writestr(
                "CONVERSION_NOTES.md", notes_markdown(context["notes"], context["source"])
            )
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="terraform.zip"'},
        )

    # --- YAML check -----------------------------------------------------------------------

    @app.get("/validate-yaml", response_class=HTMLResponse)
    def validate_form(request: Request) -> HTMLResponse:
        return page(request, "validate_yaml.html", {"yaml_text": ""})

    @app.post("/validate-yaml", response_class=HTMLResponse)
    async def validate(
        request: Request,
        yaml_text: str = Form(""),
        yaml_file: UploadFile | None = None,
    ) -> HTMLResponse:
        context: dict[str, Any] = {"yaml_text": yaml_text}
        try:
            context["yaml_text"] = await read_input(yaml_text, yaml_file)
        except InputError as exc:
            return tool_page(
                request,
                "validate_yaml.html",
                "_validation.html",
                context | {"error": str(exc)},
                400,
            )

        try:
            status, body = await request.app.state.api.post(
                "/validate/yaml", {"yaml": context["yaml_text"]}
            )
        except ApiUnavailableError as exc:
            logger.error("converter-api unavailable: %s", exc)
            return tool_page(
                request,
                "validate_yaml.html",
                "_validation.html",
                context | {"error": UNAVAILABLE},
                502,
            )

        if status != 200 or "valid" not in body:
            return tool_page(
                request,
                "validate_yaml.html",
                "_validation.html",
                context | {"error": "The validator couldn't process this input."},
                400,
            )
        return tool_page(
            request, "validate_yaml.html", "_validation.html", context | {"validation": body}
        )

    return app


app = create_app()
