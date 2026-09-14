from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from . import __version__
from .cache import BlockCache, MetaCache
from .config import load_config
from .storage import StorageClient
from .teldrive import TeldriveClient
from .webdav import WebDAVClient
from .write import WriteStore


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _make_client(cfg) -> StorageClient:
    if cfg.backend == "webdav":
        return WebDAVClient(cfg.webdav_url, cfg.webdav_user, cfg.webdav_password)
    return TeldriveClient(
        cfg.api_host,
        access_token=cfg.access_token,
        api_key=cfg.api_key,
        page_size=cfg.page_size,
    )


def _make_caches(cfg, client: StorageClient) -> tuple[MetaCache, BlockCache]:
    meta = MetaCache(client, cfg.meta_ttl, negative_ttl=cfg.negative_meta_ttl)
    # Prefetch runs on BlockCache's own thread pool (not Dokany native threads),
    # so it is safe on Windows and is what prevents mid-stream stalls.
    prefetch_workers = int(os.environ.get("PREFETCH_WORKERS", "0") or 0)
    if prefetch_workers <= 0:
        prefetch_workers = max(4, cfg.prefetch_blocks) if cfg.prefetch_blocks else 4
    blocks = BlockCache(
        client,
        cfg.block_size,
        cfg.ram_cache_bytes,
        prefetch_blocks=cfg.prefetch_blocks,
        disk_dir=cfg.cache_dir,
        disk_bytes=cfg.disk_cache_bytes,
        read_ttl=cfg.cache_read_ttl,
        clean_interval=cfg.cache_clean_interval,
        namespace=cfg.cache_namespace,
        prefetch_after=cfg.prefetch_after,
        workers=prefetch_workers,
        fast_first_bytes=cfg.fast_first_bytes,
    )
    return meta, blocks


def _make_writes(cfg, client: StorageClient, meta, blocks) -> WriteStore | None:
    if cfg.read_only:
        return None
    if cfg.backend == "teldrive":
        if not isinstance(client, TeldriveClient):
            return None
    elif cfg.backend == "webdav":
        if not isinstance(client, WebDAVClient):
            return None
    else:
        return None
    return WriteStore(
        client,
        meta,
        blocks,
        chunk_size=cfg.upload_chunk_size,
        channel_id=cfg.channel_id,
    )


def _backend_label(cfg) -> str:
    if cfg.backend == "webdav":
        return f"WebDAV {cfg.webdav_url}"
    return f"Teldrive {cfg.api_host}"


