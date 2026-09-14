from __future__ import annotations

from typing import Protocol, runtime_checkable

from .entry import FsEntry


@runtime_checkable
class StorageClient(Protocol):
    """Read-oriented backend used by MetaCache / BlockCache / mount layers."""

    def close(self) -> None: ...

    def stat(self, mount_path: str) -> FsEntry | None: ...

    def list_children(self, mount_path: str) -> list[FsEntry]: ...

    def propfind(self, mount_path: str, depth: int = 1) -> list[FsEntry]: ...

    def read_range(self, mount_path: str, start: int, end: int) -> bytes: ...
