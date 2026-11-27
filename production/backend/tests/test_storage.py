from datetime import datetime, timezone

import pytest

from .. import config
from ..services import storage


def _cfg(monkeypatch, prefix=""):
    for k, v in {"R2_BLOB_PHONE_CALLS_BUCKET": "https://acct.r2.cloudflarestorage.com/phone-calls",
                 "R2_ACCESS_KEY_ID": "AK", "R2_SECRET_ACCESS_KEY": "SK",
                 "R2_PHONE_CALLS_PUBLIC_URL": "https://calls.example.com/, https://pub-x.r2.dev",
                 "R2_KEY_PREFIX": prefix}.items():
        monkeypatch.setattr(config, k, v)


def test_public_link_uses_the_first_base(monkeypatch):
    _cfg(monkeypatch)
    assert storage.storage_enabled() is True
    assert storage.recording_url("calling-console/recordings/2026/11/x y.mp3") == \
        "https://calls.example.com/calling-console/recordings/2026/11/x%20y.mp3"


@pytest.mark.parametrize("var", ["R2_BLOB_PHONE_CALLS_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                                 "R2_PHONE_CALLS_PUBLIC_URL"])
def test_storage_disabled_without_any_setting(monkeypatch, var):
    _cfg(monkeypatch)
    monkeypatch.setattr(config, var, "")
    assert storage.storage_enabled() is False


def test_bucket_url_without_a_bucket_fails_loudly(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(config, "R2_BLOB_PHONE_CALLS_BUCKET", "https://acct.r2.cloudflarestorage.com")
    with pytest.raises(RuntimeError, match="R2_BLOB_PHONE_CALLS_BUCKET must be"):
        storage._bucket()


def test_archive_recording_key_and_upload(monkeypatch):
    _cfg(monkeypatch, prefix="dev/")
    puts, clients = [], []

    class Res:
        status_code = 200
        content = b"RIFF"
        headers = {"content-type": "audio/wav"}

    class Client:
        def put_object(self, **kw):
            puts.append(kw)

    monkeypatch.setattr(storage.requests, "get", lambda url, timeout=None: Res())
    monkeypatch.setattr(storage.boto3, "client", lambda *a, **kw: clients.append(kw) or Client())
    key = storage.archive_recording("ex1", "https://b/rec", datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert key == "dev/calling-console/recordings/2026/11/ex1.wav"
    assert puts[0]["Bucket"] == "phone-calls" and puts[0]["Key"] == key and puts[0]["Body"] == b"RIFF"
    assert clients[0]["endpoint_url"] == "https://acct.r2.cloudflarestorage.com"


def test_kb_pdf_copies_live_under_the_prefix_and_a_missing_one_is_none(monkeypatch):
    import botocore.exceptions
    _cfg(monkeypatch, prefix="dev/")
    calls = []

    class Body:
        def read(self):
            return b"%PDF"

    class Client:
        def put_object(self, **kw):
            calls.append(("put", kw["Key"], kw["ContentType"]))

        def get_object(self, **kw):
            if kw["Key"].endswith("missing.pdf"):
                raise botocore.exceptions.ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {"Body": Body()}

        def delete_object(self, **kw):
            calls.append(("delete", kw["Key"]))

    monkeypatch.setattr(storage.boto3, "client", lambda *a, **kw: Client())
    storage.save_kb_pdf("kb1", b"%PDF")
    assert storage.load_kb_pdf("kb1") == b"%PDF" and storage.load_kb_pdf("missing") is None
    storage.delete_kb_pdf("kb1")
    assert calls == [("put", "dev/calling-console/knowledgebases/kb1.pdf", "application/pdf"),
                     ("delete", "dev/calling-console/knowledgebases/kb1.pdf")]
