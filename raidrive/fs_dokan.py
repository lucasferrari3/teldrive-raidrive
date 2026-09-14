from __future__ import annotations

import faulthandler
import logging
import os
import queue
import signal
import sys
import threading
import traceback
from ctypes import (
    byref,
    c_char,
    c_void_p,
    c_wchar,
    c_wchar_p,
    cast,
    create_unicode_buffer,
    memmove,
    sizeof,
    string_at,
)
from functools import wraps
from typing import Any, Callable

from .cache import CACHE_MISS, BlockCache, MetaCache
from .dokan_api import (
    BY_HANDLE_FILE_INFORMATION,
    CREATE_ALWAYS,
    CREATE_NEW,
    DOKAN_OPERATIONS,
    DOKAN_OPTION_DEBUG,
    DOKAN_OPTION_MOUNT_MANAGER,
    DOKAN_OPTION_STDERR,
    DOKAN_OPTION_WRITE_PROTECT,
    DOKAN_OPTIONS,
    DOKAN_SUCCESS,
    DOKAN_VERSION,
    FILE_ATTRIBUTE_ARCHIVE,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_NORMAL,
    FILE_ATTRIBUTE_READONLY,
    FILE_CASE_PRESERVED_NAMES,
    FILE_CREATE,
    FILE_DELETE_ON_CLOSE,
    FILE_DIRECTORY_FILE,
    FILE_NON_DIRECTORY_FILE,
    FILE_OPEN,
    FILE_OPEN_IF,
    FILE_OVERWRITE,
    FILE_OVERWRITE_IF,
    FILE_PERSISTENT_ACLS,
    FILE_READ_ONLY_VOLUME,
    FILE_SUPERSEDE,
    FILE_UNICODE_ON_DISK,
    MAX_PATH,
    OPEN_ALWAYS,
    OPEN_EXISTING,
    STATUS_ACCESS_DENIED,
    STATUS_DIRECTORY_NOT_EMPTY,
    STATUS_FILE_IS_A_DIRECTORY,
    STATUS_MEDIA_WRITE_PROTECTED,
    STATUS_NOT_A_DIRECTORY,
    STATUS_OBJECT_NAME_COLLISION,
    STATUS_OBJECT_NAME_NOT_FOUND,
    STATUS_OBJECT_PATH_NOT_FOUND,
    STATUS_SUCCESS,
    TRUNCATE_EXISTING,
    WIN32_FIND_DATAW,
    CleanupProto,
    CloseFileProto,
    DeleteDirectoryProto,
    DeleteFileProto,
    FindFilesProto,
    FlushFileBuffersProto,
    GetDiskFreeSpaceProto,
    GetFileInformationProto,
    GetVolumeInformationProto,
    MountedProto,
    MoveFileProto,
    ReadFileProto,
    SetAllocationSizeProto,
    SetEndOfFileProto,
    SetFileAttributesProto,
    SetFileTimeProto,
    UnmountedProto,
    WriteFileProto,
    ZwCreateFileProto,
    dokan_error_name,
    get_dokan,
    unix_to_filetime,
)
from .entry import FsEntry
from .storage import StorageClient
from .write import WriteStore

log = logging.getLogger("raidrive.dokan")

# Windows probes these on every mount; never hit Teldrive for them.
_IGNORED_PREFIXES = (
    "/system volume information",
    "/$recycle.bin",
    "/recycler",
    "/.raidrive",
    "/pagefile.sys",
    "/hiberfil.sys",
    "/swapfile.sys",
)

# Media players probe these beside videos; skip without API.
_IGNORED_SUFFIXES = (
    ".xchp",
    ".disc",
    ".dvdmedia",
)
_IGNORED_NAMES = {
    "bdmv",
    "video_ts",
    "certificate",
    "subtitles",
    "subs",
    "extrafanart",
    "extrathumbs",
    ".actors",
    "trailer",
}


