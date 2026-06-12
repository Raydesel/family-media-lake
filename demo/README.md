# Streamlit demo (Phase 4)

Instagram-style family browser: photos load automatically (20 per page), year
filter buttons, optional tag search, and one-tap original downloads.

```bash
# Option A: environment variables
export SEARCH_API_URL="$(terraform -chdir=terraform output -raw search_api_endpoint)"
export COGNITO_CLIENT_ID="$(terraform -chdir=terraform output -raw cognito_client_id)"
export AWS_REGION=us-east-1

# Option B: copy demo/.streamlit/secrets.toml.example → secrets.toml (gitignored)

pip install -r demo/requirements.txt
streamlit run demo/app.py
```

**Streamlit Community Cloud:** paste the same keys into the app Secrets UI.
Never commit `secrets.toml`, API URLs, or AWS keys.

### Seeing logs / debugging

Streamlit prints logs in the **same terminal** where you run the command (not in the browser).

```bash
# Verbose Streamlit + app HTTP traces
export STREAMLIT_DEBUG=1
streamlit run demo/app.py --logger.level=debug 2>&1 | tee streamlit.log
```

Then reproduce the issue and read `streamlit.log` or scroll the terminal.

**Backend (API / Athena)** — CloudWatch log group `/aws/lambda/family-media-search-api`.

**Quick API test** (bypasses Streamlit):

```bash
API="$(terraform -chdir=terraform output -raw search_api_endpoint)"
CLIENT="$(terraform -chdir=terraform output -raw cognito_client_id)"
TOKEN="$(aws cognito-idp initiate-auth --client-id "$CLIENT" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=you@example.com,PASSWORD='...' \
  --query 'AuthenticationResult.IdToken' --output text)"
curl -s -H "Authorization: Bearer $TOKEN" "$API/search?year=all&page=1" | jq .
```

**After upgrading the API** (pagination + `/download`), redeploy Terraform:

```bash
cd terraform && terraform apply
```

Create a Cognito user first — see `terraform/modules/access/README.md`.
