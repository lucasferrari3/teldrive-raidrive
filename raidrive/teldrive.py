from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .entry import FsEntry

log = logging.getLogger("raidrive.teldrive")


def _norm_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return path or "/"


def _parse_mtime(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _parent_path(path: str) -> str:
    path = _norm_path(path)
    if path == "/":
        return "/"
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


@dataclass
class _HttpResponse:
    status_code: int
    content: bytes
    text: str

    def json(self) -> Any:
        return json.loads(self.text or "null")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise urllib.error.HTTPError(
                "", self.status_code, self.text[:200], hdrs=None, fp=None  # type: ignore[arg-type]
            )


class TeldriveClient:
    """Read-only Teldrive REST client (list / stat / Range GET).

    Uses stdlib urllib (more stable than httpx under Dokany/Python 3.13).
    """

    def __init__(
        self,
        api_host: str,
        access_token: str = "",
        api_key: str = "",
        timeout: float = 120.0,
        page_size: int = 500,
    ):
        self.api_host = api_host.rstrip("/")
        if not self.api_host:
            raise ValueError("api_host is required")
        self._timeout = timeout
        self._page_size = max(1, min(page_size, 1000))
        self._id_lock = threading.Lock()
        self._path_to_id: dict[str, str] = {"/": ""}
        self._id_to_entry: dict[str, FsEntry] = {}
        self._stream_style: str | None = None

        self._headers = {
            "User-Agent": "teldrive-raidrive/0.3",
            "Accept": "application/json",
        }
        if access_token:
            self._headers["Authorization"] = f"Bearer {access_token}"
            self._headers["Cookie"] = f"access_token={access_token}"
        if api_key:
            self._headers["X-Api-Key"] = api_key

    def close(self) -> None:
        return

    def _http_request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        data: bytes | None = None,
        json_body: dict | list | None = None,
        timeout: float | None = None,
    ) -> _HttpResponse:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = self.api_host + path
        if params:
            clean = {str(k): str(v) for k, v in params.items() if v is not None}
            url = url + "?" + urllib.parse.urlencode(clean)

        req_headers = dict(self._headers)
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(headers)
        if body is not None and "Content-Length" not in req_headers:
            req_headers["Content-Length"] = str(len(body))

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method.upper())
        to = self._timeout if timeout is None else timeout
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                resp_body = resp.read()
                status = getattr(resp, "status", None) or resp.getcode()
                text = resp_body.decode("utf-8", errors="replace")
                log.debug('HTTP Request: %s %s "%s"', method.upper(), url.split("?")[0], status)
                return _HttpResponse(int(status), resp_body, text)
        except urllib.error.HTTPError as exc:
            resp_body = exc.read() if exc.fp else b""
            text = resp_body.decode("utf-8", errors="replace")
            log.debug('HTTP Request: %s %s "%s"', method.upper(), url.split("?")[0], exc.code)
            return _HttpResponse(int(exc.code), resp_body, text)

    def _http_get(
        self,
        path: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> _HttpResponse:
        return self._http_request("GET", path, params=params, headers=headers)

    def validate_session(self) -> dict:
        resp = self._http_get("/api/auth/session")
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json() or {}

    def _remember(self, entry: FsEntry) -> None:
        if not entry.file_id and entry.path != "/":
            return
        with self._id_lock:
            self._path_to_id[_norm_path(entry.path)] = entry.file_id
            if entry.file_id:
                self._id_to_entry[entry.file_id] = entry

    def _file_to_entry(self, item: dict, fallback_parent: str = "/") -> FsEntry:
        name = item.get("name") or ""
        is_dir = (item.get("type") or "").lower() == "folder"
        size = int(item.get("size") or 0)
        mtime = _parse_mtime(item.get("updatedAt"))
        file_id = str(item.get("id") or "")

        raw_path = item.get("path")
        if raw_path:
            path = _norm_path(str(raw_path))
            if not (is_dir and path.endswith("/" + name)) and name and not path.endswith(name):
                path = _norm_path(f"{path.rstrip('/')}/{name}")
        else:
            parent = fallback_parent.rstrip("/") if fallback_parent != "/" else ""
            path = f"{parent}/{name}" if name else (fallback_parent or "/")
            path = _norm_path(path)

        if path == "/root" or path.startswith("/root/"):
            path = path[5:] or "/"
            path = _norm_path(path)

        entry = FsEntry(
            path=path,
            name="" if path == "/" else name,
            is_dir=is_dir,
            size=size,
            mtime=mtime,
            file_id=file_id,
        )
        self._remember(entry)
        return entry

    def _list_page(self, params: dict) -> tuple[list[dict], dict]:
        resp = self._http_get("/api/files", params=params)
        if resp.status_code == 404:
            return [], {}
        if resp.status_code >= 400:
            log.warning("GET /api/files %s -> %s %s", params, resp.status_code, resp.text[:200])
            resp.raise_for_status()
        data = resp.json() or {}
        items = data.get("items") or data.get("files") or []
        meta = data.get("meta") or {}
        return items, meta

    def _list_all(self, params: dict) -> list[dict]:
        out: list[dict] = []
        page = 1
        cursor = ""
        base = dict(params)
        base.setdefault("limit", str(self._page_size))
        base.setdefault("operation", "list")

        while True:
            q = dict(base)
            if cursor:
                q["cursor"] = cursor
            else:
                q["page"] = str(page)

            items, meta = self._list_page(q)
            out.extend(items)

            next_cursor = meta.get("nextCursor") or ""
            total_pages = int(meta.get("totalPages") or 1)
            current = int(meta.get("currentPage") or page)

            if next_cursor:
                cursor = next_cursor
                continue
            if current < total_pages and items:
                page = current + 1
                continue
            break
        return out

    def list_children(self, mount_path: str) -> list[FsEntry]:
        path = _norm_path(mount_path)
        params = {"path": path if path != "/" else "/", "operation": "list"}
        try:
            items = self._list_all(params)
        except Exception:
            parent_id = self._resolve_id(path)
            if parent_id is None and path != "/":
                return []
            params = {
                "parentId": parent_id if parent_id else "root",
                "operation": "list",
            }
            items = self._list_all(params)

        children = [self._file_to_entry(it, fallback_parent=path) for it in items]
        result = []
        for e in children:
            parent = _parent_path(e.path)
            if parent == path or (path == "/" and e.path.count("/") == 1):
                result.append(e)
            else:
                rebuilt = FsEntry(
                    path=_norm_path(f"{path.rstrip('/')}/{e.name}"),
                    name=e.name,
                    is_dir=e.is_dir,
                    size=e.size,
                    mtime=e.mtime,
                    file_id=e.file_id,
                )
                self._remember(rebuilt)
                result.append(rebuilt)
        return result

    def propfind(self, mount_path: str, depth: int = 1) -> list[FsEntry]:
        path = _norm_path(mount_path.rstrip("/") or "/")
        self_entry = self.stat(path)
        entries: list[FsEntry] = []
        if self_entry is not None:
            entries.append(self_entry)
        elif path == "/":
            entries.append(
                FsEntry(path="/", name="", is_dir=True, size=0, mtime=0.0, file_id="")
            )
        if depth >= 1:
            entries.extend(self.list_children(path))
        return entries

    def _find_by_path(self, mount_path: str) -> FsEntry | None:
        path = _norm_path(mount_path)
        if path == "/":
            return FsEntry(path="/", name="", is_dir=True, size=0, mtime=0.0, file_id="")

        # Prefer local memory from prior listings.
        with self._id_lock:
            fid = self._path_to_id.get(path)
            if fid and fid in self._id_to_entry:
                return self._id_to_entry[fid]

        try:
            items, _ = self._list_page(
                {"path": path, "operation": "find", "limit": "1"}
            )
            if items:
                return self._file_to_entry(items[0], fallback_parent=_parent_path(path))
            # Empty find result => does not exist. Do NOT list the parent (was very slow).
            return None
        except Exception as exc:
            log.debug("find by path failed %s: %s", path, exc)

        # Network find failed: last resort, list parent once.
        parent = _parent_path(path)
        name = path.rsplit("/", 1)[-1]
        try:
            for child in self.list_children(parent):
                if child.name == name:
                    return child
        except Exception:
            log.debug("list parent for find failed %s", parent, exc_info=True)
        return None

    def _resolve_id(self, mount_path: str) -> str | None:
        path = _norm_path(mount_path)
        with self._id_lock:
            cached = self._path_to_id.get(path)
        if cached is not None and cached != "":
            return cached
        if path == "/":
            try:
                items, _ = self._list_page(
                    {
                        "parentId": "nil",
                        "operation": "find",
                        "name": "root",
                        "type": "folder",
                        "limit": "1",
                    }
                )
                if items:
                    entry = self._file_to_entry(items[0])
                    root = FsEntry(
                        path="/",
                        name="",
                        is_dir=True,
                        size=0,
                        mtime=entry.mtime,
                        file_id=entry.file_id,
                    )
                    self._remember(root)
                    return entry.file_id
            except Exception:
                log.debug("root id lookup failed", exc_info=True)
            return None

        entry = self._find_by_path(path)
        return entry.file_id if entry else None

    def stat(self, mount_path: str) -> FsEntry | None:
        path = _norm_path(mount_path)
        if path == "/":
            with self._id_lock:
                fid = self._path_to_id.get("/") or ""
            return FsEntry(path="/", name="", is_dir=True, size=0, mtime=0.0, file_id=fid)

        with self._id_lock:
            fid = self._path_to_id.get(path)
            if fid and fid in self._id_to_entry:
                return self._id_to_entry[fid]

        return self._find_by_path(path)

    def get_by_id(self, file_id: str) -> FsEntry | None:
        with self._id_lock:
            hit = self._id_to_entry.get(file_id)
        if hit is not None:
            return hit
        resp = self._http_get(f"/api/files/{file_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._file_to_entry(resp.json())

    def _stream_urls(self, file_id: str, name: str) -> list[str]:
        enc = quote(name, safe="")
        name_url = f"/api/files/{file_id}/{enc}"
        content_url = f"/api/files/{file_id}/content"
        if self._stream_style == "content":
            return [content_url, name_url]
        return [name_url, content_url]

    def read_range(self, mount_path: str, start: int, end: int) -> bytes:
        if end < start:
            return b""
        want = end - start + 1

        path = _norm_path(mount_path)
        entry = self.stat(path)
        if entry is None or entry.is_dir:
            raise FileNotFoundError(path)
        if not entry.file_id:
            raise FileNotFoundError(f"no file id for {path}")

        headers = {"Range": f"bytes={start}-{end}", "Accept": "*/*"}
        last_exc: Exception | None = None
        for url_path in self._stream_urls(entry.file_id, entry.name or "file"):
            url = self.api_host + url_path
            req_headers = dict(self._headers)
            req_headers.update(headers)
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
                    if status == 404:
                        last_exc = FileNotFoundError(url)
                        continue
                    if status not in (200, 206):
                        body = resp.read(4096)
                        log.warning(
                            "GET Range %s [%s-%s] via %s -> %s",
                            path,
                            start,
                            end,
                            url_path,
                            status,
                        )
                        last_exc = urllib.error.HTTPError(
                            url,
                            status,
                            body.decode("utf-8", errors="replace")[:200],
                            hdrs=None,
                            fp=None,  # type: ignore[arg-type]
                        )
                        continue
                    if status == 200 and start > 0:
                        log.warning(
                            "Teldrive ignored Range for %s (HTTP 200 at start=%s)",
                            path,
                            start,
                        )
                        last_exc = OSError(
                            f"Teldrive ignored Range request for {path}"
                        )
                        continue
                    data = resp.read(want)
                    self._stream_style = (
                        "content" if "/content" in url_path else "name"
                    )
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code == 416:
                    return b""
                if exc.code == 404:
                    last_exc = FileNotFoundError(url)
                    continue
                last_exc = exc
                log.warning(
                    "GET Range %s [%s-%s] via %s -> %s",
                    path,
                    start,
                    end,
                    url_path,
                    exc.code,
                )
            except Exception as exc:
                last_exc = exc
                log.warning("GET Range %s via %s failed: %s", path, url_path, exc)
        if last_exc:
            raise last_exc
        raise FileNotFoundError(path)

    def forget(self, mount_path: str) -> None:
        path = _norm_path(mount_path)
        with self._id_lock:
            fid = self._path_to_id.pop(path, None)
            if fid:
                self._id_to_entry.pop(fid, None)

    def mkdir(self, mount_path: str) -> None:
        path = _norm_path(mount_path)
        if path == "/":
            return
        resp = self._http_request(
            "POST",
            "/api/files/mkdir",
            json_body={"path": path},
        )
        if resp.status_code not in (200, 201, 204):
            log.warning("mkdir %s -> %s %s", path, resp.status_code, resp.text[:200])
            resp.raise_for_status()

    def delete_ids(self, ids: list[str]) -> None:
        ids = [i for i in ids if i]
        if not ids:
            return
        resp = self._http_request(
            "POST",
            "/api/files/delete",
            json_body={"ids": ids},
        )
        if resp.status_code not in (200, 204):
            log.warning("delete %s -> %s %s", ids, resp.status_code, resp.text[:200])
            resp.raise_for_status()

    def move(
        self,
        ids: list[str],
        destination_parent: str,
        destination_name: str | None = None,
    ) -> None:
        ids = [i for i in ids if i]
        if not ids:
            return
        body: dict[str, Any] = {
            "ids": ids,
            "destinationParent": destination_parent,
        }
        if destination_name:
            body["destinationName"] = destination_name
        resp = self._http_request("POST", "/api/files/move", json_body=body)
        if resp.status_code not in (200, 204):
            log.warning("move %s -> %s %s", ids, resp.status_code, resp.text[:200])
            resp.raise_for_status()

    def upload_part(
        self,
        upload_id: str,
        data: bytes,
        *,
        file_name: str,
        part_no: int,
        part_name: str,
        channel_id: int = 0,
        encrypted: bool = False,
        hashing: bool = False,
        timeout: float | None = None,
    ) -> None:
        params: dict[str, Any] = {
            "fileName": file_name,
            "partName": part_name,
            "partNo": part_no,
            "encrypted": str(encrypted).lower(),
            "hashing": str(hashing).lower(),
        }
        if channel_id:
            params["channelId"] = channel_id
        resp = self._http_request(
            "POST",
            f"/api/uploads/{upload_id}",
            params=params,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(data)),
            },
            data=data,
            timeout=timeout,
        )
        if resp.status_code not in (200, 201, 204):
            log.warning(
                "upload part %s#%s -> %s %s",
                upload_id,
                part_no,
                resp.status_code,
                resp.text[:200],
            )
            resp.raise_for_status()

    def create_file(
        self,
        *,
        name: str,
        parent_path: str,
        size: int,
        upload_id: str = "",
        mime_type: str = "application/octet-stream",
        channel_id: int = 0,
        encrypted: bool = False,
        parent_id: str | None = None,
    ) -> FsEntry | None:
        parent_path = _norm_path(parent_path)
        body: dict[str, Any] = {
            "name": name,
            "type": "file",
            "size": int(size),
            "mimeType": mime_type,
            "encrypted": encrypted,
            "path": parent_path,
        }
        if parent_id:
            body["parentId"] = parent_id
        if upload_id:
            body["uploadId"] = upload_id
        if channel_id:
            body["channelId"] = channel_id
        resp = self._http_request("POST", "/api/files", json_body=body)
        if resp.status_code not in (200, 201):
            log.warning("create file %s -> %s %s", name, resp.status_code, resp.text[:200])
            resp.raise_for_status()
        if not resp.content:
            # Some servers return 201 with empty body — resolve by path.
            return self.stat(_norm_path(f"{parent_path.rstrip('/')}/{name}"))
        return self._file_to_entry(resp.json(), fallback_parent=parent_path)

    def update_file(
        self,
        file_id: str,
        *,
        name: str | None = None,
        size: int | None = None,
        upload_id: str | None = None,
        parent_id: str | None = None,
        channel_id: int | None = None,
        encrypted: bool | None = None,
    ) -> FsEntry | None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if size is not None:
            body["size"] = int(size)
        if upload_id is not None:
            body["uploadId"] = upload_id
        if parent_id is not None:
            body["parentId"] = parent_id
        if channel_id is not None:
            body["channelId"] = channel_id
        if encrypted is not None:
            body["encrypted"] = encrypted
        resp = self._http_request("PATCH", f"/api/files/{file_id}", json_body=body)
        if resp.status_code not in (200, 204):
            log.warning("update file %s -> %s %s", file_id, resp.status_code, resp.text[:200])
            resp.raise_for_status()
        if resp.content:
            return self._file_to_entry(resp.json())
        return self.get_by_id(file_id)
