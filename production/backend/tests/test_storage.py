from datetime import datetime, timezone

from .. import config
from ..services import storage


def _cfg(monkeypatch):
    for k, v in {"S3_ENDPOINT": "https://acct.r2.cloudflarestorage.com", "S3_BUCKET": "recs",
                 "S3_ACCESS_KEY_ID": "AK", "S3_SECRET_ACCESS_KEY": "SK", "S3_REGION": "auto"}.items():
        monkeypatch.setattr(config, k, v)


def test_signed_url_without_network(monkeypatch):
    _cfg(monkeypatch)
    assert storage.storage_enabled() is True
    url = storage.signed_recording_url("recordings/2026/11/x.mp3")
    assert "recs" in url and "X-Amz-Signature" in url and "recordings/2026/11/x.mp3" in url


def test_storage_disabled_without_bucket(monkeypatch):
    monkeypatch.setattr(config, "S3_BUCKET", "")
    assert storage.storage_enabled() is False


def test_archive_recording_key_and_upload(monkeypatch):
    _cfg(monkeypatch)
    puts = []

    class Res:
        status_code = 200
        content = b"RIFF"
        headers = {"content-type": "audio/wav"}

    class Client:
        def put_object(self, **kw):
            puts.append(kw)

    monkeypatch.setattr(storage.requests, "get", lambda url, timeout=None: Res())
    monkeypatch.setattr(storage, "_client", lambda: Client())
    key = storage.archive_recording("ex1", "https://b/rec", datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert key == "recordings/2026/11/ex1.wav"
    assert puts[0]["Bucket"] == "recs" and puts[0]["Key"] == key and puts[0]["Body"] == b"RIFF"


def test_storage_disabled_without_endpoint(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(config, "S3_ENDPOINT", "")
    assert storage.storage_enabled() is False
