from __future__ import annotations

import errno
import logging
import os
import stat
from typing import Any

from functools import partial

import pyfuse3
import trio

from .cache import BlockCache, MetaCache
from .storage import StorageClient
from .write import WriteStore

log = logging.getLogger("raidrive.fs")


class RaidriveFS(pyfuse3.Operations):
    """FUSE filesystem (Teldrive or WebDAV) with optional write-back."""

    def __init__(
        self,
        client: StorageClient,
        meta: MetaCache,
        blocks: BlockCache,
        writes: WriteStore | None = None,
        *,
        dir_mode: int = 0o555,
        file_mode: int = 0o444,
        uid: int | None = None,
        gid: int | None = None,
        read_only: bool = True,
    ):
        super().__init__()
        self._client = client
        self._meta = meta
        self._blocks = blocks
        self._writes = writes
        self._dir_mode = dir_mode & 0o777
        self._file_mode = file_mode & 0o777
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid
        self._read_only = read_only
        self._inode_to_path: dict[int, str] = {pyfuse3.ROOT_INODE: "/"}
        self._path_to_inode: dict[str, int] = {"/": pyfuse3.ROOT_INODE}
        self._next_inode = pyfuse3.ROOT_INODE + 1
        self._inode_lock = trio.Lock()
        self._open_handles: dict[int, dict[str, Any]] = {}
        self._next_fh = 1

    def _deny_if_ro(self) -> None:
        if self._read_only or self._writes is None:
            raise pyfuse3.FUSEError(errno.EROFS)

    def _norm(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        return path or "/"

    async def _inode_for(self, path: str) -> int:
        path = self._norm(path)
        async with self._inode_lock:
            inode = self._path_to_inode.get(path)
            if inode is not None:
                return inode
            inode = self._next_inode
            self._next_inode += 1
            self._path_to_inode[path] = inode
            self._inode_to_path[inode] = path
            return inode

    async def _path_for(self, inode: int) -> str:
        async with self._inode_lock:
            path = self._inode_to_path.get(inode)
        if path is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return path

    async def _forget_inode_path(self, path: str) -> None:
        path = self._norm(path)
        async with self._inode_lock:
            inode = self._path_to_inode.pop(path, None)
            if inode is not None and inode != pyfuse3.ROOT_INODE:
                self._inode_to_path.pop(inode, None)

    def _attrs_for(self, inode: int, path: str, entry) -> pyfuse3.EntryAttributes:
        attrs = pyfuse3.EntryAttributes()
        attrs.st_ino = inode
        attrs.generation = 1
        attrs.entry_timeout = 5 if not self._read_only else 30
        attrs.attr_timeout = 5 if not self._read_only else 30
        attrs.st_uid = self._uid
        attrs.st_gid = self._gid
        attrs.st_atime_ns = int(entry.mtime * 1e9)
        attrs.st_mtime_ns = int(entry.mtime * 1e9)
        attrs.st_ctime_ns = int(entry.mtime * 1e9)
        attrs.st_blksize = self._blocks.block_size
        if entry.is_dir:
            attrs.st_mode = stat.S_IFDIR | self._dir_mode
            attrs.st_nlink = 2
            attrs.st_size = 0
        else:
            attrs.st_mode = stat.S_IFREG | self._file_mode
            attrs.st_nlink = 1
            # Prefer dirty local write buffer size when present.
            size = entry.size
            if self._writes is not None:
                buf = self._writes.get(path)
                if buf is not None:
                    size = buf.size
            attrs.st_size = size
            attrs.st_blocks = (size + 511) // 512
        return attrs

    async def getattr(self, inode: int, ctx=None) -> pyfuse3.EntryAttributes:
        path = await self._path_for(inode)
        if self._writes is not None:
            buf = self._writes.get(path)
            if buf is not None:
                from .entry import FsEntry

                entry = FsEntry(
                    path=path,
                    name=buf.name,
                    is_dir=False,
                    size=buf.size,
                    mtime=0.0,
                    file_id=buf.file_id or "",
                )
                return self._attrs_for(inode, path, entry)
        entry = await trio.to_thread.run_sync(self._meta.stat, path)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attrs_for(inode, path, entry)

    async def lookup(self, parent_inode: int, name: bytes, ctx=None) -> pyfuse3.EntryAttributes:
        parent = await self._path_for(parent_inode)
        child_name = os.fsdecode(name)
        if child_name in (".", ".."):
            raise pyfuse3.FUSEError(errno.ENOENT)
        child_path = self._norm("/" + child_name if parent == "/" else f"{parent}/{child_name}")

        entry = None
        try:
            children = await trio.to_thread.run_sync(self._meta.list_dir, parent)
            entry = next((c for c in children if c.name == child_name or c.path == child_path), None)
        except Exception:
            log.debug("list_dir during lookup failed for %s", parent, exc_info=True)

        if entry is None:
            try:
                entry = await trio.to_thread.run_sync(self._meta.stat, child_path)
            except Exception:
                log.debug("stat during lookup failed for %s", child_path, exc_info=True)
                raise pyfuse3.FUSEError(errno.ENOENT)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        inode = await self._inode_for(entry.path)
        return self._attrs_for(inode, entry.path, entry)

    async def opendir(self, inode: int, ctx) -> Any:
        path = await self._path_for(inode)
        entry = await trio.to_thread.run_sync(self._meta.stat, path)
        if entry is None or not entry.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, inode: int, start_id: int, token: Any) -> None:
        path = await self._path_for(inode)
        children = await trio.to_thread.run_sync(self._meta.list_dir, path)
        for idx, child in enumerate(children, start=1):
            if idx <= start_id:
                continue
            child_inode = await self._inode_for(child.path)
            attrs = self._attrs_for(child_inode, child.path, child)
            if not pyfuse3.readdir_reply(token, os.fsencode(child.name), attrs, idx):
                break

    async def open(self, inode: int, flags: int, ctx) -> pyfuse3.FileInfo:
        path = await self._path_for(inode)
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND))
        trunc = bool(flags & os.O_TRUNC)
        if writing or trunc:
            self._deny_if_ro()
            assert self._writes is not None
            try:
                await trio.to_thread.run_sync(
                    partial(self._writes.begin_open, path, truncate=trunc)
                )
            except FileNotFoundError:
                raise pyfuse3.FUSEError(errno.ENOENT)
            except Exception:
                log.exception("open for write failed %s", path)
                raise pyfuse3.FUSEError(errno.EIO)

        entry = await trio.to_thread.run_sync(self._meta.stat, path)
        if entry is None and not writing:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if entry is not None and entry.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR)

        fh = self._next_fh
        self._next_fh += 1
        self._open_handles[fh] = {"path": path, "writable": writing or trunc}
        info = pyfuse3.FileInfo(fh=fh)
        info.direct_io = writing or trunc
        info.keep_cache = not (writing or trunc)
        return info

    async def create(
        self, parent_inode: int, name: bytes, mode, flags, ctx
    ) -> tuple[pyfuse3.EntryAttributes, pyfuse3.FileInfo]:
        self._deny_if_ro()
        assert self._writes is not None
        parent = await self._path_for(parent_inode)
        child_name = os.fsdecode(name)
        path = self._norm("/" + child_name if parent == "/" else f"{parent}/{child_name}")

        try:
            await trio.to_thread.run_sync(self._writes.begin_create, path)
        except Exception:
            log.exception("create failed %s", path)
            raise pyfuse3.FUSEError(errno.EIO)

        from .entry import FsEntry
        import time as _time

        entry = FsEntry(path=path, name=child_name, is_dir=False, size=0, mtime=_time.time(), file_id="")
        inode = await self._inode_for(path)
        attrs = self._attrs_for(inode, path, entry)
        fh = self._next_fh
        self._next_fh += 1
        self._open_handles[fh] = {"path": path, "writable": True}
        info = pyfuse3.FileInfo(fh=fh)
        info.direct_io = True
        info.keep_cache = False
        return attrs, info

    async def read(self, fh: int, off: int, size: int) -> bytes:
        handle = self._open_handles.get(fh)
        if handle is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        path = handle["path"]
        if self._writes is not None:
            buf = self._writes.get(path)
            if buf is not None:
                return buf.read_at(off, size)
        entry = await trio.to_thread.run_sync(self._meta.stat, path)
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return await trio.to_thread.run_sync(
            self._blocks.read, path, off, size, entry.size
        )

    async def write(self, fh: int, off: int, buf: bytes) -> int:
        self._deny_if_ro()
        assert self._writes is not None
        handle = self._open_handles.get(fh)
        if handle is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        path = handle["path"]
        try:
            return await trio.to_thread.run_sync(self._writes.write_at, path, off, buf)
        except FileNotFoundError:
            raise pyfuse3.FUSEError(errno.EBADF)
        except Exception:
            log.exception("write failed %s", path)
            raise pyfuse3.FUSEError(errno.EIO)

    async def release(self, fh: int) -> None:
        handle = self._open_handles.pop(fh, None)
        if handle is None:
            return
        path = handle["path"]
        if handle.get("writable") and self._writes is not None:
            try:
                await trio.to_thread.run_sync(self._writes.commit, path)
            except Exception:
                log.exception("commit on release failed %s", path)
                raise pyfuse3.FUSEError(errno.EIO)

    async def flush(self, fh: int) -> None:
        return

    async def releasedir(self, inode: int) -> None:
        return

    async def mkdir(self, parent_inode: int, name: bytes, mode, ctx) -> pyfuse3.EntryAttributes:
        self._deny_if_ro()
        assert self._writes is not None
        parent = await self._path_for(parent_inode)
        child_name = os.fsdecode(name)
        path = self._norm("/" + child_name if parent == "/" else f"{parent}/{child_name}")
        try:
            await trio.to_thread.run_sync(self._writes.mkdir, path)
        except Exception:
            log.exception("mkdir failed %s", path)
            raise pyfuse3.FUSEError(errno.EIO)
        from .entry import FsEntry
        import time as _time

        entry = FsEntry(path=path, name=child_name, is_dir=True, size=0, mtime=_time.time(), file_id="")
        inode = await self._inode_for(path)
        return self._attrs_for(inode, path, entry)

    async def unlink(self, parent_inode: int, name: bytes, ctx) -> None:
        self._deny_if_ro()
        assert self._writes is not None
        parent = await self._path_for(parent_inode)
        child_name = os.fsdecode(name)
        path = self._norm("/" + child_name if parent == "/" else f"{parent}/{child_name}")
        try:
            await trio.to_thread.run_sync(self._writes.unlink, path)
        except FileNotFoundError:
            raise pyfuse3.FUSEError(errno.ENOENT)
        except IsADirectoryError:
            raise pyfuse3.FUSEError(errno.EISDIR)
        except Exception:
            log.exception("unlink failed %s", path)
            raise pyfuse3.FUSEError(errno.EIO)
        await self._forget_inode_path(path)

    async def rmdir(self, parent_inode: int, name: bytes, ctx) -> None:
        self._deny_if_ro()
        assert self._writes is not None
        parent = await self._path_for(parent_inode)
        child_name = os.fsdecode(name)
        path = self._norm("/" + child_name if parent == "/" else f"{parent}/{child_name}")
        try:
            await trio.to_thread.run_sync(self._writes.rmdir, path)
        except FileNotFoundError:
            raise pyfuse3.FUSEError(errno.ENOENT)
        except OSError as exc:
            if "not empty" in str(exc).lower():
                raise pyfuse3.FUSEError(errno.ENOTEMPTY)
            raise pyfuse3.FUSEError(errno.EIO)
        except Exception:
            log.exception("rmdir failed %s", path)
            raise pyfuse3.FUSEError(errno.EIO)
        await self._forget_inode_path(path)

    async def rename(
        self,
        parent_inode: int,
        name: bytes,
        new_parent_inode: int,
        new_name: bytes,
        flags,
        ctx,
    ) -> None:
        self._deny_if_ro()
        assert self._writes is not None
        parent = await self._path_for(parent_inode)
        new_parent = await self._path_for(new_parent_inode)
        src_name = os.fsdecode(name)
        dst_name = os.fsdecode(new_name)
        src = self._norm("/" + src_name if parent == "/" else f"{parent}/{src_name}")
        dst = self._norm("/" + dst_name if new_parent == "/" else f"{new_parent}/{dst_name}")
        try:
            await trio.to_thread.run_sync(self._writes.rename, src, dst)
        except FileNotFoundError:
            raise pyfuse3.FUSEError(errno.ENOENT)
        except Exception:
            log.exception("rename failed %s -> %s", src, dst)
            raise pyfuse3.FUSEError(errno.EIO)
        await self._forget_inode_path(src)
        await self._inode_for(dst)

    async def setattr(self, inode, attr, fields, fh, ctx):
        path = await self._path_for(inode)
        if fields.update_size:
            self._deny_if_ro()
            assert self._writes is not None
            if self._writes.get(path) is None:
                try:
                    await trio.to_thread.run_sync(
                        partial(self._writes.begin_open, path, truncate=False)
                    )
                except FileNotFoundError:
                    raise pyfuse3.FUSEError(errno.ENOENT)
            try:
                await trio.to_thread.run_sync(self._writes.truncate, path, attr.st_size)
            except Exception:
                log.exception("truncate failed %s", path)
                raise pyfuse3.FUSEError(errno.EIO)
        return await self.getattr(inode)

    async def mknod(self, *args, **kwargs):
        self._deny_if_ro()
        raise pyfuse3.FUSEError(errno.ENOSYS)

    async def symlink(self, *args, **kwargs):
        raise pyfuse3.FUSEError(errno.ENOSYS)

    async def link(self, *args, **kwargs):
        raise pyfuse3.FUSEError(errno.ENOSYS)

    async def fsync(self, *args, **kwargs):
        return

    async def fsyncdir(self, *args, **kwargs):
        return
