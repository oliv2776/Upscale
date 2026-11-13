"""Analyse ffprobe et construction des commandes FFmpeg."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from . import config
from .settings import model_scales, render_params

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".vob",
}
# Sous-titres que Matroska accepte tels quels.
MKV_SUBTITLE_COPY = {
    "subrip", "srt", "ass", "ssa", "webvtt", "text",
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle",
}
# Sous-titres texte à convertir en SRT (mov_text des MP4 notamment).
MKV_SUBTITLE_TO_SRT = {
    "mov_text", "microdvd", "subviewer", "subviewer1", "sami", "realtext",
    "jacosub", "mpl2", "pjs", "vplayer", "stl",
}
# Audio non accepté par Matroska : converti en FLAC (sans perte).
MKV_AUDIO_TO_FLAC = {"pcm_dvd", "pcm_bluray"}
DENOISE_FILTERS = {
    "light": "hqdn3d=1.5:1.5:6:6",
    "medium": "hqdn3d=3:3:9:9",
    "strong": "nlmeans=s=3:p=7:r=15",
}
# Anti-blocs avant l'IA : les vieilles vidéos très compressées (FLV, MPEG-4, 240p/360p) ont des blocs de
# 8 pixels que l'IA prendrait pour du détail et renforcerait.
DEBLOCK_FILTERS = {
    "light": "deblock=filter=weak:block=8",
    "strong": "deblock=filter=strong:block=8:alpha=0.14:beta=0.07:gamma=0.07:delta=0.07",
}
# Débruitage « Fort » sur la carte graphique (Vulkan : AMD, Intel, NVIDIA) quand FFmpeg et le pilote le
# permettent ; sinon version processeur, identique mais beaucoup plus lente.
NLMEANS_CPU = DENOISE_FILTERS["strong"]
NLMEANS_VULKAN = "format=yuv420p,hwupload,nlmeans_vulkan=s=3:p=7:r=15,hwdownload,format=yuv420p"
VULKAN_GLOBALS = ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]
_vulkan_denoise: dict[str, bool] = {}
IMAGE_CODECS = {"mjpeg", "png", "bmp", "gif", "webp", "tiff"}
# Intermédiaire sans perte FFV1 intra (chaque image est une image clé), encodé sur tous les cœurs.
FFV1_ARGS = ["-c:v", "ffv1", "-level", "3", "-slices", "16", "-threads", "0", "-g", "1"]
SVT_PRESETS = {"ultrafast": 12, "fast": 8, "medium": 6, "slow": 4, "slower": 3}
FFMPEG_BASE = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-nostats", "-progress", "pipe:1"]


class ProbeError(Exception):
    pass


def ffprobe_json(path: Path, *args: str, timeout: int = 60) -> dict[str, Any]:
    probe = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=timeout)
    if probe.returncode:
        raise ProbeError(f"ffprobe a échoué sur {path.name} : {probe.stderr.strip()[-300:]}")
    return json.loads(probe.stdout or "{}")


def probe_metadata(path: Path) -> tuple[int, int, float | None] | None:
    """Dimensions et durée de la vraie piste vidéo (pas d'une jaquette)."""
    try:
        data = ffprobe_json(path, "-select_streams", "v", "-show_entries",
                            "stream=codec_name,width,height,disposition,duration:format=duration", timeout=30)
        streams = data.get("streams", [])
        video = next((s for s in streams
                      if not s.get("disposition", {}).get("attached_pic")
                      and s.get("codec_name") not in IMAGE_CODECS), None)
        if video is None:
            video = next((s for s in streams if not s.get("disposition", {}).get("attached_pic")), None)
        if not video:
            return None
        duration = (parse_ratio(data.get("format", {}).get("duration"))
                    or parse_ratio(video.get("duration")))
        return int(video["width"]), int(video["height"]), (duration if duration > 0 else None)
    except (ProbeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def probe_dimensions(path: Path) -> tuple[int, int] | None:
    metadata = probe_metadata(path)
    return (metadata[0], metadata[1]) if metadata else None


def parse_ratio(raw: Any, default: float = 0.0) -> float:
    try:
        text = str(raw)
        if ":" in text or "/" in text:
            num, den = re.split(r"[:/]", text, maxsplit=1)
            return float(num) / float(den) if float(den) else default
        return float(text)
    except (TypeError, ValueError):
        return default


def probe_duration(path: Path) -> float:
    try:
        data = ffprobe_json(path, "-show_entries", "format=duration", timeout=30)
    except (ProbeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return 0.0
    return parse_ratio(data.get("format", {}).get("duration"))


def probe_source(path: Path) -> dict[str, Any]:
    data = ffprobe_json(path, "-show_entries",
                        "stream=index,codec_type,codec_name,width,height,sample_aspect_ratio,field_order,"
                        "avg_frame_rate,r_frame_rate,nb_frames,start_time,disposition,color_space,color_range:"
                        "format=duration")
    streams = data.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    # Une image fixe (couverture) n'est jamais la vidéo à traiter, même placée en premier.
    video = next((s for s in videos if s.get("codec_name") not in IMAGE_CODECS), videos[0] if videos else None)
    if not video:
        raise ProbeError("Aucune piste vidéo dans la source")
    width, height = int(video["width"]), int(video["height"])
    fps = parse_ratio(video.get("avg_frame_rate")) or parse_ratio(video.get("r_frame_rate")) or 25.0
    duration = parse_ratio(data.get("format", {}).get("duration"))
    frames = int(parse_ratio(video.get("nb_frames"))) or int(duration * fps)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    video_start = parse_ratio(video.get("start_time"))
    starts = [parse_ratio(s.get("start_time")) for s in [video, *audio, *subtitles] if s.get("start_time") not in (None, "N/A")]
    return {
        "width": width, "height": height, "video_index": int(video.get("index", 0)),
        "sar": parse_ratio(video.get("sample_aspect_ratio"), 1.0) or 1.0,
        "fps": fps, "duration": duration, "frames": frames,
        "field_order": str(video.get("field_order", "unknown")),
        "color_space": str(video.get("color_space") or ""),
        "audio": [s.get("codec_name", "") for s in audio],
        "subtitles": [s.get("codec_name", "") for s in subtitles],
        # Décalage de la vidéo par rapport au premier flux : à réappliquer à la nouvelle vidéo.
        "video_offset": max(0.0, video_start - min(starts)) if starts else 0.0,
    }


# ---------------------------------------------------------------- calculs

def choose_scale(model: str, source_height: int, output_height: int) -> int:
    """Plus petit facteur du modèle qui atteint la hauteur demandée."""
    scales = model_scales(model)
    needed = output_height / max(source_height, 1)
    for scale in scales:
        if scale >= needed:
            return scale
    return scales[-1]


def output_size(info: dict[str, Any], output_height: int) -> tuple[int, int]:
    """Taille finale en pixels carrés qui respecte le format d'affichage (DVD anamorphiques)."""
    display_aspect = info["width"] * info["sar"] / info["height"]
    width = max(2, int(round(output_height * display_aspect / 2)) * 2)
    return width, output_height


def output_relative(rel: Path, ambiguous: bool, suffix: str = "") -> Path:
    """film.avi -> film<suffixe>.mkv, ou film.avi<suffixe>.mkv si un autre film.* existe à côté."""
    stem = rel.name if ambiguous else rel.stem
    return rel.with_name(f"{stem}{suffix}.mkv")


def needs_deinterlace(mode: str, info: dict[str, Any]) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    return info.get("field_order") in {"tt", "bb", "tb", "bt"}


def prefilters(settings: dict[str, Any], info: dict[str, Any]) -> list[str]:
    filters = []
    if needs_deinterlace(settings.get("deinterlace", "auto"), info):
        filters.append("bwdif=mode=send_frame")
    deblock = DEBLOCK_FILTERS.get(settings.get("deblock", "off"))
    if deblock:
        filters.append(deblock)
    denoise = DENOISE_FILTERS.get(settings.get("denoise", "off"))
    if denoise:
        filters.append(denoise)
    return filters


def vulkan_denoise_available() -> bool:
    """Vrai si FFmpeg sait débruiter sur une vraie carte graphique ici (essai réel, une seule fois).

    Un Vulkan logiciel (llvmpipe : conteneur sans accès au GPU) est refusé : il serait plus lent que le
    filtre processeur. UPSCALE_GPU_DENOISE=force l'accepte quand même (tests).
    """
    if "ok" not in _vulkan_denoise:
        ok = False
        if config.GPU_DENOISE:
            try:
                test = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-nostdin", "-v", "verbose", *VULKAN_GLOBALS, "-f", "lavfi",
                     "-i", "testsrc2=size=128x96:rate=5:duration=0.4", "-vf", NLMEANS_VULKAN, "-f", "null", "-"],
                    capture_output=True, text=True, errors="replace", timeout=60)
                selected = next((line for line in test.stderr.splitlines() if "selected:" in line), "")
                software = "(software)" in selected or "llvmpipe" in selected.lower()
                ok = test.returncode == 0 and (not software or config.GPU_DENOISE == "force")
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        _vulkan_denoise["ok"] = ok
    return _vulkan_denoise["ok"]


def accelerated(filters: list[str]) -> tuple[list[str], list[str]]:
    """(options globales FFmpeg, filtres) : remplace le débruitage « Fort » par sa version carte graphique.

    L'empreinte de reprise d'une tâche garde les noms « processeur » : une tâche commencée avant
    l'accélération reprend sans repartir de zéro.
    """
    if NLMEANS_CPU in filters and vulkan_denoise_available():
        return list(VULKAN_GLOBALS), [NLMEANS_VULKAN if f == NLMEANS_CPU else f for f in filters]
    return [], filters


# ---------------------------------------------------------------- couleurs

SD_MATRICES = {"smpte170m", "bt470bg"}


def color_plan(info: dict[str, Any], output_height: int) -> dict[str, str] | None:
    """Matrices de couleur de la source et de la sortie.

    Une vidéo SD est codée en BT.601 ; au-dessus de 720 lignes, les lecteurs supposent du BT.709. Sans
    conversion, une source 360p agrandie en 720p est lue avec la mauvaise matrice : rouges et teints de
    peau décalés. La sortie HD est donc convertie en BT.709 et étiquetée. None : espace non géré (HDR /
    BT.2020...), laissé tel quel.
    """
    space = str(info.get("color_space") or "").lower()
    if space in SD_MATRICES:
        source = "smpte170m"
    elif space == "bt709":
        source = "bt709"
    elif space in {"", "unknown", "unspecified", "gbr"}:
        source = "bt709" if int(info.get("height", 0)) >= 720 else "smpte170m"
    else:
        return None
    return {"src": source, "dst": "bt709" if output_height >= 720 else source}


def color_tags(color: dict[str, str] | None) -> list[str]:
    if not color:
        return []
    if color["dst"] == "bt709":
        return ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    return ["-colorspace", "smpte170m", "-color_range", "tv"]


def estimate_ffv1_bytes(width: int, height: int, frames: int) -> int:
    return int(width * height * config.FFV1_BYTES_PER_PIXEL * max(frames, 1))


# ---------------------------------------------------------------- encodage

def encoder_args(settings: dict[str, Any]) -> tuple[list[str], str, list[str]]:
    """(options globales avant les entrées, suffixe du filtre vidéo, options de l'encodeur).

    Le CRF de l'interface est pensé pour x264 ; il est converti en valeur équivalente pour les autres encodeurs.
    """
    codec = settings.get("codec", "h264")
    crf = int(settings.get("crf", 16))
    preset = settings.get("preset", "slow")
    if codec == "hevc":
        return [], "", ["-c:v", "libx265", "-preset", preset, "-crf", str(crf + 2),
                        "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=error"]
    if codec == "av1":
        return [], "", ["-c:v", "libsvtav1", "-preset", str(SVT_PRESETS.get(preset, 6)), "-crf", str(crf + 10),
                        "-pix_fmt", "yuv420p10le"]
    if codec in {"h264_vaapi", "hevc_vaapi"}:
        return (["-vaapi_device", config.VAAPI_DEVICE], ",format=nv12,hwupload",
                ["-c:v", codec, "-rc_mode", "CQP", "-qp", str(crf + 4)])
    if codec == "av1_vaapi":
        # Cartes avec encodeur AV1 (Intel Arc, AMD RX 7000 et suivantes). L'échelle de qualité AV1 va de
        # 0 à 255 : environ 4 fois celle de H.264/HEVC.
        return (["-vaapi_device", config.VAAPI_DEVICE], ",format=nv12,hwupload",
                ["-c:v", codec, "-rc_mode", "CQP", "-global_quality", str(min(255, (crf + 10) * 4))])
    return [], "", ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]


