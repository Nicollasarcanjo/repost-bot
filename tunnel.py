"""Hospeda os arquivos prontos numa URL pública temporária.

A API de publicação do Instagram não aceita upload direto de arquivo: ela baixa
a mídia de uma URL pública. Este módulo sobe um servidor HTTP local que só
serve a pasta data/public e, se PUBLIC_BASE_URL não estiver definido, abre um
"quick tunnel" gratuito do Cloudflare (cloudflared) apontando para ele.

Os arquivos têm nomes aleatórios e são apagados logo depois da publicação.
"""
from __future__ import annotations

import functools
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TRYCF_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class TunnelError(Exception):
    pass


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: N802
        log.debug("http: " + fmt, *args)

    def list_directory(self, path):  # não lista a pasta
        self.send_error(404)
        return None


class MediaHost:
    def __init__(self, public_dir: Path, port: int, base_url: str = "",
                 cloudflared: str = "cloudflared"):
        self.public_dir = public_dir
        self.port = port
        self.fixed_base_url = base_url
        self.cloudflared = cloudflared
        self.base_url = base_url
        self._server: ThreadingHTTPServer | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- servidor
    def start(self) -> None:
        self.public_dir.mkdir(parents=True, exist_ok=True)
        (self.public_dir / "ping.txt").write_text("ok")
        handler = functools.partial(_QuietHandler, directory=str(self.public_dir))
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        log.info("servidor local em http://127.0.0.1:%s", self.port)
        if not self.fixed_base_url:
            self._start_tunnel()

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._server:
            self._server.shutdown()

    # ---------------------------------------------------------------- túnel
    def _start_tunnel(self) -> None:
        exe = shutil.which(self.cloudflared) or (
            self.cloudflared if Path(self.cloudflared).is_file() else None)
        if not exe:
            raise TunnelError(
                "cloudflared não encontrado. Instale com: winget install Cloudflare.cloudflared "
                "(ou defina PUBLIC_BASE_URL no .env)")
        self._proc = subprocess.Popen(
            [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        found: dict[str, str] = {}

        def reader():
            assert self._proc and self._proc.stdout
            for line in self._proc.stdout:
                if "url" not in found:
                    m = TRYCF_RE.search(line)
                    if m:
                        found["url"] = m.group(0)
                log.debug("cloudflared: %s", line.rstrip())

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.time() + 45
        while "url" not in found and time.time() < deadline:
            if self._proc.poll() is not None:
                raise TunnelError("cloudflared fechou ao iniciar (veja data/bot.log)")
            time.sleep(0.5)
        if "url" not in found:
            raise TunnelError("cloudflared não informou a URL do túnel a tempo")
        self.base_url = found["url"]
        log.info("túnel público: %s", self.base_url)

    def _tunnel_alive(self) -> bool:
        if self.fixed_base_url:
            return True
        return self._proc is not None and self._proc.poll() is None

    def _reachable(self, timeout: float = 90) -> bool:
        """Espera a URL pública responder (o DNS do túnel leva alguns segundos)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = httpx.get(f"{self.base_url}/ping.txt", timeout=10)
                if r.status_code == 200 and r.text.strip() == "ok":
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(3)
        return False

    def ensure_ready(self) -> str:
        """Garante um túnel vivo e acessível; recria se tiver caído."""
        with self._lock:
            if not self._tunnel_alive():
                log.warning("túnel caiu, recriando")
                self._start_tunnel()
            if not self._reachable():
                if self.fixed_base_url:
                    raise TunnelError(f"{self.base_url} não está acessível")
                log.warning("túnel não responde, recriando")
                if self._proc and self._proc.poll() is None:
                    self._proc.terminate()
                self._start_tunnel()
                if not self._reachable():
                    raise TunnelError("o túnel do Cloudflare não ficou acessível")
            return self.base_url

    # ---------------------------------------------------------------- arquivos
    def publish_file(self, path: Path) -> tuple[str, Path]:
        """Copia o arquivo para a pasta pública com nome aleatório; retorna (url, caminho)."""
        name = f"{uuid.uuid4().hex}{path.suffix.lower()}"
        dest = self.public_dir / name
        shutil.copy2(path, dest)
        return f"{self.base_url}/{name}", dest
