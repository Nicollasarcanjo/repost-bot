"""Bot do Telegram que reposta memes no Instagram (post, reel, carrossel e story).

Mande (ou compartilhe) para o bot um link de Instagram, TikTok ou X — ou envie
direto uma foto/vídeo. O texto que vier junto com o link vira a legenda.
Escreva #story, #post ou #ambos na mensagem para escolher o destino na hora.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, InputMediaVideo, Message, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import config
from downloader import DownloadError, download, extract_url
from history import History
from instagram import InstagramClient, InstagramError, PublishItem
from media import (MediaError, PreparedItem, check_tools, make_stories, make_thumbnail,
                   prepare_all)
from panel import PanelServer
from settings import DEST_LABEL, settings
from tunnel import MediaHost, TunnelError

# ---------------------------------------------------------------- logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(),
              RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3,
                                  encoding="utf-8")],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("repost-bot")

DEST_TAGS = {"#story": "story", "#stories": "story", "#post": "feed", "#feed": "feed",
             "#ambos": "both", "#both": "both"}
DEST_TAG_RE = re.compile(r"(?<!\w)(#stor(?:y|ies)|#post|#feed|#ambos|#both)\b", re.IGNORECASE)


# ---------------------------------------------------------------- estado
@dataclass
class Job:
    id: str
    chat_id: int
    status_msg: Message
    caption_text: str
    url: str | None = None
    destination: str | None = None  # None = usar o padrão
    local_file: Path | None = None  # mídia enviada direto pelo Telegram
    workdir: Path = field(default_factory=Path)
    items: list[PreparedItem] = field(default_factory=list)
    caption: str = ""
    thumb: str = ""
    ready_to_publish: bool = False


class Runtime:
    """O que o painel enxerga do bot."""

    def __init__(self):
        self.started_at = time.time()
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.pending: dict[str, Job] = {}
        self.history = History(config.HISTORY_FILE)
        self.host = MediaHost(config.PUBLIC_DIR, config.LOCAL_PORT, config.PUBLIC_BASE_URL,
                              config.CLOUDFLARED_PATH)
        self.ig = InstagramClient(config.IG_ACCESS_TOKEN, config.TOKEN_FILE,
                                  config.GRAPH_VERSION)
        self.current: str | None = None  # o que o worker está fazendo agora
        self.bot_username = ""

    def cookies_file(self) -> str | None:
        if config.UPLOADED_COOKIES.is_file():
            return str(config.UPLOADED_COOKIES)
        return config.COOKIES_FILE or None


rt = Runtime()


def allowed(update: Update) -> bool:
    user = update.effective_user
    return settings.is_allowed(user.id if user else None)


def build_caption(user_text: str, original: str) -> str:
    text = user_text.strip()
    if not text and settings.keep_original_caption:
        text = original.strip()
    if not text:
        text = settings.default_caption.strip()
    tags = settings.hashtags.strip()
    if tags:
        text = f"{text}\n\n{tags}" if text else tags
    return text[:2200]


def split_destination(text: str) -> tuple[str, str | None]:
    """Tira #story/#post/#ambos do texto e devolve (texto, destino)."""
    found = DEST_TAG_RE.findall(text)
    dest = DEST_TAGS[found[-1].lower()] if found else None
    clean = DEST_TAG_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", clean).strip(), dest


async def set_status(job: Job, text: str, markup=None) -> None:
    try:
        await job.status_msg.edit_text(text, reply_markup=markup,
                                       disable_web_page_preview=True)
    except Exception:  # mensagem igual / apagada — não é crítico
        pass


def cleanup(job: Job) -> None:
    shutil.rmtree(job.workdir, ignore_errors=True)


def panel_url() -> str:
    return f"http://localhost:{config.PANEL_PORT}"


