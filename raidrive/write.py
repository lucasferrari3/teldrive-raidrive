from __future__ import annotations

import logging
import mimetypes
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

from .cache import BlockCache, MetaCache
from .entry import FsEntry
from .teldrive import TeldriveClient
from .webdav import WebDAVClient

log = logging.getLogger("raidrive.write")


def _norm(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return path or "/"


def _parent(path: str) -> str:
    path = _norm(path)
    if path == "/":
        return "/"
    return path.rsplit("/", 1)[0] or "/"


def _name(path: str) -> str:
    path = _norm(path)
    if path == "/":
        return ""
    return path.rsplit("/", 1)[-1]


@dataclass
class _WriteBuf:
    path: str
    name: str
    parent_path: str
    file_id: str | None = None
    size: int = 0
    dirty: bool = False
    delete_pending: bool = False
    _fp: tempfile.SpooledTemporaryFile = field(repr=False, default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._fp is None:
            self._fp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)

    def write_at(self, offset: int, data: bytes) -> int:
        if offset < 0:
            raise ValueError("negative offset")
        self._fp.seek(offset)
        self._fp.write(data)
        end = offset + len(data)
        if end > self.size:
            self.size = end
        # If we wrote past previous EOF into a hole, SpooledTemporaryFile already
        # extended; keep size consistent with truncate semantics.
        self.dirty = True
        return len(data)

    def truncate(self, size: int) -> None:
        size = max(0, int(size))
        self._fp.truncate(size)
        self.size = size
        self.dirty = True

    def read_at(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset >= self.size:
            return b""
        self._fp.seek(offset)
        return self._fp.read(min(length, self.size - offset))

    def read_all(self) -> bytes:
        self._fp.seek(0)
        return self._fp.read()

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass

    def fileobj(self):
        self._fp.seek(0)
        return self._fp


class WriteStore:
    """Local write-back buffers flushed to the backend on commit/close."""

    def __init__(
        self,
        client: TeldriveClient | WebDAVClient,
        meta: MetaCache,
        blocks: BlockCache,
        *,
        chunk_size: int = 64 * 1024 * 1024,
        channel_id: int = 0,
    ):
        self._client = client
        self._meta = meta
        self._blocks = blocks
        self._chunk_size = max(1 * 1024 * 1024, int(chunk_size))
        self._channel_id = int(channel_id or 0)
        self._lock = threading.RLock()
        self._bufs: dict[str, _WriteBuf] = {}

    def get(self, path: str) -> _WriteBuf | None:
        with self._lock:
            return self._bufs.get(_norm(path))

    def begin_create(self, path: str) -> _WriteBuf:
        path = _norm(path)
        with self._lock:
            old = self._bufs.pop(path, None)
            if old:
                old.close()
            buf = _WriteBuf(
                path=path,
                name=_name(path),
                parent_path=_parent(path),
                file_id=None,
                size=0,
                dirty=True,
            )
            self._bufs[path] = buf
            return buf

    def begin_open(self, path: str, *, truncate: bool) -> _WriteBuf:
        path = _norm(path)
        entry = self._meta.stat(path)
        if entry is None or entry.is_dir:
            raise FileNotFoundError(path)

        with self._lock:
            old = self._bufs.pop(path, None)
            if old:
                old.close()
            buf = _WriteBuf(
                path=path,
                name=entry.name or _name(path),
                parent_path=_parent(path),
                file_id=entry.file_id or None,
                size=0 if truncate else int(entry.size),
                dirty=bool(truncate),
            )
            self._bufs[path] = buf

        if truncate:
            buf.truncate(0)
            return buf

        # Hydrate local buffer from remote (full rewrite on commit).
        size = int(entry.size)
        if size <= 0:
            return buf
        offset = 0
        step = min(self._chunk_size, 8 * 1024 * 1024)
        while offset < size:
            end = min(size - 1, offset + step - 1)
            chunk = self._client.read_range(path, offset, end)
            if not chunk:
                break
            buf.write_at(offset, chunk)
            offset += len(chunk)
        buf.dirty = False
        return buf

    def write_at(self, path: str, offset: int, data: bytes) -> int:
        buf = self.get(path)
        if buf is None:
            raise FileNotFoundError(path)
        return buf.write_at(offset, data)

    def truncate(self, path: str, size: int) -> None:
        buf = self.get(path)
        if buf is None:
            raise FileNotFoundError(path)
        buf.truncate(size)

    def mark_delete(self, path: str) -> None:
        path = _norm(path)
        with self._lock:
            buf = self._bufs.get(path)
            if buf:
                buf.delete_pending = True

    def abort(self, path: str) -> None:
        path = _norm(path)
        with self._lock:
            buf = self._bufs.pop(path, None)
        if buf:
            buf.close()

    def _is_webdav(self) -> bool:
        return isinstance(self._client, WebDAVClient)

    def _invalidate(self, path: str) -> None:
        path = _norm(path)
        parent = _parent(path)
        forget = getattr(self._client, "forget", None)
        if callable(forget):
            forget(path)
        self._meta.invalidate(path)
        self._meta.invalidate(parent)
        self._blocks.invalidate_path(path)

    def commit(self, path: str) -> FsEntry | None:
        path = _norm(path)
        with self._lock:
            buf = self._bufs.get(path)
        if buf is None:
            return None
        if buf.delete_pending:
            self.abort(path)
            return None
        if not buf.dirty:
            self.abort(path)
            entry = self._meta.stat(path)
            return entry

        try:
            if self._is_webdav():
                entry = self._upload_webdav(buf)
            else:
                entry = self._upload_buffer(buf)
        except Exception:
            log.exception("commit failed for %s", path)
            raise
        finally:
            self.abort(path)

        self._invalidate(path)
        if entry is not None:
            # Refresh parent listing and seed the new/updated entry.
            try:
                self._meta.invalidate(buf.parent_path)
                self._meta.list_dir(buf.parent_path)
            except Exception:
                log.debug("post-commit list refresh failed", exc_info=True)
        return entry

    def _upload_webdav(self, buf: _WriteBuf) -> FsEntry | None:
        assert isinstance(self._client, WebDAVClient)
        mime = mimetypes.guess_type(buf.name)[0] or "application/octet-stream"
        size = int(buf.size)
        timeout = max(300.0, size / (64 * 1024) + 60)
        log.info("uploading %s via WebDAV PUT (%d bytes)", buf.path, size)
        if size <= 0:
            self._client.put(buf.path, b"", size=0, content_type=mime, timeout=timeout)
        else:
            self._client.put(
                buf.path,
                buf.fileobj(),
                size=size,
                content_type=mime,
                timeout=timeout,
            )
        return FsEntry(
            path=buf.path,
            name=buf.name,
            is_dir=False,
            size=size,
            mtime=time.time(),
            file_id="",
        )

    def _upload_buffer(self, buf: _WriteBuf) -> FsEntry | None:
        assert isinstance(self._client, TeldriveClient)
        mime = mimetypes.guess_type(buf.name)[0] or "application/octet-stream"
        size = int(buf.size)
        upload_id = ""
        channel_id = self._channel_id

        if size > 0:
            upload_id = uuid.uuid4().hex
            sent = 0
            part_no = 1
            data = buf.read_all()
            while sent < size:
                chunk = data[sent : sent + self._chunk_size]
                part_name = uuid.uuid4().hex
                log.info(
                    "uploading %s part %d (%d bytes)",
                    buf.path,
                    part_no,
                    len(chunk),
                )
                self._client.upload_part(
                    upload_id,
                    chunk,
                    file_name=buf.name,
                    part_no=part_no,
                    part_name=part_name,
                    channel_id=channel_id,
                    encrypted=False,
                    hashing=False,
                    timeout=max(300.0, len(chunk) / (64 * 1024) + 60),
                )
                sent += len(chunk)
                part_no += 1

        parent_id = self._client._resolve_id(buf.parent_path)  # noqa: SLF001
        if parent_id == "":
            parent_id = None

        if buf.file_id:
            updated = self._client.update_file(
                buf.file_id,
                name=buf.name,
                size=size,
                upload_id=upload_id or None,
                parent_id=parent_id,
                channel_id=channel_id or None,
                encrypted=False,
            )
            if updated is None:
                return FsEntry(
                    path=buf.path,
                    name=buf.name,
                    is_dir=False,
                    size=size,
                    mtime=time.time(),
                    file_id=buf.file_id,
                )
            # Ensure mount path stays stable even if API returns odd path.
            return FsEntry(
                path=buf.path,
                name=buf.name,
                is_dir=False,
                size=size,
                mtime=updated.mtime or time.time(),
                file_id=updated.file_id or buf.file_id,
            )

        created = self._client.create_file(
            name=buf.name,
            parent_path=buf.parent_path,
            size=size,
            upload_id=upload_id,
            mime_type=mime,
            channel_id=channel_id,
            encrypted=False,
            parent_id=parent_id,
        )
        if created is None:
            return FsEntry(
                path=buf.path,
                name=buf.name,
                is_dir=False,
                size=size,
                mtime=time.time(),
                file_id="",
            )
        return FsEntry(
            path=buf.path,
            name=buf.name,
            is_dir=False,
            size=size,
            mtime=created.mtime or time.time(),
            file_id=created.file_id,
        )

    def mkdir(self, path: str) -> None:
        path = _norm(path)
        self._client.mkdir(path)
        self._invalidate(path)

    def unlink(self, path: str) -> None:
        path = _norm(path)
        self.abort(path)
        entry = self._meta.stat(path)
        if entry is None:
            # Still try resolve once from API.
            entry = self._client.stat(path)
        if entry is None:
            raise FileNotFoundError(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        if self._is_webdav():
            assert isinstance(self._client, WebDAVClient)
            self._client.delete(path, is_dir=False)
        else:
            assert isinstance(self._client, TeldriveClient)
            if not entry.file_id:
                raise OSError("missing file id")
            self._client.delete_ids([entry.file_id])
        self._invalidate(path)

    def rmdir(self, path: str) -> None:
        path = _norm(path)
        if path == "/":
            raise OSError("cannot remove root")
        children = self._meta.list_dir(path)
        if children:
            raise OSError("directory not empty")
        entry = self._meta.stat(path) or self._client.stat(path)
        if entry is None or not entry.is_dir:
            raise FileNotFoundError(path)
        if self._is_webdav():
            assert isinstance(self._client, WebDAVClient)
            self._client.delete(path, is_dir=True)
        else:
            assert isinstance(self._client, TeldriveClient)
            if not entry.file_id:
                raise OSError("missing folder id")
            self._client.delete_ids([entry.file_id])
        self._invalidate(path)

    def rename(self, src: str, dst: str) -> None:
        src = _norm(src)
        dst = _norm(dst)
        if src == dst:
            return
        # Flush pending writes on source first.
        if self.get(src) is not None:
            self.commit(src)

        entry = self._meta.stat(src) or self._client.stat(src)
        if entry is None:
            raise FileNotFoundError(src)

        if self._is_webdav():
            assert isinstance(self._client, WebDAVClient)
            self._client.move_path(src, dst, overwrite=True, is_dir=entry.is_dir)
        else:
            assert isinstance(self._client, TeldriveClient)
            if not entry.file_id:
                raise FileNotFoundError(src)
            dest_parent = _parent(dst)
            dest_name = _name(dst)
            self._client.move(
                [entry.file_id],
                destination_parent=dest_parent,
                destination_name=dest_name if dest_name != entry.name else None,
            )
        self._invalidate(src)
        self._invalidate(dst)
