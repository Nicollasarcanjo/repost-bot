"""Histórico de envios (para o painel e para evitar repostagem)."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from downloader import normalize_url

MAX_ENTRIES = 300


def platform_of(url: str | None) -> str:
    if not url:
        return "telegram"
    host = urlparse(url).netloc.lower()
    if "tiktok" in host:
        return "tiktok"
    if "instagr" in host:
        return "instagram"
    return "x"


class History:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.entries: list[dict] = []
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "entries" in raw:
                    self.entries = raw["entries"]
                elif isinstance(raw, dict):  # formato antigo {url: {...}}
                    self.entries = [{"id": k, "key": k, "source_url": "https://" + k,
                                     "platform": platform_of("https://" + k), "status": "ok",
                                     "destination": "feed", **v} for k, v in raw.items()]
            except ValueError:
                self.entries = []

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": self.entries[:MAX_ENTRIES]}, indent=1,
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def find_published(self, url: str) -> dict | None:
        key = normalize_url(url)
        with self._lock:
            return next((e for e in self.entries
                         if e.get("key") == key and e.get("status") == "ok"), None)

    def add(self, *, job_id: str, url: str | None, status: str, destination: str = "",
            kind: str = "", permalink: str = "", caption: str = "", thumb: str = "",
            error: str = "") -> None:
        entry = {
            "id": job_id,
            "key": normalize_url(url) if url else None,
            "source_url": url or "",
            "platform": platform_of(url),
            "status": status,
            "destination": destination,
            "kind": kind,
            "permalink": permalink,
            "caption": caption,
            "thumb": thumb,
            "error": error,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        with self._lock:
            self.entries.insert(0, entry)
            del self.entries[MAX_ENTRIES:]
            self._save()

    def recent(self, limit: int = 60) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self.entries[:limit]]

    def counts_today(self) -> dict:
        today = datetime.now().date().isoformat()
        with self._lock:
            todays = [e for e in self.entries if str(e.get("at", "")).startswith(today)]
        return {"ok": sum(e["status"] == "ok" for e in todays),
                "failed": sum(e["status"] != "ok" for e in todays)}
