from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .entry import FsEntry
from .storage import StorageClient

log = logging.getLogger("raidrive.cache")


@dataclass
class CachedMeta:
    entry: FsEntry | None
    children: list[FsEntry] | None
    expires_at: float


@dataclass
class _ReadCursor:
    """Tracks per-file read pattern to detect streaming vs scan/probe."""

    last_end: int = -1
    sequential: int = 0


def _parent_key(path: str) -> str:
    path = path.rstrip("/") or "/"
    if path == "/":
        return "/"
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


# Sentinel: peek_* missed the in-memory cache (caller should hit the API path).
CACHE_MISS = object()


def clamp_range_bytes(data: bytes, start: int, end: int, *, path: str = "") -> bytes:
    """Ensure Range payload is at most the requested window.

    Some WebDAV servers ignore ``Range`` and return HTTP 200 + the whole file.
    Never keep/cache more than ``end - start + 1`` bytes.
    """
    if end < start:
        return b""
    want = end - start + 1
    if len(data) <= want:
        return data
    log.warning(
        "Range response too large for %s [%s-%s]: got %s bytes (limit %s) — truncating",
        path or "?",
        start,
        end,
        len(data),
        want,
    )
    return data[:want]


class MetaCache:
    def __init__(self, client: StorageClient, ttl: float, negative_ttl: float | None = None):
        self._client = client
        self._ttl = ttl
        # Missing paths (player probes) are cached shorter, but still avoid API storms.
        self._negative_ttl = ttl if negative_ttl is None else negative_ttl
        self._lock = threading.Lock()
        self._items: dict[str, CachedMeta] = {}

    def _key(self, path: str) -> str:
        return path.rstrip("/") or "/"

    def invalidate(self, path: str | None = None) -> None:
        with self._lock:
            if path is None:
                self._items.clear()
                return
            key = self._key(path)
            self._items.pop(key, None)
            parent = _parent_key(key)
            if parent != key:
                phit = self._items.get(parent)
                if phit is not None:
                    # Drop listing so next list_dir refreshes children.
                    self._items[parent] = CachedMeta(phit.entry, None, phit.expires_at)

    def _store(self, key: str, entry: FsEntry | None, children: list[FsEntry] | None, now: float) -> None:
        ttl = self._ttl if entry is not None else self._negative_ttl
        self._items[key] = CachedMeta(entry, children, now + ttl)

    def _resolve_from_parent_locked(self, key: str, now: float) -> object:
        """Under lock: resolve from parent listing, or CACHE_MISS if parent not listed."""
        if key == "/":
            return CACHE_MISS
        parent = _parent_key(key)
        name = key.rsplit("/", 1)[-1]
        phit = self._items.get(parent)
        if phit is None or phit.children is None or phit.expires_at <= now:
            return CACHE_MISS
        for child in phit.children:
            if child.name == name or self._key(child.path) == key:
                self._store(key, child, None if child.is_dir else [], now)
                return child
        # Parent listed and name absent => negative hit
        self._store(key, None, [], now)
        return None

    def peek_stat(self, path: str) -> object:
        """Return FsEntry|None if cached; CACHE_MISS if API would be needed.

        Safe for Dokany native threads (lock + dict only, no HTTP).
        """
        key = self._key(path)
        now = time.monotonic()
        with self._lock:
            hit = self._items.get(key)
            if hit and hit.expires_at > now:
                return hit.entry
            return self._resolve_from_parent_locked(key, now)

    def peek_list(self, path: str) -> list[FsEntry] | None:
        """Return children if listing is cached; None if API needed."""
        key = self._key(path)
        now = time.monotonic()
        with self._lock:
            hit = self._items.get(key)
            if hit and hit.children is not None and hit.expires_at > now:
                return list(hit.children)
        return None

    def stat(self, path: str) -> FsEntry | None:
        peeked = self.peek_stat(path)
        if peeked is not CACHE_MISS:
            return peeked  # type: ignore[return-value]

        key = self._key(path)
        now = time.monotonic()
        entry = self._client.stat(path)
        with self._lock:
            prev = self._items.get(key)
            children = (
                prev.children
                if prev and prev.expires_at > now and entry is not None
                else (None if entry is None or entry.is_dir else [])
            )
            if entry is None:
                children = []
            self._store(key, entry, children, now)
        return entry

    def list_dir(self, path: str) -> list[FsEntry]:
        cached = self.peek_list(path)
        if cached is not None:
            return cached

        key = self._key(path)
        now = time.monotonic()
        # Prefer a single list call; don't propfind(stat+list) which doubles latency.
        try:
            children = self._client.list_children(key)
        except Exception:
            log.exception("list_dir failed for %s", key)
            children = []

        self_entry = FsEntry(
            path=key,
            name="" if key == "/" else key.rsplit("/", 1)[-1],
            is_dir=True,
            size=0,
            mtime=0,
        )
        # If we already knew the folder entry, keep richer metadata.
        with self._lock:
            prev = self._items.get(key)
            if prev and prev.entry is not None and prev.entry.is_dir:
                self_entry = prev.entry

            self._store(key, self_entry, children, now)
            for child in children:
                ck = self._key(child.path)
                self._store(ck, child, None if child.is_dir else [], now)
        return list(children)


