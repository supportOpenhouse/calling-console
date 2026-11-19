"""R2 recording archive, on the house R2 settings (Openhouse_Meetings backend/lib/r2Config.js): the shared phone-calls
bucket, each app in its own folder -- ours is calling-console/, after R2_KEY_PREFIX (empty in production, dev/
locally). Links are the bucket's public base + key."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote, urlparse

import boto3
import botocore.exceptions
import requests
from botocore.config import Config

from .. import config


# The recording host or R2 is down or not answering (archive_recordings stops its run on these).
UNREACHABLE = (requests.Timeout, requests.ConnectionError, botocore.exceptions.ConnectionError,
               botocore.exceptions.ReadTimeoutError)


def storage_enabled() -> bool:
    return bool(config.R2_BLOB_PHONE_CALLS_BUCKET and config.R2_ACCESS_KEY_ID and config.R2_SECRET_ACCESS_KEY
                and config.R2_PHONE_CALLS_PUBLIC_URL)


def _bucket() -> tuple[str, str]:
    """(endpoint, bucket name) from R2_BLOB_PHONE_CALLS_BUCKET = https://<account>.r2.cloudflarestorage.com/<bucket>."""
    u = urlparse(config.R2_BLOB_PHONE_CALLS_BUCKET)
    name = u.path.strip("/").split("/")[0]
    if u.scheme != "https" or not u.netloc or not name:
        raise RuntimeError("R2_BLOB_PHONE_CALLS_BUCKET must be https://<account>.r2.cloudflarestorage.com/<bucket>, "
                           f"got {config.R2_BLOB_PHONE_CALLS_BUCKET!r}")
    return f"https://{u.netloc}", name


def _client():
    return boto3.client(
        "s3", endpoint_url=_bucket()[0], region_name="auto",
        aws_access_key_id=config.R2_ACCESS_KEY_ID, aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def archive_recording(execution_id: str, url: str, when: datetime) -> str:
    """Download a recording and store it under <R2_KEY_PREFIX>calling-console/recordings/<yyyy>/<mm>/<execution_id>.<ext>.
    Returns the key."""
    res = requests.get(url, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"recording fetch {res.status_code}")
    ctype = res.headers.get("content-type") or "audio/mpeg"
    ext = "wav" if "wav" in ctype else "ogg" if "ogg" in ctype else "mp3"
    key = f"{config.R2_KEY_PREFIX}calling-console/recordings/{when.year}/{when.month:02d}/{execution_id}.{ext}"
    _client().put_object(Bucket=_bucket()[1], Key=key, Body=res.content, ContentType=ctype)
    return key


def recording_url(key: str) -> str:
    """Public link: the first base of R2_PHONE_CALLS_PUBLIC_URL (comma-separated; the first is for new links) + key."""
    base = config.R2_PHONE_CALLS_PUBLIC_URL.split(",")[0].strip().rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError(f"R2_PHONE_CALLS_PUBLIC_URL must start with https://, got {base!r}")
    return f"{base}/{quote(key)}"
