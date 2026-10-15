"""Configurações que podem mudar com o bot rodando (painel e comandos do Telegram).

Os valores iniciais vêm do .env; o que for alterado pelo painel ou pelo Telegram
fica salvo em data/settings.json e vale a partir do próximo post, sem reiniciar.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import config

DESTINATIONS = ("feed", "story", "both")
DEST_LABEL = {"feed": "post", "story": "story", "both": "post + story"}

DEFAULTS: dict[str, Any] = {
    "paused": False,
    "destination": config.DEFAULT_DESTINATION if config.DEFAULT_DESTINATION in DESTINATIONS else "feed",
    "confirm_before_post": config.CONFIRM_BEFORE_POST,
    "default_caption": config.DEFAULT_CAPTION,
    "hashtags": config.HASHTAGS,
    "keep_original_caption": config.KEEP_ORIGINAL_CAPTION,
    "skip_duplicates": config.SKIP_DUPLICATES,
    "pad_color": config.PAD_COLOR,
    "max_carousel_items": config.MAX_CAROUSEL_ITEMS,
    "allowed_user_ids": sorted(config.ALLOWED_USER_IDS),
}


def _clean(key: str, value: Any) -> Any:
    """Valida e normaliza um valor vindo do painel/Telegram."""
    if key not in DEFAULTS:
        raise ValueError(f"configuração desconhecida: {key}")
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "sim", "on", "yes")
        return bool(value)
    if key == "destination":
        if value not in DESTINATIONS:
            raise ValueError("destino deve ser feed, story ou both")
        return value
    if key == "max_carousel_items":
        n = int(value)
        if not 1 <= n <= 10:
            raise ValueError("o carrossel aceita de 1 a 10 itens")
        return n
    if key == "pad_color":
        v = str(value).strip()
        if not (len(v) == 7 and v.startswith("#")
                and all(c in "0123456789abcdefABCDEF" for c in v[1:])):
            raise ValueError("cor deve estar no formato #RRGGBB")
        return v.lower()
    if key == "allowed_user_ids":
        if isinstance(value, str):
            value = value.replace(";", ",").replace(" ", ",").split(",")
        ids = sorted({int(x) for x in value if str(x).strip().lstrip("-").isdigit()})
        return ids
    if key in ("default_caption", "hashtags"):
        v = str(value).strip()
        if len(v) > 2000:
            raise ValueError("texto longo demais (máx. 2000 caracteres)")
        return v
    return value


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        if path.is_file():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                for k, v in saved.items():
                    if k in DEFAULTS:
                        try:
                            self._data[k] = _clean(k, v)
                        except (ValueError, TypeError):
                            pass
            except ValueError:
                pass

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_") or key not in DEFAULTS:
            raise AttributeError(key)
        with self._lock:
            return self._data[key]

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        cleaned = {k: _clean(k, v) for k, v in changes.items()}
        with self._lock:
            self._data.update(cleaned)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
            return dict(self._data)

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_user_ids


settings = Settings(config.SETTINGS_FILE)