def scale_filter(width: int, height: int, color: dict[str, str] | None = None) -> str:
    matrix = f":in_color_matrix={color['src']}:out_color_matrix={color['dst']}" if color else ""
    return f"scale={width}:{height}:flags=lanczos+accurate_rnd+full_chroma_int{matrix},setsar=1"


def segment_command(source: Path, pattern: Path, filters: list[str], seconds: int, stream: str = "0:v:0",
                    segment_list: Path | None = None) -> list[str]:
    """Découpe la vidéo (après filtres) en segments FFV1 intra : chaque image est une image clé.

    Avec segment_list, FFmpeg ajoute le nom de chaque segment au fichier dès qu'il est terminé : l'IA peut
    alors commencer sans attendre la fin de la découpe.
    """
    pre, filters = accelerated(filters)
    command = [*FFMPEG_BASE, *pre, "-i", str(source), "-map", stream, "-an", "-sn", "-dn"]
    if filters:
        command += ["-vf", ",".join(filters)]
    command += [*FFV1_ARGS, "-f", "segment", "-segment_time", str(seconds),
                "-reset_timestamps", "1", "-segment_format", "matroska"]
    if segment_list is not None:
        command += ["-segment_list", str(segment_list), "-segment_list_type", "flat"]
    return command + [str(pattern)]


