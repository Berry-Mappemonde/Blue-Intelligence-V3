"""object_storage — Optional upload of run exports to Nebius Object Storage.

Off by default. Enabled only when BI_OBJECT_STORAGE_UPLOAD is truthy.
Nebius Object Storage speaks the S3 API, so the client is a thin wrapper
around boto3 (already a backend dependency). Endpoint, bucket and region
come from the environment; credentials are left to boto3's standard
chain and are never read, stored or logged here. Tests inject a stub.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class StorageConfig:
    enabled: bool = False
    endpoint_url: str | None = None
    bucket: str | None = None
    region: str | None = None
    prefix: str = "runs"

    @classmethod
    def from_env(cls) -> "StorageConfig":
        env = os.environ
        return cls(
            enabled=(env.get("BI_OBJECT_STORAGE_UPLOAD") or "").strip().lower() in _TRUE,
            endpoint_url=(env.get("BI_OBJECT_STORAGE_ENDPOINT") or "").strip() or None,
            bucket=(env.get("BI_OBJECT_STORAGE_BUCKET") or "").strip() or None,
            region=(env.get("BI_OBJECT_STORAGE_REGION") or "").strip() or None,
            prefix=(env.get("BI_OBJECT_STORAGE_PREFIX") or "runs").strip().strip("/") or "runs",
        )


class S3Client:
    """Minimal S3-compatible client: one method, put_file."""

    def __init__(self, config: StorageConfig, session=None):
        import boto3  # lazy: never imported when upload is off

        session = session or boto3.session.Session()
        self._client = session.client(
            "s3", endpoint_url=config.endpoint_url, region_name=config.region)

    def put_file(self, local_path: Path, bucket: str, key: str) -> None:
        self._client.upload_file(str(local_path), bucket, key)


def upload_export(export_dir: Path, run_id: str, *, config: StorageConfig | None = None,
                  client=None) -> dict:
    """Upload every file of an export folder. No-op unless enabled."""
    cfg = config or StorageConfig.from_env()
    if not cfg.enabled:
        return {"uploaded": False, "reason": "disabled"}
    if not cfg.bucket or not cfg.endpoint_url:
        return {"uploaded": False, "reason": "missing endpoint or bucket"}
    client = client or S3Client(cfg)
    base = Path(export_dir)
    keys = []
    # manifest.json last: its presence in the bucket means the rest is there.
    files = sorted((p for p in base.iterdir() if p.is_file()),
                   key=lambda p: (p.name == "manifest.json", p.name))
    for path in files:
        key = f"{cfg.prefix}/{run_id}/{path.name}"
        client.put_file(path, cfg.bucket, key)
        keys.append(key)
    return {"uploaded": True, "bucket": cfg.bucket, "keys": keys}
