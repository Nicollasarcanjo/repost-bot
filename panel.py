"""Painel de configuração local (http://localhost:PANEL_PORT).

Escuta só em 127.0.0.1 — não fica acessível pela rede nem pelo túnel do
Cloudflare (que aponta para outra porta e só serve as mídias). Recusa pedidos
com Host diferente de localhost, o que bloqueia sites tentando falar com ele.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import config
from instagram import InstagramError
from settings import settings

if TYPE_CHECKING:
    from bot import Runtime

log = logging.getLogger(__name__)

MAX_BODY = 1_000_000
# No Windows o registro às vezes mapeia .js/.css errado
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


def _cookie_domains(text: str) -> list[str]:
    domains = set()
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domains.add(parts[0].replace("#HttpOnly_", "").lstrip("."))
    return sorted(domains)


class PanelServer:
    def __init__(self, rt: "Runtime", port: int, password: str = ""):
        self.rt = rt
        self.port = port
        self.password = password
        self._account_cache: tuple[float, dict] | None = None
        self._cache_lock = threading.Lock()

    # ---------------------------------------------------------------- dados
    def account(self, force: bool = False) -> dict:
        with self._cache_lock:
            if not force and self._account_cache and time.time() - self._account_cache[0] < 60:
                return self._account_cache[1]
        ig = self.rt.ig
        info: dict[str, Any] = {"connected": ig.connected}
        if ig.connected:
            try:
                ig.whoami()
                info.update(username=ig.username, picture=ig.picture,
                            account_type=ig.account_type, token=ig.token_info())
                try:
                    info["quota"] = ig.quota()
                except InstagramError as e:
                    info["quota_error"] = str(e)
            except InstagramError as e:
                info["error"] = str(e)
        with self._cache_lock:
            self._account_cache = (time.time(), info)
        return info

    def cookies(self) -> dict:
        if config.UPLOADED_COOKIES.is_file():
            path, source = config.UPLOADED_COOKIES, "painel"
        elif config.COOKIES_FILE and Path(config.COOKIES_FILE).is_file():
            path, source = Path(config.COOKIES_FILE), "env"
        else:
            return {"source": None, "domains": []}
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"source": source, "domains": _cookie_domains(text),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime)
                .isoformat(timespec="seconds")}

    def status(self) -> dict:
        rt = self.rt
        return {
            "bot": {"username": rt.bot_username, "uptime": int(time.time() - rt.started_at),
                    "current": rt.current, "queue": rt.queue.qsize(),
                    "pending": len(rt.pending)},
            "tunnel": {"url": rt.host.base_url},
            "today": rt.history.counts_today(),
            "settings": settings.all(),
            "instagram": self.account(),
            "cookies": self.cookies(),
            "panel_port": self.port,
        }

    # ---------------------------------------------------------------- servidor
    def start(self) -> None:
        panel = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "RepostPanel"

            def log_message(self, fmt, *args):  # noqa: N802
                log.debug("painel: " + fmt, *args)

            # ---- helpers
            def _send(self, code: int, body: bytes, ctype: str, cache: bool = False) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data: Any, code: int = 200) -> None:
                self._send(code, json.dumps(data, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")

            def _error(self, msg: str, code: int = 400) -> None:
                self._json({"error": msg}, code)

            def _guard(self) -> bool:
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                if host not in ("localhost", "127.0.0.1"):
                    self._error("host não permitido", 403)
                    return False
                if panel.password:
                    auth = self.headers.get("Authorization", "")
                    ok = False
                    if auth.startswith("Basic "):
                        try:
                            _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                            ok = hmac.compare_digest(pw, panel.password)
                        except Exception:
                            ok = False
                    if not ok:
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", 'Basic realm="Repost Bot"')
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return False
                return True

            def _body(self) -> dict | None:
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    self._error("envie JSON", 415)
                    return None
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self._error("conteúdo grande demais", 413)
                    return None
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._error("JSON inválido")
                    return None

            def _static(self, base: Path, name: str, cache: bool = False) -> None:
                path = (base / name).resolve()
                if base.resolve() not in path.parents or not path.is_file():
                    self._error("não encontrado", 404)
                    return
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                self._send(200, path.read_bytes(), ctype, cache)

            # ---- rotas
            def do_GET(self):  # noqa: N802
                if not self._guard():
                    return
                url = urlparse(self.path)
                p = url.path
                if p in ("/", "/index.html"):
                    return self._static(config.PANEL_DIR, "index.html")
                if p.startswith("/assets/"):
                    return self._static(config.PANEL_DIR, p.removeprefix("/"), cache=True)
                if p.startswith("/thumbs/"):
                    return self._static(config.THUMBS_DIR, p.removeprefix("/thumbs/"), cache=True)
                if p == "/api/status":
                    force = "fresh" in parse_qs(url.query)
                    if force:
                        panel.account(force=True)
                    return self._json(panel.status())
                if p == "/api/history":
                    limit = int(parse_qs(url.query).get("limit", ["60"])[0])
                    return self._json({"entries": panel.rt.history.recent(min(limit, 300))})
                self._error("não encontrado", 404)

            def do_POST(self):  # noqa: N802
                if not self._guard():
                    return
                body = self._body()
                if body is None:
                    return
                p = urlparse(self.path).path
                try:
                    if p == "/api/settings":
                        return self._json({"settings": settings.update(body)})
                    if p == "/api/instagram/token":
                        panel.rt.ig.set_token(str(body.get("token", "")))
                        return self._json({"instagram": panel.account(force=True)})
                    if p == "/api/instagram/refresh":
                        panel.rt.ig.refresh_token_if_needed(force=True)
                        return self._json({"instagram": panel.account(force=True)})
                    if p == "/api/cookies":
                        text = str(body.get("content", ""))
                        if not text.strip():
                            config.UPLOADED_COOKIES.unlink(missing_ok=True)
                            return self._json({"cookies": panel.cookies()})
                        if not _cookie_domains(text):
                            return self._error("isso não parece um cookies.txt (formato Netscape)")
                        config.UPLOADED_COOKIES.write_text(text, encoding="utf-8")
                        return self._json({"cookies": panel.cookies()})
                except (ValueError, TypeError) as e:
                    return self._error(str(e))
                except InstagramError as e:
                    return self._error(f"Instagram: {e}", 502)
                self._error("não encontrado", 404)

        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True, name="panel").start()
        self._httpd = httpd

    def stop(self) -> None:
        if getattr(self, "_httpd", None):
            self._httpd.shutdown()