# ---------------------------------------------------------------- comandos
HELP = (
    "Me mande um link de Instagram, TikTok ou X (ou uma foto/vídeo) e eu reposto no seu "
    "Instagram.\n\n"
    "• O texto junto com o link vira a legenda.\n"
    "• #story, #post ou #ambos na mensagem escolhe o destino só daquele envio.\n\n"
    "/legenda — ver ou trocar a legenda padrão\n"
    "/hashtags — ver ou trocar as hashtags fixas\n"
    "/destino — post, story ou ambos por padrão\n"
    "/pausar e /retomar — parar de publicar por um tempo\n"
    "/status — conta, fila e túnel\n"
    "/painel — endereço do painel de configuração"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not allowed(update):
        await update.message.reply_text(
            f"Seu ID do Telegram é {uid}.\nAdicione esse número em \"Quem pode usar o bot\" "
            f"no painel ({panel_url()}) para liberar o acesso.")
        return
    await update.message.reply_text(HELP)


async def cmd_legenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _text_setting(update, context, "default_caption", "Legenda padrão",
                        "/legenda Seu texto aqui")


async def cmd_hashtags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _text_setting(update, context, "hashtags", "Hashtags fixas",
                        "/hashtags #memes #humor", plural=True)


async def _text_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str,
                        label: str, example: str, plural: bool = False) -> None:
    if not allowed(update):
        return
    msg = update.effective_message
    # pega o texto bruto depois do comando, preservando quebras de linha
    raw = (msg.text or "").split(maxsplit=1)
    value = raw[1].strip() if len(raw) > 1 else ""
    if not value:
        current = getattr(settings, key) or "(vazia)"
        await msg.reply_text(f"{label} {'atuais' if plural else 'atual'}:\n\n{current}\n\n"
                             f"Para trocar: {example}\nPara apagar: {example.split()[0]} limpar")
        return
    if value.lower() in ("limpar", "apagar", "nenhuma", "-"):
        value = ""
    try:
        settings.update({key: value})
    except ValueError as e:
        await msg.reply_text(f"❌ {e}")
        return
    word = ("apagada" if not value else "atualizada") + ("s" if plural else "")
    await msg.reply_text(f"✅ {label} {word}."
                         + (f"\n\n{value}" if value else ""))


async def cmd_destino(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    arg = " ".join(context.args).lower().strip() if context.args else ""
    mapping = {"post": "feed", "feed": "feed", "story": "story", "stories": "story",
               "ambos": "both", "both": "both"}
    if arg in mapping:
        settings.update({"destination": mapping[arg]})
        await update.message.reply_text(f"✅ Destino padrão: {DEST_LABEL[mapping[arg]]}")
        return
    cur = settings.destination
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton(("● " if cur == d else "") + DEST_LABEL[d].capitalize(),
                             callback_data=f"dest:{d}")
        for d in ("feed", "story", "both")]])
    await update.message.reply_text(f"Destino padrão: {DEST_LABEL[cur]}. Trocar para:",
                                    reply_markup=buttons)


async def cmd_pausar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        settings.update({"paused": True})
        await update.message.reply_text("⏸️ Pausado. Links recebidos agora serão ignorados. "
                                        "Use /retomar para voltar.")


async def cmd_retomar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        settings.update({"paused": False})
        await update.message.reply_text("▶️ Publicação retomada.")


async def cmd_painel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text(
            f"Painel: {panel_url()}\n(abre só no PC onde o bot está rodando)")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    lines = []
    try:
        _, username = await asyncio.to_thread(rt.ig.whoami)
        quota = await asyncio.to_thread(rt.ig.publishing_quota)
        lines.append(f"📸 Instagram: @{username} — {quota}")
    except InstagramError as e:
        lines.append(f"❌ Instagram: {e}")
    lines.append(f"{'⏸️ Pausado' if settings.paused else '▶️ Ativo'} • destino padrão: "
                 f"{DEST_LABEL[settings.destination]}")
    lines.append(f"🌐 Túnel: {rt.host.base_url or 'não iniciado'}")
    lines.append(f"📥 Na fila: {rt.queue.qsize()} • aguardando você: {len(rt.pending)}")
    lines.append(f"🖥️ Painel: {panel_url()}")
    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


