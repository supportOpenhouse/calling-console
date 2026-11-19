"""R2 (S3-compatible) recording archive. Port of the old lib/storage.ts."""
from __future__ import annotations

from datetime import datetime

import boto3
import botocore.exceptions
import requests
from botocore.config import Config

from .. import config


# The recording host or R2 is down or not answering (archive_recordings stops its run on these).
UNREACHABLE = (requests.Timeout, requests.ConnectionError, botocore.exceptions.ConnectionError,
               botocore.exceptions.ReadTimeoutError)


def storage_enabled() -> bool:
    return bool(config.S3_ENDPOINT and config.S3_BUCKET and config.S3_ACCESS_KEY_ID and config.S3_SECRET_ACCESS_KEY)


def _client():
    return boto3.client(
        "s3", endpoint_url=config.S3_ENDPOINT, region_name=config.S3_REGION,
        aws_access_key_id=config.S3_ACCESS_KEY_ID, aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def archive_recording(execution_id: str, url: str, when: datetime) -> str:
    """Download a recording and store it under recordings/<yyyy>/<mm>/<execution_id>.<ext>. Returns the key."""
    res = requests.get(url, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"recording fetch {res.status_code}")
    ctype = res.headers.get("content-type") or "audio/mpeg"
    ext = "wav" if "wav" in ctype else "ogg" if "ogg" in ctype else "mp3"
    key = f"recordings/{when.year}/{when.month:02d}/{execution_id}.{ext}"
    _client().put_object(Bucket=config.S3_BUCKET, Key=key, Body=res.content, ContentType=ctype)
    return key


def signed_recording_url(key: str, expires: int = 3600) -> str:
    return _client().generate_presigned_url(
        "get_object", Params={"Bucket": config.S3_BUCKET, "Key": key}, ExpiresIn=expires)
