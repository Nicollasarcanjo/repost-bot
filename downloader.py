"""Baixa imagens e vídeos de Instagram, TikTok e X.

Estratégia: tenta o yt-dlp primeiro (melhor para vídeo e já pega o TikTok sem
marca d'água). Se não vier nenhuma imagem/vídeo (post só de foto, carrossel,
slideshow do TikTok), cai para o gallery-dl.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from yt_dlp import YoutubeDL

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
SUPPORTED_HOSTS = (
    "instagram.com", "instagr.am",
    "tiktok.com",
    "x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com",
)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}

# Melhor qualidade disponível que NÃO seja a versão com marca d'água do TikTok.
# A versão com marca d'água só é usada se for a única existente.
YTDLP_FORMAT = (
    "bv*[format_note!*=watermark]+ba/"
    "b[format_note!*=watermark]/"
    "bv*+ba/b"
)


class DownloadError(Exception):
    pass


@dataclass
class DownloadResult:
    files: list[Path] = field(default_factory=list)
    description: str = ""
    watermarked: bool = False


def extract_url(text: str | None) -> str | None:
    if not text:
        return None
    for url in URL_RE.findall(text):
        url = url.rstrip(".,);]")
        if any(h in url.lower() for h in SUPPORTED_HOSTS):
            return url
    return None


def normalize_url(url: str) -> str:
    """Versão do link usada para detectar repostagens (sem parâmetros de rastreio)."""
    url = url.split("#")[0].split("?")[0].rstrip("/").lower()
    url = re.sub(r"^https?://(www\.|m\.|vm\.|vt\.)?", "", url)
    for alias in ("fxtwitter.com", "vxtwitter.com", "fixupx.com", "twitter.com"):
        url = url.replace(alias, "x.com")
    return url


def _media_files(folder: Path) -> list[Path]:
    files = [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXT | VIDEO_EXT
        and not p.name.endswith(".part")
    ]
    return sorted(files, key=lambda p: (str(p.parent), p.name))


def _ytdlp(url: str, dest: Path, cookies: str | None) -> DownloadResult:
    opts = {
        "outtmpl": str(dest / "%(playlist_index|0)03d_%(id)s.%(ext)s"),
        "format": YTDLP_FORMAT,
        "format_sort": ["res:1080", "vcodec:h264", "acodec:aac"],
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": False,
        "playlistend": 20,
        "socket_timeout": 30,
        "retries": 3,
    }
    if cookies:
        opts["cookiefile"] = cookies

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    entries = info.get("entries") or [info]
    entries = [e for e in entries if e]
    description = (info.get("description") or entries[0].get("description") or "").strip()
    watermarked = any("watermark" in str(e.get("format_note", "")).lower() for e in entries)
    return DownloadResult(_media_files(dest), description, watermarked)


def _gallery_dl(url: str, dest: Path, cookies: str | None) -> DownloadResult:
    cmd = [sys.executable, "-m", "gallery_dl", "--directory", str(dest), "--no-mtime"]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    files = _media_files(dest)
    if not files:
        raise DownloadError((proc.stderr or proc.stdout or "sem saída").strip()[-500:])
    return DownloadResult(files)


def download(url: str, dest: Path, cookies: str | None = None) -> DownloadResult:
    dest.mkdir(parents=True, exist_ok=True)
    cookies = cookies if cookies and Path(cookies).is_file() else None
    errors: list[str] = []

    try:
        result = _ytdlp(url, dest, cookies)
        if result.files:
            return result
        errors.append("yt-dlp: nenhuma imagem ou vídeo no resultado")
    except Exception as e:  # yt-dlp falha em posts só de imagem
        log.info("yt-dlp falhou (%s), tentando gallery-dl", e)
        errors.append(f"yt-dlp: {e}")

    # Limpa restos (ex.: áudio do slideshow do TikTok) antes do fallback
    for p in dest.rglob("*"):
        if p.is_file():
            p.unlink(missing_ok=True)

    try:
        result = _gallery_dl(url, dest, cookies)
        if result.files:
            return result
        errors.append("gallery-dl: nenhum arquivo baixado")
    except Exception as e:
        errors.append(f"gallery-dl: {e}")

    raise DownloadError("\n".join(errors))
