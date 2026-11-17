"""Authenticated browser uploads to private Blob Storage using managed identity."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import ipaddress
from itertools import islice
import logging
import os
from pathlib import Path
import re
import secrets
import socket
from tempfile import SpooledTemporaryFile

from azure.core.exceptions import AzureError, ResourceExistsError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

LOG = logging.getLogger("uploader")
ROOT = Path(__file__).resolve().parent
CU_MAX_PAGES = 300


@dataclass(frozen=True)
class Settings:
    account: str
    token: str
    container: str = "raw"
    max_bytes: int = 32 * 1024 * 1024
    cu_enabled: bool = True
    require_private: bool = True
    client_id: str | None = None

    def validate(self):
        if not re.fullmatch(r"[a-z0-9]{3,24}", self.account):
            raise ValueError("UPLOADER_STORAGE_ACCOUNT must be an Azure Storage account name.")
        if len(self.token) < 32 or not self.token.isascii():
            raise ValueError("UPLOADER_TOKEN must contain at least 32 ASCII characters.")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61})[a-z0-9]", self.container) or "--" in self.container:
            raise ValueError("UPLOADER_CONTAINER must be a valid Blob container name.")
        if not 1 <= self.max_bytes <= 128 * 1024 * 1024:
            raise ValueError("UPLOADER_MAX_BYTES must be between 1 and 134217728.")

    @classmethod
    def from_env(cls):
        def boolean(name, default):
            value = os.getenv(name, default).lower()
            if value not in {"true", "false"}:
                raise ValueError(f"{name} must be true or false.")
            return value == "true"

        return cls(
            account=os.getenv("UPLOADER_STORAGE_ACCOUNT", ""),
            token=os.getenv("UPLOADER_TOKEN", ""),
            container=os.getenv("UPLOADER_CONTAINER", "raw"),
            max_bytes=int(os.getenv("UPLOADER_MAX_BYTES", str(32 * 1024 * 1024))),
            cu_enabled=boolean("UPLOADER_CU_ENABLED", "true"),
            require_private=boolean("UPLOADER_REQUIRE_PRIVATE_STORAGE", "true"),
            client_id=os.getenv("AZURE_CLIENT_ID") or None,
        )

    @property
    def hostname(self):
        return f"{self.account}.blob.core.windows.net"


def private_addresses(settings):
    addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(
        settings.hostname, 443, type=socket.SOCK_STREAM,
    )})
    if not addresses or (settings.require_private and not all(
        ipaddress.ip_address(address).is_private for address in addresses
    )):
        raise OSError("Storage DNS must resolve only to private addresses.")
    return addresses


class BlobStore:
    def __init__(self, settings):
        self.settings = settings
        self.credential = DefaultAzureCredential(managed_identity_client_id=settings.client_id)
        self.service = BlobServiceClient(
            f"https://{settings.hostname}", credential=self.credential,
            connection_timeout=10, read_timeout=60, retry_total=2,
        )
        self.container = self.service.get_container_client(settings.container)

    def ready(self):
        addresses = private_addresses(self.settings)
        self.container.get_container_properties()
        return {"storageReady": True, "storageAddresses": addresses}

    def upload(self, stream, name, size, metadata):
        private_addresses(self.settings)
        result = self.container.get_blob_client(name).upload_blob(
            data=stream, length=size, overwrite=False,
            content_settings=ContentSettings(content_type="application/pdf"),
            metadata=metadata, validate_content=True, max_concurrency=1,
        )
        return result["etag"]

    def files(self):
        private_addresses(self.settings)
        rows, truncated = [], False
        for prefix in ("cu/", "di/"):
            batch = list(islice(self.container.list_blobs(
                name_starts_with=prefix, include=["metadata"],
            ), 51))
            truncated = truncated or len(batch) > 50
            for blob in batch[:50]:
                rows.append({
                    "blob": blob.name, "bytes": blob.size,
                    "pages": (blob.metadata or {}).get("pages"),
                    "sha256": (blob.metadata or {}).get("sha256"),
                })
        return {"files": rows, "truncated": truncated}

    def close(self):
        self.service.close()
        self.credential.close()


def pdf_pages(stream):
    stream.seek(0)
    if stream.read(5) != b"%PDF-":
        raise ValueError("The uploaded file does not start with a PDF header.")
    stream.seek(0)
    try:
        reader = PdfReader(stream)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported.")
        count = len(reader.pages)
    except (PdfReadError, KeyError, TypeError) as exc:
        raise ValueError("The PDF could not be parsed.") from exc
    if count < 1:
        raise ValueError("The PDF contains no pages.")
    stream.seek(0)
    return count


def create_app(settings=None, store=None):
    settings = settings or Settings.from_env()
    settings.validate()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = store if store is not None else BlobStore(settings)
        app.state.upload_lock = asyncio.Lock()
        try:
            yield
        finally:
            app.state.store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def authorize(request: Request):
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            supplied.encode("utf-8"), settings.token.encode("ascii"),
        ):
            raise HTTPException(401, "A valid upload token is required.", headers={"WWW-Authenticate": "Bearer"})

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/status", dependencies=[Depends(authorize)])
    async def status(request: Request):
        try:
            result = await run_in_threadpool(request.app.state.store.ready)
        except (AzureError, OSError):
            LOG.exception("Storage readiness check failed")
            raise HTTPException(503, "Storage is unavailable. Check private DNS, managed identity, and server logs.")
        return {**result, "maxBytes": settings.max_bytes, "cuEnabled": settings.cu_enabled,
                "cuMaxPages": CU_MAX_PAGES}

    @app.get("/api/files", dependencies=[Depends(authorize)])
    async def files(request: Request):
        try:
            return await run_in_threadpool(request.app.state.store.files)
        except (AzureError, OSError):
            LOG.exception("Blob listing failed")
            raise HTTPException(503, "Storage listing failed. Check the server logs.")

    @app.post("/api/upload", dependencies=[Depends(authorize)])
    async def upload(request: Request, filename: str, allow_large: bool = False):
        if (len(filename) > 180 or filename in {".", ".."} or
                any(char in filename for char in '/\\:') or
                any(ord(char) < 32 or ord(char) == 127 for char in filename) or
                not filename.lower().endswith(".pdf") or filename != filename.strip()):
            raise HTTPException(400, "Provide a PDF basename without path separators or control characters.")
        declared = request.headers.get("content-length")
        if declared:
            if not declared.isdigit():
                raise HTTPException(400, "Invalid Content-Length.")
            if int(declared) > settings.max_bytes:
                raise HTTPException(413, "The PDF exceeds the configured upload size limit.")
        lock = request.app.state.upload_lock
        if lock.locked():
            raise HTTPException(429, "Another PDF upload is in progress. Retry when it completes.")
        async with lock:
            size, digest = 0, hashlib.sha256()
            with SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as stream:
                try:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > settings.max_bytes:
                            raise HTTPException(413, "The PDF exceeds the configured upload size limit.")
                        digest.update(chunk)
                        stream.write(chunk)
                except ClientDisconnect:
                    LOG.warning("Upload client disconnected before the PDF was received")
                    raise HTTPException(400, "The upload was interrupted; no blob was created.")
                try:
                    pages = await run_in_threadpool(pdf_pages, stream)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                if settings.cu_enabled and pages > CU_MAX_PAGES and not allow_large:
                    raise HTTPException(
                        422, "This PDF exceeds 300 pages. Split it first, or explicitly allow an unsplit DI+ upload.",
                    )
                prefix = "cu" if settings.cu_enabled and pages <= CU_MAX_PAGES else "di"
                blob = f"{prefix}/{filename}"
                checksum = digest.hexdigest()
                try:
                    etag = await run_in_threadpool(
                        request.app.state.store.upload, stream, blob, size,
                        {"pages": str(pages), "sha256": checksum, "tier": prefix},
                    )
                except ResourceExistsError:
                    raise HTTPException(409, "That blob already exists. This uploader never overwrites documents.")
                except (AzureError, OSError):
                    LOG.exception("PDF storage write failed")
                    raise HTTPException(503, "The Storage write failed. Check server logs before retrying.")
                LOG.info("Stored PDF bytes=%s pages=%s tier=%s", size, pages, prefix)
                return {"filename": filename, "blob": blob, "pages": pages, "bytes": size,
                        "tier": prefix, "sha256": checksum, "etag": etag}

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
