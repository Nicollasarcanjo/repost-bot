"""Configuração do bot, lida do arquivo .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "sim", "yes", "on")


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}


# --- Telegram ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
# Só esses IDs podem usar o bot. Mande /start para o bot para descobrir o seu.
ALLOWED_USER_IDS = _ids("ALLOWED_USER_IDS")

# --- Instagram (Instagram API with Instagram Login) ---
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v25.0").strip()

# --- Painel de configuração (só abre no próprio PC: http://localhost:PANEL_PORT) ---
PANEL_PORT = int(os.getenv("PANEL_PORT", "8080"))
# Opcional: senha para abrir o painel (usuário qualquer)
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "").strip()

# --- Comportamento (valores iniciais; depois o painel/Telegram manda) ---
# feed = post/reel/carrossel • story • both = os dois
DEFAULT_DESTINATION = os.getenv("DEFAULT_DESTINATION", "feed").strip().lower()
# true = o bot mostra uma prévia e espera você tocar em "Postar"
CONFIRM_BEFORE_POST = _bool("CONFIRM_BEFORE_POST", False)
# Usa a legenda original do post quando você não escrever uma
KEEP_ORIGINAL_CAPTION = _bool("KEEP_ORIGINAL_CAPTION", False)
DEFAULT_CAPTION = os.getenv("DEFAULT_CAPTION", "").replace("\\n", "\n")
HASHTAGS = os.getenv("HASHTAGS", "").strip()
# Avisa (e não reposta) links que já foram publicados antes
SKIP_DUPLICATES = _bool("SKIP_DUPLICATES", True)
# Cor das bordas quando a imagem precisa ser ajustada à proporção do Instagram
PAD_COLOR = os.getenv("PAD_COLOR", "#000000").strip()
MAX_CAROUSEL_ITEMS = int(os.getenv("MAX_CAROUSEL_ITEMS", "10"))

# --- Download ---
# Arquivo cookies.txt (formato Netscape) — necessário para Instagram e às vezes X
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()

# --- Hospedagem temporária dos arquivos (a API da Meta exige uma URL pública) ---
# Se vazio, o bot abre um túnel gratuito do Cloudflare automaticamente.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
CLOUDFLARED_PATH = os.getenv("CLOUDFLARED_PATH", "cloudflared").strip()
LOCAL_PORT = int(os.getenv("LOCAL_PORT", "8787"))

# --- Pastas ---
DATA_DIR = BASE_DIR / "data"
WORK_DIR = DATA_DIR / "work"
PUBLIC_DIR = DATA_DIR / "public"
TOKEN_FILE = DATA_DIR / "ig_token.json"
HISTORY_FILE = DATA_DIR / "history.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
THUMBS_DIR = DATA_DIR / "thumbs"
UPLOADED_COOKIES = DATA_DIR / "cookies.txt"  # cookies enviados pelo painel
PANEL_DIR = BASE_DIR / "panel"
LOG_FILE = DATA_DIR / "bot.log"

for d in (DATA_DIR, WORK_DIR, PUBLIC_DIR, THUMBS_DIR):
    d.mkdir(parents=True, exist_ok=True)