# ---------------------------------------------------------------- entrada
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    url = extract_url(text)
    attachment = msg.photo[-1] if msg.photo else (msg.video or msg.animation or msg.document)

    if not url and not attachment:
        await msg.reply_text("Não achei um link de Instagram, TikTok ou X nessa mensagem.")
        return
    if settings.paused:
        await msg.reply_text("⏸️ O bot está pausado. Use /retomar para voltar a publicar.")
        return

    caption_text = text.replace(url, "").strip() if url else text.strip()
    caption_text, dest = split_destination(caption_text)

    if url and settings.skip_duplicates and (prev := rt.history.find_published(url)):
        await msg.reply_text(f"⚠️ Esse post já foi publicado em {prev['at'][:10]}:\n"
                             f"{prev.get('permalink') or '(sem link)'}",
                             disable_web_page_preview=True)
        return

    job_id = uuid.uuid4().hex[:10]
    status = await msg.reply_text("⏳ Na fila…")
    job = Job(id=job_id, chat_id=msg.chat_id, status_msg=status, caption_text=caption_text,
              url=url, destination=dest, workdir=config.WORK_DIR / job_id)
    job.workdir.mkdir(parents=True, exist_ok=True)

    if not url and attachment:
        try:
            tg_file = await attachment.get_file()
        except Exception as e:  # o bot só baixa arquivos de até 20 MB
            await set_status(job, f"❌ Não consegui baixar o arquivo do Telegram: {e}")
            cleanup(job)
            return
        name = Path(tg_file.file_path or "arquivo").name
        if msg.photo and "." not in name:
            name += ".jpg"
        dest_file = job.workdir / "src" / name
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        await tg_file.download_to_drive(dest_file)
        job.local_file = dest_file

    await rt.queue.put(job)


# ---------------------------------------------------------------- processamento
async def prepare(job: Job) -> None:
    original = ""
    if job.url:
        rt.current = "baixando"
        await set_status(job, "⏬ Baixando…")
        result = await asyncio.to_thread(download, job.url, job.workdir / "src",
                                         rt.cookies_file())
        files, original = result.files, result.description
        if result.watermarked:
            await job.status_msg.reply_text(
                "⚠️ Só encontrei a versão com marca d'água desse vídeo. Vou usar ela mesmo.")
    else:
        files = [job.local_file]

    rt.current = "ajustando formato"
    await set_status(job, "🛠️ Ajustando formato para o Instagram…")
    job.items = await asyncio.to_thread(prepare_all, files, job.workdir / "out",
                                        settings.pad_color, settings.max_carousel_items)
    job.caption = build_caption(job.caption_text, original)
    thumb = await asyncio.to_thread(make_thumbnail, job.items[0],
                                    config.THUMBS_DIR / f"{job.id}.jpg")
    job.thumb = thumb.name if thumb else ""


def feed_kind(job: Job) -> str:
    if len(job.items) > 1:
        return "carrossel"
    return "reel" if job.items[0].kind == "video" else "post"


