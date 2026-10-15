"""Publicação no Instagram pela API oficial (Instagram API with Instagram Login).

Fluxo: cria um "container" de mídia apontando para a URL pública do arquivo,
espera a Meta processar (status FINISHED) e publica. Carrossel = um container
por item + um container CAROUSEL com os filhos.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

HOST = "https://graph.instagram.com"
REFRESH_AFTER = timedelta(days=20)  # token dura 60 dias; renovamos bem antes


class InstagramError(Exception):
    pass


@dataclass
class PublishItem:
    url: str
    kind: str  # "image" | "video"


class InstagramClient:
    def __init__(self, initial_token: str, token_file: Path, version: str = "v25.0"):
        self.version = version
        self.token_file = token_file
        self.http = httpx.Client(timeout=60)
        self.token = initial_token
        self.env_token = initial_token  # o do .env; só vence o salvo se for um token novo
        self.refreshed_at = datetime.now(timezone.utc)
        self._load_token()
        self.user_id: str | None = None
        self.username: str | None = None
        self.picture = ""
        self.account_type = ""

    # ---------------------------------------------------------------- token
    def _load_token(self) -> None:
        if self.token_file.is_file():
            data = json.loads(self.token_file.read_text())
            # Um token novo colado no .env vence o salvo
            if not self.token or data.get("source_token") == self.token:
                self.token = data["token"]
                self.refreshed_at = datetime.fromisoformat(data["refreshed_at"])
                return
        if self.token:  # sem token: a conta é conectada depois, pelo painel
            self._save_token(source=self.token)

    def _save_token(self, source: str | None = None) -> None:
        prev = {}
        if self.token_file.is_file():
            prev = json.loads(self.token_file.read_text())
        self.token_file.write_text(json.dumps({
            "token": self.token,
            "refreshed_at": self.refreshed_at.isoformat(),
            "source_token": source or prev.get("source_token", ""),
        }, indent=2))

    def refresh_token_if_needed(self, force: bool = False) -> bool:
        if not self.token:
            return False
        if not force and datetime.now(timezone.utc) - self.refreshed_at < REFRESH_AFTER:
            return False
        r = self.http.get(f"{HOST}/refresh_access_token",
                          params={"grant_type": "ig_refresh_token", "access_token": self.token})
        data = self._check(r)
        self.token = data["access_token"]
        self.refreshed_at = datetime.now(timezone.utc)
        self._save_token()
        log.info("token do Instagram renovado (expira em %s dias)",
                 int(data.get("expires_in", 0)) // 86400)
        return True

    # ---------------------------------------------------------------- base
    def _url(self, path: str) -> str:
        return f"{HOST}/{self.version}/{path.lstrip('/')}"

    @staticmethod
    def _check(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except ValueError:
            raise InstagramError(f"resposta inválida ({r.status_code}): {r.text[:300]}")
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            msg = err.get("error_user_msg") or err.get("message") or r.text[:300]
            code = err.get("code")
            sub = err.get("error_subcode")
            raise InstagramError(f"{msg} (code {code}{f'/{sub}' if sub else ''})")
        return data

    @property
    def connected(self) -> bool:
        return bool(self.token)

    def _require(self) -> None:
        if not self.token:
            raise InstagramError("conta do Instagram não conectada — cole o token no painel")

    def _get(self, path: str, **params) -> dict:
        self._require()
        params["access_token"] = self.token
        return self._check(self.http.get(self._url(path), params=params))

    def _post(self, path: str, **data) -> dict:
        self._require()
        data["access_token"] = self.token
        return self._check(self.http.post(self._url(path), data=data))

    # ---------------------------------------------------------------- conta
    def whoami(self) -> tuple[str, str]:
        data = self._get("me", fields="user_id,username,account_type,profile_picture_url")
        self.user_id = str(data.get("user_id") or data["id"])
        self.username = data.get("username", "?")
        self.picture = data.get("profile_picture_url", "")
        self.account_type = data.get("account_type", "")
        return self.user_id, self.username

    def token_info(self) -> dict:
        expires = self.refreshed_at + timedelta(days=60)
        return {"refreshed_at": self.refreshed_at.isoformat(timespec="seconds"),
                "expires_at": expires.isoformat(timespec="seconds"),
                "days_left": max(0, (expires - datetime.now(timezone.utc)).days)}

    def set_token(self, token: str) -> tuple[str, str]:
        """Troca a conta/token. Só salva se o token novo funcionar."""
        token = token.strip()
        if not token:
            raise InstagramError("token vazio")
        old = (self.token, self.refreshed_at, self.user_id, self.username)
        self.token, self.refreshed_at = token, datetime.now(timezone.utc)
        try:
            result = self.whoami()
        except InstagramError:
            self.token, self.refreshed_at, self.user_id, self.username = old
            raise
        # marca o token do .env como "já visto", para o do painel continuar valendo
        self._save_token(source=self.env_token or token)
        return result

    def quota(self) -> dict:
        if not self.user_id:
            self.whoami()
        data = self._get(f"{self.user_id}/content_publishing_limit", fields="quota_usage,config")
        row = (data.get("data") or [{}])[0]
        return {"used": row.get("quota_usage"),
                "total": (row.get("config") or {}).get("quota_total")}

    def publishing_quota(self) -> str:
        q = self.quota()
        return f"{q['used'] if q['used'] is not None else '?'}/{q['total'] or '?'} nas últimas 24h"

    # ---------------------------------------------------------------- publicação
    def _create(self, **params) -> str:
        return self._post(f"{self.user_id}/media", **params)["id"]

    def _wait(self, container_id: str, timeout: int = 900) -> None:
        deadline = time.time() + timeout
        delay = 3
        while time.time() < deadline:
            data = self._get(container_id, fields="status_code,status")
            status = data.get("status_code")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise InstagramError(f"a Meta recusou a mídia: {data.get('status') or status}")
            time.sleep(delay)
            delay = min(delay + 2, 15)
        raise InstagramError("tempo esgotado esperando a Meta processar a mídia")

    def publish(self, items: list[PublishItem], caption: str) -> str:
        """Publica e retorna o link do post."""
        if not self.user_id:
            self.whoami()
        caption = caption[:2200]

        if len(items) == 1:
            it = items[0]
            if it.kind == "image":
                cid = self._create(image_url=it.url, caption=caption)
            else:
                cid = self._create(media_type="REELS", video_url=it.url,
                                   caption=caption, share_to_feed="true")
            self._wait(cid)
        else:
            children = []
            for it in items:
                if it.kind == "image":
                    children.append(self._create(image_url=it.url, is_carousel_item="true"))
                else:
                    children.append(self._create(media_type="VIDEO", video_url=it.url,
                                                 is_carousel_item="true"))
            for c in children:
                self._wait(c)
            cid = self._create(media_type="CAROUSEL", children=",".join(children),
                               caption=caption)
            self._wait(cid)

        media_id = self._publish_with_retry(cid)
        try:
            return self._get(media_id, fields="permalink").get("permalink", "")
        except InstagramError:
            return ""

    def publish_story(self, item: PublishItem) -> str:
        """Publica uma story (imagem ou vídeo 9:16). Retorna o id da mídia."""
        if not self.user_id:
            self.whoami()
        if item.kind == "image":
            cid = self._create(media_type="STORIES", image_url=item.url)
        else:
            cid = self._create(media_type="STORIES", video_url=item.url)
        self._wait(cid)
        return self._publish_with_retry(cid)

    def _publish_with_retry(self, creation_id: str) -> str:
        last: Exception | None = None
        for attempt in range(4):
            try:
                return self._post(f"{self.user_id}/media_publish", creation_id=creation_id)["id"]
            except InstagramError as e:  # às vezes a mídia ainda "não está pronta"
                last = e
                time.sleep(5 * (attempt + 1))
        raise last  # type: ignore[misc]