def cmd_probe(args: argparse.Namespace) -> int:
    """Exercise listing/Range reads without mounting a filesystem."""
    cfg = load_config(args.env)
    client = _make_client(cfg)
    meta, blocks = _make_caches(cfg, client)
    log = logging.getLogger("raidrive.probe")
    log.info("backend=%s", cfg.backend)
    if cfg.cache_dir:
        log.info(
            "cache: RAM=%dMiB disk=%s max=%dMiB ttl=%ss clean=%ss prefetch_after=%d",
            cfg.ram_cache_bytes // (1024 * 1024),
            cfg.cache_dir,
            cfg.disk_cache_bytes // (1024 * 1024),
            int(cfg.cache_read_ttl),
            int(cfg.cache_clean_interval),
            cfg.prefetch_after,
        )

    try:
        if isinstance(client, TeldriveClient):
            try:
                session = client.validate_session()
                if session:
                    log.info(
                        "session ok user=%s id=%s",
                        session.get("userName") or session.get("name"),
                        session.get("userId"),
                    )
            except Exception as exc:
                log.warning("session check skipped/failed: %s", exc)

        root = meta.stat("/")
        log.info("root: dir=%s", root.is_dir if root else None)
        children = meta.list_dir("/")
        log.info("listed %d entries at /", len(children))
        for c in children[:15]:
            kind = "DIR " if c.is_dir else "FILE"
            log.info("  %s %12d  %s", kind, c.size, c.name)

        sample = None
        files = sorted((e for e in children if not e.is_dir), key=lambda e: e.size)
        if files:
            sample = files[0]
        else:
            for folder in (e for e in children if e.is_dir):
                try:
                    nested = meta.list_dir(folder.path)
                    nested_files = sorted(
                        (e for e in nested if not e.is_dir), key=lambda e: e.size
                    )
                    if nested_files:
                        sample = nested_files[0]
                        break
                except Exception as exc:
                    log.warning("could not list %s: %s", folder.path, exc)

        if sample:
            log.info(
                "range-test on %s id=%s (%d bytes)",
                sample.path,
                sample.file_id or "-",
                sample.size,
            )
            chunk = blocks.read(sample.path, 0, min(64 * 1024, sample.size), sample.size)
            log.info("read %d bytes ok (head=%s)", len(chunk), chunk[:8].hex())
            mid = max(0, sample.size // 2)
            chunk2 = blocks.read(
                sample.path, mid, min(32 * 1024, sample.size - mid), sample.size
            )
            log.info("mid read %d bytes ok", len(chunk2))
        else:
            log.warning("no sample file found for range test")

        log.info("PROBE OK — no writes/deletes were issued")
        return 0
    finally:
        blocks.close()
        client.close()


def _cmd_mount_fuse(args: argparse.Namespace) -> int:
    try:
        import pyfuse3
        import trio
    except ImportError as exc:
        print(
            "pyfuse3/trio not installed. On Linux:\n"
            "  sudo apt install fuse3 python3-pyfuse3\n"
            "  pip install -r requirements-linux.txt\n"
            f"Detail: {exc}",
            file=sys.stderr,
        )
        return 1

    if not Path("/dev/fuse").exists():
        print(
            "ERROR: /dev/fuse not found.\n"
            "This host cannot run FUSE (common on WSL1).\n"
            "On Windows use Dokany mount instead, or run: python -m raidrive probe",
            file=sys.stderr,
        )
        return 2

    from .fs import RaidriveFS

    cfg = load_config(args.env)
    mount = Path(args.mount_point or cfg.mount_point)
    mount.mkdir(parents=True, exist_ok=True)

    client = _make_client(cfg)
    meta, blocks = _make_caches(cfg, client)
    writes = _make_writes(cfg, client, meta, blocks)
    ops = RaidriveFS(
        client,
        meta,
        blocks,
        writes,
        dir_mode=cfg.dir_mode,
        file_mode=cfg.file_mode,
        uid=cfg.uid,
        gid=cfg.gid,
        read_only=cfg.read_only,
    )

    meta.list_dir("/")

    fuse_options = set(pyfuse3.default_options)
    fuse_options.add(f"fsname={cfg.backend}")
    if cfg.read_only:
        fuse_options.add("ro")
    fuse_options.add("default_permissions")
    if args.allow_other or cfg.allow_other:
        fuse_options.add("allow_other")

    log = logging.getLogger("raidrive")
    mode = "READ-ONLY" if cfg.read_only else "READ-WRITE"
    log.info(
        "mounting %s %s (FUSE) -> %s (block=%dMiB prefetch=%d ram=%dMiB disk=%s/%dMiB)",
        mode,
        _backend_label(cfg),
        mount,
        cfg.block_size // (1024 * 1024),
        cfg.prefetch_blocks,
        cfg.ram_cache_bytes // (1024 * 1024),
        cfg.cache_dir or "(disabled)",
        cfg.disk_cache_bytes // (1024 * 1024),
    )

    pyfuse3.init(ops, str(mount), fuse_options)

    def _stop(*_):
        log.info("signal received, unmounting...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        trio.run(pyfuse3.main)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        pyfuse3.close(unmount=True)
        blocks.close()
        client.close()
        log.info("unmounted")
    return 0


def _cmd_mount_dokan(args: argparse.Namespace) -> int:
    try:
        from .fs_dokan import DokanTeldriveFS
    except ImportError as exc:
        print(
            "Dokany backend failed to load.\n"
            "Install Dokany 2: winget install dokan-dev.Dokany\n"
            "Then: pip install -r requirements.txt\n"
            f"Detail: {exc}",
            file=sys.stderr,
        )
        return 1

    cfg = load_config(args.env)
    mount = args.mount_point or str(cfg.mount_point)
    # Default drive letter on Windows if path looks like a Unix mount leftover
    if mount in ("./mnt", "mnt", "/mnt/telegram") or mount.replace("\\", "/").startswith(
        "/mnt/"
    ):
        mount = "T:"

    client = _make_client(cfg)
    meta, blocks = _make_caches(cfg, client)
    writes = _make_writes(cfg, client, meta, blocks)
    volume = "WebDAV" if cfg.backend == "webdav" else "Teldrive"
    fs = DokanTeldriveFS(
        client,
        meta,
        blocks,
        writes,
        read_only=cfg.read_only,
        volume_name=volume,
        timeout_ms=int(os.environ.get("DOKAN_TIMEOUT_MS", "300000")),
    )

    try:
        meta.list_dir("/")
    except Exception as exc:
        log = logging.getLogger("raidrive")
        log.warning("warmup list_dir failed: %s", exc)

    log = logging.getLogger("raidrive")
    mode = "READ-ONLY" if cfg.read_only else "READ-WRITE"
    log.info(
        "mounting %s %s (Dokany) -> %s (block=%dMiB prefetch=%d ram=%dMiB disk=%s/%dMiB)",
        mode,
        _backend_label(cfg),
        mount,
        cfg.block_size // (1024 * 1024),
        cfg.prefetch_blocks,
        cfg.ram_cache_bytes // (1024 * 1024),
        cfg.cache_dir or "(disabled)",
        cfg.disk_cache_bytes // (1024 * 1024),
    )
    log.info("Press Ctrl+C in this window or use: dokanctl /u %s", mount.rstrip("\\"))

    status = -1
    try:
        status = fs.mount(mount)
    except KeyboardInterrupt:
        log.info("interrupted — cleaning up")
        try:
            fs.unmount()
        except Exception:
            pass
        status = 0
    finally:
        blocks.close()
        client.close()
        log.info("stopped")

    return 0 if status == 0 else 3


def cmd_mount(args: argparse.Namespace) -> int:
    if sys.platform == "win32":
        return _cmd_mount_dokan(args)
    return _cmd_mount_fuse(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="raidrive",
        description=(
            "Teldrive/WebDAV mount with RaiDrive-style cache "
            "(FUSE on Linux, Dokany on Windows)"
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--env", help="Path to .env file")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser(
        "probe", help="Test listing/Range reads (no mount, no writes)"
    )
    p_probe.add_argument("-v", "--verbose", action="store_true")
    p_probe.set_defaults(func=cmd_probe)

    p_mount = sub.add_parser(
        "mount",
        help="Mount backend from .env (BACKEND=teldrive|webdav; FUSE/Dokany; READ_ONLY=0 enables writes)",
    )
    p_mount.add_argument("-v", "--verbose", action="store_true")
    p_mount.add_argument(
        "mount_point",
        nargs="?",
        default=None,
        help="Mount point: Linux path or Windows drive letter (e.g. T:)",
    )
    p_mount.add_argument(
        "--allow-other",
        action="store_true",
        help="Linux only: allow other users (needs user_allow_other in fuse.conf)",
    )
    p_mount.set_defaults(func=cmd_mount)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if not args.env:
        root_env = Path(__file__).resolve().parent.parent / ".env"
        if root_env.exists():
            os.environ.setdefault("DOTENV_PATH", str(root_env))
            args.env = str(root_env)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