async def publish(job: Job) -> None:
    dest = job.destination or settings.destination
    rt.current = f"publicando ({DEST_LABEL[dest]})"
    await set_status(job, f"📤 Publicando como {DEST_LABEL[dest]}…")
    base = await asyncio.to_thread(rt.host.ensure_ready)
    log.info("job %s: %s, %d item(ns) via %s", job.id, dest, len(job.items), base)

    public_files: list[Path] = []

    def expose(item: PreparedItem) -> PublishItem:
        url, path = rt.host.publish_file(item.path)
        public_files.append(path)
        return PublishItem(url, item.kind)

    done: list[str] = []
    permalink = ""
    try:
        if dest in ("feed", "both"):
            permalink = await asyncio.to_thread(
                rt.ig.publish, [expose(it) for it in job.items], job.caption)
            done.append(feed_kind(job))

        if dest in ("story", "both"):
            try:
                stories, trimmed = await asyncio.to_thread(
                    make_stories, job.items, job.workdir / "stories", settings.pad_color)
                if trimmed:
                    await job.status_msg.reply_text(
                        f"✂️ {trimmed} vídeo(s) passavam de 60 s e foram cortados para a story.")
                for st in stories:
                    await asyncio.to_thread(rt.ig.publish_story, expose(st))
            except Exception as e:
                if done:  # o post saiu, só a story falhou — registra o post como publicado
                    rt.history.add(job_id=job.id, url=job.url, status="ok", destination="feed",
                                   kind=done[0], permalink=permalink, caption=job.caption,
                                   thumb=job.thumb)
                    await set_status(job, f"⚠️ O {done[0]} foi publicado, mas a story falhou: {e}"
                                          f"\n{permalink}")
                    return
                raise
            done.append("story" if len(stories) == 1 else f"{len(stories)} stories")
    finally:
        for p in public_files:
            p.unlink(missing_ok=True)

    rt.history.add(job_id=job.id, url=job.url, status="ok", destination=dest,
                   kind=" + ".join(done), permalink=permalink, caption=job.caption,
                   thumb=job.thumb)
    text = f"✅ Publicado: {' + '.join(done)}!"
    if permalink:
        text += f"\n{permalink}"
    await set_status(job, text)


async def send_preview(job: Job, app: Application) -> None:
    group, handles = [], []
    for i, it in enumerate(job.items[:10]):
        cap = (job.caption[:1000] or None) if i == 0 else None
        f = it.path.open("rb")
        handles.append(f)
        group.append(InputMediaPhoto(f, caption=cap) if it.kind == "image"
                     else InputMediaVideo(f, caption=cap, supports_streaming=True))
    try:
        await app.bot.send_media_group(job.chat_id, group, read_timeout=120,
                                       write_timeout=120)
    except Exception as e:  # prévia acima de 50 MB, etc. — segue sem ela
        log.warning("prévia falhou: %s", e)
    finally:
        for f in handles:
            f.close()
    feed_label = feed_kind(job).capitalize()
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📰 {feed_label}", callback_data=f"go:feed:{job.id}"),
         InlineKeyboardButton("📱 Story", callback_data=f"go:story:{job.id}")],
        [InlineKeyboardButton(f"{feed_label} + story", callback_data=f"go:both:{job.id}"),
         InlineKeyboardButton("❌ Cancelar", callback_data=f"no::{job.id}")],
    ])
    rt.pending[job.id] = job
    await set_status(job, "👀 Prévia acima. Publicar como?", buttons)


async def worker(app: Application) -> None:
    while True:
        job = await rt.queue.get()
        try:
            if not job.ready_to_publish:
                await prepare(job)
                if settings.confirm_before_post and job.destination is None:
                    await send_preview(job, app)
                    continue  # a publicação acontece no botão
            await publish(job)
            cleanup(job)
        except Exception as e:
            if isinstance(e, DownloadError):
                log.warning("job %s: download falhou: %s", job.id, e)
                text = ("❌ Não consegui baixar esse post.\nSe for do Instagram (ou um post "
                        f"privado), confira os cookies no painel.\n\nDetalhe: {str(e)[-300:]}")
            elif isinstance(e, (MediaError, InstagramError, TunnelError)):
                log.warning("job %s: %s", job.id, e)
                text = f"❌ Falhou: {e}"
            else:
                log.exception("job %s: erro inesperado", job.id)
                text = f"❌ Erro inesperado: {e}"
            rt.history.add(job_id=job.id, url=job.url, status="erro",
                           destination=job.destination or settings.destination,
                           caption=job.caption, thumb=job.thumb, error=str(e)[-400:])
            await set_status(job, text)
            cleanup(job)
        finally:
            rt.current = None
            rt.queue.task_done()


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    parts = (q.data or "").split(":")

    if parts[0] == "dest" and len(parts) == 2:
        settings.update({"destination": parts[1]})
        await q.edit_message_text(f"✅ Destino padrão: {DEST_LABEL[parts[1]]}")
        return

    if len(parts) != 3:
        return
    action, dest, job_id = parts
    job = rt.pending.pop(job_id, None)
    if not job:
        await q.edit_message_text("Esse pedido já foi resolvido ou expirou.")
        return
    if action == "no":
        cleanup(job)
        await set_status(job, "🚫 Cancelado.")
        return
    job.destination = dest
    job.ready_to_publish = True
    await set_status(job, "⏳ Na fila para publicar…")
    await rt.queue.put(job)


