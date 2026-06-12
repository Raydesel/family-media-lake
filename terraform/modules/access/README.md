# access module (Phase 4)

Family-facing access layer:

- **Cognito** user pool (`admin_create_user_only`) + public app client
  (`USER_PASSWORD_AUTH` for the Streamlit demo).
- **HTTP API** (API Gateway v2) with JWT authorizer on `GET /search`;
  `GET /health` is open for liveness checks.
- **search_api Lambda** runs partition-pruned Athena queries and returns
  JSON with CloudFront thumbnail URLs.
- **CloudFront** (PriceClass_100, default `*.cloudfront.net` cert) serves
  `thumbnails/*` from the processed bucket via OAC.

## After apply

Create a family user (replace email):

```bash
POOL="$(terraform -chdir=terraform output -raw cognito_user_pool_id)"
aws cognito-idp admin-create-user \
  --user-pool-id "$POOL" \
  --username "family@example.com" \
  --user-attributes Name=email,Value=family@example.com Name=email_verified,Value=true \
  --temporary-password 'ChangeMeNow1!' \
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password \
  --user-pool-id "$POOL" \
  --username "family@example.com" \
  --password 'YourSecurePass1!' \
  --permanent
```

Run the Streamlit demo (see `demo/README.md`).
