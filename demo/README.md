# Streamlit demo (Phase 4)

Local UI for searching the lake and browsing CloudFront thumbnails.

```bash
export SEARCH_API_URL="$(terraform -chdir=terraform output -raw search_api_endpoint)"
export COGNITO_CLIENT_ID="$(terraform -chdir=terraform output -raw cognito_client_id)"
export AWS_REGION=us-east-1

pip install -r demo/requirements.txt
streamlit run demo/app.py
```

Create a Cognito user first — see `terraform/modules/access/README.md`.
