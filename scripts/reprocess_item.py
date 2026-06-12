#!/usr/bin/env python3
"""Re-run enrichment for an existing catalog item (e.g. after deploying video support).

Does not re-upload the raw file. Fetches the DynamoDB row and invokes the same
process_item() logic the stream trigger uses.

Example:
    python scripts/reprocess_item.py 8b3e1fa9-1732-431b-92e0-f01ebf7b6bc6 \\
        --table family-media-catalog
"""
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

import boto3

# Import enrichment handler from the lambda source tree.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas" / "enrichment"))
import enrichment_handler as eh  # noqa: E402


def _deserialize(item: dict) -> dict:
    out: dict = {}
    for key, value in item.items():
        if isinstance(value, Decimal):
            out[key] = int(value) if value % 1 == 0 else float(value)
        else:
            out[key] = value
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file_id", help="Catalog file_id (UUID).")
    parser.add_argument(
        "--table",
        default=os.environ.get("CATALOG_TABLE"),
        help="DynamoDB catalog table (default: $CATALOG_TABLE).",
    )
    args = parser.parse_args(argv)

    if not args.table:
        print("error: pass --table or set $CATALOG_TABLE", file=sys.stderr)
        return 2

    table = boto3.resource("dynamodb").Table(args.table)
    resp = table.get_item(Key={"file_id": args.file_id})
    item = resp.get("Item")
    if not item:
        print(f"error: no catalog item for file_id={args.file_id}", file=sys.stderr)
        return 1

    record = eh.process_item(_deserialize(item))
    print(
        f"reprocessed {args.file_id}: "
        f"capture_ts={record.get('capture_ts')} "
        f"thumbnail={record.get('s3_thumbnail_key')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
