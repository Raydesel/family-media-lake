# enrichment module (Phase 2)

DynamoDB catalog stream → enrichment Lambda → processed bucket → Glue/Athena.

- **Lambda** (`family-media-enrichment`, py3.11/arm64): EXIF via Pillow,
  Rekognition DetectLabels + DetectFaces, JPEG thumbnail, one-row Parquet
  file per media item written with pyarrow.
- **Packaging**: `scripts/build_lambda_package.sh` pip-installs aarch64
  wheels, `archive_file` zips them, and the zip is staged in the artifacts
  bucket (Pillow + pyarrow exceed the direct-upload limit).
- **Glue**: database + table with **partition projection** (year/month/day) —
  no crawler, no MSCK REPAIR, zero standing cost.
- **Athena**: workgroup with results in `processed/athena-results/` (7-day
  expiry) and a per-query bytes-scanned cutoff as a cost guard.

If the local build directory is wiped without source changes:

    terraform apply -replace=module.enrichment.null_resource.build
