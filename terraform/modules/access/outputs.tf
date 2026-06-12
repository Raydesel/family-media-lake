output "cloudfront_domain_name" {
  description = "CloudFront domain for thumbnail URLs."
  value       = aws_cloudfront_distribution.thumbnails.domain_name
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution id."
  value       = aws_cloudfront_distribution.thumbnails.id
}

output "api_endpoint" {
  description = "Invoke URL for the search HTTP API."
  value       = aws_apigatewayv2_api.search.api_endpoint
}

output "search_function_name" {
  description = "Name of the search_api Lambda."
  value       = aws_lambda_function.search_api.function_name
}

output "cognito_user_pool_id" {
  description = "Cognito user pool id for family login."
  value       = aws_cognito_user_pool.family.id
}

output "cognito_client_id" {
  description = "Cognito app client id (public, no secret)."
  value       = aws_cognito_user_pool_client.web.id
}

output "cognito_issuer" {
  description = "JWT issuer URL for the Cognito user pool."
  value       = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.family.id}"
}
