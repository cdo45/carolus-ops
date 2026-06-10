"""Document blob storage behind one interface, keyed by content hash.

Keys are sha256 hex digests — content-addressed, so the same bytes land
at the same key and duplicate uploads are free. Document BYTES never
enter git or Postgres: LocalFS lives under data/docstore/ (gitignored);
the R2 backend implements the same interface and raises NotConfigured
until credentials are wired (no R2 creds exist yet).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCAL_ROOT: Path = _REPO_ROOT / "data" / "docstore"


class NotConfigured(RuntimeError):
    """Backend exists but is not wired up in this environment."""


class DocumentStorage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


class LocalFSStorage:
    """Content-addressed files under root/<first-two-hex>/<key>."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DEFAULT_LOCAL_ROOT

    def _path(self, key: str) -> Path:
        if len(key) < 3 or not all(c in "0123456789abcdef" for c in key):
            raise ValueError(f"storage keys are sha256 hex digests, got {key!r}")
        return self.root / key[:2] / key

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class R2Storage:
    """Cloudflare R2 backend — same interface, NOT wired yet.

    Stub on purpose: instantiating is allowed (so wiring code can be
    written against it), every operation raises NotConfigured until
    R2_* credentials exist and the client is implemented.
    """

    _MESSAGE = (
        "R2 storage is not configured — Phase 4 runs on LocalFSStorage;"
        " wire R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY first"
    )

    def put(self, key: str, data: bytes) -> None:
        raise NotConfigured(self._MESSAGE)

    def get(self, key: str) -> bytes:
        raise NotConfigured(self._MESSAGE)

    def exists(self, key: str) -> bool:
        raise NotConfigured(self._MESSAGE)
