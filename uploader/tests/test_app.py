import hashlib
from io import BytesIO
import socket
from types import SimpleNamespace
from unittest.mock import patch

from azure.core.exceptions import ResourceExistsError, ServiceRequestError
from fastapi.testclient import TestClient
from pypdf import PdfWriter
import pytest

from uploader.app import BlobStore, Settings, create_app, private_addresses

TOKEN = "test-token-" + "x" * 40
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/pdf"}


def pdf(pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("not-an-upload-secret")
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


class Store:
    def __init__(self):
        self.blobs = {}
        self.error = None

    def ready(self):
        if self.error:
            raise self.error
        return {"storageReady": True, "storageAddresses": ["172.16.0.4"]}

    def upload(self, stream, name, size, metadata):
        if self.error:
            raise self.error
        if name in self.blobs:
            raise ResourceExistsError("already exists")
        data = stream.read()
        assert len(data) == size
        self.blobs[name] = (data, metadata)
        return "example-etag"

    def files(self):
        return {"files": [{"blob": name, "bytes": len(data), **metadata}
                          for name, (data, metadata) in self.blobs.items()], "truncated": False}

    def close(self):
        pass


@pytest.fixture
def client():
    store = Store()
    with TestClient(create_app(Settings(account="examplestorage", token=TOKEN), store)) as http:
        yield http, store


def test_upload_requires_authentication_before_storing_anything(client):
    http, store = client
    for route in ("/api/status", "/api/files"):
        assert http.get(route).status_code == 401
    assert http.post("/api/upload?filename=test.pdf", content=pdf()).status_code == 401
    assert not store.blobs
    assert http.get("/").status_code == 200
    assert http.get("/healthz").json() == {"status": "ok"}


def test_document_upload_routes_and_preserves_bytes_and_checksum(client):
    http, store = client
    content = pdf(2)
    response = http.post("/api/upload", params={"filename": "Policies.pdf"}, headers=HEADERS, content=content)
    assert response.status_code == 200
    assert response.json()["blob"] == "cu/Policies.pdf"
    assert response.json()["pages"] == 2
    assert response.json()["sha256"] == hashlib.sha256(content).hexdigest()
    assert store.blobs["cu/Policies.pdf"][0] == content
    assert http.get("/api/files", headers=HEADERS).json()["files"][0]["blob"] == "cu/Policies.pdf"


@pytest.mark.parametrize("filename", ["../test.pdf", r"..\test.pdf", "dir/test.pdf", "a:stream.pdf", "test.txt", "a\n.pdf", " test.pdf"])
def test_rejects_non_pdf_or_unsafe_names(client, filename):
    http, store = client
    response = http.post("/api/upload", params={"filename": filename}, headers=HEADERS, content=pdf())
    assert response.status_code == 400
    assert not store.blobs


@pytest.mark.parametrize("content", [b"", b"not a pdf", b"%PDF-1.7\nbroken", pdf(encrypted=True)])
def test_rejects_invalid_and_encrypted_pdfs(client, content):
    http, store = client
    response = http.post("/api/upload?filename=test.pdf", headers=HEADERS, content=content)
    assert response.status_code == 400
    assert not store.blobs


def test_oversized_pdf_requires_explicit_di_consent(client):
    http, store = client
    boundary = http.post("/api/upload?filename=boundary.pdf", headers=HEADERS, content=pdf(300))
    assert boundary.json()["blob"] == "cu/boundary.pdf"
    content = pdf(301)
    assert http.post("/api/upload?filename=large.pdf", headers=HEADERS, content=content).status_code == 422
    assert len(store.blobs) == 1
    response = http.post("/api/upload?filename=large.pdf&allow_large=true", headers=HEADERS, content=content)
    assert response.status_code == 200
    assert response.json()["blob"] == "di/large.pdf"


def test_disabled_cu_routes_a_different_corpus_to_di():
    store = Store()
    with TestClient(create_app(Settings(account="examplestorage", token=TOKEN, cu_enabled=False), store)) as http:
        result = http.post("/api/upload?filename=Lab-Protocol.pdf", headers=HEADERS, content=pdf()).json()
    assert result["blob"] == "di/Lab-Protocol.pdf"


def test_rejects_size_limit_with_and_without_content_length():
    store = Store()
    with TestClient(create_app(Settings(account="examplestorage", token=TOKEN, max_bytes=16), store)) as http:
        assert http.post("/api/upload?filename=test.pdf", headers=HEADERS, content=b"x" * 17).status_code == 413
        assert http.post("/api/upload?filename=test.pdf", headers=HEADERS,
                         content=iter([b"%PDF-", b"x" * 20])).status_code == 413
    assert not store.blobs


def test_never_overwrites_and_surfaces_storage_errors(client):
    http, store = client
    content = pdf()
    assert http.post("/api/upload?filename=test.pdf", headers=HEADERS, content=content).status_code == 200
    assert http.post("/api/upload?filename=test.pdf", headers=HEADERS, content=content).status_code == 409
    store.error = ServiceRequestError("Storage connection failed")
    assert http.post("/api/upload?filename=other.pdf", headers=HEADERS, content=content).status_code == 503
    assert http.get("/api/status", headers=HEADERS).status_code == 503
    assert len(store.blobs) == 1


def test_private_storage_fails_closed_for_public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.60.1.1", 443)),
    ])
    with pytest.raises(OSError, match="private addresses"):
        private_addresses(Settings(account="examplestorage", token=TOKEN))


def test_missing_token_prevents_insecure_startup():
    with pytest.raises(ValueError, match="UPLOADER_TOKEN"):
        create_app(Settings(account="examplestorage", token=""))


@patch("uploader.app.private_addresses", return_value=["172.16.0.4"])
@patch("uploader.app.DefaultAzureCredential")
@patch("uploader.app.BlobServiceClient")
def test_sdk_upload_uses_blob_client_response_without_overwrite(service, credential, addresses):
    store = BlobStore(Settings(account="examplestorage", token=TOKEN))
    container = service.return_value.get_container_client.return_value
    blob = container.get_blob_client.return_value
    blob.upload_blob.return_value = {"etag": "stored-etag"}
    assert store.upload(BytesIO(b"%PDF-"), "cu/example.pdf", 5, {"pages": "1"}) == "stored-etag"
    container.get_blob_client.assert_called_once_with("cu/example.pdf")
    assert blob.upload_blob.call_args.kwargs["overwrite"] is False
    assert blob.upload_blob.call_args.kwargs["validate_content"] is True


@patch("uploader.app.private_addresses", return_value=["172.16.0.4"])
@patch("uploader.app.DefaultAzureCredential")
@patch("uploader.app.BlobServiceClient")
def test_listing_reports_truncation_even_when_only_one_tier_is_populated(service, credential, addresses):
    store = BlobStore(Settings(account="examplestorage", token=TOKEN))
    container = service.return_value.get_container_client.return_value
    container.list_blobs.side_effect = [
        iter(SimpleNamespace(name=f"cu/{i}.pdf", size=5, metadata={}) for i in range(60)),
        iter([]),
    ]
    result = store.files()
    assert len(result["files"]) == 50
    assert result["truncated"] is True
