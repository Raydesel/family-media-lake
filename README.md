# Family Media Data Lake

A self-hosted alternative to Google Photos, built on AWS as a **Data Engineering
portfolio project**. Originals land in S3, get enriched with Rekognition + EXIF
(or ffmpeg/ffprobe for videos), indexed in a Glue/Athena lakehouse, organized
into albums by a Claude-powered agent (via Amazon Bedrock), and served through
CloudFront + a search API.

**Stack:** Python 3.11 · Terraform · S3 · Lambda · DynamoDB · Rekognition · Glue ·
Athena · Step Functions · API Gateway · CloudFront · Cognito · Bedrock · SNS

All infrastructure is Terraform — no ClickOps, no CDK/SAM. Four phases, one
`terraform apply` from a clean AWS account (after bootstrap).

## Architecture

Four phases: ingestion → enrichment → nightly AI agent → family access.

![End-to-end architecture](docs/screenshots/01-architecture.png)

## Build phases

| Phase | Scope | Status |
|------:|-------|--------|
| 1 | S3 lake, DynamoDB catalog, upload trigger, pre-signed URLs | **done** |
| 2 | Rekognition + EXIF enrichment, Parquet, Glue/Athena | **done** |
| 3 | Step Functions, face clustering, Claude album agent, SNS | **done** |
| 4 | CloudFront, API Gateway search, Cognito, Streamlit demo | **done** |

## Repository layout

```
terraform/            Root stack + reusable modules
  bootstrap/          Remote-state bucket + lock table (run FIRST, local state)
  modules/storage/    S3 raw + processed + artifacts buckets, DynamoDB catalog
  modules/ingestion/  upload_trigger Lambda + S3 notification
  modules/enrichment/ enrichment Lambda + Glue table + Athena workgroup
  modules/agent/      nightly Step Functions + album_agent Lambda + SNS
  modules/access/     CloudFront + Cognito + HTTP API + search Lambda
demo/                 Optional Streamlit search UI
lambdas/              One folder per Lambda, each self-contained
scripts/              Pre-signed URL generator, bulk upload, Lambda package builder
step_functions/       nightly_agent.asl.json (templated by Terraform)
tests/                pytest unit tests for Lambda + script logic
```

## Prerequisites

- Terraform >= 1.5
- AWS CLI configured with credentials (`aws sts get-caller-identity` works)
- Python 3.11 (Lambda runtime). Local 3.8+ is fine for the helper scripts/tests.
- Network access at `terraform apply` time: the enrichment and album_agent
  Lambda packages are built locally by `scripts/build_lambda_package.sh`,
  which pip-installs linux/arm64 Python 3.11 wheels (Pillow, pyarrow)
  regardless of your local Python.
- **Bedrock model access** for the configured Claude model must be granted
  once in the AWS console (account-level; not Terraformable) before the
  nightly agent's first run.

## Deploy (Phase 1)

### 1. Bootstrap the remote state backend (one time)

```bash
cd terraform/bootstrap
terraform init
terraform apply        # creates the tfstate bucket + lock table
terraform output       # note state_bucket_name + lock_table_name
```

### 2. Point the root stack at that backend

Keep `terraform/backend.tf` as-is (placeholders). For your account, copy
`terraform/backend.hcl.example` → `terraform/backend.hcl` (gitignored), fill in
your account id from bootstrap outputs, then init with that file (see below).
Do **not** commit `backend.hcl` or `terraform.tfvars`.

### 3. Deploy the lake

```bash
cd terraform
cp backend.hcl.example backend.hcl   # once; edit account id — file is gitignored
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

Outputs include the raw/processed bucket names, catalog table name, and the
upload-trigger function name.

## Upload a file

```bash
export RAW_BUCKET="$(terraform -chdir=terraform output -raw raw_bucket_name)"

# Generate a URL and upload in one step:
python scripts/generate_upload_url.py path/to/photo.jpg --uploader family --upload
```

The S3 event fires `upload_trigger`, which writes a catalog item to DynamoDB.
Check it:

```bash
aws dynamodb scan --table-name "$(terraform -chdir=terraform output -raw catalog_table_name)" --max-items 5
```

### Bulk import

Tag batches with `--uploader` (e.g. `family`, `sesion_xv`) for later API
filtering (`GET /search?uploader=sesion_xv`):

```bash
RAW_BUCKET="$(terraform -chdir=terraform output -raw raw_bucket_name)" \
  ./scripts/bulk_upload.sh
