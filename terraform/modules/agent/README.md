# agent module (Phase 3)

Nightly EventBridge cron → Step Functions (`nightly_agent`) → `album_agent`
Lambda (two actions) → SNS approval email.

- **cluster_faces**: IndexFaces new enriched photos into the Rekognition
  collection, group via SearchFaces (threshold 90), persist face→cluster in
  the `family-media-faces` table and `face_cluster_ids` on catalog items.
- **propose_albums**: compact unalbumed catalog items into metadata JSON,
  ask Claude (Bedrock Converse) for album proposals, then write
  `albums/<album_id>/manifest.json` (status=proposed), an
  `album_assignments/<run_id>.parquet` row set (Glue: `album_assignments`),
  and append `album_ids` on catalog items.
- The workflow emails the proposal summary to the approval SNS topic.

## Before the first run

1. Grant **Bedrock model access** for the configured model in the AWS console
   (account-level; not Terraformable).
2. If `approval_email` is set, **confirm the SNS subscription** email.

## Manual run

    aws stepfunctions start-execution \
      --state-machine-arn "$(terraform -chdir=terraform output -raw nightly_agent_state_machine_arn)"

## Cost levers

- `bedrock_model_id` (default Claude 3.5 Haiku ≈ cents/run; Sonnet-class is ~10x)
- `schedule_enabled` / `schedule_expression`
- IndexFaces+SearchFaces ≈ $2 per 1,000 new face photos (incremental only)
