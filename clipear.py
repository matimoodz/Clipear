#!/usr/bin/env python3
"""
clipear.py - Saca clips verticales (TikTok / Reels / Shorts) de un VOD de Kick.

Uso típico:
    python clipear.py --url https://kick.com/canal/videos/<id> --cantidad 10 --formato split

Pasos: descarga (yt-dlp) -> audio -> transcripción (faster-whisper) -> picos de volumen
-> chat de Kick -> puntaje combinado -> lista para aprobar -> render (ffmpeg) -> clips.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import wave
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
FORMATOS = ("split", "full", "cam")
URL_FUENTE = "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf"

# Palabras que suelen indicar momento gracioso / intenso
RE_RISA = re.compile(r"\b(?:j[aeiou]j[aeiou](?:j[aeiou])*|ja|jsjs\w*|xd+|ajaj\w*|lol|lmao)\b", re.I)
RE_INTENSO = re.compile(
    r"\b(?:no puede ser|qu[eé] (?:hac[eé]s|pas[oó])|boludo|la puta|la concha|mierda|dios m[ií]o|"
    r"noo+|nooo+|wow|guau|increíble|increible|mal[ií]simo|ay+|vamo+s|goo+l|epa|uff+|ah+)\b", re.I)
RE_CHAT_GRACIOSO = re.compile(r"(kekw|omegalul|lul|lmao|jaj|jsjs|xd|😂|🤣|💀|icant|kek)", re.I)


# ─────────────────────────── utilidades ───────────────────────────

def info(msg: str) -> None:
    print(f"» {msg}", flush=True)


def aviso(msg: str) -> None:
    print(f"⚠  {msg}", flush=True)


def hms(seg: float) -> str:
    seg = max(0, seg)
    h, r = divmod(int(seg), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def slug(texto: str, largo: int = 40) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "_", t).strip("_").lower()
    return (t[:largo].rstrip("_")) or "clip"


def correr(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("stdin", subprocess.DEVNULL)  # que ffmpeg/yt-dlp no se coman lo que escribís en la terminal
    return subprocess.run(cmd, **kw)


def alto_video(ruta: Path) -> int:
    r = correr(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height",
                "-of", "default=nw=1:nk=1", str(ruta)], capture_output=True, text=True)
    try:
        return int(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def duracion_video(ruta: Path) -> float:
    r = correr(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(ruta)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        sys.exit(f"No pude leer la duración de {ruta}. ¿Es un video válido?")


def cargar_config(ruta: Path) -> dict:
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────── 0. chequeo de dependencias ───────────────────────────

def chequear_dependencias(necesita_ytdlp: bool) -> None:
    faltan = []
    if sys.version_info < (3, 9):
        faltan.append(("Python 3.9+", "Bajalo de https://www.python.org/downloads/ (tildá 'Add to PATH')"))
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        faltan.append(("ffmpeg", "Windows: winget install Gyan.FFmpeg   |  Mac: brew install ffmpeg  |  "
                                 "Linux: sudo apt install ffmpeg   (después cerrá y abrí la terminal)"))
    if necesita_ytdlp and not (shutil.which("yt-dlp") or _modulo_existe("yt_dlp")):
        faltan.append(("yt-dlp", "pip install -U yt-dlp"))
    for mod, pip in (("numpy", "numpy"), ("faster_whisper", "faster-whisper"), ("requests", "requests")):
        if not _modulo_existe(mod):
            faltan.append((pip, f"pip install {pip}"))
    if faltan:
        print("\nFaltan cosas para poder correr la herramienta:\n")
        for nombre, como in faltan:
            print(f"  ✗ {nombre:15s} → {como}")
        print("\nAtajo para lo de Python:  pip install -r requirements.txt\n")
        sys.exit(1)
    info(f"Dependencias OK (Python {sys.version.split()[0]}, ffmpeg, "
         f"{'yt-dlp, ' if necesita_ytdlp else ''}faster-whisper, numpy)")


def _modulo_existe(nombre: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(nombre) is not None


def comando_ytdlp() -> list[str]:
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


# ─────────────────────────── 1. obtener el video ───────────────────────────

def id_vod(url: str) -> str:
    m = re.search(r"/videos/([0-9a-zA-Z-]+)", url or "")
    return m.group(1) if m else slug(url or "vod")


def canal_de_url(url: str | None) -> str:
    m = re.search(r"kick\.com/([^/]+)/", url or "")
    return m.group(1) if m else ""


def descargar_vod(url: str, trabajo: Path) -> Path | None:
    existentes = [p for p in trabajo.glob("video.*") if p.suffix in (".mp4", ".mkv", ".webm", ".ts")]
    if existentes:
        info(f"Video ya descargado: {existentes[0].name}")
        return existentes[0]
    info("Descargando el VOD con yt-dlp (puede tardar)…")
    cmd = comando_ytdlp() + [
        # -S res:1080 = la mejor calidad hasta 1080p (con [height<=1080] Kick caía a 480p
        # porque sus formatos a veces no traen el alto informado)
        "-f", "bv*+ba/b", "-S", "res:1080,fps",
        "--merge-output-format", "mp4", "-N", "8",
        "-o", str(trabajo / "video.%(ext)s"), url,
    ]
    r = correr(cmd)
    if r.returncode != 0:
        return None
    existentes = [p for p in trabajo.glob("video.*") if p.suffix in (".mp4", ".mkv", ".webm", ".ts")]
    return existentes[0] if existentes else None


def pedir_archivo_local() -> Path:
    print("\n⚠  Kick bloqueó la descarga (o yt-dlp falló).")
    print("   Bajá el VOD a mano (ej. desde el panel de Kick o con un descargador) y pasame la ruta.")
    print("   La próxima podés usar directamente:  python clipear.py --archivo ruta\\al\\video.mp4 --url <link>\n")
    while True:
        try:
            ruta = input("Ruta del archivo de video (Enter para salir): ").strip().strip('"').strip("'")
        except EOFError:
            sys.exit(1)
        if not ruta:
            sys.exit(1)
        p = Path(ruta).expanduser()
        if p.is_file():
            return p
        print("   No encuentro ese archivo, probá de nuevo.")


# ─────────────────────────── 2. audio + volumen ───────────────────────────

def extraer_audio(video: Path, trabajo: Path) -> Path:
    wav = trabajo / "audio.wav"
    if wav.exists():
        return wav
    info("Extrayendo audio…")
    r = correr(["ffmpeg", "-y", "-v", "error", "-stats", "-i", str(video), "-vn", "-ac", "1",
                "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    if r.returncode != 0:
        sys.exit("ffmpeg no pudo extraer el audio.")
    return wav


def volumen_por_segundo(wav: Path, trabajo: Path):
    """dB RMS por segundo (máximo de 4 sub-bloques de 250 ms para captar gritos cortos)."""
    import numpy as np
    cache = trabajo / "volumen.npy"
    if cache.exists():
        return np.load(cache)
    info("Analizando picos de volumen…")
    vals = []
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        sub = sr // 4
        while True:
            frames = w.readframes(sr)
            if not frames:
                break
            x = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            mejores = []
            for i in range(0, len(x), sub):
                b = x[i:i + sub]
                if len(b):
                    mejores.append(20 * math.log10(float(np.sqrt(np.mean(b * b))) + 1e-6))
            vals.append(max(mejores) if mejores else -120.0)
    arr = np.array(vals, dtype=np.float32)
    np.save(cache, arr)
    return arr


# ─────────────────────────── 3. transcripción ───────────────────────────

def transcribir(wav: Path, trabajo: Path, modelo: str) -> list[dict]:
    cache = trabajo / f"transcripcion_{modelo}.json"
    if cache.exists():
        info(f"Uso la transcripción guardada ({cache.name}).")
        return json.loads(cache.read_text(encoding="utf-8"))

    from faster_whisper import WhisperModel
    info(f"Cargando Whisper '{modelo}' (la primera vez se descarga el modelo)…")
    try:
        m = WhisperModel(modelo, device="auto", compute_type="auto")
    except Exception:
        m = WhisperModel(modelo, device="cpu", compute_type="int8")
    segs_iter, inf = m.transcribe(str(wav), language="es", word_timestamps=True, vad_filter=True,
                                  vad_parameters={"min_silence_duration_ms": 500})
    total = inf.duration or 1
    segs, t0, ultimo = [], time.time(), -1
    for s in segs_iter:
        segs.append({
            "start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
            "words": [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
                      for w in (s.words or []) if w.word.strip()],
        })
        pct = int(s.end / total * 100)
        if pct != ultimo and pct % 2 == 0:
            ultimo = pct
            trans = time.time() - t0
            resta = trans / max(s.end, 1) * (total - s.end)
            print(f"\r   Transcribiendo… {pct:3d}%  ({hms(s.end)} / {hms(total)})  faltan ~{hms(resta)}  ",
                  end="", flush=True)
    print()
    cache.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    info(f"Transcripción lista: {len(segs)} frases.")
    return segs


# ─────────────────────────── 4. chat de Kick ───────────────────────────

def _sesion_http():
    try:  # curl_cffi imita un navegador real y suele pasar Cloudflare
        from curl_cffi import requests as creq
        return creq.Session(impersonate="chrome")
    except ImportError:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                          "Accept": "application/json"})
        return s


def _parse_fecha(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def descargar_chat(url: str, duracion: float, trabajo: Path) -> list[tuple[float, str]] | None:
    cache = trabajo / "chat.json"
    if cache.exists():
        return [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
    uuid = re.search(r"/videos/([0-9a-fA-F-]{36})", url or "")
    if not uuid:
        aviso("No reconozco el ID del VOD en el link: sigo sin chat.")
        return None
    s = _sesion_http()
    try:
        r = s.get(f"https://kick.com/api/v1/video/{uuid.group(1)}", timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        d = r.json()
        ls = d.get("livestream") or {}
        canal_id = ls.get("channel_id") or (ls.get("channel") or {}).get("id")
        inicio = _parse_fecha(ls.get("start_time") or d.get("start_time") or d.get("created_at"))
        if not canal_id:
            raise RuntimeError("sin channel_id")
    except Exception as e:
        aviso(f"No pude leer los datos del VOD en la API de Kick ({e}). Sigo sin chat. "
              "Tip: pip install curl_cffi suele destrabar esto.")
        return None

    info(f"Bajando chat del VOD (~{int(duracion // 5)} pedidos; Ctrl+C para cortar y seguir con lo bajado)…")
    msgs: dict[str, tuple[float, str]] = {}
    t, fallos, n = 0.0, 0, 0
    try:
        while t < duracion:
            ts = (inicio + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            try:
                r = s.get(f"https://kick.com/api/v2/channels/{canal_id}/messages",
                          params={"start_time": ts}, timeout=20)
            except Exception:
                r = None
            if r is None or r.status_code == 429:
                fallos += 1
                time.sleep(min(30, 2 ** fallos))
                if fallos > 6:
                    break
                continue
            if r.status_code != 200:
                fallos += 1
                if fallos > 6:
                    break
                t += 5
                continue
            fallos = 0
            for m in (r.json().get("data") or {}).get("messages") or []:
                try:
                    off = (_parse_fecha(m["created_at"]) - inicio).total_seconds()
                    msgs[str(m.get("id", off))] = (off, m.get("content") or "")
                except Exception:
                    pass
            t += 5
            n += 1
            if n % 40 == 0:
                print(f"\r   Chat… {hms(t)} / {hms(duracion)}  ({len(msgs)} mensajes)  ", end="", flush=True)
    except KeyboardInterrupt:
        print()
        aviso("Corté la descarga del chat, uso lo que se bajó hasta ahora.")
    print()
    if not msgs:
        aviso("La API de chat no devolvió mensajes (Kick puede estar bloqueando). Sigo sin chat.")
        return None
    lista = sorted(msgs.values())
    cache.write_text(json.dumps(lista, ensure_ascii=False), encoding="utf-8")
    info(f"Chat listo: {len(lista)} mensajes.")
    return lista


# ─────────────────────────── 5. puntaje y selección ───────────────────────────

@dataclass
class Clip:
    n: int
    inicio: float
    fin: float
    pico: float
    puntaje: float
    motivo: str
    titulo: str
    formato: str = "split"
    descripcion: str = ""
    hashtags: list[str] = field(default_factory=list)

    @property
    def duracion(self) -> float:
        return self.fin - self.inicio


def _suavizar(x, ventana: int):
    import numpy as np
    if ventana <= 1:
        return x
    k = np.ones(ventana, dtype=np.float32) / ventana
    return np.convolve(x, k, mode="same")


def _normalizar(x):
    import numpy as np
    pos = x[x > 0]
    if len(pos) == 0:
        return np.zeros_like(x)
    tope = np.percentile(pos, 99) or pos.max()
    return np.clip(x / tope, 0, 1)


def _linea_base(db, bloque: int = 30):
    """Mediana móvil gruesa (por bloques interpolados) para medir 'cuánto más fuerte de lo normal'."""
    import numpy as np
    n = len(db)
    centros, meds = [], []
    for i in range(0, n, bloque):
        lo, hi = max(0, i - 60), min(n, i + 60)
        seg = db[lo:hi]
        seg = seg[seg > -70]  # ignoro silencio total
        meds.append(np.median(seg) if len(seg) else -70.0)
        centros.append(i + bloque / 2)
    return np.interp(np.arange(n), centros, meds)


def calcular_senales(dur: int, volumen, segs, chat, cfg):
    import numpy as np
    n = dur + 1
    vol = np.full(n, -90.0, dtype=np.float32)
    vol[:min(n, len(volumen))] = volumen[:n]
    exceso_db = np.clip(vol - _linea_base(vol), 0, None)
    s_vol = _normalizar(_suavizar(exceso_db, 3))

    texto = np.zeros(n, dtype=np.float32)
    risas = np.zeros(n, dtype=np.float32)
    for s in segs:
        for w in s.get("words", []):
            i = int(w["s"])
            if i >= n:
                continue
            if RE_RISA.search(w["w"]):
                texto[i] += 2
                risas[i] += 1
            if RE_INTENSO.search(w["w"]):
                texto[i] += 1
        if RE_INTENSO.search(s["text"]) or s["text"].count("!") >= 1:
            i = int(s["start"])
            if i < n:
                texto[i] += 1
    s_txt = _normalizar(_suavizar(texto, 6))

    chat_cnt = np.zeros(n, dtype=np.float32)
    chat_fun = np.zeros(n, dtype=np.float32)
    retraso = cfg.get("chat_retraso_seg", 6)
    if chat:
        for off, txt in chat:
            i = int(off - retraso)  # el chat reacciona unos segundos después
            if 0 <= i < n:
                chat_cnt[i] += 1
                if RE_CHAT_GRACIOSO.search(txt):
                    chat_fun[i] += 1
    chat_v = _suavizar(chat_cnt + 2 * chat_fun, 10)
    base_chat = _suavizar(chat_cnt + 2 * chat_fun, 300) + 0.05
    s_chat = _normalizar(np.clip(chat_v / base_chat - 1, 0, None)) if chat else np.zeros(n, np.float32)

    p = dict(cfg.get("pesos", {}))
    if not chat:
        p["chat"] = 0
    total = sum(p.values()) or 1
    comb = (p.get("volumen", 0) * s_vol + p.get("chat", 0) * s_chat + p.get("texto", 0) * s_txt) / total
    comb = _suavizar(comb, 8)
    return {"comb": comb, "exceso_db": exceso_db, "risas": risas, "chat_cnt": chat_cnt,
            "chat_fun": chat_fun, "chat_v": chat_v, "base_chat": base_chat}


def _ventana_por_frases(pico: float, segs: list[dict], dmin: float, dmax: float):
    """Arma una ventana que arranca y termina en frases completas alrededor del pico."""
    if not segs:
        return max(0, pico - dmin * 0.6), pico + dmin * 0.4
    k = min(range(len(segs)), key=lambda i: 0 if segs[i]["start"] <= pico <= segs[i]["end"]
            else min(abs(segs[i]["start"] - pico), abs(segs[i]["end"] - pico)))
    i = j = k
    dur = lambda a, b: segs[b]["end"] - segs[a]["start"]  # noqa: E731
    objetivo = min(dmax, max(dmin, 32))
    contexto_previo = 15  # segundos de "previa" antes del pico

    while dur(i, j) < objetivo:
        quiere_previa = pico - segs[i]["start"] < contexto_previo and i > 0
        cands = []
        if quiere_previa:
            cands.append("prev")
        if j + 1 < len(segs):
            cands.append("next")
        if i > 0 and "prev" not in cands:
            cands.append("prev")
        avanzo = False
        for c in cands:
            a, b = (i - 1, j) if c == "prev" else (i, j + 1)
            hueco = (segs[i]["start"] - segs[i - 1]["end"]) if c == "prev" else (segs[j + 1]["start"] - segs[j]["end"])
            if dur(a, b) <= dmax and (hueco < 4 or dur(i, j) < dmin):
                i, j, avanzo = a, b, True
                break
        if not avanzo:
            break
    # preferir terminar en punto/exclamación/pregunta
    if not re.search(r"[.!?…]$", segs[j]["text"]) and j + 1 < len(segs) and dur(i, j + 1) <= dmax:
        j += 1

    ini, fin = segs[i]["start"], segs[j]["end"]
    if fin - ini > dmax:  # una sola frase larguísima: corto en límites de palabra
        palabras = [w for s in segs[i:j + 1] for w in s.get("words", [])]
        if palabras:
            a = min(palabras, key=lambda w: abs(w["s"] - (pico - contexto_previo)))
            fin_ok = [w for w in palabras if a["s"] < w["e"] <= a["s"] + dmax]
            punt = [w for w in fin_ok if re.search(r"[.!?,…]$", w["w"]) and w["e"] - a["s"] >= dmin]
            b = (punt or fin_ok)[-1] if fin_ok else palabras[-1]
            ini, fin = a["s"], b["e"]
        else:
            ini, fin = max(ini, pico - dmax / 2), min(fin, pico + dmax / 2)
    return max(0.0, ini - 0.15), fin + 0.35


def _titulo_sugerido(ini: float, fin: float, pico: float, segs: list[dict]) -> str:
    dentro = [s for s in segs if s["end"] > ini and s["start"] < fin]
    def peso(s):
        t = s["text"]
        sin_risa = RE_RISA.sub("", t).strip(" ,.!¡¿?")
        distancia = 0 if s["start"] <= pico <= s["end"] else min(abs(s["start"] - pico), abs(s["end"] - pico))
        return (2 * bool(RE_INTENSO.search(t)) + bool(RE_RISA.search(t)) + ("!" in t)
                + max(0.0, 4 - distancia / 2)            # la frase del pico pesa más
                + (0 if len(sin_risa.split()) >= 3 else -6))
    if not dentro:
        return "Momento épico del vivo"
    mejor = max(dentro, key=peso)
    t = RE_RISA.sub("", mejor["text"])
    t = re.sub(r"\s+", " ", t).strip(" ,.-¡!¿?…")
    palabras = t.split()
    if len(palabras) > 8:
        t = " ".join(palabras[:8]) + "…"
    if not t:
        return "Momento épico del vivo"
    return t[0].upper() + t[1:]


def elegir_clips(sen, segs, chat, cantidad, dmin, dmax, formato, canal, cfg) -> list[Clip]:
    import numpy as np
    comb = sen["comb"]
    orden = np.argsort(-comb)
    elegidos: list[Clip] = []
    usados: list[tuple[float, float]] = []
    prom_chat = float(np.mean(sen["chat_v"])) + 1e-6
    for idx in orden[: max(4000, cantidad * 200)]:
        if len(elegidos) >= cantidad or comb[idx] <= 0:
            break
        pico = float(idx)
        if any(a - 5 <= pico <= b + 5 for a, b in usados):
            continue
        ini, fin = _ventana_por_frases(pico, segs, dmin, dmax)
        if fin - ini < dmin * 0.75:
            continue
        if any(not (fin <= a or ini >= b) for a, b in usados):
            continue
        usados.append((ini, fin))

        lo, hi = int(ini), int(fin) + 1
        motivos = []
        db = float(sen["exceso_db"][lo:hi].max()) if hi > lo else 0
        if db >= 4:
            motivos.append(f"pico de volumen +{db:.0f} dB sobre lo normal (grito/risa)")
        r = int(sen["risas"][lo:hi].sum())
        if r:
            motivos.append(f"risas en el audio ({r})")
        if chat:
            ratio = float(sen["chat_v"][lo:hi].max()) / prom_chat
            fun = int(sen["chat_fun"][lo:hi].sum())
            if ratio >= 1.5:
                motivos.append(f"chat {ratio:.1f}x más activo que el promedio")
            if fun:
                motivos.append(f"{fun} mensajes KEKW/LUL/jaja")
        if not motivos:
            motivos.append("pico combinado de volumen + habla intensa")
        titulo = _titulo_sugerido(ini, fin, pico, segs)
        tags = list(cfg.get("hashtags_base", [])) + ([f"#{canal}"] if canal else [])
        if r or (chat and int(sen["chat_fun"][lo:hi].sum()) > 2):
            tags += ["#humor", "#risas"]
        elegidos.append(Clip(
            n=0, inicio=round(ini, 2), fin=round(fin, 2), pico=pico, puntaje=round(float(comb[idx]), 3),
            motivo="; ".join(motivos), titulo=titulo, formato=formato,
            descripcion=(f"{titulo} 😂 Clip del vivo de {canal} en Kick. Seguime en kick.com/{canal}" if canal
                         else f"{titulo} 😂 Clip del vivo en Kick."),
            hashtags=tags,
        ))
    elegidos.sort(key=lambda c: c.inicio)
    for i, c in enumerate(elegidos, 1):
        c.n = i
    return elegidos


# ─────────────────────────── 6. revisión con el usuario ───────────────────────────

def mostrar_lista(clips: list[Clip]) -> None:
    print("\n" + "═" * 100)
    print(f"{'#':>2}  {'INICIO':8}  {'DUR':>4}  {'FMT':5}  TÍTULO / POR QUÉ")
    print("─" * 100)
    for c in clips:
        print(f"{c.n:>2}  {hms(c.inicio):8}  {c.duracion:>3.0f}s  {c.formato:5}  {c.titulo}")
        print(f"{'':25}↳ {c.motivo}  (puntaje {c.puntaje})")
    print("═" * 100)


AYUDA_REVISION = """
Comandos:
  ok                      → renderizar todo
  quitar 3 7              → sacar clips de la lista
  formato 2 cam           → cambiar formato de un clip (split | full | cam)
  formato todos full      → cambiar el formato de todos
  titulo 4 Nuevo título   → cambiar el título
  mover 5 +3 -2           → correr inicio +3 s y fin -2 s del clip 5
  editar                  → abrir/editar candidatos.json a mano y recargar
  salir                   → cortar sin renderizar