```

## Enrichment pipeline (Phase 2)

The catalog INSERT flows through the DynamoDB stream into the `enrichment`
Lambda, which:

1. **Photos:** extracts EXIF (capture timestamp + GPS) with Pillow, calls
   Rekognition `DetectLabels` + `DetectFaces`, writes a 512px JPEG thumbnail.
2. **Videos:** uses ffprobe for container `creation_time` → `capture_ts` and
   ffmpeg for a poster-frame thumbnail (Rekognition on full video is deferred).
3. writes a one-row Parquet file to `processed/metadata/year=.../...parquet`,
4. flips the catalog item to `status=enriched` with denormalized highlights.

The Glue table `family_media.media_metadata` sits over the metadata prefix
using **partition projection** — there is no crawler to run or pay for;
new partitions are queryable immediately.

### Query with Athena

```bash
WG="$(terraform -chdir=terraform output -raw athena_workgroup_name)"

aws athena start-query-execution \
  --work-group "$WG" \
  --query-execution-context Database=family_media \
  --query-string "
    SELECT file_id, original_filename, capture_ts, rekognition_labels
    FROM media_metadata
    WHERE year = 2026 AND month = 6
      AND contains(rekognition_labels, 'Beach')
    LIMIT 50;"
```

Two ready-made named queries (`photos-by-label`, `face-stats-by-day`) are
provisioned in the workgroup. **Always filter on `year`/`month`/`day`** —
Athena bills per byte scanned, and the workgroup enforces a 1 GiB per-query
cutoff as a backstop.

### Known limitations (deliberate deferrals)

- HEIC/HEIF: Pillow can't decode them without `pillow-heif`, and Rekognition
  doesn't accept them — such files are cataloged with null EXIF/labels.
- `location_name` stays null until a reverse-geocoding source is wired up
  (Amazon Location / Nominatim — external cost/ToS decision).
- The `face_id`s in the metadata parquet are per-photo placeholders; stable
  person identities live in the Phase 3 faces table (`cluster_id`).

## AI agent (Phase 3)

Every night (08:00 UTC by default), EventBridge starts the
`family-media-nightly-agent` Step Functions workflow:

1. **ClusterFaces** — `album_agent {action: cluster_faces}` indexes new
   enriched photos into a Rekognition face collection and groups faces into
   person clusters (SearchFaces, threshold 90). Mapping persisted in the
   `family-media-faces` table; `face_cluster_ids` denormalized onto the
   catalog.
2. **ProposeAlbums** — `album_agent {action: propose_albums}` compacts
   unalbumed items (date, labels, people clusters, GPS) into JSON and asks
   Claude (Bedrock Converse API) for album proposals. Hallucinated file ids
   and undersized albums are dropped. Valid albums are written as
   `albums/<album_id>/manifest.json` (status=proposed), appended to the
   `family_media.album_assignments` Glue table (join on `file_id`), and
   `album_ids` updated on catalog items.
3. **NotifyForApproval** — the proposal summary is published to the SNS
   topic (set `approval_email` in Terraform and confirm the subscription).

Run it manually any time:

```bash
aws stepfunctions start-execution \
  --state-machine-arn "$(terraform -chdir=terraform output -raw nightly_agent_state_machine_arn)"
