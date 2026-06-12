# ---------------------------------------------------------------------------
# Access module (Phase 4).
#
#   Cognito user pool (admin-created family accounts)
#     -> HTTP API (JWT authorizer) -> search_api Lambda -> Athena
#   CloudFront (OAC) -> processed bucket /thumbnails/*
#
# Cost posture: PriceClass_100 CloudFront, HTTP API (not REST), no API access
# logs, no custom domain (avoids Route53 + ACM recurring cost).
# ---------------------------------------------------------------------------

locals {
  function_name = "${var.project_name}-search-api"
  source_dir    = "${path.module}/../../../lambdas/search_api"
  # AWS managed cache policy: CachingOptimized
  cloudfront_cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
}

data "aws_s3_bucket" "processed" {
  bucket = var.processed_bucket_name
}

# ===========================================================================
# COGNITO
# ===========================================================================

resource "aws_cognito_user_pool" "family" {
  name = "${var.project_name}-family"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Family accounts are created by an admin, not open registration.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 10
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
    require_uppercase = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = var.tags
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.project_name}-web"
  user_pool_id = aws_cognito_user_pool.family.id

  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

# ===========================================================================
# CLOUDFRONT (thumbnails only)
# ===========================================================================

resource "aws_cloudfront_origin_access_control" "thumbnails" {
  name                              = "${var.project_name}-thumbnails-oac"
  description                       = "OAC for processed-bucket thumbnails"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "thumbnails" {
  enabled         = true
  comment         = "${var.project_name} thumbnails"
  is_ipv6_enabled = true
  price_class     = var.cloudfront_price_class
  tags            = var.tags

  origin {
    domain_name              = data.aws_s3_bucket.processed.bucket_regional_domain_name
    origin_id                = "processed-thumbnails"
    origin_access_control_id = aws_cloudfront_origin_access_control.thumbnails.id
  }

  # Requests outside thumbnails/* still reach S3 but the bucket policy only
  # grants CloudFront GetObject on thumbnails/* — everything else is denied.
  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "processed-thumbnails"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true
    cache_policy_id        = local.cloudfront_cache_policy_id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

data "aws_iam_policy_document" "cloudfront_thumbnails" {
  statement {
    sid    = "AllowCloudFrontReadThumbnails"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    actions   = ["s3:GetObject"]
    resources = ["${var.processed_bucket_arn}/thumbnails/*"]
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.thumbnails.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "processed_thumbnails" {
  bucket = var.processed_bucket_name
  policy = data.aws_iam_policy_document.cloudfront_thumbnails.json
}

# ===========================================================================
# SEARCH API LAMBDA
# ===========================================================================

data "archive_file" "search_api" {
  type        = "zip"
  source_dir  = local.source_dir
  output_path = "${path.module}/build/search_api.zip"
  excludes    = ["__pycache__", "requirements.txt"]
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "search_api" {
  name               = "${local.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "search_api" {
  statement {
    sid = "AthenaQuery"
    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:StopQueryExecution",
      "athena:GetWorkGroup",
    ]
    resources = [
      "arn:aws:athena:${var.aws_region}:${var.account_id}:workgroup/${var.athena_workgroup_name}",
    ]
  }

  statement {
    sid = "GlueReadCatalog"
    actions = [
      "glue:GetDatabase",
      "glue:GetTable",
      "glue:GetPartitions",
    ]
    resources = [
      "arn:aws:glue:${var.aws_region}:${var.account_id}:catalog",
      "arn:aws:glue:${var.aws_region}:${var.account_id}:database/${var.glue_database_name}",
      "arn:aws:glue:${var.aws_region}:${var.account_id}:table/${var.glue_database_name}/*",
    ]
  }

  # Athena reads parquet metadata and writes scratch results with the
  # Lambda execution role when no workgroup role is configured.
  statement {
    sid = "S3ForAthena"
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [
      var.processed_bucket_arn,
      "${var.processed_bucket_arn}/athena-results/*",
      "${var.processed_bucket_arn}/metadata/*",
    ]
  }
}

resource "aws_iam_role_policy" "search_api" {
  name   = "${local.function_name}-policy"
  role   = aws_iam_role.search_api.id
  policy = data.aws_iam_policy_document.search_api.json
}

resource "aws_cloudwatch_log_group" "search_api" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "lambda_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.search_api.arn}:*"]
  }
}

resource "aws_iam_role_policy" "lambda_logs" {
  name   = "${local.function_name}-logs"
  role   = aws_iam_role.search_api.id
  policy = data.aws_iam_policy_document.lambda_logs.json
}

resource "aws_lambda_function" "search_api" {
  function_name    = local.function_name
  role             = aws_iam_role.search_api.arn
  handler          = "search_handler.handler"
  runtime          = "python3.11"
  architectures    = ["arm64"]
  timeout          = 30
  memory_size      = 256
  filename         = data.archive_file.search_api.output_path
  source_code_hash = data.archive_file.search_api.output_base64sha256

  environment {
    variables = {
      GLUE_DATABASE     = var.glue_database_name
      GLUE_TABLE        = var.glue_table_name
      ATHENA_WORKGROUP  = var.athena_workgroup_name
      CLOUDFRONT_DOMAIN = aws_cloudfront_distribution.thumbnails.domain_name
      QUERY_TIMEOUT_SEC = "25"
      DEFAULT_LIMIT     = "50"
      MAX_LIMIT         = "100"
      LOG_LEVEL         = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.search_api]
  tags       = var.tags
}

# ===========================================================================
# HTTP API (API Gateway v2)
# ===========================================================================

resource "aws_apigatewayv2_api" "search" {
  name          = "${var.project_name}-search-api"
  protocol_type = "HTTP"
  tags          = var.tags

  cors_configuration {
    allow_origins = var.cors_allowed_origins
    allow_methods = ["GET", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.search.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web.id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.family.id}"
  }
}

resource "aws_apigatewayv2_integration" "search_lambda" {
  api_id                 = aws_apigatewayv2_api.search.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.search_api.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.search.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.search_lambda.id}"
}

resource "aws_apigatewayv2_route" "search" {
  api_id             = aws_apigatewayv2_api.search.id
  route_key          = "GET /search"
  target             = "integrations/${aws_apigatewayv2_integration.search_lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.search.id
  name        = "$default"
  auto_deploy = true
  tags        = var.tags

  # No access logs: saves ~$0.50/GB ingested; Lambda logs cover debugging.
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.search_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.search.execution_arn}/*/*"
}