class DiskBlockStore:
    """Persistent per-block cache (``.blk`` files) with read TTL and size budget."""

    def __init__(
        self,
        root: Path,
        max_bytes: int,
        read_ttl: float,
        namespace: str,
        clean_interval: float = 3600,
        block_size: int = 0,
    ):
        self._root = root / hashlib.sha1(namespace.encode()).hexdigest()[:16]
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max(max_bytes, 0)
        self._read_ttl = max(read_ttl, 0.0)
        self._block_size = max(int(block_size), 0)
        self._clean_interval = max(clean_interval, 60.0)
        self._lock = threading.Lock()
        self._bytes = 0
        self._stop = threading.Event()
        self._purge_orphan_sparse()
        self._scan()
        self.cleanup_expired()
        self._janitor = threading.Thread(
            target=self._janitor_loop,
            name="raidrive-cache-janitor",
            daemon=True,
        )
        self._janitor.start()
        log.info(
            "block disk cache at %s (ttl=%ss)",
            self._root,
            int(self._read_ttl),
        )

    def close(self) -> None:
        self._stop.set()
        self._janitor.join(timeout=5)

    def _block_path(self, path: str, index: int) -> Path:
        digest = hashlib.sha1(path.encode("utf-8")).hexdigest()
        return self._root / digest[:2] / digest / f"{index:08d}.blk"

    def _purge_orphan_sparse(self) -> None:
        """Remove broken ``.bin``/``.map`` pairs from the sparse experiment."""
        removed = 0
        freed = 0
        for dirpath, _, filenames in os.walk(self._root):
            base = Path(dirpath)
            for name in filenames:
                if not (name.endswith(".bin") or name.endswith(".map") or name.endswith(".maptmp")):
                    continue
                p = base / name
                try:
                    sz = p.stat().st_size if name.endswith(".bin") else 0
                    p.unlink(missing_ok=True)
                    removed += 1
                    freed += sz
                except OSError:
                    pass
        if removed:
            log.warning(
                "removed %d leftover sparse cache files (%.1f MiB) — using .blk blocks again",
                removed,
                freed / (1024 * 1024),
            )

    def _scan(self) -> None:
        total = 0
        for dirpath, _, filenames in os.walk(self._root):
            for name in filenames:
                if name.endswith(".blk") or name.endswith(".tmp"):
                    try:
                        total += (Path(dirpath) / name).stat().st_size
                    except OSError:
                        pass
        self._bytes = total
        log.info(
            "disk cache at %s: %.1f MiB used / %.1f MiB max (ttl=%ss)",
            self._root,
            self._bytes / (1024 * 1024),
            self._max_bytes / (1024 * 1024),
            int(self._read_ttl),
        )

    def _janitor_loop(self) -> None:
        while not self._stop.wait(self._clean_interval):
            try:
                removed, freed = self.cleanup_expired()
                if removed:
                    log.info(
                        "cache janitor removed %d expired blocks (%.1f MiB)",
                        removed,
                        freed / (1024 * 1024),
                    )
                with self._lock:
                    if self._bytes > self._max_bytes:
                        before = self._bytes
                        self._evict_locked()
                        freed2 = before - self._bytes
                        if freed2 > 0:
                            log.info(
                                "cache janitor size-evicted %.1f MiB (now %.1f MiB)",
                                freed2 / (1024 * 1024),
                                self._bytes / (1024 * 1024),
                            )
            except Exception:
                log.exception("cache janitor failed")

    def cleanup_expired(self) -> tuple[int, int]:
        """Delete blocks older than read TTL. Returns (files_removed, bytes_freed)."""
        if self._read_ttl <= 0:
            return 0, 0
        now = time.time()
        removed = 0
        freed = 0
        for dirpath, dirnames, filenames in os.walk(self._root, topdown=False):
            base = Path(dirpath)
            for name in filenames:
                if not (name.endswith(".blk") or name.endswith(".tmp")):
                    continue
                p = base / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                if name.endswith(".blk") and (now - st.st_mtime) <= self._read_ttl:
                    continue
                try:
                    p.unlink(missing_ok=True)
                    removed += 1
                    freed += st.st_size
                    with self._lock:
                        self._bytes = max(0, self._bytes - st.st_size)
                except OSError:
                    pass
            if base != self._root:
                try:
                    base.rmdir()
                except OSError:
                    pass
        return removed, freed

    def get(self, path: str, index: int, file_size: int = 0) -> bytes | None:
        if self._max_bytes <= 0:
            return None
        fp = self._block_path(path, index)
        try:
            st = fp.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return None

        age = time.time() - st.st_mtime
        if self._read_ttl > 0 and age > self._read_ttl:
            try:
                fp.unlink(missing_ok=True)
                with self._lock:
                    self._bytes = max(0, self._bytes - st.st_size)
            except OSError:
                pass
            return None

        # Guard against a corrupt oversized block left by a bad Range response.
        if self._block_size > 0 and st.st_size > self._block_size * 2:
            log.warning(
                "dropping oversized cache block %s (%s bytes)",
                fp,
                st.st_size,
            )
            try:
                fp.unlink(missing_ok=True)
                with self._lock:
                    self._bytes = max(0, self._bytes - st.st_size)
            except OSError:
                pass
            return None

        try:
            return fp.read_bytes()
        except OSError:
            return None

    def put(self, path: str, index: int, data: bytes, file_size: int = 0) -> None:
        if self._max_bytes <= 0 or not data:
            return
        if self._block_size > 0 and len(data) > self._block_size * 2:
            log.warning(
                "refusing to cache oversized block %s#%s (%s bytes)",
                path,
                index,
                len(data),
            )
            data = data[: self._block_size]
        fp = self._block_path(path, index)
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            tmp = fp.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(fp)
        except OSError:
            log.exception("disk cache write failed %s#%s", path, index)
            return

        with self._lock:
            self._bytes += len(data)
            if self._bytes > self._max_bytes:
                self._evict_locked()

    def purge_path(self, path: str) -> None:
        """Remove all cached blocks for a mount path."""
        digest = hashlib.sha1(path.encode("utf-8")).hexdigest()
        base = self._root / digest[:2] / digest
        if not base.exists():
            return
        freed = 0
        for p in base.rglob("*"):
            if p.is_file():
                try:
                    sz = p.stat().st_size
                    p.unlink(missing_ok=True)
                    freed += sz
                except OSError:
                    pass
        try:
            for child in sorted(base.rglob("*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
            base.rmdir()
        except OSError:
            pass
        if freed:
            with self._lock:
                self._bytes = max(0, self._bytes - freed)

    def _evict_locked(self) -> None:
        files: list[tuple[float, int, Path]] = []
        for dirpath, _, filenames in os.walk(self._root):
            for name in filenames:
                if not name.endswith(".blk"):
                    continue
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
        files.sort()  # oldest first
        target = int(self._max_bytes * 0.85)
        for _, size, p in files:
            if self._bytes <= target:
                break
            try:
                p.unlink(missing_ok=True)
                self._bytes = max(0, self._bytes - size)
            except OSError:
                pass


class BlockCache:
    """RAM LRU + optional disk cache, with sequential prefetch.

    Cold-start / probe reads use a small Range (``fast_first_bytes``) so the
    player gets first bytes quickly; full ``block_size`` chunks fill in the
    background once streaming is detected.
    """

    def __init__(
        self,
        client: StorageClient,
        block_size: int,
        ram_bytes: int,
        prefetch_blocks: int,
        *,
        disk_dir: Path | None = None,
        disk_bytes: int = 0,
        read_ttl: float = 12 * 3600,
        clean_interval: float = 3600,
        namespace: str = "default",
        workers: int = 4,
        prefetch_after: int = 2,
        fast_first_bytes: int = 1024 * 1024,
    ):
        self._client = client
        self.block_size = block_size
        self._max_blocks = max(1, ram_bytes // max(block_size, 1))
        self._prefetch = prefetch_blocks
        # Need this many forward-continuing reads before prefetch starts.
        # Stops Jellyfin library scans (1 small header read) from pulling N blocks.
        self._prefetch_after = max(1, prefetch_after)
        # Minimum window pulled on a cold miss (player header / first frames).
        self._fast_first = max(64 * 1024, int(fast_first_bytes))
        self._lock = threading.Lock()
        self._blocks: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        # Prefix of a block (from block start) while the rest is still downloading.
        self._partials: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._inflight: set[tuple[str, int]] = set()
        self._cv = threading.Condition(self._lock)
        self._closed = False
        self._cursors: dict[str, _ReadCursor] = {}
        # Keep prefetch modest so it doesn't starve the active ReadFile.
        self._pool = ThreadPoolExecutor(
            max_workers=max(2, min(workers, 4)),
            thread_name_prefix="prefetch",
        )
        self._disk: DiskBlockStore | None = None
        if disk_dir is not None and disk_bytes > 0:
            self._disk = DiskBlockStore(
                disk_dir,
                disk_bytes,
                read_ttl,
                namespace,
                clean_interval=clean_interval,
                block_size=block_size,
            )
        log.info(
            "block cache: block=%dMiB fast_first=%dKiB prefetch=%d after=%d",
            block_size // (1024 * 1024),
            self._fast_first // 1024,
            prefetch_blocks,
            self._prefetch_after,
        )

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        # Don't wait on in-flight HTTP — Ctrl+C must exit promptly.
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._disk is not None:
            self._disk.close()

    def invalidate_path(self, path: str) -> None:
        with self._cv:
            victims = [k for k in list(self._blocks) if k[0] == path]
            for k in victims:
                self._blocks.pop(k, None)
            for k in [k for k in list(self._partials) if k[0] == path]:
                self._partials.pop(k, None)
            self._cursors.pop(path, None)
        if self._disk is not None:
            self._disk.purge_path(path)

    def _get_locked(self, key: tuple[str, int]) -> bytes | None:
        data = self._blocks.get(key)
        if data is not None:
            self._blocks.move_to_end(key)
        return data

    def _put_locked(self, key: tuple[str, int], data: bytes) -> None:
        self._blocks[key] = data
        self._blocks.move_to_end(key)
        self._partials.pop(key, None)
        while len(self._blocks) > self._max_blocks:
            self._blocks.popitem(last=False)

    def _put_partial_locked(self, key: tuple[str, int], data: bytes) -> None:
        """Store a longer prefix for a block (never shrinks)."""
        if key in self._blocks:
            return
        prev = self._partials.get(key)
        if prev is not None and len(prev) >= len(data):
            self._partials.move_to_end(key)
            return
        self._partials[key] = data
        self._partials.move_to_end(key)
        while len(self._partials) > self._max_blocks * 2:
            self._partials.popitem(last=False)

    def _slice_cached_locked(
        self, path: str, offset: int, end: int
    ) -> bytes | None:
        """Assemble [offset, end) from full blocks and prefixes; None if any gap."""
        if end <= offset:
            return b""
        parts: list[bytes] = []
        pos = offset
        while pos < end:
            idx = pos // self.block_size
            bstart = idx * self.block_size
            key = (path, idx)
            block = self._get_locked(key)
            if block is None:
                block = self._partials.get(key)
                if block is not None:
                    self._partials.move_to_end(key)
            if block is None:
                return None
            a = pos - bstart
            if a >= len(block):
                return None
            b = min(end - bstart, len(block))
            if b <= a:
                return None
            parts.append(block[a:b])
            pos = bstart + b
        return b"".join(parts)

    def try_read_ram(self, path: str, offset: int, size: int, file_size: int) -> bytes | None:
        """Return bytes if already in RAM (full or partial); else None.

        Safe for Dokany native threads: lock + dict only (no disk/HTTP).
        """
        if size <= 0 or offset >= file_size:
            return b""
        size = min(size, file_size - offset)
        end = offset + size
        last = (end - 1) // self.block_size
        with self._lock:
            data = self._slice_cached_locked(path, offset, end)
        if data is None:
            return None
        do_prefetch, _seq = self._note_read(path, offset, size)
        if do_prefetch:
            self._prefetch_async(path, last + 1, file_size)
        return data

    def _persist_disk(self, path: str, index: int, data: bytes, file_size: int = 0) -> None:
        if self._disk is None or not data:
            return
        try:
            self._disk.put(path, index, data, file_size=file_size)
        except Exception:
            log.exception("async disk cache write failed %s#%s", path, index)

    def _network_range(self, path: str, start: int, end: int) -> bytes:
        want = end - start + 1
        data = self._client.read_range(path, start, end)
        data = clamp_range_bytes(data, start, end, path=path)
        if len(data) > want:
            data = data[:want]
        return data

    def _absorb_prefix(self, path: str, start: int, data: bytes, file_size: int) -> None:
        """If ``data`` begins at a block boundary (or extends a partial), store prefixes."""
        if not data:
            return
        pos = start
        view = memoryview(data)
        off = 0
        with self._cv:
            while off < len(data):
                idx = pos // self.block_size
                bstart = idx * self.block_size
                key = (path, idx)
                if key in self._blocks:
                    # Skip ahead past this full block.
                    advance = min(len(data) - off, self.block_size - (pos - bstart))
                    pos += advance
                    off += advance
                    continue
                if pos != bstart:
                    # Mid-block hole — only extend if we already have a prefix to here.
                    prev = self._partials.get(key)
                    if prev is None or len(prev) != (pos - bstart):
                        break
                    take = min(len(data) - off, self.block_size - len(prev))
                    merged = prev + view[off : off + take].tobytes()
                    if len(merged) >= self.block_size or bstart + len(merged) >= file_size:
                        self._put_locked(key, bytes(merged))
                    else:
                        self._put_partial_locked(key, merged)
                    pos += take
                    off += take
                    continue
                take = min(len(data) - off, self.block_size)
                chunk = view[off : off + take].tobytes()
                if take >= self.block_size or bstart + take >= file_size:
                    self._put_locked(key, chunk)
                else:
                    self._put_partial_locked(key, chunk)
                pos += take
                off += take

    def _fetch_block(self, path: str, index: int, file_size: int) -> bytes:
        key = (path, index)
        start = index * self.block_size
        if start >= file_size:
            with self._cv:
                self._inflight.discard(key)
                self._cv.notify_all()
            return b""

        # Disk hit before network.
        if self._disk is not None:
            cached = self._disk.get(path, index, file_size=file_size)
            if cached is not None:
                with self._cv:
                    if not self._closed:
                        self._put_locked(key, cached)
                    self._inflight.discard(key)
                    self._cv.notify_all()
                return cached

        end = min(file_size - 1, start + self.block_size - 1)
        try:
            with self._cv:
                if self._closed:
                    self._inflight.discard(key)
                    self._cv.notify_all()
                    return b""
            data = self._network_range(path, start, end)
        except Exception:
            log.exception("failed reading block %s#%s", path, index)
            with self._cv:
                self._inflight.discard(key)
                self._cv.notify_all()
            raise

        with self._cv:
            if not self._closed:
                self._put_locked(key, data)
            self._inflight.discard(key)
            self._cv.notify_all()

        if self._disk is not None and data:
            try:
                self._pool.submit(self._persist_disk, path, index, data, file_size)
            except RuntimeError:
                self._persist_disk(path, index, data, file_size)
        return data

    def _ensure_block(self, path: str, index: int, file_size: int) -> bytes:
        key = (path, index)
        with self._cv:
            if self._closed:
                return b""
            hit = self._get_locked(key)
            if hit is not None:
                return hit
            if key in self._inflight:
                while key in self._inflight and not self._closed:
                    self._cv.wait(timeout=30)
                hit = self._get_locked(key)
                if hit is not None:
                    return hit
                if self._closed:
                    return b""
            self._inflight.add(key)

        return self._fetch_block(path, index, file_size)

    def _hydrate_disk(self, path: str, first: int, last: int, file_size: int) -> None:
        if self._disk is None:
            return
        for idx in range(first, last + 1):
            key = (path, idx)
            with self._cv:
                if self._closed or key in self._blocks or key in self._inflight:
                    continue
            cached = self._disk.get(path, idx, file_size=file_size)
            if cached is None:
                continue
            with self._cv:
                if not self._closed and key not in self._blocks:
                    self._put_locked(key, cached)

    def _schedule_full_blocks(self, path: str, first: int, last: int, file_size: int) -> None:
        max_index = (file_size + self.block_size - 1) // self.block_size

        def worker(idx: int) -> None:
            key = (path, idx)
            with self._cv:
                if self._closed or key in self._blocks or key in self._inflight:
                    return
                if idx * self.block_size >= file_size:
                    return
                self._inflight.add(key)
            try:
                self._fetch_block(path, idx, file_size)
            except Exception:
                with self._cv:
                    self._inflight.discard(key)
                    self._cv.notify_all()

        for i in range(first, min(last + 1, max_index)):
            with self._cv:
                if self._closed or (path, i) in self._blocks:
                    continue
            try:
                self._pool.submit(worker, i)
            except RuntimeError:
                return

    def _note_read(self, path: str, offset: int, size: int) -> tuple[bool, int]:
        """Update cursor; return (do_prefetch, sequential_count)."""
        end = offset + size
        slack = self.block_size
        with self._lock:
            cur = self._cursors.get(path)
            if cur is None:
                cur = _ReadCursor()
                self._cursors[path] = cur

            if cur.last_end >= 0 and offset <= cur.last_end + slack and offset + size >= cur.last_end - slack:
                cur.sequential += 1
                cur.last_end = max(cur.last_end, end)
            else:
                cur.sequential = 1
                cur.last_end = end

            if len(self._cursors) > 256:
                victims = [k for k in self._cursors if k != path][:64]
                for k in victims:
                    self._cursors.pop(k, None)

            seq = cur.sequential
            return seq >= self._prefetch_after, seq

    def _want_fast_first(self, sequential: int, size: int) -> bool:
        # Cold / probe / small reads: prioritize first-byte latency.
        if sequential < self._prefetch_after:
            return True
        if size <= self._fast_first:
            return True
        return False

    def _prefetch_async(self, path: str, start_index: int, file_size: int) -> None:
        if self._prefetch <= 0:
            return
        max_index = (file_size + self.block_size - 1) // self.block_size

        def worker(idx: int) -> None:
            key = (path, idx)
            with self._cv:
                if self._closed or key in self._blocks or key in self._inflight:
                    return
                if idx * self.block_size >= file_size:
                    return
                self._inflight.add(key)
            try:
                self._fetch_block(path, idx, file_size)
            except Exception:
                with self._cv:
                    self._inflight.discard(key)
                    self._cv.notify_all()

        for i in range(start_index, min(start_index + self._prefetch, max_index)):
            with self._cv:
                if self._closed:
                    return
            try:
                self._pool.submit(worker, i)
            except RuntimeError:
                return

    def read(self, path: str, offset: int, size: int, file_size: int) -> bytes:
        if size <= 0 or offset >= file_size:
            return b""
        size = min(size, file_size - offset)
        end = offset + size
        first = offset // self.block_size
        last = (end - 1) // self.block_size

        do_prefetch, sequential = self._note_read(path, offset, size)

        # RAM hit (full or partial prefix).
        with self._lock:
            hit = self._slice_cached_locked(path, offset, end)
        if hit is not None:
            if do_prefetch:
                self._prefetch_async(path, last + 1, file_size)
            return hit

        # Pull any complete .blk files from disk without waiting on network.
        self._hydrate_disk(path, first, last, file_size)
        with self._lock:
            hit = self._slice_cached_locked(path, offset, end)
        if hit is not None:
            if do_prefetch:
                self._prefetch_async(path, last + 1, file_size)
            # Finish filling touched blocks in the background.
            self._schedule_full_blocks(path, first, last, file_size)
            return hit

        if self._want_fast_first(sequential, size):
            # Download a small window covering this ReadFile, return ASAP.
            win = max(size, self._fast_first)
            # Prefer extending from the start of the first touched block so we
            # can store a clean prefix partial.
            fetch_start = first * self.block_size
            # But never re-download bytes we already have in a partial.
            with self._lock:
                partial = self._partials.get((path, first))
            if partial is not None:
                fetch_start = first * self.block_size + len(partial)
            fetch_end = min(file_size, offset + win)
            if fetch_start < fetch_end:
                try:
                    with self._cv:
                        if self._closed:
                            return b""
                    chunk = self._network_range(path, fetch_start, fetch_end - 1)
                    self._absorb_prefix(path, fetch_start, chunk, file_size)
                except Exception:
                    log.exception(
                        "fast-first range failed %s [%s-%s]",
                        path,
                        fetch_start,
                        fetch_end - 1,
                    )
                    raise
            with self._lock:
                hit = self._slice_cached_locked(path, offset, end)
            if hit is None:
                # Mid-block seek without prefix: fall back to exact window.
                exact = self._network_range(path, offset, end - 1)
                self._schedule_full_blocks(path, first, last, file_size)
                if do_prefetch:
                    self._prefetch_async(path, last + 1, file_size)
                return exact
            # Complete full blocks + ahead in background (RaiDrive-style fill).
            self._schedule_full_blocks(path, first, last + 1, file_size)
            if do_prefetch:
                self._prefetch_async(path, last + 1, file_size)
            return hit

        # Steady-state streaming: wait on full blocks (better throughput).
        out = bytearray()
        for idx in range(first, last + 1):
            block = self._ensure_block(path, idx, file_size)
            bstart = idx * self.block_size
            a = max(offset, bstart) - bstart
            b = min(end, bstart + len(block)) - bstart
            out.extend(block[a:b])

        if do_prefetch:
            self._prefetch_async(path, last + 1, file_size)
        return bytes(out)
