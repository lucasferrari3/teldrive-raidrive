from __future__ import annotations

import base64
import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import BinaryIO
from urllib.parse import quote, unquote, urljoin, urlparse

from . import __version__
from .entry import FsEntry

log = logging.getLogger("raidrive.webdav")

DAV_NS = {"D": "DAV:"}


def _join_url(base: str, path: str) -> str:
    base = base.rstrip("/") + "/"
    path = path.lstrip("/")
    parts = [quote(unquote(p), safe="") for p in path.split("/") if p != ""]
    return urljoin(base, "/".join(parts))


def _href_to_mount_path(href: str, webdav_root_path: str) -> str:
    """Convert /webdav/Filmes/x.mkv -> /Filmes/x.mkv"""
    parsed = urlparse(href)
    raw = unquote(parsed.path)
    root = webdav_root_path.rstrip("/")
    if raw.startswith(root):
        rest = raw[len(root) :]
    else:
        rest = raw
    if not rest.startswith("/"):
        rest = "/" + rest
    if rest != "/" and rest.endswith("/"):
        rest = rest[:-1]
    return rest or "/"


def _parse_mtime(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0


class _LimitedReader:
    """File-like wrapper that stops after `size` bytes (keeps Content-Length honest)."""

    def __init__(self, fp: BinaryIO, size: int):
        self._fp = fp
        self._left = max(0, int(size))

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        if n is None or n < 0:
            n = self._left
        n = min(int(n), self._left)
        chunk = self._fp.read(n)
        self._left -= len(chunk)
        return chunk


_OK = (200, 201, 202, 204)


def _fail(method: str, path: str, status: int, text: str) -> None:
    snippet = (text or "").strip().replace("\n", " ")[:200]
    raise OSError(f"WebDAV {method} {path} failed: HTTP {status} {snippet}".rstrip())


class WebDAVClient:
    """WebDAV client (PROPFIND / Range GET; PUT / MKCOL / DELETE / MOVE when RW)."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        if not self.base_url:
            raise ValueError("base_url is required")
        parsed = urlparse(self.base_url)
        self.root_path = parsed.path.rstrip("/") or "/"
        self._timeout = timeout
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._headers = {
            "User-Agent": f"teldrive-raidrive/{__version__}",
            "Authorization": f"Basic {token}",
            "Accept": "*/*",
        }

    def close(self) -> None:
        return

    def url_for(self, mount_path: str) -> str:
        return _join_url(self.base_url, mount_path)

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | BinaryIO | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, str]:
        req_headers = dict(self._headers)
        if headers:
            req_headers.update(headers)
        if data is not None and "Content-Length" not in req_headers:
            if isinstance(data, (bytes, bytearray)):
                req_headers["Content-Length"] = str(len(data))
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
        wait = self._timeout if timeout is None else timeout
        try:
            with urllib.request.urlopen(req, timeout=wait) as resp:
                body = resp.read()
                status = getattr(resp, "status", None) or resp.getcode()
                text = body.decode("utf-8", errors="replace")
                log.debug('HTTP Request: %s %s "%s"', method.upper(), url.split("?")[0], status)
                return int(status), body, text
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            text = body.decode("utf-8", errors="replace")
            log.debug('HTTP Request: %s %s "%s"', method.upper(), url.split("?")[0], exc.code)
            return int(exc.code), body, text

    def propfind(self, mount_path: str, depth: int = 1) -> list[FsEntry]:
        url = self.url_for(mount_path)
        if mount_path.endswith("/") or mount_path == "/":
            url = url if url.endswith("/") else url + "/"

        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:propfind xmlns:D="DAV:">'
            "<D:prop>"
            "<D:displayname/>"
            "<D:resourcetype/>"
            "<D:getcontentlength/>"
            "<D:getlastmodified/>"
            "<D:getcontenttype/>"
            "</D:prop>"
            "</D:propfind>"
        ).encode("utf-8")

        status, _raw, text = self._request(
            "PROPFIND",
            url,
            data=body,
            headers={
                "Depth": str(depth),
                "Content-Type": "application/xml; charset=utf-8",
            },
        )
        if status == 404:
            return []
        if status not in (207, 200):
            log.warning("PROPFIND %s -> %s %s", mount_path, status, text[:200])
            raise urllib.error.HTTPError(url, status, text[:200], hdrs=None, fp=None)  # type: ignore[arg-type]

        return self._parse_multistatus(text)

    def _parse_multistatus(self, xml_text: str) -> list[FsEntry]:
        root = ET.fromstring(xml_text)
        entries: list[FsEntry] = []
        for response in root.findall("D:response", DAV_NS):
            href_el = response.find("D:href", DAV_NS)
            if href_el is None or not href_el.text:
                continue
            path = _href_to_mount_path(href_el.text, self.root_path)

            propstat = response.find("D:propstat", DAV_NS)
            if propstat is None:
                continue
            prop = propstat.find("D:prop", DAV_NS)
            if prop is None:
                continue

            rtype = prop.find("D:resourcetype", DAV_NS)
            is_dir = rtype is not None and rtype.find("D:collection", DAV_NS) is not None

            size_el = prop.find("D:getcontentlength", DAV_NS)
            size = int(size_el.text) if size_el is not None and size_el.text else 0

            mtime_el = prop.find("D:getlastmodified", DAV_NS)
            mtime = _parse_mtime(mtime_el.text if mtime_el is not None else None)

            name_el = prop.find("D:displayname", DAV_NS)
            name = name_el.text if name_el is not None and name_el.text else path.rsplit("/", 1)[-1]
            if path == "/":
                name = ""

            entries.append(
                FsEntry(
                    path=path,
                    name=name,
                    is_dir=is_dir,
                    size=size,
                    mtime=mtime,
                    file_id="",
                )
            )
        return entries

    def list_children(self, mount_path: str) -> list[FsEntry]:
        path = mount_path if mount_path.endswith("/") or mount_path == "/" else mount_path + "/"
        all_entries = self.propfind(path, depth=1)
        self_path = mount_path.rstrip("/") or "/"
        children = []
        for e in all_entries:
            ep = e.path.rstrip("/") or "/"
            if ep == self_path:
                continue
            parent = ep.rsplit("/", 1)[0] or "/"
            if parent == self_path or (self_path == "/" and ep.count("/") == 1):
                children.append(e)
        return children

    def stat(self, mount_path: str) -> FsEntry | None:
        entries = self.propfind(mount_path, depth=0)
        if not entries:
            return None
        target = mount_path.rstrip("/") or "/"
        for e in entries:
            if (e.path.rstrip("/") or "/") == target:
                return e
        return entries[0]

    def read_range(self, mount_path: str, start: int, end: int) -> bytes:
        """GET bytes start..end inclusive (never downloads more than the window)."""
        if end < start:
            return b""
        want = end - start + 1
        url = self.url_for(mount_path)
        req_headers = dict(self._headers)
        req_headers.update(
            {
                "Range": f"bytes={start}-{end}",
                "Accept": "*/*",
            }
        )
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = int(getattr(resp, "status", None) or resp.getcode())
                log.debug(
                    'HTTP Request: GET %s "%s"',
                    url.split("?")[0],
                    status,
                )
                if status == 416:
                    return b""
                if status not in (200, 206):
                    body = resp.read(4096)
                    text = body.decode("utf-8", errors="replace")
                    log.warning(
                        "GET Range %s [%s-%s] -> %s", mount_path, start, end, status
                    )
                    raise urllib.error.HTTPError(
                        url, status, text[:200], hdrs=None, fp=None  # type: ignore[arg-type]
                    )
                # HTTP 200 usually means the server ignored Range. If start>0 the
                # first `want` bytes are the WRONG window — abort instead of caching garbage.
                if status == 200 and start > 0:
                    log.warning(
                        "WebDAV ignored Range for %s (HTTP 200 at start=%s) — aborting",
                        mount_path,
                        start,
                    )
                    raise OSError(
                        f"WebDAV server ignored Range request for {mount_path}"
                    )
                # Only pull the bytes we asked for (drops the rest of a full-file 200).
                data = resp.read(want)
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                return b""
            body = exc.read() if exc.fp else b""
            text = body.decode("utf-8", errors="replace")
            log.debug('HTTP Request: GET %s "%s"', url.split("?")[0], exc.code)
            if exc.code in (200, 206):
                return body[:want]
            log.warning("GET Range %s [%s-%s] -> %s", mount_path, start, end, exc.code)
            raise urllib.error.HTTPError(
                url, exc.code, text[:200], hdrs=None, fp=None  # type: ignore[arg-type]
            )

    def forget(self, mount_path: str) -> None:
        return

    def put(
        self,
        mount_path: str,
        data: bytes | BinaryIO,
        *,
        size: int | None = None,
        content_type: str = "application/octet-stream",
        timeout: float | None = None,
    ) -> None:
        url = self.url_for(mount_path)
        if isinstance(data, (bytes, bytearray)):
            body: bytes | BinaryIO = bytes(data)
            nbytes = len(body)
        else:
            try:
                data.seek(0)
            except Exception:
                pass
            nbytes = int(size if size is not None else 0)
            body = b"" if nbytes <= 0 else _LimitedReader(data, nbytes)
        headers = {
            "Content-Type": content_type or "application/octet-stream",
            "Content-Length": str(nbytes),
        }
        status, _, text = self._request(
            "PUT", url, data=body, headers=headers, timeout=timeout
        )
        if status not in _OK:
            log.warning("PUT %s -> %s %s", mount_path, status, text[:200])
            _fail("PUT", mount_path, status, text)

    def mkdir(self, mount_path: str) -> None:
        path = mount_path if mount_path.startswith("/") else "/" + mount_path
        if path in ("", "/"):
            return
        url = self.url_for(path)
        status, _, text = self._request("MKCOL", url)
        if status in _OK:
            return
        if status == 405:
            existing = self.stat(path)
            if existing is not None and existing.is_dir:
                return
        log.warning("MKCOL %s -> %s %s", path, status, text[:200])
        _fail("MKCOL", path, status, text)

    def delete(self, mount_path: str, *, is_dir: bool = False) -> None:
        url = self.url_for(mount_path)
        headers = None
        if is_dir:
            url = url if url.endswith("/") else url + "/"
            headers = {"Depth": "infinity"}
        status, _, text = self._request("DELETE", url, headers=headers)
        if status in _OK or status == 404:
            return
        if is_dir and status in (409, 301, 302):
            alt = url.rstrip("/")
            status, _, text = self._request("DELETE", alt, headers=headers)
            if status in _OK or status == 404:
                return
        log.warning("DELETE %s -> %s %s", mount_path, status, text[:200])
        _fail("DELETE", mount_path, status, text)

    def move_path(
        self,
        src: str,
        dst: str,
        *,
        overwrite: bool = True,
        is_dir: bool = False,
    ) -> None:
        src_url = self.url_for(src)
        dst_url = self.url_for(dst)
        if is_dir:
            src_url = src_url if src_url.endswith("/") else src_url + "/"
            dst_url = dst_url if dst_url.endswith("/") else dst_url + "/"
        status, _, text = self._request(
            "MOVE",
            src_url,
            headers={
                "Destination": dst_url,
                "Overwrite": "T" if overwrite else "F",
            },
        )
        if status in _OK:
            return
        log.warning("MOVE %s -> %s : %s %s", src, dst, status, text[:200])
        _fail("MOVE", f"{src} -> {dst}", status, text)
