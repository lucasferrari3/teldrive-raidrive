from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_mode(value: str, default: int) -> int:
    value = (value or "").strip()
    if not value:
        return default
    return int(value, 8) if value.isdigit() else int(value, 0)


def _parse_bool(value: str, default: bool = False) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def parse_duration(value: str, default_seconds: float) -> float:
    """Parse RaiDrive-like durations: 12:00:00, 12h, 30m, 90s, or raw seconds."""
    value = (value or "").strip()
    if not value:
        return default_seconds
    if re.fullmatch(r"\d+:\d{2}:\d{2}", value):
        h, m, s = value.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([HhMmSs])?", value)
    if m:
        num = float(m.group(1))
        unit = (m.group(2) or "s").lower()
        if unit == "h":
            return num * 3600
        if unit == "m":
            return num * 60
        return num
    return float(value)


def _mib_to_bytes(env_name: str, default_mib: int, legacy_bytes_name: str | None = None) -> int:
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return int(float(raw) * 1024 * 1024)
    if legacy_bytes_name:
        legacy = os.environ.get(legacy_bytes_name, "").strip()
        if legacy:
            return int(legacy)
    return default_mib * 1024 * 1024


@dataclass(frozen=True)
class Config:
    backend: str
    # Teldrive
    api_host: str
    access_token: str
    api_key: str
    # WebDAV
    webdav_url: str
    webdav_user: str
    webdav_password: str
    mount_point: Path
    block_size: int
    prefetch_blocks: int
    ram_cache_bytes: int
    disk_cache_bytes: int
    cache_dir: Path | None
    cache_read_ttl: float
    cache_clean_interval: float
    prefetch_after: int
    fast_first_bytes: int
    meta_ttl: float
    negative_meta_ttl: float
    page_size: int
    upload_chunk_size: int
    channel_id: int
    dir_mode: int
    file_mode: int
    uid: int
    gid: int
    allow_other: bool
    read_only: bool

    @property
    def cache_namespace(self) -> str:
        if self.backend == "webdav":
            return self.webdav_url or "webdav"
        return self.api_host or "teldrive"


def load_config(env_file: str | None = None) -> Config:
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    backend = (os.environ.get("BACKEND", "teldrive") or "teldrive").strip().lower()
    if backend not in ("teldrive", "webdav"):
        raise SystemExit("BACKEND must be 'teldrive' or 'webdav'")

    api_host = (
        os.environ.get("TELDRIVE_API_HOST", "")
        or os.environ.get("API_HOST", "")
    ).rstrip("/")
    access_token = (
        os.environ.get("TELDRIVE_ACCESS_TOKEN", "")
        or os.environ.get("ACCESS_TOKEN", "")
    ).strip()
    api_key = (
        os.environ.get("TELDRIVE_API_KEY", "")
        or os.environ.get("API_KEY", "")
    ).strip()

    webdav_url = os.environ.get("WEBDAV_URL", "").rstrip("/")
    webdav_user = os.environ.get("WEBDAV_USER", "")
    webdav_password = os.environ.get("WEBDAV_PASSWORD", "")

    if backend == "teldrive":
        if not api_host:
            raise SystemExit(
                "Missing TELDRIVE_API_HOST (e.g. http://localhost:8080). "
                "Copy .env.example to .env"
            )
        if not access_token and not api_key:
            raise SystemExit(
                "Missing TELDRIVE_ACCESS_TOKEN (cookie/session) or TELDRIVE_API_KEY. "
                "Copy the access_token cookie from the Teldrive web UI."
            )
    else:
        if not webdav_url or not webdav_user or not webdav_password:
            raise SystemExit(
                "Missing WEBDAV_URL / WEBDAV_USER / WEBDAV_PASSWORD "
                "(set BACKEND=webdav and copy .env.example)"
            )

    read_only = _parse_bool(os.environ.get("READ_ONLY", "1"), default=True)

    shared = os.environ.get("MODE", "").strip()
    if shared:
        default_dir = _parse_mode(shared, 0o555)
        default_file = _parse_mode(shared, 0o444)
    elif read_only:
        default_dir, default_file = 0o555, 0o444
    else:
        default_dir, default_file = 0o755, 0o644

    cache_dir_raw = os.environ.get("CACHE_DIR", "").strip()
    cache_dir = Path(cache_dir_raw) if cache_dir_raw else None

    disk_bytes = _mib_to_bytes("CACHE_MIB", 2048, legacy_bytes_name="CACHE_BYTES")
    ram_default_mib = min(512, max(64, disk_bytes // (1024 * 1024)))
    ram_bytes = _mib_to_bytes("RAM_CACHE_MIB", ram_default_mib)
    block_bytes = _mib_to_bytes("BLOCK_MIB", 2, legacy_bytes_name="BLOCK_SIZE")

    if os.environ.get("FAST_FIRST_MIB", "").strip():
        fast_first_bytes = _mib_to_bytes("FAST_FIRST_MIB", 1)
    else:
        fast_first_bytes = int(
            float(os.environ.get("FAST_FIRST_KIB", "1024") or 1024) * 1024
        )

    return Config(
        backend=backend,
        api_host=api_host,
        access_token=access_token,
        api_key=api_key,
        webdav_url=webdav_url,
        webdav_user=webdav_user,
        webdav_password=webdav_password,
        mount_point=Path(os.environ.get("MOUNT_POINT", "./mnt")),
        block_size=block_bytes,
        prefetch_blocks=int(os.environ.get("PREFETCH_BLOCKS", "8")),
        ram_cache_bytes=ram_bytes,
        disk_cache_bytes=disk_bytes,
        cache_dir=cache_dir,
        cache_read_ttl=parse_duration(
            os.environ.get("CACHE_READ_TTL", os.environ.get("CACHE_TTL", "12:00:00")),
            12 * 3600,
        ),
        cache_clean_interval=parse_duration(
            os.environ.get("CACHE_CLEAN_INTERVAL", "1:00:00"),
            3600,
        ),
        prefetch_after=int(os.environ.get("PREFETCH_AFTER", "2")),
        fast_first_bytes=fast_first_bytes,
        meta_ttl=float(os.environ.get("META_TTL", "300")),
        negative_meta_ttl=float(
            os.environ.get("NEGATIVE_META_TTL", os.environ.get("META_TTL", "300"))
        ),
        page_size=int(os.environ.get("PAGE_SIZE", "500")),
        upload_chunk_size=_mib_to_bytes("UPLOAD_CHUNK_MIB", 64),
        channel_id=int(os.environ.get("CHANNEL_ID", "0") or 0),
        dir_mode=_parse_mode(os.environ.get("DIR_MODE", ""), default_dir) & 0o777,
        file_mode=_parse_mode(os.environ.get("FILE_MODE", ""), default_file) & 0o777,
        uid=int(
            os.environ.get(
                "MOUNT_UID",
                os.environ.get(
                    "RAIDRIVE_UID",
                    str(os.getuid() if hasattr(os, "getuid") else 0),
                ),
            )
        ),
        gid=int(
            os.environ.get(
                "MOUNT_GID",
                os.environ.get(
                    "RAIDRIVE_GID",
                    str(os.getgid() if hasattr(os, "getgid") else 0),
                ),
            )
        ),
        allow_other=_parse_bool(os.environ.get("ALLOW_OTHER", "0"), default=False),
        read_only=read_only,
    )