# ---------------------------------------------------------------- tarefas periódicas
async def refresh_token_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.to_thread(rt.ig.refresh_token_if_needed)
    except InstagramError as e:
        log.error("falha ao renovar token: %s", e)
        for uid in settings.allowed_user_ids:
            await context.bot.send_message(uid, f"⚠️ Não consegui renovar o token do Instagram: {e}")


async def post_init(app: Application) -> None:
    missing = check_tools()
    if missing:
        raise SystemExit(f"Faltando no PATH: {', '.join(missing)}. Instale o ffmpeg "
                         "(winget install Gyan.FFmpeg) e reabra o terminal.")
    me = await app.bot.get_me()
    rt.bot_username = me.username or ""

    panel = PanelServer(rt, config.PANEL_PORT, config.PANEL_PASSWORD)
    panel.start()
    log.info("Painel em %s", panel_url())

    username = None
    if rt.ig.connected:
        try:
            _, username = await asyncio.to_thread(rt.ig.whoami)
            log.info("Instagram conectado: @%s", username)
        except InstagramError as e:
            log.error("Instagram: %s — corrija pelo painel", e)
    else:
        log.warning("Instagram não conectado — cole o token no painel (%s)", panel_url())

    await asyncio.to_thread(rt.host.start)
    app.create_task(worker(app))
    app.job_queue.run_repeating(refresh_token_job, interval=12 * 3600, first=60)

    await app.bot.set_my_commands([
        BotCommand("legenda", "Ver ou trocar a legenda padrão"),
        BotCommand("hashtags", "Ver ou trocar as hashtags fixas"),
        BotCommand("destino", "Post, story ou ambos"),
        BotCommand("pausar", "Parar de publicar"),
        BotCommand("retomar", "Voltar a publicar"),
        BotCommand("status", "Conta, fila e túnel"),
        BotCommand("painel", "Endereço do painel"),
    ])
    hello = (f"🟢 Bot online — postando em @{username}" if username
             else f"🟡 Bot online, mas o Instagram não está conectado. Abra o painel: {panel_url()}")
    for chat in settings.allowed_user_ids:
        try:
            await app.bot.send_message(chat, hello)
        except Exception:
            pass


async def post_shutdown(app: Application) -> None:
    rt.host.stop()


def main() -> None:
    if not config.TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN não configurado no .env")
    if not settings.allowed_user_ids:
        log.warning("Ninguém liberado ainda: mande /start para o bot e adicione o ID no painel.")

    app = (Application.builder().token(config.TELEGRAM_TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown)
           .concurrent_updates(True).build())
    app.add_handler(CommandHandler(["start", "ajuda", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("legenda", cmd_legenda))
    app.add_handler(CommandHandler("hashtags", cmd_hashtags))
    app.add_handler(CommandHandler("destino", cmd_destino))
    app.add_handler(CommandHandler("pausar", cmd_pausar))
    app.add_handler(CommandHandler("retomar", cmd_retomar))
    app.add_handler(CommandHandler("painel", cmd_painel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL)
        & ~filters.COMMAND, on_message))
    log.info("Bot iniciado. Ctrl+C para parar.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
