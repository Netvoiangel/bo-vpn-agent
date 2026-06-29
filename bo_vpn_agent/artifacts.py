from __future__ import annotations

import os
import uuid
from datetime import timedelta
from pathlib import Path

from .models import isoformat, utc_now


class ArtifactStore:
    def __init__(self, directory: Path, ttl_hours: int, max_bytes: int) -> None:
        self.directory = directory
        self.ttl_hours = ttl_hours
        self.max_bytes = max_bytes

    def create_text_artifact(self, filename: str, content: str, content_type: str = "text/plain") -> dict[str, object]:
        payload = content.encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ValueError("artifact exceeds max size")
        self.directory.mkdir(parents=True, exist_ok=True)
        artifact_id = str(uuid.uuid4())
        path = self.directory / f"{artifact_id}-{filename}"
        path.write_bytes(payload)
        expires_at = utc_now() + timedelta(hours=self.ttl_hours)
        return {
            "artifact_id": artifact_id,
            "type": "diagnostic_bundle",
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(payload),
            "expires_at": isoformat(expires_at),
        }

    def cleanup_expired(self) -> None:
        if not self.directory.exists():
            return
        now = utc_now().timestamp()
        ttl = self.ttl_hours * 60 * 60
        for path in self.directory.iterdir():
            if path.is_file() and now - path.stat().st_mtime > ttl:
                os.unlink(path)