def prefilter_command(source: Path, output: Path, filters: list[str], stream: str = "0:v:0") -> list[str]:
    pre, filters = accelerated(filters)
    command = [*FFMPEG_BASE, *pre, "-i", str(source), "-map", stream, "-an", "-sn", "-dn"]
    if filters:
        command += ["-vf", ",".join(filters)]
    # FFV1 intra : Video2X lit toutes les images (sur du H.264 il peut perdre les dernières).
    return command + [*FFV1_ARGS, str(output)]


GRAIN_FILTERS = {"light": "noise=c0s=4:c0f=t+u", "medium": "noise=c0s=7:c0f=t+u"}


def render_filter(size: tuple[int, int], strength: int, grain: str, vf_suffix: str = "",
                  color: dict[str, str] | None = None) -> tuple[list[str], str]:
    """Graphe de filtres : sortie IA (entrée 0) éventuellement mélangée à l'original agrandi (entrée 1).

    strength = part de l'IA (100 = IA pure). Le mélange garde la texture naturelle (peau, grain) que
    Real-ESRGAN a tendance à lisser ; un grain léger peut ensuite être réinjecté.
    Renvoie (options -filter_complex, étiquette de sortie).
    """
    steps = [f"[0:v]{scale_filter(*size, color)},format=yuv420p[ai]"]
    current = "ai"
    if strength < 100:
        weight = strength / 100
        steps.append(f"[1:v]{scale_filter(*size, color)},format=yuv420p[ref]")
        steps.append(f"[ai][ref]blend=all_expr='A*{weight:.2f}+B*{1 - weight:.2f}'[mix]")
        current = "mix"
    grain_filter = GRAIN_FILTERS.get(grain)
    chain = ",".join(f for f in (grain_filter, vf_suffix.lstrip(",")) if f)
    if chain:
        steps.append(f"[{current}]{chain}[out]")
        current = "out"
    return ["-filter_complex", ";".join(steps)], f"[{current}]"