"""


def revisar(clips: list[Clip], ruta_json: Path) -> list[Clip]:
    print(AYUDA_REVISION)
    while True:
        mostrar_lista(clips)
        try:
            cmd = input("\n¿Le doy OK? > ").strip()
        except EOFError:
            sys.exit("\nSin respuesta: no renderizo nada. (Usá --si para no preguntar.)")
        partes = cmd.split()
        if not partes:
            continue
        op = partes[0].lower()
        try:
            if op in ("ok", "si", "sí", "dale", "y", "yes"):
                return clips
            if op in ("salir", "q", "no"):
                guardar_candidatos(clips, ruta_json)
                sys.exit(f"Listo, no renderizo. Quedó guardado en {ruta_json} (podés retomar con --desde-candidatos).")
            if op == "quitar":
                fuera = {int(x) for x in partes[1:]}
                clips = [c for c in clips if c.n not in fuera]
            elif op == "formato":
                f = partes[2].lower()
                if f not in FORMATOS:
                    print(f"Formato inválido. Opciones: {', '.join(FORMATOS)}")
                    continue
                for c in clips:
                    if partes[1] == "todos" or c.n == int(partes[1]):
                        c.formato = f
            elif op == "titulo":
                n, nuevo = int(partes[1]), " ".join(partes[2:])
                for c in clips:
                    if c.n == n:
                        c.titulo = nuevo
            elif op == "mover":
                n, di, df = int(partes[1]), float(partes[2]), float(partes[3])
                for c in clips:
                    if c.n == n:
                        c.inicio, c.fin = max(0, c.inicio + di), c.fin + df
            elif op == "editar":
                guardar_candidatos(clips, ruta_json)
                input(f"Editá {ruta_json} y apretá Enter cuando termines… ")
                clips = cargar_candidatos(ruta_json)
            else:
                print(AYUDA_REVISION)
        except (IndexError, ValueError):
            print("No entendí el comando." + AYUDA_REVISION)


def guardar_candidatos(clips: list[Clip], ruta: Path) -> None:
    ruta.write_text(json.dumps([asdict(c) for c in clips], ensure_ascii=False, indent=2), encoding="utf-8")


def cargar_candidatos(ruta: Path) -> list[Clip]:
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    return [Clip(**d) for d in datos]


# ─────────────────────────── 7. subtítulos (ASS estilo TikTok) ───────────────────────────

def _color_ass(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _t_ass(s: float) -> str:
    s = max(0.0, s)
    h, r = divmod(s, 3600)
    m, r = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{r:05.2f}"


def _escapar_ass(t: str) -> str:
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def generar_ass(clip: Clip, segs: list[dict], ruta: Path, cfg: dict, fuente: str) -> int:
    sc = cfg["subtitulos"]
    y = {"split": sc["y_split"], "full": sc["y_full"], "cam": sc["y_cam"]}[clip.formato]
    blanco, amarillo, borde = _color_ass(sc["color"]), _color_ass(sc["resaltado"]), _color_ass(sc["borde_color"])

    palabras = [w for s in segs for w in s.get("words", []) if w["e"] > clip.inicio and w["s"] < clip.fin]
    # agrupar de a N palabras, cortando en puntuación o silencios
    grupos, actual = [], []
    for w in palabras:
        if actual and (len(actual) >= sc["palabras_por_grupo"] or w["s"] - actual[-1]["e"] > 0.6
                       or re.search(r"[.!?,…]$", actual[-1]["w"])):
            grupos.append(actual)
            actual = []
        actual.append(w)
    if actual:
        grupos.append(actual)

    def fmt(w: str) -> str:
        w = _escapar_ass(w)
        return w.upper() if sc.get("mayusculas", True) else w

    eventos = []
    for gi, g in enumerate(grupos):
        fin_grupo = g[-1]["e"]
        sig = grupos[gi + 1][0]["s"] if gi + 1 < len(grupos) else fin_grupo + 0.4
        fin_grupo = min(max(fin_grupo, g[-1]["s"] + 0.25), sig)
        for wi, w in enumerate(g):
            a = w["s"] - clip.inicio
            b = (g[wi + 1]["s"] if wi + 1 < len(g) else fin_grupo) - clip.inicio
            if b <= 0 or a >= clip.duracion:
                continue
            b = max(b, a + 0.08)
            txt = " ".join(
                (f"{{\\c{amarillo}\\fscx112\\fscy112}}{fmt(x['w'])}{{\\c{blanco}\\fscx100\\fscy100}}"
                 if k == wi else fmt(x["w"]))
                for k, x in enumerate(g))
            eventos.append(f"Dialogue: 0,{_t_ass(a)},{_t_ass(b)},TT,,0,0,0,,{{\\an5\\pos(540,{y})}}{txt}")

    ruta.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: TT,{fuente},{sc['tamano']},{blanco},{amarillo},{borde},&H80000000,0,0,0,0,100,100,0,0,1,"
        f"{sc['borde']},2,5,60,60,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(eventos) + "\n", encoding="utf-8")
    return len(eventos)


def preparar_fuente(cfg: dict, trabajo: Path) -> tuple[str, Path]:
    """Deja la fuente en <trabajo>/fonts. Devuelve (nombre de fuente para ASS, carpeta)."""
    carpeta = trabajo / "fonts"
    carpeta.mkdir(exist_ok=True)
    nombre = cfg["subtitulos"]["fuente"]
    propias = list((AQUI / "fonts").glob("*.[ot]tf")) if (AQUI / "fonts").exists() else []
    for f in propias:
        shutil.copy(f, carpeta / f.name)
    destino = carpeta / "Montserrat-ExtraBold.ttf"
    if not propias and nombre.startswith("Montserrat") and not destino.exists():
        try:
            import requests
            r = requests.get(URL_FUENTE, timeout=30)
            r.raise_for_status()
            destino.write_bytes(r.content)
            (AQUI / "fonts").mkdir(exist_ok=True)
            shutil.copy(destino, AQUI / "fonts" / destino.name)
            info("Fuente Montserrat ExtraBold descargada.")
        except Exception:
            aviso("No pude bajar Montserrat ExtraBold; uso Arial Black. "
                  "(Podés poner cualquier .ttf en la carpeta fonts/ y su nombre en config.json)")
            return "Arial Black", carpeta
    return nombre, carpeta


# ─────────────────────────── 8. render ───────────────────────────

def _par(v: float) -> int:
    return int(v) // 2 * 2


def recorte_aspecto(x, y, w, h, aspecto: float):
    """Recorte centrado más grande posible con ese aspecto (ancho/alto) dentro del rectángulo."""
    if w / h > aspecto:
        nw, nh = h * aspecto, h
    else:
        nw, nh = w, w / aspecto
    return _par(nw), _par(nh), _par(x + (w - nw) / 2), _par(y + (h - nh) / 2)


def recorte_contenido(cfg: dict, aspecto: float):
    """Recorte del contenido lo más centrado posible, esquivando la facecam si está a un costado."""
    W, H = cfg["resolucion_stream"]
    c, cam = cfg["contenido"], cfg["facecam"]
    x0, x1, y0, y1 = c["x"], c["x"] + c["w"], c["y"], c["y"] + c["h"]
    if c.get("evitar_facecam", True):
        if cam["x"] >= W / 2:          # cam a la derecha
            x1 = min(x1, cam["x"])
        elif cam["x"] + cam["w"] <= W / 2:  # cam a la izquierda
            x0 = max(x0, cam["x"] + cam["w"])
    h = y1 - y0
    w = min(h * aspecto, x1 - x0)
    h = w / aspecto
    centro = W / 2 if c.get("evitar_facecam", True) else (x0 + x1) / 2  # lo más cerca del centro de pantalla
    cx = min(max(centro, x0 + w / 2), x1 - w / 2)
    return _par(w), _par(h), _par(cx - w / 2), _par(y0 + (y1 - y0 - h) / 2)


def filtro_video(formato: str, cfg: dict, ass: str | None, fontsdir: str | None) -> str:
    W, H = cfg["resolucion_stream"]
    cam = cfg["facecam"]
    base = f"[0:v]scale={W}:{H}:flags=lanczos,setsar=1"
    subs = f",subtitles={ass}:fontsdir={fontsdir}" if ass else ""
    up = "flags=lanczos"

    if formato == "split":
        cw, ch, cx, cy = recorte_aspecto(cam["x"], cam["y"], cam["w"], cam["h"], 1080 / 960)
        gw, gh, gx, gy = recorte_contenido(cfg, 1080 / 960)
        return (f"{base},split=2[a][b];"
                f"[a]crop={cw}:{ch}:{cx}:{cy},scale=1080:960:{up}[arriba];"
                f"[b]crop={gw}:{gh}:{gx}:{gy},scale=1080:960:{up}[abajo];"
                f"[arriba][abajo]vstack=inputs=2,"
                f"drawbox=x=0:y=957:w=1080:h=6:color=white@0.95:t=fill,fps=30{subs},format=yuv420p[v]")
    if formato == "full":
        p = cfg["pip"]
        gw, gh, gx, gy = recorte_contenido(cfg, 9 / 16)
        pw = _par(p["ancho"])
        ph = _par(pw * cam["h"] / cam["w"])
        bx = p["borde"]
        px = p["margen_x"] if "izquierda" in p["esquina"] else 1080 - pw - p["margen_x"]
        py = p["margen_y"] if "arriba" in p["esquina"] else 1920 - ph - p["margen_y"]
        return (f"{base},split=2[a][b];"
                f"[a]crop={gw}:{gh}:{gx}:{gy},scale=1080:1920:{up}[fondo];"
                f"[b]crop={_par(cam['w'])}:{_par(cam['h'])}:{cam['x']}:{cam['y']},scale={pw}:{ph}:{up},"
                f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t={bx}[pip];"
                f"[fondo][pip]overlay={px}:{py},fps=30{subs},format=yuv420p[v]")
    if formato == "cam":
        cw, ch, cx, cy = recorte_aspecto(cam["x"], cam["y"], cam["w"], cam["h"], 9 / 16)
        return f"{base},crop={cw}:{ch}:{cx}:{cy},scale=1080:1920:{up},fps=30{subs},format=yuv420p[v]"
    raise ValueError(formato)


def render_clip(video: Path, clip: Clip, salida: Path, cfg: dict, segs, fuente: str, trabajo: Path) -> bool:
    ass_nombre = f"subs_{clip.n:02d}.ass"
    hay_subs = generar_ass(clip, segs, trabajo / ass_nombre, cfg, fuente) > 0
    filtro = filtro_video(clip.formato, cfg, ass_nombre if hay_subs else None, "fonts")
    cmd = ["ffmpeg", "-y", "-v", "error", "-stats",
           "-ss", f"{clip.inicio:.2f}", "-to", f"{clip.fin:.2f}", "-i", str(video.resolve()),
           "-filter_complex", filtro, "-map", "[v]", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high", "-r", "30",
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
           "-movflags", "+faststart", str(salida.resolve())]
    # corro ffmpeg dentro de la carpeta de trabajo: rutas relativas para subtítulos/fuentes (evita líos con C:\)
    return correr(cmd, cwd=trabajo).returncode == 0


def escribir_csv(clips: list[Clip], archivos: dict[int, str], ruta: Path) -> None:
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig + ';' = abre bien en Excel en español
        w = csv.writer(f, delimiter=";")
        w.writerow(["numero", "archivo", "timestamp_inicio", "timestamp_fin", "duracion_seg", "formato",
                    "titulo", "descripcion", "hashtags", "motivo"])
        for c in clips:
            w.writerow([c.n, archivos.get(c.n, ""), hms(c.inicio), hms(c.fin), round(c.duracion),
                        c.formato, c.titulo, c.descripcion, " ".join(c.hashtags), c.motivo])


def probar_layout(video: Path, seg: float, cfg: dict, trabajo: Path, carpeta: Path) -> None:
    """Saca capturas para verificar que los recortes de cámara/contenido estén bien."""
    W, H = cfg["resolucion_stream"]
    cam = cfg["facecam"]
    out = carpeta / "layout_original.png"
    gw, gh, gx, gy = recorte_contenido(cfg, 1080 / 960)
    correr(["ffmpeg", "-y", "-v", "error", "-ss", str(seg), "-i", str(video.resolve()), "-frames:v", "1", "-update", "1",
            "-vf", f"scale={W}:{H},drawbox=x={cam['x']}:y={cam['y']}:w={cam['w']}:h={cam['h']}:color=red:t=6,"
                   f"drawbox=x={gx}:y={gy}:w={gw}:h={gh}:color=lime:t=6", str(out.resolve())])
    for f in FORMATOS:
        correr(["ffmpeg", "-y", "-v", "error", "-ss", str(seg), "-i", str(video.resolve()), "-frames:v", "1", "-update", "1",
                "-filter_complex", filtro_video(f, cfg, None, None), "-map", "[v]",
                str((carpeta / f"layout_{f}.png").resolve())], cwd=trabajo)
    info(f"Capturas en {carpeta}: layout_original.png (rojo = facecam, verde = recorte del contenido en split) "
         "y layout_split/full/cam.png. Si la cámara no calza, ajustá 'facecam' en config.json.")


# ─────────────────────────── main ───────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Clips verticales de VODs de Kick.")
    ap.add_argument("--url", help="Link del VOD de Kick")
    ap.add_argument("--archivo", help="Video local (si ya lo bajaste o Kick bloquea la descarga)")
    ap.add_argument("--cantidad", type=int, default=10)
    ap.add_argument("--formato", choices=FORMATOS, default="split", help="Formato por defecto de los clips")
    ap.add_argument("--min", type=float, default=20, help="Duración mínima (s)")
    ap.add_argument("--max", type=float, default=60, help="Duración máxima (s)")
    ap.add_argument("--modelo", default="small", choices=["tiny", "base", "small", "medium", "large-v3"],
                    help="Modelo de Whisper (small = rápido, medium = más preciso)")
    ap.add_argument("--sin-chat", action="store_true", help="No intentar bajar el chat")
    ap.add_argument("--si", action="store_true", help="No pedir confirmación antes de renderizar")
    ap.add_argument("--salida", default="clips", help="Carpeta de salida")
    ap.add_argument("--config", default=str(AQUI / "config.json"))
    ap.add_argument("--desde-candidatos", action="store_true",
                    help="Saltar el análisis y usar el candidatos.json guardado")
    ap.add_argument("--probar-layout", type=float, metavar="SEGUNDO",
                    help="Sólo sacar capturas de prueba del layout en ese segundo del video")
    args = ap.parse_args()

    if not args.url and not args.archivo:
        ap.error("Pasame --url (link de Kick) y/o --archivo (video local).")
    if args.min >= args.max:
        ap.error("--min tiene que ser menor que --max")

    chequear_dependencias(necesita_ytdlp=bool(args.url and not args.archivo))
    cfg = cargar_config(Path(args.config))

    nombre = id_vod(args.url) if args.url else slug(Path(args.archivo).stem)
    trabajo = (AQUI / "trabajo" / nombre).resolve()
    trabajo.mkdir(parents=True, exist_ok=True)
    salida = Path(args.salida).resolve()
    salida.mkdir(parents=True, exist_ok=True)
    canal = canal_de_url(args.url)

    # 1) video
    if args.archivo:
        video = Path(args.archivo).expanduser()
        if not video.is_file():
            sys.exit(f"No existe el archivo {video}")
    else:
        video = descargar_vod(args.url, trabajo) or pedir_archivo_local()
    dur = duracion_video(video)
    alto = alto_video(video)
    info(f"Video: {video.name}  ({hms(dur)}, {alto}p)")
    if 0 < alto < 1080:
        aviso(f"El video está en {alto}p: los clips van a salir borrosos (sobre todo la cámara). "
              f"Si es la descarga automática, borrá {video} y volvé a correr el comando para bajarlo en 1080p.")

    if args.probar_layout is not None:
        probar_layout(video, args.probar_layout, cfg, trabajo, salida)
        return

    ruta_cand = trabajo / "candidatos.json"
    wav = extraer_audio(video, trabajo)
    segs = transcribir(wav, trabajo, args.modelo)

    if args.desde_candidatos and ruta_cand.exists():
        clips = cargar_candidatos(ruta_cand)
    else:
        volumen = volumen_por_segundo(wav, trabajo)
        chat = None
        if args.url and not args.sin_chat:
            chat = descargar_chat(args.url, dur, trabajo)
        sen = calcular_senales(int(dur), volumen, segs, chat, cfg)
        clips = elegir_clips(sen, segs, chat, args.cantidad, args.min, args.max, args.formato, canal, cfg)
        if not clips:
            sys.exit("No encontré momentos que cumplan los requisitos. Probá bajar --min.")
        guardar_candidatos(clips, ruta_cand)
        if len(clips) < args.cantidad:
            aviso(f"Sólo encontré {len(clips)} momentos que no se pisan.")

    if args.si:
        mostrar_lista(clips)
    else:
        clips = revisar(clips, ruta_cand)
    guardar_candidatos(clips, ruta_cand)

    fuente, _ = preparar_fuente(cfg, trabajo)
    archivos: dict[int, str] = {}
    for c in clips:
        nombre_mp4 = f"clip_{c.n:02d}_{slug(c.titulo)}.mp4"
        info(f"Renderizando {c.n}/{len(clips)} [{c.formato}] {hms(c.inicio)} → {nombre_mp4}")
        if render_clip(video, c, salida / nombre_mp4, cfg, segs, fuente, trabajo):
            archivos[c.n] = nombre_mp4
        else:
            aviso(f"Falló el render del clip {c.n}.")
    escribir_csv(clips, archivos, salida / "clips.csv")
    info(f"¡Listo! {len(archivos)} clips en {salida}  +  clips.csv")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelado.")
