"""Shared pytest setup: make the Lambda + script sources importable and set
the environment variables their modules read at import time.
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# Each Lambda is packaged standalone, so add the relevant source dirs to the
# import path for testing. Module filenames are unique across lambdas
# (handler.py vs enrichment_handler.py) so imports cannot collide.
sys.path.insert(0, str(_ROOT / "lambdas" / "upload_trigger"))
sys.path.insert(0, str(_ROOT / "lambdas" / "enrichment"))
sys.path.insert(0, str(_ROOT / "lambdas" / "album_agent"))
sys.path.insert(0, str(_ROOT / "lambdas" / "search_api"))
sys.path.insert(0, str(_ROOT / "scripts"))

# The Lambda modules read these at import time.
os.environ.setdefault("CATALOG_TABLE", "family-media-catalog-test")
os.environ.setdefault("RAW_BUCKET", "family-media-raw-test")
os.environ.setdefault("PROCESSED_BUCKET", "family-media-processed-test")
os.environ.setdefault("FACES_TABLE", "family-media-faces-test")
os.environ.setdefault("FACE_COLLECTION_ID", "family-media-faces-test")
os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0")
os.environ.setdefault("GLUE_DATABASE", "family_media")
os.environ.setdefault("GLUE_TABLE", "media_metadata")
os.environ.setdefault("ATHENA_WORKGROUP", "family-media-wg")
os.environ.setdefault("CLOUDFRONT_DOMAIN", "d111111abcdef8.cloudfront.net")
os.environ.setdefault("AWS_REGION_", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("LOG_LEVEL", "INFO")
