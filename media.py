"""Converte o que foi baixado para o formato que a API do Instagram aceita.

Imagens: JPEG, proporção entre 4:5 (0,8) e 1,91:1 — fora disso, adiciona bordas
em vez de cortar o meme. Vídeos: MP4 H.264 + AAC, yuv420p, até 1080 de largura,
com áudio (adiciona trilha muda se não tiver) e no mínimo 3 segundos.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

try:  # suporte a .heic, se o pacote estiver instalado
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

from downloader import IMAGE_EXT, VIDEO_EXT

log = logging.getLogger(__name__)

MIN_RATIO, MAX_RATIO = 4 / 5, 1.91
MAX_IMAGE_WIDTH = 1440
MAX_VIDEO_WIDTH = 1080
MIN_VIDEO_SECONDS = 3.0


class MediaError(Exception):
    pass


@dataclass
class PreparedItem:
    path: Path
    kind: str  # "image" ou "video"


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) != 6:
        return (0, 0, 0)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"{Path(cmd[0]).name} falhou: {proc.stderr.strip()[-600:]}")
    return proc


def check_tools() -> list[str]:
    return [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]


# ---------------------------------------------------------------- imagens
def _is_animated(path: Path) -> bool:
    try:
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1) > 1
    except Exception:
        return False


def prepare_image(src: Path, out_dir: Path, pad_color: str = "#000000") -> Path:
    bg = _hex_to_rgb(pad_color)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        im.seek(0) if hasattr(im, "seek") else None
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            base = Image.new("RGB", im.size, (255, 255, 255))
            base.paste(im, mask=im.split()[-1])
            im = base
        else:
            im = im.convert("RGB")

        w, h = im.size
        ratio = w / h
        if ratio < MIN_RATIO:  # alta demais → bordas nas laterais
            new_w = round(h * MIN_RATIO)
            canvas = Image.new("RGB", (new_w, h), bg)
            canvas.paste(im, ((new_w - w) // 2, 0))
            im = canvas
        elif ratio > MAX_RATIO:  # larga demais → bordas em cima/embaixo
            new_h = round(w / MAX_RATIO)
            canvas = Image.new("RGB", (w, new_h), bg)
            canvas.paste(im, (0, (new_h - h) // 2))
            im = canvas

        if im.width > MAX_IMAGE_WIDTH:
            im = im.resize((MAX_IMAGE_WIDTH, round(im.height * MAX_IMAGE_WIDTH / im.width)),
                           Image.LANCZOS)
        elif im.width < 320:  # Instagram amplia para 320; fazemos nós para manter qualidade
            im = im.resize((320, round(im.height * 320 / im.width)), Image.LANCZOS)

        out = out_dir / f"{uuid.uuid4().hex}.jpg"
        im.save(out, "JPEG", quality=95, optimize=True)
    return out


# ---------------------------------------------------------------- vídeos
def probe(path: Path) -> dict:
    proc = _run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                 "-of", "json", str(path)])
    data = json.loads(proc.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or (v or {}).get("duration") or 0)
    return {"video": v, "audio": a, "duration": duration}


def prepare_video(src: Path, out_dir: Path) -> Path:
    info = probe(src)
    v, a, duration = info["video"], info["audio"], info["duration"]
    if v is None:
        raise MediaError("o arquivo não tem faixa de vídeo")

    out = out_dir / f"{uuid.uuid4().hex}.mp4"
    width = int(v.get("width") or 0)
    height = int(v.get("height") or 0)
    compliant_video = (
        v.get("codec_name") == "h264"
        and v.get("pix_fmt") == "yuv420p"
        and width <= MAX_VIDEO_WIDTH
        and width % 2 == 0 and height % 2 == 0
    )
    compliant_audio = a is not None and a.get("codec_name") == "aac"
    too_short = 0 < duration < MIN_VIDEO_SECONDS

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if too_short:  # repete o vídeo até passar de 3 s
        cmd += ["-stream_loop", str(int(MIN_VIDEO_SECONDS // duration) + 1)]
    cmd += ["-i", str(src)]
    if a is None:  # Reels sem áudio às vezes são recusados → trilha muda
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    cmd += ["-map", "0:v:0", "-map", "0:a:0" if a is not None else "1:a:0"]

    if compliant_video and not too_short:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-vf", f"scale='trunc(min({MAX_VIDEO_WIDTH},iw)/2)*2':-2"]
    if compliant_audio and not too_short:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]

    if a is None or too_short:
        cmd += ["-shortest"]
    if too_short:
        cmd += ["-t", f"{max(MIN_VIDEO_SECONDS, duration) + 0.2:.2f}"]
    cmd += ["-movflags", "+faststart", str(out)]

    try:
        _run(cmd)
    except MediaError:
        if "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy":
            log.info("cópia direta falhou, recodificando %s", src.name)
            _transcode_full(src, out, has_audio=a is not None)
        else:
            raise
    return out


def _transcode_full(src: Path, out: Path, has_audio: bool) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-vf", f"scale='trunc(min({MAX_VIDEO_WIDTH},iw)/2)*2':-2",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-movflags", "+faststart", str(out)]
    _run(cmd)


def gif_to_mp4(src: Path, out_dir: Path) -> Path:
    tmp = out_dir / f"{uuid.uuid4().hex}_gif.mp4"
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
          "-movflags", "+faststart", "-pix_fmt", "yuv420p", "-c:v", "libx264",
          "-vf", "scale='trunc(iw/2)*2':'trunc(ih/2)*2'", str(tmp)])
    return tmp


# ---------------------------------------------------------------- geral
def prepare_all(files: list[Path], out_dir: Path, pad_color: str,
                max_items: int = 10) -> list[PreparedItem]:
    out_dir.mkdir(parents=True, exist_ok=True)
    items: list[PreparedItem] = []
    for f in files[:max_items]:
        ext = f.suffix.lower()
        if ext in IMAGE_EXT and _is_animated(f):
            items.append(PreparedItem(prepare_video(gif_to_mp4(f, out_dir), out_dir), "video"))
        elif ext in IMAGE_EXT:
            items.append(PreparedItem(prepare_image(f, out_dir, pad_color), "image"))
        elif ext in VIDEO_EXT:
            items.append(PreparedItem(prepare_video(f, out_dir), "video"))
    if not items:
        raise MediaError("nenhum arquivo de mídia utilizável")
    return items


# ---------------------------------------------------------------- stories
STORY_W, STORY_H = 1080, 1920
MAX_STORY_SECONDS = 60.0


def make_story_image(src: Path, out_dir: Path, bg_color: str = "#000000") -> Path:
    """Centraliza a imagem numa tela 9:16 (1080x1920) sem cortar nada."""
    bg = _hex_to_rgb(bg_color)
    with Image.open(src) as im:
        im = im.convert("RGB")
        scale = min(STORY_W / im.width, STORY_H / im.height)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                       Image.LANCZOS)
        canvas = Image.new("RGB", (STORY_W, STORY_H), bg)
        canvas.paste(im, ((STORY_W - im.width) // 2, (STORY_H - im.height) // 2))
        out = out_dir / f"{uuid.uuid4().hex}_story.jpg"
        canvas.save(out, "JPEG", quality=95, optimize=True)
    return out


def make_story_video(src: Path, out_dir: Path, bg_color: str = "#000000") -> tuple[Path, bool]:
    """Vídeo 9:16 com bordas, cortado em 60 s. Retorna (arquivo, foi_cortado)."""
    duration = probe(src)["duration"]
    trimmed = duration > MAX_STORY_SECONDS
    color = "0x" + bg_color.lstrip("#")
    out = out_dir / f"{uuid.uuid4().hex}_story.mp4"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-vf", (f"scale={STORY_W}:{STORY_H}:force_original_aspect_ratio=decrease,"
                   f"pad={STORY_W}:{STORY_H}:(ow-iw)/2:(oh-ih)/2:color={color},setsar=1"),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
    if trimmed:
        cmd += ["-t", f"{MAX_STORY_SECONDS:.0f}"]
    cmd += ["-movflags", "+faststart", str(out)]
    _run(cmd)
    return out, trimmed


def make_stories(items: list[PreparedItem], out_dir: Path,
                 bg_color: str) -> tuple[list[PreparedItem], int]:
    """Uma story por item. Retorna (stories, quantos vídeos foram cortados)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stories, trimmed = [], 0
    for it in items:
        if it.kind == "image":
            stories.append(PreparedItem(make_story_image(it.path, out_dir, bg_color), "image"))
        else:
            path, cut = make_story_video(it.path, out_dir, bg_color)
            trimmed += cut
            stories.append(PreparedItem(path, "video"))
    return stories, trimmed


# ---------------------------------------------------------------- miniatura
def make_thumbnail(item: PreparedItem, dest: Path, width: int = 360) -> Path | None:
    """Miniatura JPEG para o histórico do painel."""
    try:
        if item.kind == "video":
            frame = dest.with_suffix(".frame.jpg")
            seek = min(1.0, max(0.0, probe(item.path)["duration"] / 3))
            _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{seek:.2f}",
                  "-i", str(item.path), "-frames:v", "1", str(frame)])
            src = frame
        else:
            src = item.path
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((width, width * 2))
            im.save(dest, "JPEG", quality=82)
        if item.kind == "video":
            src.unlink(missing_ok=True)
        return dest
    except Exception as e:
        log.debug("miniatura falhou: %s", e)
        return None