def segment_encode_command(ai_segment: Path, output: Path, size: tuple[int, int],
                           settings: dict[str, Any], original: Path | None = None,
                           color: dict[str, str] | None = None) -> list[str]:
    """Encode un segment agrandi ; avec un rendu « mélangé », l'original du segment est la 2e entrée."""
    pre, vf_suffix, codec = encoder_args(settings)
    strength, grain = render_params(settings)
    if strength < 100 and original is None:
        strength = 100
    inputs = ["-i", str(ai_segment)] + (["-i", str(original)] if strength < 100 else [])
    graph, label = render_filter(size, strength, grain, vf_suffix, color)
    return [*FFMPEG_BASE, *pre, *inputs, *graph, "-map", label, *codec, *color_tags(color), str(output)]


def subtitle_args(codecs: list[str], input_index: int = 1) -> tuple[list[str], list[str]]:
    args: list[str] = []
    skipped: list[str] = []
    out = 0
    for index, codec in enumerate(codecs):
        if codec in MKV_SUBTITLE_COPY:
            args += ["-map", f"{input_index}:s:{index}", f"-c:s:{out}", "copy"]
        elif codec in MKV_SUBTITLE_TO_SRT:
            args += ["-map", f"{input_index}:s:{index}", f"-c:s:{out}", "srt"]
        else:
            skipped.append(f"#{index} ({codec or 'inconnu'})")
            continue
        out += 1
    return args, skipped