def _win_to_posix(path: str) -> str:
    if not path or path in ("\\", "/"):
        return "/"
    p = str(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    return p or "/"


def _is_ignored_path(path: str) -> bool:
    low = path.lower()
    if any(low == p or low.startswith(p + "/") for p in _IGNORED_PREFIXES):
        return True
    if any(low.endswith(suf) for suf in _IGNORED_SUFFIXES):
        return True
    name = low.rsplit("/", 1)[-1]
    if name in _IGNORED_NAMES:
        return True
    # /foo/BDMV/... or /foo/VIDEO_TS/...
    parts = low.split("/")
    if any(part in _IGNORED_NAMES for part in parts):
        return True
    return False


class _PyBridge:
    """Run callables on dedicated Python worker threads.

    Dokany invokes WINFUNCTYPE callbacks from native threads. Doing HTTP/alloc
    there under Python 3.13 can hard-crash. Cache hits can stay on the Dokany
    thread; API/I-O goes through this pool.
    """

    def __init__(self, workers: int = 4, name: str = "dokan-pybridge"):
        self._q: queue.Queue = queue.Queue()
        self._stop = object()
        self._threads: list[threading.Thread] = []
        n = max(1, int(workers))
        for i in range(n):
            t = threading.Thread(target=self._loop, name=f"{name}-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is self._stop:
                return
            fn, args, kwargs, box, event = item
            try:
                box["ok"] = fn(*args, **kwargs)
            except BaseException as exc:
                box["err"] = exc
            finally:
                event.set()

    def call(self, fn: Callable, *args, timeout: float = 300.0, **kwargs) -> Any:
        box: dict[str, Any] = {}
        event = threading.Event()
        self._q.put((fn, args, kwargs, box, event))
        if not event.wait(timeout):
            raise TimeoutError(f"Dokany Python bridge timed out calling {fn!r}")
        if "err" in box:
            raise box["err"]
        return box.get("ok")

    def close(self) -> None:
        for _ in self._threads:
            self._q.put(self._stop)
        for t in self._threads:
            t.join(timeout=5)


def _safe_ntstatus(default: int = STATUS_ACCESS_DENIED):
    def deco(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                log.error(
                    "Dokany callback %s crashed:\n%s",
                    fn.__name__,
                    traceback.format_exc(),
                )
                return default

        return wrapper

    return deco


def _safe_void(fn: Callable):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.error(
                "Dokany callback %s crashed:\n%s",
                fn.__name__,
                traceback.format_exc(),
            )

    return wrapper


class DokanTeldriveFS:
    """Read-oriented Teldrive filesystem exposed via Dokany on Windows."""

    def __init__(
        self,
        client: StorageClient,
        meta: MetaCache,
        blocks: BlockCache,
        writes: WriteStore | None = None,
        *,
        read_only: bool = True,
        volume_name: str = "Teldrive",
        timeout_ms: int = 300_000,
    ):
        self._client = client
        self._meta = meta
        self._blocks = blocks
        self._writes = writes
        self._read_only = read_only
        self._volume_name = volume_name
        self._timeout_ms = max(timeout_ms, 60_000)
        self._serial = 0x54444C56  # 'TDLV'
        self._lock = threading.Lock()
        # path, size, writable
        self._handles: dict[int, tuple[str, int, bool]] = {}
        self._next_ctx = 1
        self._callbacks: list = []
        workers = int(os.environ.get("DOKAN_BRIDGE_WORKERS", "4"))
        self._bridge = _PyBridge(workers=workers)
        self._ops = DOKAN_OPERATIONS()
        self._bind_ops()

    def _keep(self, cb):
        self._callbacks.append(cb)
        return cb

    def _bind_ops(self) -> None:
        o = self._ops
        o.ZwCreateFile = self._keep(ZwCreateFileProto(self._zw_create_file))
        o.Cleanup = self._keep(CleanupProto(self._cleanup))
        o.CloseFile = self._keep(CloseFileProto(self._close_file))
        o.ReadFile = self._keep(ReadFileProto(self._read_file))
        o.WriteFile = self._keep(WriteFileProto(self._write_file))
        o.FlushFileBuffers = self._keep(FlushFileBuffersProto(self._flush))
        o.GetFileInformation = self._keep(
            GetFileInformationProto(self._get_file_information)
        )
        o.FindFiles = self._keep(FindFilesProto(self._find_files))
        o.SetFileAttributes = self._keep(SetFileAttributesProto(self._deny_set_attr))
        o.SetFileTime = self._keep(SetFileTimeProto(self._deny_set_time))
        o.DeleteFile = self._keep(DeleteFileProto(self._delete_file))
        o.DeleteDirectory = self._keep(DeleteDirectoryProto(self._delete_directory))
        o.MoveFile = self._keep(MoveFileProto(self._move_file))
        o.SetEndOfFile = self._keep(SetEndOfFileProto(self._set_end_of_file))
        o.SetAllocationSize = self._keep(SetAllocationSizeProto(self._set_end_of_file))
        o.GetDiskFreeSpace = self._keep(GetDiskFreeSpaceProto(self._get_disk_free_space))
        o.GetVolumeInformation = self._keep(
            GetVolumeInformationProto(self._get_volume_information)
        )
        o.Mounted = self._keep(MountedProto(self._mounted))
        o.Unmounted = self._keep(UnmountedProto(self._unmounted))

    def _alloc_ctx(self, path: str, size: int, writable: bool = False) -> int:
        with self._lock:
            ctx = self._next_ctx
            self._next_ctx += 1
            self._handles[ctx] = (path, size, writable)
            return ctx

    def _free_ctx(self, ctx: int) -> tuple[str, int, bool] | None:
        with self._lock:
            return self._handles.pop(ctx, None)

    def _lookup(self, path: str) -> FsEntry | None:
        """Must run on the Python bridge thread for API misses."""
        try:
            if self._writes is not None:
                buf = self._writes.get(path)
                if buf is not None:
                    return FsEntry(
                        path=path,
                        name=buf.name,
                        is_dir=False,
                        size=buf.size,
                        mtime=0.0,
                        file_id=buf.file_id or "",
                    )
            return self._meta.stat(path)
        except Exception:
            log.debug("stat failed for %s", path, exc_info=True)
            return None

    def _py_lookup(self, path: str) -> FsEntry | None:
        # Cache hit (incl. parent-listing / negative): no bridge, no HTTP.
        peeked = self._meta.peek_stat(path)
        if peeked is not CACHE_MISS:
            return peeked  # type: ignore[return-value]
        return self._bridge.call(self._lookup, path, timeout=self._timeout_ms / 1000.0)

    def _py_list(self, path: str) -> list[FsEntry]:
        cached = self._meta.peek_list(path)
        if cached is not None:
            return cached
        return self._bridge.call(
            self._meta.list_dir, path, timeout=self._timeout_ms / 1000.0
        )

    def _py_read(self, path: str, offset: int, size: int, file_size: int) -> bytes:
        # Hot path: serve entirely from RAM without queueing a bridge worker.
        # This is what keeps streaming smooth once prefetch has filled ahead.
        try:
            hit = self._blocks.try_read_ram(path, offset, size, file_size)
        except Exception:
            hit = None
        if hit is not None:
            return hit
        return self._bridge.call(
            self._blocks.read,
            path,
            offset,
            size,
            file_size,
            timeout=self._timeout_ms / 1000.0,
        )

    def _mutating_open(
        self, create_disposition: int, create_options: int, user_disp: int
    ) -> bool:
        if create_options & FILE_DELETE_ON_CLOSE:
            return True
        if create_disposition in (
            FILE_SUPERSEDE,
            FILE_CREATE,
            FILE_OVERWRITE,
            FILE_OVERWRITE_IF,
        ):
            return True
        if user_disp in (CREATE_NEW, CREATE_ALWAYS, TRUNCATE_EXISTING):
            return True
        return False

    def _bump_timeout(self, dokan_file_info) -> None:
        try:
            get_dokan().DokanResetTimeout(self._timeout_ms, dokan_file_info)
        except Exception:
            pass

    @_safe_ntstatus(STATUS_ACCESS_DENIED)
    def _zw_create_file(
        self,
        file_name,
        _security,
        desired_access,
        _file_attrs,
        _share,
        create_disposition,
        create_options,
        dokan_file_info,
    ):
        info = dokan_file_info.contents
        path = _win_to_posix(file_name)
        if _is_ignored_path(path):
            return STATUS_OBJECT_NAME_NOT_FOUND

        disp_map = {
            FILE_SUPERSEDE: CREATE_ALWAYS,
            FILE_OPEN: OPEN_EXISTING,
            FILE_CREATE: CREATE_NEW,
            FILE_OPEN_IF: OPEN_ALWAYS,
            FILE_OVERWRITE: TRUNCATE_EXISTING,
            FILE_OVERWRITE_IF: CREATE_ALWAYS,
        }
        user_disp = disp_map.get(int(create_disposition), OPEN_EXISTING)
        is_dir_request = bool(create_options & FILE_DIRECTORY_FILE) or bool(
            info.IsDirectory
        )
        wants_write = self._mutating_open(
            create_disposition, create_options, user_disp
        ) or bool(desired_access & (0x0002 | 0x0004))  # WRITE_DATA | APPEND_DATA

        if self._read_only and wants_write:
            return STATUS_MEDIA_WRITE_PROTECTED

        self._bump_timeout(dokan_file_info)
        entry = self._py_lookup(path)
        writable = False
        created_new = False

        if entry is None:
            if path == "/":
                entry = FsEntry("/", "", True, 0, 0.0, "")
            elif user_disp in (OPEN_EXISTING, TRUNCATE_EXISTING):
                return STATUS_OBJECT_NAME_NOT_FOUND
            elif user_disp in (CREATE_NEW, CREATE_ALWAYS, OPEN_ALWAYS):
                if self._read_only or self._writes is None:
                    return STATUS_MEDIA_WRITE_PROTECTED
                if is_dir_request or (create_options & FILE_DIRECTORY_FILE):
                    try:
                        self._bridge.call(
                            self._writes.mkdir,
                            path,
                            timeout=self._timeout_ms / 1000.0,
                        )
                    except Exception:
                        log.exception("mkdir failed %s", path)
                        return STATUS_ACCESS_DENIED
                    entry = FsEntry(path, path.rsplit("/", 1)[-1], True, 0, 0.0, "")
                    created_new = True
                else:
                    try:
                        self._bridge.call(
                            self._writes.begin_create,
                            path,
                            timeout=self._timeout_ms / 1000.0,
                        )
                    except Exception:
                        log.exception("create failed %s", path)
                        return STATUS_ACCESS_DENIED
                    entry = FsEntry(path, path.rsplit("/", 1)[-1], False, 0, 0.0, "")
                    writable = True
                    created_new = True
            else:
                return STATUS_OBJECT_NAME_NOT_FOUND
        elif user_disp == CREATE_NEW:
            return STATUS_OBJECT_NAME_COLLISION

        if entry.is_dir:
            if create_options & FILE_NON_DIRECTORY_FILE:
                return STATUS_FILE_IS_A_DIRECTORY
            info.IsDirectory = 1
        else:
            if is_dir_request or (create_options & FILE_DIRECTORY_FILE):
                return STATUS_NOT_A_DIRECTORY
            info.IsDirectory = 0
            trunc = user_disp in (TRUNCATE_EXISTING, CREATE_ALWAYS) or (
                int(create_disposition) in (FILE_OVERWRITE, FILE_OVERWRITE_IF, FILE_SUPERSEDE)
            )
            # Only hydrate/truncate on overwrite. Plain write opens stay lazy
            # until the first WriteFile (avoids downloading huge videos on open).
            if (
                not created_new
                and not entry.is_dir
                and self._writes is not None
                and not self._read_only
                and trunc
            ):
                try:
                    from functools import partial

                    self._bridge.call(
                        partial(self._writes.begin_open, path, truncate=True),
                        timeout=self._timeout_ms / 1000.0,
                    )
                    writable = True
                    entry = FsEntry(
                        path, entry.name, False, 0, entry.mtime, entry.file_id
                    )
                except FileNotFoundError:
                    return STATUS_OBJECT_NAME_NOT_FOUND
                except Exception:
                    log.exception("open for overwrite failed %s", path)
                    return STATUS_ACCESS_DENIED
            elif (
                not created_new
                and not entry.is_dir
                and self._writes is not None
                and not self._read_only
                and wants_write
            ):
                # May write later; commit only if a buffer was created.
                writable = True

        ctx = self._alloc_ctx(
            path, entry.size if not entry.is_dir else 0, writable=writable
        )
        info.Context = ctx

        if created_new and user_disp in (OPEN_ALWAYS, CREATE_ALWAYS):
            return STATUS_SUCCESS
        if (not created_new) and user_disp in (OPEN_ALWAYS, CREATE_ALWAYS):
            return STATUS_OBJECT_NAME_COLLISION
        return STATUS_SUCCESS

    @_safe_void
    def _cleanup(self, file_name, dokan_file_info):
        info = dokan_file_info.contents
        path = _win_to_posix(file_name)
        if info.DeletePending and not self._read_only and self._writes is not None:
            try:
                if info.IsDirectory:
                    self._bridge.call(
                        self._writes.rmdir, path, timeout=self._timeout_ms / 1000.0
                    )
                else:
                    self._bridge.call(
                        self._writes.unlink, path, timeout=self._timeout_ms / 1000.0
                    )
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("cleanup delete failed %s: %s", path, exc)
            except Exception:
                log.exception("cleanup delete failed %s", path)

    @_safe_void
    def _close_file(self, file_name, dokan_file_info):
        info = dokan_file_info.contents
        handle = None
        if info.Context:
            handle = self._free_ctx(int(info.Context))
            info.Context = 0
        if handle is None:
            return
        path, _size, writable = handle
        if writable and self._writes is not None and not info.DeletePending:
            if self._writes.get(path) is None:
                return
            try:
                self._bridge.call(
                    self._writes.commit, path, timeout=self._timeout_ms / 1000.0
                )
            except Exception:
                log.exception("commit on close failed %s", path)

    @_safe_ntstatus(STATUS_ACCESS_DENIED)
    def _read_file(
        self, file_name, buffer, buffer_length, read_length, offset, dokan_file_info
    ):
        self._bump_timeout(dokan_file_info)
        info = dokan_file_info.contents
        path = None
        size = 0
        writable = False
        if info.Context:
            with self._lock:
                handle = self._handles.get(int(info.Context))
            if handle:
                path, size, writable = handle
        if path is None:
            path = _win_to_posix(file_name)
            entry = self._py_lookup(path)
            if entry is None or entry.is_dir:
                read_length[0] = 0
                return STATUS_OBJECT_PATH_NOT_FOUND
            size = int(entry.size)

        offset = int(offset)
        if offset < 0:
            read_length[0] = 0
            return STATUS_ACCESS_DENIED

        # Prefer local write buffer when open for write.
        if self._writes is not None:
            buf = self._writes.get(path)
            if buf is not None:
                size = buf.size
                if offset >= size:
                    read_length[0] = 0
                    return STATUS_SUCCESS
                to_read = min(int(buffer_length), size - offset)
                data = buf.read_at(offset, to_read)
                n = len(data)
                if n > 0:
                    src = (c_char * n).from_buffer_copy(data)
                    memmove(buffer, src, n)
                read_length[0] = n
                return STATUS_SUCCESS

        if offset >= size:
            read_length[0] = 0
            return STATUS_SUCCESS

        to_read = min(int(buffer_length), size - offset)
        if to_read <= 0:
            read_length[0] = 0
            return STATUS_SUCCESS

        log.debug("ReadFile %s off=%d len=%d size=%d", path, offset, to_read, size)
        self._bump_timeout(dokan_file_info)
        data = self._py_read(path, offset, to_read, size)
        self._bump_timeout(dokan_file_info)

        n = len(data)
        if n > int(buffer_length):
            n = int(buffer_length)
        if n > 0:
            src = (c_char * n).from_buffer_copy(data[:n])
            memmove(buffer, src, n)
        read_length[0] = n
        return STATUS_SUCCESS

    @_safe_ntstatus(STATUS_MEDIA_WRITE_PROTECTED)
    def _write_file(
        self, file_name, buffer, num_bytes, written, offset, dokan_file_info
    ):
        written[0] = 0
        if self._read_only or self._writes is None:
            return STATUS_MEDIA_WRITE_PROTECTED

        info = dokan_file_info.contents
        path = _win_to_posix(file_name)
        if info.Context:
            with self._lock:
                handle = self._handles.get(int(info.Context))
            if handle:
                path = handle[0]

        n = int(num_bytes)
        if n <= 0:
            return STATUS_SUCCESS
        addr = int(cast(buffer, c_void_p).value or 0)
        if not addr:
            return STATUS_ACCESS_DENIED
        data = string_at(addr, n)
        if info.WriteToEndOfFile:
            buf = self._writes.get(path)
            off = buf.size if buf is not None else 0
        else:
            off = int(offset)

        self._bump_timeout(dokan_file_info)
        try:
            wrote = self._bridge.call(
                self._writes.write_at,
                path,
                off,
                data,
                timeout=self._timeout_ms / 1000.0,
            )
        except FileNotFoundError:
            # Open for write may not have been initialized (write DesiredAccess only).
            try:
                from functools import partial

                self._bridge.call(
                    partial(self._writes.begin_open, path, truncate=False),
                    timeout=self._timeout_ms / 1000.0,
                )
                wrote = self._bridge.call(
                    self._writes.write_at,
                    path,
                    off,
                    data,
                    timeout=self._timeout_ms / 1000.0,
                )
            except Exception:
                log.exception("WriteFile failed %s", path)
                return STATUS_ACCESS_DENIED
        except Exception:
            log.exception("WriteFile failed %s", path)
            return STATUS_ACCESS_DENIED

        written[0] = int(wrote or 0)
        if info.Context:
            with self._lock:
                h = self._handles.get(int(info.Context))
                if h is not None:
                    buf = self._writes.get(path)
                    self._handles[int(info.Context)] = (
                        path,
                        buf.size if buf else h[1],
                        True,
                    )
        return STATUS_SUCCESS

    @_safe_ntstatus()
    def _flush(self, file_name, dokan_file_info):
        return STATUS_SUCCESS

    def _fill_info(self, entry: FsEntry, buf: BY_HANDLE_FILE_INFORMATION) -> None:
        if entry.is_dir:
            buf.dwFileAttributes = FILE_ATTRIBUTE_DIRECTORY
            buf.nFileSizeHigh = 0
            buf.nFileSizeLow = 0
            buf.nNumberOfLinks = 1
        else:
            attrs = FILE_ATTRIBUTE_NORMAL | FILE_ATTRIBUTE_ARCHIVE
            if self._read_only:
                attrs |= FILE_ATTRIBUTE_READONLY
            buf.dwFileAttributes = attrs
            size = max(0, int(entry.size))
            buf.nFileSizeHigh = (size >> 32) & 0xFFFFFFFF
            buf.nFileSizeLow = size & 0xFFFFFFFF
            buf.nNumberOfLinks = 1
        ft = unix_to_filetime(entry.mtime)
        buf.ftCreationTime = ft
        buf.ftLastAccessTime = ft
        buf.ftLastWriteTime = ft
        buf.dwVolumeSerialNumber = self._serial
        h = abs(hash(entry.path)) & 0xFFFFFFFFFFFFFFFF
        buf.nFileIndexHigh = (h >> 32) & 0xFFFFFFFF
        buf.nFileIndexLow = h & 0xFFFFFFFF

    @_safe_ntstatus(STATUS_OBJECT_NAME_NOT_FOUND)
    def _get_file_information(self, file_name, buffer, dokan_file_info):
        path = _win_to_posix(file_name)
        if _is_ignored_path(path):
            return STATUS_OBJECT_NAME_NOT_FOUND
        entry = self._py_lookup(path)
        if entry is None:
            if path == "/":
                entry = FsEntry("/", "", True, 0, 0.0, "")
            else:
                return STATUS_OBJECT_NAME_NOT_FOUND
        self._fill_info(entry, buffer.contents)
        return STATUS_SUCCESS

    @_safe_ntstatus(STATUS_OBJECT_PATH_NOT_FOUND)
    def _find_files(self, file_name, fill_find_data, dokan_file_info):
        path = _win_to_posix(file_name)
        if _is_ignored_path(path):
            return STATUS_OBJECT_NAME_NOT_FOUND
        self._bump_timeout(dokan_file_info)
        children = self._py_list(path)

        for child in children:
            data = WIN32_FIND_DATAW()
            if child.is_dir:
                data.dwFileAttributes = FILE_ATTRIBUTE_DIRECTORY
            else:
                attrs = FILE_ATTRIBUTE_NORMAL | FILE_ATTRIBUTE_ARCHIVE
                if self._read_only:
                    attrs |= FILE_ATTRIBUTE_READONLY
                data.dwFileAttributes = attrs
            ft = unix_to_filetime(child.mtime)
            data.ftCreationTime = ft
            data.ftLastAccessTime = ft
            data.ftLastWriteTime = ft
            size = max(0, int(child.size))
            data.nFileSizeHigh = (size >> 32) & 0xFFFFFFFF
            data.nFileSizeLow = size & 0xFFFFFFFF
            name = child.name or ""
            data.cFileName = name[: MAX_PATH - 1]
            fill_find_data(byref(data), dokan_file_info)
        return STATUS_SUCCESS

    @_safe_ntstatus(STATUS_MEDIA_WRITE_PROTECTED)
    def _deny_set_attr(self, *args):
        # Attributes are mostly cosmetic for Teldrive; allow silently when RW.
        return STATUS_SUCCESS if not self._read_only else STATUS_MEDIA_WRITE_PROTECTED

    @_safe_ntstatus(STATUS_MEDIA_WRITE_PROTECTED)
    def _deny_set_time(self, *args):
        return STATUS_SUCCESS if not self._read_only else STATUS_MEDIA_WRITE_PROTECTED

    @_safe_ntstatus(STATUS_MEDIA_WRITE_PROTECTED)
    def _delete_file(self, file_name, dokan_file_info):
        if self._read_only or self._writes is None:
            return STATUS_MEDIA_WRITE_PROTECTED
        path = _win_to_posix(file_name)
        entry = self._py_lookup(path)
        if entry is None:
            return STATUS_OBJECT_NAME_NOT_FOUND
        if entry.is_dir:
            return STATUS_FILE_IS_A_DIRECTORY
        dokan_file_info.contents.DeletePending = 1
        return STATUS_SUCCESS

    @_safe_ntstatus(STATUS_MEDIA_WRITE_PROTECTED)
    def _delete_directory(self, file_name, dokan_file_info):
        if self._read_only or self._writes is None:
            return STATUS_MEDIA_WRITE_PROTECTED
        path = _win_to_posix(file_name)
        entry = self._py_lookup(path)
        if entry is None or not entry.is_dir:
            return STATUS_OBJECT_NAME_NOT_FOUND
        try:
            children = self._py_list(path)
        except Exception:
            return STATUS_ACCESS_DENIED
        if children:
            return STATUS_DIRECTORY_NOT_EMPTY
        dokan_file_info.contents.DeletePending = 1
        return STATUS_SUCCESS

    @_safe_ntstatus(STATUS_MEDIA_WRITE_PROTECTED)
    def _move_file(self, file_name, new_file_name, replace_if_existing, dokan_file_info):
        if self._read_only or self._writes is None:
            return STATUS_MEDIA_WRITE_PROTECTED
        src = _win_to_posix(file_name)
        dst = _win_to_posix(new_file_name)
        if not replace_if_existing:
            existing = self._py_lookup(dst)
            if existing is not None:
                return STATUS_OBJECT_NAME_COLLISION
        try:
            self._bridge.call(
                self._writes.rename, src, dst, timeout=self._timeout_ms / 1000.0
            )
        except FileNotFoundError:
            return STATUS_OBJECT_NAME_NOT_FOUND
        except Exception:
            log.exception("MoveFile %s -> %s failed", src, dst)
            return STATUS_ACCESS_DENIED
        return STATUS_SUCCESS

    @_safe_ntstatus(STATUS_MEDIA_WRITE_PROTECTED)
    def _set_end_of_file(self, file_name, length, dokan_file_info):
        if self._read_only or self._writes is None:
            return STATUS_MEDIA_WRITE_PROTECTED
        path = _win_to_posix(file_name)
        if self._writes.get(path) is None:
            try:
                from functools import partial

                self._bridge.call(
                    partial(self._writes.begin_open, path, truncate=False),
                    timeout=self._timeout_ms / 1000.0,
                )
            except FileNotFoundError:
                return STATUS_OBJECT_NAME_NOT_FOUND
            except Exception:
                log.exception("SetEndOfFile open failed %s", path)
                return STATUS_ACCESS_DENIED
        try:
            self._bridge.call(
                self._writes.truncate,
                path,
                int(length),
                timeout=self._timeout_ms / 1000.0,
            )
        except Exception:
            log.exception("SetEndOfFile failed %s", path)
            return STATUS_ACCESS_DENIED
        info = dokan_file_info.contents
        if info.Context:
            with self._lock:
                h = self._handles.get(int(info.Context))
                if h is not None:
                    self._handles[int(info.Context)] = (path, int(length), True)
        return STATUS_SUCCESS

    @_safe_ntstatus()
    def _get_disk_free_space(
        self, free_available, total_bytes, total_free, dokan_file_info
    ):
        total = 8 * 1024 * 1024 * 1024 * 1024
        free_available[0] = total
        total_bytes[0] = total
        total_free[0] = total
        return STATUS_SUCCESS

    @staticmethod
    def _write_wstr(dest, text: str, max_chars: int) -> None:
        """Write UTF-16 into a Dokany output buffer.

        Callback args typed as LPWSTR are converted to Python str by ctypes and
        are NOT writable — callers must pass c_void_p / raw pointer.
        """
        if not dest or max_chars <= 0:
            return
        addr = int(cast(dest, c_void_p).value or 0)
        if not addr:
            return
        n = min(len(text), int(max_chars) - 1)
        buf = (c_wchar * int(max_chars)).from_address(addr)
        for i in range(n):
            buf[i] = text[i]
        buf[n] = "\0"

    @_safe_ntstatus()
    def _get_volume_information(
        self,
        volume_name_buffer,
        volume_name_size,
        volume_serial,
        max_component,
        fs_flags,
        fs_name_buffer,
        fs_name_size,
        dokan_file_info,
    ):
        self._write_wstr(volume_name_buffer, self._volume_name, int(volume_name_size))
        volume_serial[0] = self._serial
        max_component[0] = 255
        flags = (
            FILE_CASE_PRESERVED_NAMES
            | FILE_UNICODE_ON_DISK
            | FILE_PERSISTENT_ACLS
        )
        if self._read_only:
            flags |= FILE_READ_ONLY_VOLUME
        fs_flags[0] = flags
        self._write_wstr(fs_name_buffer, "NTFS", int(fs_name_size))
        return STATUS_SUCCESS

    @_safe_ntstatus()
    def _mounted(self, mount_point, dokan_file_info):
        log.info("Dokany mounted at %s", mount_point)
        return STATUS_SUCCESS

    @_safe_ntstatus()
    def _unmounted(self, dokan_file_info):
        log.info("Dokany unmounted")
        return STATUS_SUCCESS

    def unmount(self) -> bool:
        """Ask Dokany to tear down the mount so DokanMain can return."""
        mp = getattr(self, "_mount_point", None)
        if not mp:
            return False
        try:
            dokan = get_dokan()
            ok = bool(dokan.DokanRemoveMountPoint(mp))
            log.info("DokanRemoveMountPoint(%s) -> %s", mp, ok)
            return ok
        except Exception:
            log.exception("DokanRemoveMountPoint failed for %s", mp)
            return False

    def mount(self, mount_point: str) -> int:
        faulthandler.enable(file=sys.stderr, all_threads=True)

        mp = mount_point.strip()
        if len(mp) == 2 and mp[1] == ":":
            mp = mp + "\\"
        elif len(mp) >= 2 and mp[1] == ":" and not mp.endswith("\\"):
            mp = mp + "\\"
        self._mount_point = mp

        dokan = get_dokan()

        self._mount_buf = create_unicode_buffer(mp)
        self._options = DOKAN_OPTIONS()
        options = self._options
        options.Version = DOKAN_VERSION
        single = os.environ.get("DOKAN_SINGLE_THREAD", "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        options.SingleThread = 1 if single else 0
        options.Options = DOKAN_OPTION_MOUNT_MANAGER
        if self._read_only:
            options.Options |= DOKAN_OPTION_WRITE_PROTECT
        debug = os.environ.get("DOKAN_DEBUG", "0").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if debug:
            options.Options |= DOKAN_OPTION_DEBUG | DOKAN_OPTION_STDERR
        options.GlobalContext = 0
        options.MountPoint = cast(self._mount_buf, c_wchar_p)
        options.UNCName = None
        options.Timeout = self._timeout_ms
        options.AllocationUnitSize = 4096
        options.SectorSize = 512
        options.VolumeSecurityDescriptorLength = 0

        log.info(
            "Dokany starting mount_point=%s version_req=%s dll=%s driver=%s "
            "timeout=%sms single_thread=%s (httpx via Python bridge thread)",
            mp,
            options.Version,
            dokan.DokanVersion(),
            dokan.DokanDriverVersion(),
            self._timeout_ms,
            options.SingleThread,
        )
        for h in logging.root.handlers:
            h.flush()

        # DokanMain is a blocking native call: Ctrl+C never interrupts it on
        # Windows unless we run it off-thread and call DokanRemoveMountPoint.
        stop = threading.Event()
        result: dict[str, int] = {"status": -1}

        def _request_unmount(reason: str) -> None:
            if stop.is_set():
                return
            log.info("%s — unmounting %s ...", reason, mp)
            for h in logging.root.handlers:
                h.flush()
            self.unmount()
            stop.set()

        def _sig_handler(signum, _frame):
            name = {
                getattr(signal, "SIGINT", -1): "Ctrl+C",
                getattr(signal, "SIGTERM", -2): "SIGTERM",
                getattr(signal, "SIGBREAK", -3): "Ctrl+Break",
            }.get(signum, f"signal {signum}")
            _request_unmount(name)

        # Console control handler (fires even while native code is blocked).
        HandlerRoutine = None
        console_handler = None
        try:
            import ctypes as _ct
            from ctypes import wintypes as _wt

            HandlerRoutine = _ct.WINFUNCTYPE(_wt.BOOL, _wt.DWORD)

            @HandlerRoutine
            def console_handler(ctrl_type):  # type: ignore[misc]
                # 0=CTRL_C 1=CTRL_BREAK 2=CTRL_CLOSE
                if int(ctrl_type) in (0, 1, 2):
                    _request_unmount(f"console ctrl {ctrl_type}")
                    return True
                return False

            _ct.windll.kernel32.SetConsoleCtrlHandler(console_handler, True)
        except Exception:
            log.debug("SetConsoleCtrlHandler unavailable", exc_info=True)
            console_handler = None

        prev_handlers: dict[int, object] = {}
        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                prev_handlers[sig] = signal.signal(sig, _sig_handler)
            except Exception:
                pass

        dokan.DokanInit()

        def _dokan_main() -> None:
            try:
                result["status"] = int(
                    dokan.DokanMain(byref(options), byref(self._ops))
                )
            except Exception:
                log.exception("DokanMain raised")
                result["status"] = -1
            finally:
                stop.set()

        t = threading.Thread(target=_dokan_main, name="dokan-main", daemon=True)
        t.start()

        try:
            # Sleep in short slices so Python signal handlers can run.
            while not stop.wait(0.25):
                if not t.is_alive():
                    break
        except KeyboardInterrupt:
            _request_unmount("KeyboardInterrupt")
        finally:
            if not stop.is_set():
                self.unmount()
            t.join(timeout=30)
            for sig, prev in prev_handlers.items():
                try:
                    signal.signal(sig, prev)  # type: ignore[arg-type]
                except Exception:
                    pass
            if console_handler is not None:
                try:
                    import ctypes as _ct

                    _ct.windll.kernel32.SetConsoleCtrlHandler(console_handler, False)
                except Exception:
                    pass
            try:
                dokan.DokanShutdown()
            except Exception:
                log.exception("DokanShutdown failed")
            self._bridge.close()
            for h in logging.root.handlers:
                h.flush()

        status = int(result["status"])
        log.info("DokanMain returned: %s", dokan_error_name(status))
        if status != DOKAN_SUCCESS:
            log.error("DokanMain failed: %s", dokan_error_name(status))
        return status
