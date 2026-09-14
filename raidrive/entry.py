from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FsEntry:
    """Filesystem entry (file or folder) from Teldrive."""

    path: str  # absolute mount path, e.g. /Filmes/foo.mkv
    name: str
    is_dir: bool
    size: int
    mtime: float  # unix timestamp
    file_id: str = ""  # Teldrive file UUID (empty for synthetic root)