def audio_args(codecs: list[str], input_index: int = 1) -> list[str]:
    args = ["-map", f"{input_index}:a?", "-c:a", "copy"]
    for index, codec in enumerate(codecs):
        if codec in MKV_AUDIO_TO_FLAC:
            args += [f"-c:a:{index}", "flac"]
    return args


def video_stream(info: dict[str, Any]) -> str:
    """Flux vidéo exact de la source (évite une image de couverture placée en premier)."""
    return f"0:{info.get('video_index', 0)}" if "video_index" in info else "0:v:0"


def final_mux_command(concat_list: Path, source: Path, output: Path,
                      info: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Assemble les segments encodés (copie) avec l'audio, les sous-titres et les chapitres de l'original."""
    subs, skipped = subtitle_args(info.get("subtitles", []))
    offset = ["-itsoffset", f"{info['video_offset']:.3f}"] if info.get("video_offset", 0) > 0.01 else []
    command = [
        *FFMPEG_BASE, *offset, "-f", "concat", "-safe", "0", "-i", str(concat_list), "-i", str(source),
        "-map", "0:v:0", "-c:v", "copy", *audio_args(info.get("audio", [])), *subs,
        "-map", "1:t?", "-c:t", "copy",
        "-map_metadata", "1", "-map_chapters", "1",
        "-max_muxing_queue_size", "4096", str(output),
    ]
    return command, skipped


def write_concat_list(path: Path, files: list[Path]) -> None:
    lines = []
    for file in files:
        escaped = str(file).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- aperçus

def clip_command(source: Path, output: Path, start: float, duration: float, filters: list[str],
                 stream: str = "0:v:0") -> list[str]:
    pre, filters = accelerated(filters)
    command = [*FFMPEG_BASE, *pre, "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
               "-map", stream, "-an", "-sn", "-dn"]
    if filters:
        command += ["-vf", ",".join(filters)]
    return command + [*FFV1_ARGS, str(output)]


def preview_encode_command(source: Path, output: Path, size: tuple[int, int], original: Path | None = None,
                           strength: int = 100, grain: str = "off",
                           color: dict[str, str] | None = None) -> list[str]:
    """MP4 H.264 lisible par tous les navigateurs (avec le rendu demandé)."""
    inputs = ["-i", str(source)] + (["-i", str(original)] if original is not None and strength < 100 else [])
    graph, label = render_filter(size, strength if original is not None else 100, grain, color=color)
    return [*FFMPEG_BASE, *inputs, *graph, "-map", label,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", *color_tags(color),
            "-movflags", "+faststart", str(output)]


def frame_extract_command(source: Path, output: Path, at: float) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(source),
            "-frames:v", "1", "-vf", "scale=512:-2", str(output)]


# ---------------------------------------------------------------- vérification

def verify_output(output: Path, info: dict[str, Any], expected_subtitles: int, interpolate: int = 1) -> list[str]:
    """Contrôles après traitement : durée, pistes audio et sous-titres. Renvoie la liste des anomalies."""
    problems = []
    try:
        data = ffprobe_json(output, "-show_entries", "stream=codec_type,avg_frame_rate:format=duration")
    except (ProbeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return [f"sortie illisible : {exc}"]
    streams = data.get("streams", [])
    duration = parse_ratio(data.get("format", {}).get("duration"))
    expected = info.get("duration", 0)
    if expected > 0 and abs(duration - expected) > max(1.0, expected * 0.01):
        problems.append(f"durée {duration:.1f} s au lieu de {expected:.1f} s")
    audio = sum(1 for s in streams if s.get("codec_type") == "audio")
    if audio != len(info.get("audio", [])):
        problems.append(f"{audio} piste(s) audio au lieu de {len(info.get('audio', []))}")
    subs = sum(1 for s in streams if s.get("codec_type") == "subtitle")
    if subs != expected_subtitles:
        problems.append(f"{subs} piste(s) de sous-titres au lieu de {expected_subtitles}")
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        problems.append("aucune piste vidéo")
    elif info.get("fps") and interpolate > 1:
        fps = parse_ratio(video.get("avg_frame_rate"))
        if fps and abs(fps - info["fps"] * interpolate) > 0.5:
            problems.append(f"{fps:.2f} images/s au lieu de {info['fps'] * interpolate:.2f}")
    return problems