```

Query album contents via Athena:

```sql
SELECT a.album_name, m.original_filename, m.capture_ts
FROM album_assignments a
JOIN media_metadata m ON m.file_id = a.file_id
WHERE m.year = 2026 AND m.month = 6;
```

Tuning knobs (Terraform variables): `bedrock_model_id`, `approval_email`,
`agent_schedule_expression`, `agent_schedule_enabled`.

## Family access (Phase 4)

- **CloudFront** (`PriceClass_100`) serves `thumbnails/*` from the processed
  bucket via Origin Access Control. Metadata and originals are not exposed.
- **Cognito** user pool with admin-only account creation (no public sign-up).
- **HTTP API** (API Gateway v2) with a JWT authorizer on `GET /search`;
  `GET /health` is open for liveness checks.
- **search_api Lambda** browses the **DynamoDB catalog** by default
  (`BROWSE_BACKEND=dynamodb`) for low-latency family-scale search. Optional
  Athena over the Glue table is available via `BROWSE_BACKEND=athena`. Returns
  JSON with CloudFront thumbnail URLs and presigned `download_url` for originals.

### Create a family user

```bash
POOL="$(terraform -chdir=terraform output -raw cognito_user_pool_id)"
aws cognito-idp admin-create-user \
  --user-pool-id "$POOL" \
  --username "you@example.com" \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --temporary-password 'ChangeMeNow1!' \
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password \
  --user-pool-id "$POOL" \
  --username "you@example.com" \
  --password 'YourSecurePass1!' \
  --permanent
```

### Search via API

```bash
API="$(terraform -chdir=terraform output -raw search_api_endpoint)"
CLIENT="$(terraform -chdir=terraform output -raw cognito_client_id)"
TOKEN="$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=you@example.com,PASSWORD='YourSecurePass1!' \
  --query 'AuthenticationResult.IdToken' --output text)"

curl -s -H "Authorization: Bearer $TOKEN" \
  "$API/search?year=2026&month=6&label=Person&when=capture&media_type=photo" | jq .
```

Query parameters include `when` (upload | capture), `media_type` (photo | video),
`uploader`, `year`, `month`, `day`, `label` / `q`, and `page`.

### Streamlit demo (family-friendly browse UI)

Grid browse with persistent Cognito login (refresh token in browser
localStorage), filters for capture vs upload date, photos vs videos, optional
Rekognition tag search, full page-number pagination, and click-to-download on
thumbnails.

```bash
export SEARCH_API_URL="$(terraform -chdir=terraform output -raw search_api_endpoint)"
export COGNITO_CLIENT_ID="$(terraform -chdir=terraform output -raw cognito_client_id)"
pip install -r demo/requirements.txt
streamlit run demo/app.py
```

API routes: `GET /search?page=1&year=all`, `GET /years`, `GET /download?file_id=…`

See `demo/README.md` for details.

## Tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

## Conventions

- Each Lambda owns its `requirements.txt`; Terraform `archive_file` zips it.
- Handlers: validate event → log → business logic → return response.
- Structured JSON logs: `{"level","function","message", ...}`.
- All names/ARNs/model-ids come from Lambda env vars set by Terraform.
- Parquet via `pyarrow` (Phase 2), never pandas, to keep packages lean.
- Every module outputs its ARNs + names for clean cross-module references.

## Cost notes

Designed to be near-zero at low usage:

- **S3 Intelligent-Tiering** on both buckets caps storage cost as the library
  grows (no retrieval fees between frequent/infrequent tiers). Raw originals
  additionally roll into IT Archive/Deep-Archive tiers after 90/180 days —
  those require an async restore before download (thumbnails stay in the
  processed bucket precisely so they remain instant).
- **DynamoDB** is on-demand (PAY_PER_REQUEST): no idle cost. PITR + streams add
  only negligible cost for a small catalog.
- **Lambda** stays within the free tier at family-scale volume; arm64 + 256 MB.
- **CloudWatch Logs** retention is capped (14 days default) to bound log spend.

Phase 2 specifics:

- **Rekognition is per-call**: DetectLabels + DetectFaces ≈ **$2 per 1,000
  photos**. Negligible at family upload rates, but bulk-importing a large
  archive (e.g. 20k photos ≈ $40) is a conscious decision — throttle or
  disable the stream trigger first if you don't want that.
- **Athena bills per byte scanned**: the workgroup enforces a 1 GiB/query
  cutoff, results expire from S3 after 7 days, and the table uses partition
  projection so there's no crawler cost at all.
- **Glue catalog**: first 1M objects/requests are free — effectively $0 here.

Phase 3 specifics:

- **Bedrock (Claude)** is per-token. The default Claude 3.5 Haiku with a
  capped payload (≤300 items/run) costs ~$0.02/night ≈ **$0.60/month**.
  Swapping `bedrock_model_id` to a Sonnet-class model is roughly 10x.
  Runs with fewer than 5 unalbumed items skip the Bedrock call entirely.
- **Rekognition IndexFaces/SearchFaces** ≈ $2 per 1,000 new face photos —
  each photo is indexed exactly once (`faces_indexed` flag).
- **Step Functions / EventBridge / SNS**: one standard-workflow run per night
  is fractions of a cent per month.
- Set `agent_schedule_enabled = false` to keep everything deployed but only
  run the agent manually.

Phase 4 specifics:

- **CloudFront**: first 1 TB/month egress free, then ~$0.085/GB — family
  thumbnail browsing is effectively $0. No custom domain (avoids Route53/ACM).
- **API Gateway HTTP API**: ~$1 per million requests — negligible.
- **Cognito**: first 50k MAU free; a handful of family accounts is $0.
- Default **search** reads DynamoDB (no Athena charge per page view). Set
  `BROWSE_BACKEND=athena` only if you need SQL-scale browse; always pass
  `year` (and `month`/`day` when you can) to keep scans small.
