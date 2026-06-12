#!/usr/bin/env python3
"""Generate a pre-signed S3 PUT URL for uploading a media file into the lake.

The object key follows the data-lake layout the upload_trigger Lambda expects:

    raw/year=YYYY/month=MM/day=DD/<uuid>_<original_filename>

The uploader name is attached as S3 object metadata (x-amz-meta-uploader) so
the Lambda can record who uploaded each file. Because the metadata is part of
the signed request, the eventual PUT MUST send the same header -- the printed
curl command already includes it.

Examples
--------
    # Just print a URL + ready-to-run curl command:
    python scripts/generate_upload_url.py photo.jpg --uploader ariel

    # Resolve the bucket automatically from terraform outputs:
    python scripts/generate_upload_url.py photo.jpg --uploader ariel \
        --bucket "$(terraform -chdir=terraform output -raw raw_bucket_name)"

    # Generate AND upload in one shot:
    python scripts/generate_upload_url.py photo.jpg --uploader ariel --upload
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config


def build_key(original_filename: str, when: datetime, file_id: str) -> str:
    """Construct the partitioned raw object key."""
    safe_name = Path(original_filename).name.replace(" ", "_")
    return (
        f"raw/year={when:%Y}/month={when:%m}/day={when:%d}/"
        f"{file_id}_{safe_name}"
    )


def generate_presigned_put(
    bucket: str,
    key: str,
    uploader: str,
    content_type: str,
    region: str,
    expires: int,
) -> str:
    s3 = boto3.client(
        "s3",
        region_name=region,
        # SigV4 + virtual-addressing keeps the URL valid in all regions.
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    return s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": content_type,
            "Metadata": {"uploader": uploader},
        },
        ExpiresIn=expires,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", help="Path to the local media file to upload.")
    parser.add_argument("--uploader", required=True, help="Name of the person uploading (stored as metadata).")
    parser.add_argument(
        "--bucket",
        default=os.environ.get("RAW_BUCKET"),
        help="Raw bucket name. Defaults to $RAW_BUCKET.",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
        help="AWS region (default: $AWS_REGION or us-east-1).",
    )
    parser.add_argument("--expires", type=int, default=3600, help="URL lifetime in seconds (default 3600).")
    parser.add_argument("--upload", action="store_true", help="Immediately PUT the file using the generated URL.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.bucket:
        print("error: no bucket given (pass --bucket or set $RAW_BUCKET)", file=sys.stderr)
        return 2

    path = Path(args.file)
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2

    file_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    key = build_key(path.name, now, file_id)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    url = generate_presigned_put(
        bucket=args.bucket,
        key=key,
        uploader=args.uploader,
        content_type=content_type,
        region=args.region,
        expires=args.expires,
    )

    print(f"file_id      : {file_id}")
    print(f"key          : {key}")
    print(f"content-type : {content_type}")
    print(f"expires (s)  : {args.expires}")
    print("\npresigned PUT url:\n" + url)
    print("\ncurl to upload:")
    print(
        f"  curl -X PUT '{url}' \\\n"
        f"    -H 'Content-Type: {content_type}' \\\n"
        f"    -H 'x-amz-meta-uploader: {args.uploader}' \\\n"
        f"    --data-binary @'{path}'"
    )

    if args.upload:
        _do_upload(url, path, content_type, args.uploader)
    return 0


def _do_upload(url: str, path: Path, content_type: str, uploader: str) -> None:
    """Optional direct upload so the script is self-contained for demos."""
    import urllib.request

    data = path.read_bytes()
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={"Content-Type": content_type, "x-amz-meta-uploader": uploader},
    )
    print("\nuploading...")
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - presigned S3 URL
        print(f"upload complete: HTTP {resp.status}")


if __name__ == "__main__":
    raise SystemExit(main())
