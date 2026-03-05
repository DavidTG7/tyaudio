#!/usr/bin/env python3
# v2 - ffmpeg fix
"""
🎵 YouTube Audio Downloader - Backend Flask
Ejecuta con: python app.py
Luego abre: http://localhost:5000
"""

import os
import threading
import uuid
import time
import shutil
import subprocess
from flask import Flask, request, jsonify, send_file, render_template_string
import yt_dlp

# Obtener ffmpeg — usa imageio-ffmpeg que trae su propio binario
def get_ffmpeg_path():
    import shutil as sh
    # 1. Buscar en PATH del sistema
    ffmpeg = sh.which("ffmpeg")
    if ffmpeg:
        print(f"✅ ffmpeg encontrado en PATH: {ffmpeg}")
        return ffmpeg
    # 2. Usar imageio-ffmpeg (binario empaquetado con pip)
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        print(f"✅ ffmpeg via imageio: {ffmpeg}")
        return ffmpeg
    except Exception as e:
        print(f"⚠️  imageio-ffmpeg no disponible: {e}")
    # 3. Fallback
    print("⚠️  ffmpeg no encontrado, usando 'ffmpeg' como fallback")
    return "ffmpeg"

FFMPEG_PATH = get_ffmpeg_path()

app = Flask(__name__)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

download_jobs = {}

MAX_FULL_DURATION = 3600  # 1 hora: si es mayor, se obliga a recortar

# ─── CACHÉ DE VIDEO ───────────────────────────────────────────────────────────
# Dos cachés separados: uno para audio y otro para video+audio.

def _new_cache():
    return {
        "url":         None,
        "filepath":    None,
        "last_used":   0,
        "lock":        threading.Lock(),
        "ready":       False,
        "downloading": False,
    }

caches = {
    "audio": _new_cache(),
    "video": _new_cache(),
}

CACHE_TTL = 300  # segundos (5 minutos)


def cache_is_valid(url, tipo="audio"):
    """Retorna True si hay caché válida para esta URL y tipo."""
    cc = caches[tipo]
    return (
        cc["url"] == url
        and cc["ready"]
        and cc["filepath"]
        and os.path.exists(cc["filepath"])
        and (time.time() - cc["last_used"]) < CACHE_TTL
    )


def clear_cache(tipo=None):
    """Elimina caché del disco. Si tipo=None, limpia ambos."""
    tipos = [tipo] if tipo else ["audio", "video"]
    for t in tipos:
        cc = caches[t]
        with cc["lock"]:
            if cc["filepath"] and os.path.exists(cc["filepath"]):
                try: os.remove(cc["filepath"])
                except Exception: pass
            cc.update({"url": None, "filepath": None,
                       "last_used": 0, "ready": False, "downloading": False})


def auto_cleanup_thread():
    """Hilo que limpia cachés automáticamente tras inactividad."""
    while True:
        time.sleep(60)
        for tipo, cc in caches.items():
            if (cc["ready"] and cc["last_used"] > 0
                    and (time.time() - cc["last_used"]) > CACHE_TTL):
                print(f"🧹 Limpiando caché {tipo} por inactividad...")
                clear_cache(tipo)

# Iniciar hilo de limpieza automática
threading.Thread(target=auto_cleanup_thread, daemon=True).start()


def cleanup_old_jobs():
    """Elimina jobs y sus archivos después de 10 minutos de completados."""
    JOB_TTL = 600  # 10 minutos
    while True:
        time.sleep(120)  # revisar cada 2 minutos
        now = time.time()
        to_delete = []
        for job_id, job in list(download_jobs.items()):
            if job.get("state") in ("done", "error"):
                completed_at = job.get("completed_at", 0)
                if completed_at and (now - completed_at) > JOB_TTL:
                    # Borrar archivos del disco
                    job_folder = os.path.join(DOWNLOAD_FOLDER, job_id)
                    if os.path.exists(job_folder):
                        try:
                            shutil.rmtree(job_folder)
                        except Exception:
                            pass
                    to_delete.append(job_id)
        for job_id in to_delete:
            download_jobs.pop(job_id, None)
            print(f"🗑️  Job expirado eliminado: {job_id[:8]}...")

threading.Thread(target=cleanup_old_jobs, daemon=True).start()


def download_raw_video(url, job_id, tipo="audio"):
    """Descarga el archivo crudo al caché. tipo='audio' o 'video'."""
    cache_path = os.path.join(DOWNLOAD_FOLDER, f"cache_{tipo}")
    os.makedirs(cache_path, exist_ok=True)
    out_template = os.path.join(cache_path, "raw.%(ext)s")

    import re as _re
    def _clean(s):
        # Eliminar códigos ANSI de color que yt-dlp inyecta
        return _re.sub(r'\x1b\[[0-9;]*m', '', s or '').strip()

    def progress_hook(d):
        if d["status"] == "downloading":
            raw = _clean(d.get("_percent_str", "0")).replace("%", "")
            try: percent = float(raw)
            except: percent = 0
            # Calcular MB descargados para mostrar progreso real
            downloaded = d.get("downloaded_bytes", 0) or 0
            total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            mb_done    = downloaded / 1024 / 1024
            mb_total   = total / 1024 / 1024
            size_str   = f"{mb_done:.1f}/{mb_total:.1f} MB" if mb_total > 0 else f"{mb_done:.1f} MB"
            download_jobs[job_id].update({
                "state":   "downloading",
                "percent": round(percent * 0.85),
                "speed":   _clean(d.get("_speed_str", "")),
                "eta":     _clean(d.get("_eta_str", "")),
                "size":    size_str,
            })
        elif d["status"] == "finished":
            download_jobs[job_id]["state"]   = "cutting"
            download_jobs[job_id]["percent"] = 88

    # Audio: solo pista de audio. Video: mejor video + audio combinados
    fmt = "bestaudio/best" if tipo == "audio" else "bestvideo+bestaudio/best"

    ydl_opts = {
        "format":           fmt,
        "outtmpl":          out_template,
        "noplaylist":       True,
        "quiet":            True,
        "merge_output_format": "mkv",
        "ffmpeg_location":  os.path.dirname(FFMPEG_PATH) or None,
        "progress_hooks":   [progress_hook],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Encontrar el archivo descargado
    files = [f for f in os.listdir(cache_path) if f.startswith("raw.")]
    if not files:
        raise Exception("No se pudo descargar el archivo")
    return os.path.join(cache_path, files[0])


def cut_and_convert(raw_path, output_path, start, end, formato, calidad, job_id):
    """Recorta y convierte a audio."""
    os.makedirs(output_path, exist_ok=True)
    out_file = os.path.join(output_path, f"output.{formato}")
    cmd = [
        FFMPEG_PATH, "-y",
        "-ss", str(start), "-t", str(end - start),
        "-i", raw_path, "-vn",
        "-ab", calidad + "k", "-f", formato,
        out_file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception("Error al cortar el audio: " + result.stderr[-300:])
    download_jobs[job_id].update({"state": "done", "percent": 100,
        "filename": out_file, "completed_at": time.time()})


def cut_video(raw_path, output_path, start, end, resolucion, fmt_video, orientacion, crop_x_pct, job_id):
    """Recorta video, aplica recorte vertical 9:16 si se solicita."""
    os.makedirs(output_path, exist_ok=True)
    out_file = os.path.join(output_path, f"output.{fmt_video}")
    duration = end - start
    height   = int(resolucion)

    if orientacion == "vertical":
        # crop_x_pct (0-100) indica el centro horizontal del recuadro 9:16
        # Fórmula: x = (iw * crop_x_pct/100) - (ih*9/16)/2, clamp a 0..iw-crop_w
        crop_expr_w = "ih*9/16"
        crop_expr_x = f"max(0\,min(iw-ih*9/16\,(iw*{crop_x_pct/100:.4f})-(ih*9/32)))"
        vf = (
            f"crop={crop_expr_w}:ih:{crop_expr_x}:0,"
            f"scale=-2:{height},"
            f"pad=ceil(iw/2)*2:ceil(ih/2)*2"
        )
    else:
        vf = f"scale=-2:{height},pad=ceil(iw/2)*2:ceil(ih/2)*2"

    cmd = [
        FFMPEG_PATH, "-y",
        "-ss", str(start), "-t", str(duration),
        "-i", raw_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_file
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception("Error al procesar el video: " + result.stderr[-300:])
    download_jobs[job_id].update({"state": "done", "percent": 100,
        "filename": out_file, "completed_at": time.time()})

# ─── HTML FRONTEND ────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0"/>
  <title>YTAUDIO</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%230a0a0a'/%3E%3Cpolygon points='10,7 26,16 10,25' fill='%23e8ff47'/%3E%3C/svg%3E"/>
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0a0a0a; --surface: #111; --border: #222;
      --accent: #e8ff47; --accent2: #ff4757; --text: #f0f0f0; --muted: #666;
    }
    body {
      background: var(--bg); color: var(--text);
      font-family: 'DM Sans', sans-serif; min-height: 100vh;
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; padding: 2rem;
      background-image: radial-gradient(ellipse at 20% 50%, #1a1a0a 0%, transparent 60%),
                        radial-gradient(ellipse at 80% 20%, #0a0f1a 0%, transparent 60%);
    }
    .container { width: 100%; max-width: 620px; }
    .header { text-align: center; margin-bottom: 3rem; animation: fadeDown 0.6s ease; }
    .logo {
      font-family: 'Bebas Neue', sans-serif;
      font-size: clamp(3rem, 10vw, 5.5rem);
      letter-spacing: 0.05em; line-height: 1;
      color: var(--accent); text-shadow: 0 0 60px rgba(232,255,71,0.3);
    }
    .logo span { color: var(--text); }
    .subtitle { color: var(--muted); font-size: 0.9rem; margin-top: 0.5rem; letter-spacing: 0.1em; text-transform: uppercase; }
    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 16px; padding: 2rem; animation: fadeUp 0.6s ease 0.1s both;
      position: relative; z-index: 10;
    }
    /* Transición elegante al limpiar la UI */
    .collapsing {
      animation: collapseOut 0.4s cubic-bezier(0.4, 0, 0.2, 1) forwards !important;
      pointer-events: none;
      overflow: hidden;
    }
    @keyframes collapseOut {
      0%   { opacity: 1; transform: translateY(0);    max-height: 400px; }
      40%  { opacity: 0; transform: translateY(-6px); max-height: 400px; }
      100% { opacity: 0; transform: translateY(-6px); max-height: 0; margin: 0; padding: 0; }
    }
    .input-group { display: flex; gap: 0.75rem; margin-bottom: 1rem; }
    input[type="text"] {
      flex: 1; background: #1a1a1a; border: 1px solid var(--border);
      border-radius: 10px; color: #ffffff; font-family: 'DM Sans', sans-serif;
      font-size: 1rem; padding: 0.85rem 1.1rem; outline: none;
      transition: border-color 0.2s, box-shadow 0.2s;
      -webkit-appearance: none;
      -webkit-text-fill-color: #ffffff;
      caret-color: var(--accent);
    }
    input[type="text"]:-webkit-autofill,
    input[type="text"]:-webkit-autofill:hover,
    input[type="text"]:-webkit-autofill:focus {
      -webkit-box-shadow: 0 0 0px 1000px #1a1a1a inset !important;
      -webkit-text-fill-color: #ffffff !important;
      caret-color: var(--accent);
    }
    input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(232,255,71,0.1); }
    input[type="text"]::placeholder {
      color: #555;
      -webkit-text-fill-color: #555;
      opacity: 1;
    }
    .btn-paste {
      background: #1a1a1a; border: 1px solid var(--border); border-radius: 10px;
      color: var(--muted); cursor: pointer; font-size: 1.2rem;
      padding: 0.85rem 1rem; transition: all 0.2s;
    }
    .btn-paste:hover { border-color: var(--accent); color: var(--accent); }
    .url-wrapper { position: relative; flex: 1; display: flex; align-items: center; }
    .url-wrapper input[type="text"] { flex: 1; width: 100%; padding-right: 2.5rem; }
    .btn-clear {
      position: absolute; right: 0.65rem;
      background: none; border: none; cursor: pointer;
      color: var(--muted); font-size: 0.95rem;
      width: 26px; height: 26px; border-radius: 50%;
      display: none; align-items: center; justify-content: center;
      transition: all 0.18s; padding: 0; line-height: 1;
      -webkit-tap-highlight-color: transparent;
    }
    .btn-clear:hover { background: rgba(255,71,87,0.15); color: var(--accent2); transform: scale(1.15); }
    .btn-clear.visible { display: flex; }
    .btn-clear.tap {
      animation: clearTap 0.35s ease forwards;
    }
    @keyframes clearTap {
      0%   { transform: scale(1);    background: transparent; color: var(--muted); }
      30%  { transform: scale(1.35); background: rgba(255,71,87,0.25); color: var(--accent2); }
      70%  { transform: scale(0.9);  background: rgba(255,71,87,0.1);  color: var(--accent2); }
      100% { transform: scale(1);    background: transparent; color: var(--muted); }
    }
    .btn-check {
      background: var(--accent); border: none; border-radius: 10px;
      color: #0a0a0a; cursor: pointer; font-family: 'Bebas Neue', sans-serif;
      font-size: 1.1rem; letter-spacing: 0.08em; padding: 0.85rem 1.4rem;
      transition: all 0.2s; white-space: nowrap;
    }
    .btn-check:hover:not(:disabled) { background: #d4eb00; transform: translateY(-1px); }
    .btn-check:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Video info card */
    .video-info {
      display: none; background: #161616; border: 1px solid var(--border);
      border-radius: 12px; padding: 1rem; margin-bottom: 1.5rem;
      gap: 1rem; align-items: center;
    }
    .video-info.visible { display: flex; animation: fadeUp 0.3s ease; }
    .video-thumb {
      width: 80px; height: 56px; border-radius: 6px;
      object-fit: cover; flex-shrink: 0; background: #222;
    }
    .video-meta { flex: 1; min-width: 0; }
    .video-title {
      font-size: 0.9rem; font-weight: 500; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis; margin-bottom: 0.3rem;
    }
    .video-duration { font-size: 0.78rem; color: var(--accent); }
    .video-channel { font-size: 0.78rem; color: var(--muted); }
    .video-orient-badge {
      display: inline-block; font-size: 0.7rem; color: var(--muted);
      border: 1px solid #333; border-radius: 4px; padding: 1px 6px;
      margin-top: 0.25rem; letter-spacing: 0.05em;
    }

    /* Warning banner */
    .warning-banner {
      display: none; background: rgba(255,71,87,0.1); border: 1px solid rgba(255,71,87,0.3);
      border-radius: 10px; padding: 0.85rem 1rem; margin-bottom: 1.25rem;
      font-size: 0.82rem; color: #ff6b78; line-height: 1.5;
    }
    .warning-banner.visible { display: block; animation: fadeUp 0.3s ease; }

    /* Range selector */
    .range-section {
      display: none; margin-bottom: 1.5rem;
    }
    .range-section.visible { display: block; animation: fadeUp 0.3s ease; }
    .range-label {
      color: var(--muted); font-size: 0.75rem; letter-spacing: 0.1em;
      text-transform: uppercase; margin-bottom: 0.75rem;
    }
    .range-bar-container {
      position: relative; height: 36px; margin-bottom: 0.75rem;
    }
    .range-track {
      position: absolute; top: 50%; transform: translateY(-50%);
      left: 0; right: 0; height: 6px; background: #222; border-radius: 999px;
    }
    .range-fill {
      position: absolute; height: 100%; background: var(--accent);
      border-radius: 999px; box-shadow: 0 0 8px rgba(232,255,71,0.4);
    }
    input[type="range"] {
      position: absolute; top: 50%; transform: translateY(-50%);
      width: 100%; appearance: none; background: transparent;
      pointer-events: none; height: 36px;
    }
    input[type="range"]::-webkit-slider-thumb {
      appearance: none; width: 18px; height: 18px;
      border-radius: 50%; background: var(--accent);
      border: 2px solid #0a0a0a; cursor: pointer;
      pointer-events: all; box-shadow: 0 0 8px rgba(232,255,71,0.5);
    }
    input[type="range"]::-webkit-slider-runnable-track { background: transparent; }
    .range-times {
      display: flex; justify-content: space-between;
      font-size: 0.8rem; color: var(--muted);
    }
    .range-times .current { color: var(--accent); font-weight: 500; }
    .range-duration-tag {
      text-align: center; font-size: 0.78rem; color: var(--muted);
      margin-top: 0.4rem;
    }
    .range-duration-tag span { color: var(--text); }

    /* Options */
    .options-grid {
      display: none; grid-template-columns: 1fr 1fr;
      gap: 1rem; margin-bottom: 1.5rem;
    }
    .options-grid.visible { display: grid; animation: fadeUp 0.3s ease; }
    .option-label { color: var(--muted); font-size: 0.75rem; letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 0.5rem; }
    select {
      width: 100%; background: #1a1a1a; border: 1px solid var(--border);
      border-radius: 10px; color: var(--text); font-family: 'DM Sans', sans-serif;
      font-size: 1rem; padding: 0.75rem 1rem; outline: none; cursor: pointer;
      transition: border-color 0.2s; appearance: none; -webkit-appearance: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23666' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
      background-repeat: no-repeat; background-position: right 1rem center;
    }
    select:focus { border-color: var(--accent); }

    /* Download button */
    .btn-download {
      display: none; width: 100%; background: var(--accent); border: none;
      border-radius: 10px; color: #0a0a0a; cursor: pointer;
      font-family: 'Bebas Neue', sans-serif; font-size: 1.3rem;
      letter-spacing: 0.1em; padding: 1rem; transition: all 0.2s;
    }
    .btn-download.visible { display: block; animation: fadeUp 0.3s ease; }
    .btn-download:hover:not(:disabled) {
      background: #d4eb00; transform: translateY(-1px);
      box-shadow: 0 8px 25px rgba(232,255,71,0.3);
    }
    .btn-download:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Progress */
    .progress-box {
      display: none; margin-top: 1.5rem; background: #1a1a1a;
      border: 1px solid var(--border); border-radius: 10px; padding: 1.25rem;
    }
    .progress-box.visible { display: block; animation: fadeUp 0.3s ease; }
    .progress-title { font-size: 0.85rem; color: var(--muted); margin-bottom: 0.75rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .progress-bar-bg { background: #222; border-radius: 999px; height: 6px; overflow: hidden; margin-bottom: 0.75rem; }
    .progress-bar-fill { background: var(--accent); border-radius: 999px; height: 100%; width: 0%; transition: width 0.4s ease; box-shadow: 0 0 10px rgba(232,255,71,0.5); }
    .progress-status { font-size: 0.8rem; color: var(--muted); }
    .progress-status.success { color: var(--accent); }
    .progress-status.error { color: var(--accent2); }

    @keyframes fadeDown { from { opacity:0; transform:translateY(-20px); } to { opacity:1; transform:translateY(0); } }
    @keyframes fadeUp   { from { opacity:0; transform:translateY(20px);  } to { opacity:1; transform:translateY(0); } }
    .pulse { animation: pulse 1.5s ease-in-out infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }

    /* Puntos animados para estados de procesamiento */
    .dots span {
      display: inline-block;
      opacity: 0;
      animation: dotBlink 1.2s infinite;
      color: var(--accent);
      font-weight: 700;
    }
    .dots span:nth-child(1) { animation-delay: 0s; }
    .dots span:nth-child(2) { animation-delay: 0.2s; }
    .dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes dotBlink {
      0%, 80%, 100% { opacity: 0; transform: translateY(0); }
      40%           { opacity: 1; transform: translateY(-3px); }
    }

    .footer { text-align:center; margin-top:2rem; color:var(--muted); font-size:0.8rem; letter-spacing:0.08em; animation: fadeUp 0.6s ease 0.2s both; position: relative; z-index: 1; }

    /* Tipo selector: Audio / Video */
    .type-selector {
      display: none; gap: 0.75rem; margin-bottom: 1.25rem;
    }
    .type-selector.visible { display: flex; animation: fadeUp 0.3s ease; }
    .type-btn {
      flex: 1; background: #1a1a1a; border: 1px solid var(--border);
      border-radius: 10px; color: var(--muted); cursor: pointer;
      font-family: 'DM Sans', sans-serif; font-size: 0.95rem;
      padding: 0.75rem; transition: all 0.2s; text-align: center;
    }
    .type-btn:hover { border-color: var(--accent); color: var(--text); }
    .type-btn.active { background: rgba(232,255,71,0.08); border-color: var(--accent); color: var(--accent); font-weight: 500; }

    /* Opciones de video */
    .video-options {
      display: none; gap: 1rem; margin-bottom: 1.5rem;
    }
    .video-options.visible { display: grid; grid-template-columns: 1fr 1fr; animation: fadeUp 0.3s ease; }

    /* Toggle orientación */
    .orient-toggle {
      display: flex; background: #1a1a1a; border: 1px solid var(--border);
      border-radius: 10px; overflow: hidden; margin-bottom: 1.5rem;
    }
    .orient-btn {
      flex: 1; background: none; border: none; color: var(--muted);
      cursor: pointer; font-family: 'DM Sans', sans-serif; font-size: 0.85rem;
      padding: 0.7rem 0.5rem; transition: all 0.2s; display: flex;
      align-items: center; justify-content: center; gap: 0.4rem;
    }
    .orient-btn.active { background: rgba(232,255,71,0.08); color: var(--accent); }
    .orient-btn:hover:not(.active) { color: var(--text); }
    .orient-section { display: none; margin-bottom: 0; }
    .orient-section.visible { display: block; animation: fadeUp 0.3s ease; }

    /* Crop picker */
    .crop-picker { display: none; }
    .crop-picker.visible { display: block; animation: fadeUp 0.3s ease; }
    .crop-preview-wrap {
      position: relative; width: 100%; border-radius: 10px;
      overflow: hidden; background: #000; cursor: grab; user-select: none;
      border: 1px solid var(--border);
    }
    .crop-preview-wrap:active { cursor: grabbing; }
    .crop-preview-wrap img {
      display: block; width: 100%; height: 160px;
      object-fit: cover; pointer-events: none;
    }
    .crop-mask {
      position: absolute; top: 0; bottom: 0;
      background: rgba(0,0,0,0.65); pointer-events: none;
    }
    .crop-mask.left  { left: 0; }
    .crop-mask.right { right: 0; }
    .crop-frame {
      position: absolute; top: 0; bottom: 0;
      border: 2px solid var(--accent);
      box-shadow: 0 0 0 1px rgba(232,255,71,0.3), inset 0 0 0 1px rgba(232,255,71,0.1);
      pointer-events: none; display: flex; align-items: center; justify-content: center;
    }
    .crop-frame-inner {
      display: flex; align-items: center; justify-content: center;
      width: 100%; height: 100%;
    }
    .crop-label {
      background: var(--accent); color: #0a0a0a;
      font-family: 'Bebas Neue', sans-serif; font-size: 0.9rem;
      padding: 2px 8px; border-radius: 4px; letter-spacing: 0.05em;
    }
    .crop-hint {
      text-align: center; font-size: 0.75rem; color: var(--muted);
      margin-top: 0.5rem; margin-bottom: 1.5rem; letter-spacing: 0.05em;
    }

    .footer span { color: var(--accent); font-weight: 500; }


    /* ── RESPONSIVE MOBILE ─────────────────────────────────────── */
    @media (max-width: 480px) {
      body { padding: 1rem 0.75rem; justify-content: flex-start; padding-top: 2rem; }
      .header { margin-bottom: 1.5rem; }
      .logo { font-size: clamp(2.5rem, 15vw, 4rem); }
      .subtitle { font-size: 0.75rem; }
      .card { padding: 1.25rem; border-radius: 14px; }
      .input-group { flex-wrap: wrap; gap: 0.5rem; }
      .url-wrapper { flex: 1 1 100%; }
      .btn-paste { flex-shrink: 0; }
      .btn-check { flex: 1; font-size: 1rem; padding: 0.85rem; }
      .type-selector { gap: 0.5rem; }
      .type-btn { font-size: 0.85rem; padding: 0.65rem 0.5rem; }
      .options-grid  { grid-template-columns: 1fr; gap: 0.75rem; }
      .video-options { grid-template-columns: 1fr; gap: 0.75rem; }
      .orient-btn { font-size: 0.78rem; padding: 0.65rem 0.4rem; }
      .crop-preview-wrap img { height: 200px; }
      .btn-download { font-size: 1.2rem; padding: 1.1rem; border-radius: 12px; }
      input[type="range"]::-webkit-slider-thumb { width: 24px; height: 24px; }
      .video-thumb { width: 64px; height: 45px; }
      .video-title { font-size: 0.82rem; }
      .video-channel, .video-duration { font-size: 0.72rem; }
      .progress-box { padding: 1rem; }
      .progress-title { font-size: 0.8rem; }
      .progress-status { font-size: 0.75rem; }
      .footer { margin-top: 1.5rem; font-size: 0.72rem; }
      .range-times { font-size: 0.72rem; }
      .range-duration-tag { font-size: 0.7rem; }
      select { font-size: 0.85rem; padding: 0.65rem 0.85rem; }
    }
    @media (min-width: 481px) and (max-width: 640px) {
      body { padding: 1.5rem 1rem; }
      .card { padding: 1.5rem; }
      .options-grid { grid-template-columns: 1fr; }
    }
    /* ── CUSTOM SELECT ──────────────────────────────────────────── */
    .custom-select { position: relative; width: 100%; }
    .cs-trigger {
      width: 100%; background: #1a1a1a; border: 1px solid var(--border);
      border-radius: 10px; color: var(--text); font-family: 'DM Sans', sans-serif;
      font-size: 0.95rem; padding: 0.75rem 1rem; cursor: pointer;
      display: flex; align-items: center; justify-content: space-between;
      transition: border-color 0.2s; text-align: left; -webkit-appearance: none;
    }
    .cs-trigger:hover, .custom-select.open .cs-trigger {
      border-color: var(--accent);
    }
    .cs-arrow {
      color: var(--muted); font-size: 0.8rem; flex-shrink: 0; margin-left: 0.5rem;
      transition: transform 0.2s;
    }
    .custom-select.open .cs-arrow { transform: rotate(180deg); color: var(--accent); }
    .cs-dropdown {
      display: none; position: absolute; top: calc(100% + 6px); left: 0; right: 0;
      background: #1e1e1e; border: 1px solid var(--accent);
      border-radius: 10px; overflow: hidden; z-index: 9999;
      box-shadow: 0 8px 32px rgba(0,0,0,0.8);
    }
    .custom-select.open .cs-dropdown {
      display: block;
      animation: ddOpen 0.18s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }
    .custom-select.closing .cs-dropdown {
      display: block;
      animation: ddClose 0.18s cubic-bezier(0.4, 0, 1, 1) forwards;
    }
    @keyframes ddOpen {
      from { opacity: 0; transform: translateY(-8px) scaleY(0.95); }
      to   { opacity: 1; transform: translateY(0)   scaleY(1); }
    }
    @keyframes ddClose {
      from { opacity: 1; transform: translateY(0)   scaleY(1); }
      to   { opacity: 0; transform: translateY(-8px) scaleY(0.95); }
    }
    .cs-option {
      padding: 0.75rem 1rem; font-size: 0.9rem; cursor: pointer;
      color: var(--muted); transition: all 0.15s; display: flex;
      align-items: center; justify-content: space-between;
    }
    .cs-option:hover   { background: rgba(232,255,71,0.07); color: var(--text); }
    .cs-option.selected { color: var(--accent); font-weight: 500; }
    .cs-option.selected::after { content: '✓'; font-size: 0.8rem; }
    .cs-option + .cs-option { border-top: 1px solid #2a2a2a; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="logo">YT<span>AUDIO</span></div>
      <p class="subtitle">YouTube · Audio &amp; Video Downloader</p>
    </div>

    <div class="card">

      <!-- PASO 1: URL -->
      <div class="input-group">
        <div class="url-wrapper">
          <input type="text" id="url" placeholder="https://youtube.com/watch?v=..." oninput="onUrlInput()" />
          <button class="btn-clear" id="btnClear" onclick="clearUrl()" title="Limpiar">✕</button>
        </div>
        <button class="btn-paste" onclick="pasteUrl()" title="Pegar">📋</button>
        <button class="btn-check" id="btnCheck" onclick="fetchInfo()">BUSCAR</button>
      </div>

      <!-- Info del video -->
      <div class="video-info" id="videoInfo">
        <img class="video-thumb" id="videoThumb" src="" alt=""/>
        <div class="video-meta">
          <div class="video-title" id="videoTitle">—</div>
          <div class="video-channel" id="videoChannel">—</div>
          <div class="video-duration" id="videoDuration">—</div>
          <div class="video-orient-badge" id="videoOrientBadge" style="display:none"></div>
        </div>
      </div>

      <!-- Advertencia video largo -->
      <div class="warning-banner" id="warningBanner"></div>

      <!-- PASO 2: Selector de rango -->
      <div class="range-section" id="rangeSection">
        <div class="range-label">Selecciona el fragmento a descargar</div>
        <div class="range-bar-container" id="rangeContainer">
          <div class="range-track">
            <div class="range-fill" id="rangeFill"></div>
          </div>
          <input type="range" id="rangeStart" min="0" max="100" value="0" step="1" oninput="onRangeChange()"/>
          <input type="range" id="rangeEnd"   min="0" max="100" value="100" step="1" oninput="onRangeChange()"/>
        </div>
        <div class="range-times">
          <span class="current" id="labelStart">0:00</span>
          <span class="current" id="labelEnd">0:00</span>
        </div>
        <div class="range-duration-tag">Duración seleccionada: <span id="labelDuration">—</span></div>
      </div>

      <!-- PASO 3: Tipo -->
      <div class="type-selector" id="typeSelector">
        <button class="type-btn active" id="btnAudio" onclick="setType('audio')">🎵 Audio</button>
        <button class="type-btn"        id="btnVideo" onclick="setType('video')">🎬 Video</button>
      </div>

      <!-- Opciones Audio -->
      <div class="options-grid" id="optionsGrid">
        <div>
          <div class="option-label">Formato</div>
          <div class="custom-select" id="dd-formato">
            <button class="cs-trigger" onclick="toggleDD('dd-formato')">
              <span class="cs-value">MP3</span><span class="cs-arrow">▾</span>
            </button>
            <div class="cs-dropdown">
              <div class="cs-option selected" data-val="mp3" onclick="selectDD('dd-formato','mp3','MP3')">MP3</div>
              <div class="cs-option" data-val="m4a"  onclick="selectDD('dd-formato','m4a','M4A')">M4A</div>
              <div class="cs-option" data-val="wav"  onclick="selectDD('dd-formato','wav','WAV')">WAV</div>
              <div class="cs-option" data-val="ogg"  onclick="selectDD('dd-formato','ogg','OGG')">OGG</div>
            </div>
          </div>
        </div>
        <div>
          <div class="option-label">Calidad</div>
          <div class="custom-select" id="dd-calidad">
            <button class="cs-trigger" onclick="toggleDD('dd-calidad')">
              <span class="cs-value">192 kbps — Estándar</span><span class="cs-arrow">▾</span>
            </button>
            <div class="cs-dropdown">
              <div class="cs-option" data-val="128"          onclick="selectDD('dd-calidad','128','128 kbps — Ligero')">128 kbps — Ligero</div>
              <div class="cs-option selected" data-val="192" onclick="selectDD('dd-calidad','192','192 kbps — Estándar')">192 kbps — Estándar</div>
              <div class="cs-option" data-val="256"          onclick="selectDD('dd-calidad','256','256 kbps — Alta')">256 kbps — Alta</div>
              <div class="cs-option" data-val="320"          onclick="selectDD('dd-calidad','320','320 kbps — Máxima')">320 kbps — Máxima</div>
            </div>
          </div>
        </div>
      </div>

      <!-- Opciones Video -->
      <div class="video-options" id="videoOptions">
        <div>
          <div class="option-label">Resolución</div>
          <div class="custom-select" id="dd-resolucion">
            <button class="cs-trigger" onclick="toggleDD('dd-resolucion')">
              <span class="cs-value">720p — HD</span><span class="cs-arrow">▾</span>
            </button>
            <div class="cs-dropdown">
              <div class="cs-option" data-val="1080"          onclick="selectDD('dd-resolucion','1080','1080p — Full HD')">1080p — Full HD</div>
              <div class="cs-option selected" data-val="720"  onclick="selectDD('dd-resolucion','720','720p — HD')">720p — HD</div>
              <div class="cs-option" data-val="480"           onclick="selectDD('dd-resolucion','480','480p — Media')">480p — Media</div>
              <div class="cs-option" data-val="360"           onclick="selectDD('dd-resolucion','360','360p — Ligero')">360p — Ligero</div>
            </div>
          </div>
        </div>
        <div>
          <div class="option-label">Formato</div>
          <div class="custom-select" id="dd-formatoVideo">
            <button class="cs-trigger" onclick="toggleDD('dd-formatoVideo')">
              <span class="cs-value">MP4</span><span class="cs-arrow">▾</span>
            </button>
            <div class="cs-dropdown">
              <div class="cs-option selected" data-val="mp4"  onclick="selectDD('dd-formatoVideo','mp4','MP4')">MP4</div>
              <div class="cs-option" data-val="webm"          onclick="selectDD('dd-formatoVideo','webm','WEBM')">WEBM</div>
            </div>
          </div>
        </div>
      </div>

      <!-- Orientación (solo video) -->
      <div class="orient-section" id="orientSection">
        <div class="option-label" style="margin-bottom:0.5rem">Orientación</div>
        <div class="orient-toggle">
          <button class="orient-btn active" id="btnHoriz" onclick="setOrient('horizontal')">🖥 Horizontal (original)</button>
          <button class="orient-btn"        id="btnVert"  onclick="setOrient('vertical')">📱 Vertical 9:16 (celular)</button>
        </div>

        <!-- Crop picker (solo visible en vertical) -->
        <div class="crop-picker" id="cropPicker">
          <div class="option-label" style="margin: 1rem 0 0.5rem">Ajusta el encuadre</div>
          <div class="crop-preview-wrap" id="cropWrap">
            <img id="cropThumb" src="" alt="preview" draggable="false"/>
            <div class="crop-mask left"  id="maskLeft"></div>
            <div class="crop-mask right" id="maskRight"></div>
            <div class="crop-frame" id="cropFrame">
              <div class="crop-frame-inner">
                <span class="crop-label">9:16</span>
              </div>
            </div>
          </div>
          <div class="crop-hint">← Arrastra el recuadro para ajustar →</div>
        </div>

      </div>

      <!-- Botón descargar -->
      <button class="btn-download" id="btnDownload" onclick="startDownload()">
        ⬇ DESCARGAR AUDIO
      </button>

      <!-- Progreso -->
      <div class="progress-box" id="progressBox">
        <div class="progress-title" id="progressTitle">Preparando</div>
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" id="progressBar"></div>
        </div>
        <div class="progress-status" id="progressStatus">Iniciando</div>
      </div>

    </div>
  </div>

  <div class="footer">Developed by <span>DavidTG</span></div>

  <script>
    let videoDuration = 0;
    const MAX_FULL = 3600; // segundos — si excede, obliga a recortar

    function fmtTime(s) {
      s = Math.round(s);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = s % 60;
      if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
      return `${m}:${String(sec).padStart(2,'0')}`;
    }

    function fmtDuration(secs) {
      const h = Math.floor(secs / 3600);
      const m = Math.floor((secs % 3600) / 60);
      const s = secs % 60;
      let parts = [];
      if (h) parts.push(h + 'h');
      if (m) parts.push(m + 'min');
      if (s || parts.length === 0) parts.push(s + 's');
      return parts.join(' ');
    }

    function onUrlInput() {
      const val = document.getElementById('url').value;
      document.getElementById('btnClear').classList.toggle('visible', val.length > 0);
    }

    function clearUrl() {
      // 1. Animación de tap en el botón X
      const btnClear = document.getElementById('btnClear');
      btnClear.classList.add('tap');
      btnClear.addEventListener('animationend', () => btnClear.classList.remove('tap'), { once: true });

      // 2. Limpiar input
      const input = document.getElementById('url');
      input.value = '';
      btnClear.classList.remove('visible');

      // IDs de elementos a desmontar con animación
      const toCollapse = [
        'videoInfo', 'warningBanner', 'rangeSection',
        'typeSelector', 'optionsGrid', 'videoOptions',
        'orientSection', 'progressBox', 'btnDownload'
      ].map(id => document.getElementById(id))
       .filter(el => el && el.classList.contains('visible'));

      if (toCollapse.length === 0) {
        input.focus();
        return;
      }

      // 3. Colapsar con animación escalonada
      toCollapse.forEach((el, i) => {
        setTimeout(() => {
          el.classList.add('collapsing');
          el.addEventListener('animationend', () => {
            el.classList.remove('visible', 'collapsing');
            if (i === toCollapse.length - 1) {
              // Al terminar el último, enfocar el input
              document.getElementById('progressBar').style.width = '0%';
              input.focus();
            }
          }, { once: true });
        }, i * 30); // 30ms de delay entre cada elemento
      });
    }

    async function pasteUrl() {
      try {
        const text = await navigator.clipboard.readText();
        document.getElementById('url').value = text;
      } catch { alert('Pega la URL manualmente (Ctrl+V)'); }
      onUrlInput();
    }

    function validateYouTubeUrl(url) {
      if (!url) return { ok: false, msg: 'Por favor pega una URL de YouTube.' };
      try { new URL(url); } catch { return { ok: false, msg: 'Eso no parece una URL válida.' }; }
      const isYT = url.includes('youtube.com') || url.includes('youtu.be');
      if (!isYT) return { ok: false, msg: 'Solo se aceptan URLs de YouTube.' };
      const isPlaylist = url.includes('playlist?list=') && !url.includes('watch?v=');
      if (isPlaylist) return { ok: false, msg: 'Las playlists no están soportadas. Pega la URL de un video específico.' };
      const hasVideoId = url.includes('watch?v=') || url.includes('youtu.be/') || url.includes('youtube.com/shorts/');
      if (!hasVideoId) return { ok: false, msg: 'No se detectó un video válido en la URL.' };
      return { ok: true };
    }

    function showError(msg) {
      const input = document.getElementById('url');
      input.style.borderColor = 'var(--accent2)';
      input.style.boxShadow = '0 0 0 3px rgba(255,71,87,0.15)';
      setTimeout(() => { input.style.borderColor=''; input.style.boxShadow=''; }, 2500);
      setStatus(msg, 'error');
      showProgress(true);
    }

    function showProgress(show) {
      document.getElementById('progressBox').classList.toggle('visible', show);
    }

    const DOTS_HTML = ' <span class="dots"><span>.</span><span>.</span><span>.</span></span>';

    function setStatus(msg, type='') {
      const el = document.getElementById('progressStatus');
      el.className = 'progress-status' + (type ? ' ' + type : '');
      // Agregar puntos animados en estados de espera (no en éxito ni error)
      const isWaiting = !type || type === 'pulse';
      el.innerHTML = isWaiting ? msg + DOTS_HTML : msg;
    }

    // ── PASO 1: Fetch info del video ──────────────────────────────────────────
    async function fetchInfo() {
      const url = document.getElementById('url').value.trim();
      const v = validateYouTubeUrl(url);
      if (!v.ok) { showError(v.msg); return; }

      const btn = document.getElementById('btnCheck');
      btn.disabled = true;
      btn.textContent = '...';

      // Reset UI
      document.getElementById('videoInfo').classList.remove('visible');
      document.getElementById('warningBanner').classList.remove('visible');
      document.getElementById('rangeSection').classList.remove('visible');
      document.getElementById('optionsGrid').classList.remove('visible');
      document.getElementById('btnDownload').classList.remove('visible');
      document.getElementById('progressBar').style.width = '0%';
      showProgress(true);
      document.getElementById('progressTitle').textContent = 'Obteniendo información';
      setStatus('Conectando con YouTube', 'pulse');

      try {
        const res = await fetch('/info', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({url})
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        // Mostrar info
        videoDuration = data.duration;
        document.getElementById('videoTitle').textContent = data.title;
        document.getElementById('videoChannel').textContent = data.channel;
        document.getElementById('videoDuration').textContent = '⏱ ' + fmtTime(data.duration);
        if (data.thumbnail) document.getElementById('videoThumb').src = data.thumbnail;
        document.getElementById('videoInfo').classList.add('visible');

        showProgress(false);

        // Configurar slider
        setupRange(data.duration);

        // Advertencia si es muy largo
        const warn = document.getElementById('warningBanner');
        if (data.duration > MAX_FULL) {
          warn.innerHTML = `⚠️ <strong>Video largo (${fmtTime(data.duration)})</strong> — Para descargar todo necesitarías recortarlo en secciones. Por defecto se selecciona la primera hora. Ajusta el rango a tu gusto.`;
          warn.classList.add('visible');
        }

        // Poner thumbnail en el crop picker
        if (data.thumbnail) {
          document.getElementById('cropThumb').src = data.thumbnail;
        }
        // Badge de orientación en la info del video
        const badge = document.getElementById('videoOrientBadge');
        if (data.width && data.height) {
          badge.textContent = data.orientation === 'vertical'
            ? `📱 Vertical ${data.width}×${data.height}`
            : `🖥 Horizontal ${data.width}×${data.height}`;
          badge.style.display = 'inline-block';
        } else {
          badge.style.display = 'none';
        }
        cropPercent = 50; // resetear al centro

        // Guardar orientación del video para usarla en setOrient
        window.videoOrientation = data.orientation || 'horizontal';

        // Mostrar opciones y botón
        document.getElementById('rangeSection').classList.add('visible');
        document.getElementById('typeSelector').classList.add('visible');
        document.getElementById('optionsGrid').classList.add('visible');
        document.getElementById('btnDownload').classList.add('visible');
        setType('audio'); // reset al tipo por defecto

      } catch(err) {
        showError('❌ ' + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = 'BUSCAR';
      }
    }

    // ── Slider de rango ───────────────────────────────────────────────────────
    function setupRange(duration) {
      const sStart = document.getElementById('rangeStart');
      const sEnd   = document.getElementById('rangeEnd');
      sStart.max = duration;
      sEnd.max   = duration;
      sStart.value = 0;
      // Si el video es largo, limita el end a MAX_FULL por defecto
      sEnd.value = duration > MAX_FULL ? MAX_FULL : duration;
      sStart.step = Math.max(1, Math.floor(duration / 500));
      sEnd.step   = sStart.step;
      onRangeChange();
    }

    function onRangeChange() {
      const sStart = document.getElementById('rangeStart');
      const sEnd   = document.getElementById('rangeEnd');
      let start = parseInt(sStart.value);
      let end   = parseInt(sEnd.value);

      // Evitar que se crucen (mínimo 5 segundos de diferencia)
      if (end - start < 5) {
        if (document.activeElement === sStart) {
          start = end - 5;
          sStart.value = start;
        } else {
          end = start + 5;
          sEnd.value = end;
        }
      }

      // Actualizar barra visual
      const pct = (v) => (v / videoDuration * 100) + '%';
      const fill = document.getElementById('rangeFill');
      fill.style.left  = pct(start);
      fill.style.width = (end - start) / videoDuration * 100 + '%';

      // Labels
      document.getElementById('labelStart').textContent = fmtTime(start);
      document.getElementById('labelEnd').textContent   = fmtTime(end);
      document.getElementById('labelDuration').textContent = fmtDuration(end - start);
    }

    // ── Custom dropdowns ──────────────────────────────────────────────────────
    const ddTimers = {};

    function closeDD(el) {
      if (!el.classList.contains('open')) return;
      el.classList.remove('open');
      el.classList.add('closing');
      el.querySelector('.cs-dropdown').addEventListener('animationend', () => {
        el.classList.remove('closing');
      }, { once: true });
      clearTimeout(ddTimers[el.id]);
    }

    function closeAllDD(except = null) {
      document.querySelectorAll('.custom-select.open').forEach(d => {
        if (d !== except) closeDD(d);
      });
    }

    function toggleDD(id) {
      const el = document.getElementById(id);
      const isOpen = el.classList.contains('open');
      closeAllDD(el);
      if (!isOpen) {
        el.classList.add('open');
        // Auto-cierre tras 6 segundos
        ddTimers[id] = setTimeout(() => closeDD(el), 6000);
      } else {
        closeDD(el);
      }
    }

    function selectDD(id, val, label) {
      const el = document.getElementById(id);
      el.querySelector('.cs-value').textContent = label;
      el.querySelectorAll('.cs-option').forEach(o => {
        o.classList.toggle('selected', o.dataset.val === val);
      });
      el.dataset.value = val;
      closeDD(el);
    }

    function getDD(id) {
      return document.getElementById(id)?.dataset.value || '';
    }

    // Cerrar al click fuera con animación smooth
    document.addEventListener('click', e => {
      if (!e.target.closest('.custom-select')) closeAllDD();
    });

    // ── Selector de tipo y orientación ─────────────────────────────────────────
    let currentType   = 'audio';
    let currentOrient = 'horizontal';

    function setType(type) {
      currentType = type;
      document.getElementById('btnAudio').classList.toggle('active', type === 'audio');
      document.getElementById('btnVideo').classList.toggle('active', type === 'video');
      document.getElementById('optionsGrid').classList.toggle('visible', type === 'audio');
      document.getElementById('videoOptions').classList.toggle('visible', type === 'video');
      document.getElementById('orientSection').classList.toggle('visible', type === 'video');
      document.getElementById('btnDownload').textContent = type === 'audio'
        ? '⬇ DESCARGAR AUDIO'
        : '⬇ DESCARGAR VIDEO';

      // Si cambia a video, aplicar lógica de orientación nativa
      if (type === 'video') {
        const isNativeVertical = window.videoOrientation === 'vertical';
        // Ocultar toda la sección de orientación si el video ya es vertical
        document.getElementById('orientSection').classList.toggle('visible', !isNativeVertical);
        setOrient(isNativeVertical ? 'vertical' : 'horizontal');
      }
    }

    // ── Crop picker ───────────────────────────────────────────────────────────
    let cropPercent = 50; // posición horizontal del centro del crop (0-100%)

    function setOrient(orient) {
      currentOrient = orient;
      const isNativeVertical = window.videoOrientation === 'vertical';

      document.getElementById('btnHoriz').classList.toggle('active', orient === 'horizontal');
      document.getElementById('btnVert').classList.toggle('active',  orient === 'vertical');

      const picker = document.getElementById('cropPicker');
      if (isNativeVertical) {
        // Video ya es vertical: ocultar crop picker, forzar vertical
        picker.classList.remove('visible');
        currentOrient = 'vertical';
      } else {
        // Video horizontal: mostrar crop picker solo si elige vertical
        picker.classList.toggle('visible', orient === 'vertical');
        if (orient === 'vertical') updateCropFrame();
      }
    }

    function updateCropFrame() {
      const wrap  = document.getElementById('cropWrap');
      const frame = document.getElementById('cropFrame');
      const mL    = document.getElementById('maskLeft');
      const mR    = document.getElementById('maskRight');
      const thumb = document.getElementById('cropThumb');

      const wrapW = wrap.offsetWidth;
      const wrapH = thumb.offsetHeight || 160;

      // El frame tiene aspect ratio 9:16, su ancho = wrapH * 9/16
      const frameW = Math.round(wrapH * 9 / 16);
      const frameW_pct = frameW / wrapW * 100;

      // Centro del frame (clampear para que no se salga)
      const minCenter = frameW_pct / 2;
      const maxCenter = 100 - frameW_pct / 2;
      cropPercent = Math.max(minCenter, Math.min(maxCenter, cropPercent));

      const leftPct  = cropPercent - frameW_pct / 2;
      const rightPct = 100 - (cropPercent + frameW_pct / 2);

      frame.style.left  = leftPct + '%';
      frame.style.width = frameW_pct + '%';
      mL.style.width    = leftPct + '%';
      mR.style.width    = rightPct + '%';
    }

    function initCropDrag() {
      const wrap = document.getElementById('cropWrap');
      let dragging = false, startX = 0, startPct = 50;

      function onDown(e) {
        dragging = true;
        startX   = e.clientX ?? e.touches[0].clientX;
        startPct = cropPercent;
        e.preventDefault();
      }
      function onMove(e) {
        if (!dragging) return;
        const x    = e.clientX ?? e.touches[0].clientX;
        const dx   = x - startX;
        const pct  = dx / wrap.offsetWidth * 100;
        cropPercent = startPct + pct;
        updateCropFrame();
      }
      function onUp() { dragging = false; }

      wrap.addEventListener('mousedown',  onDown);
      wrap.addEventListener('touchstart', onDown, { passive: false });
      window.addEventListener('mousemove',  onMove);
      window.addEventListener('touchmove',  onMove, { passive: false });
      window.addEventListener('mouseup',    onUp);
      window.addEventListener('touchend',   onUp);
    }

    initCropDrag();

    // ── PASO 2: Descargar ─────────────────────────────────────────────────────
    async function startDownload() {
      const url        = document.getElementById('url').value.trim();
      const formato    = getDD('dd-formato')    || 'mp3';
      const calidad    = getDD('dd-calidad')    || '192';
      const resolucion = getDD('dd-resolucion') || '720';
      const fmtVideo   = getDD('dd-formatoVideo') || 'mp4';
      const start      = parseInt(document.getElementById('rangeStart').value);
      const end        = parseInt(document.getElementById('rangeEnd').value);

      const btn = document.getElementById('btnDownload');
      btn.disabled = true;

      const bar    = document.getElementById('progressBar');
      const title  = document.getElementById('progressTitle');
      bar.style.width = '5%';
      title.textContent = 'Iniciando descarga';
      showProgress(true);
      setStatus('Preparando', 'pulse');

      try {
        const res = await fetch('/download', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            url, formato, calidad, start, end,
            tipo: currentType,
            resolucion, formato_video: fmtVideo,
            orientacion: currentOrient,
            crop_x_pct: cropPercent   // posición horizontal del crop (0-100)
          })
        });
        const data = await res.json();
        if (!data.job_id) throw new Error(data.error || 'Error desconocido');

        title.textContent = data.title || 'Descargando';
        bar.style.width = '15%';
        pollStatus(data.job_id, btn, bar);

      } catch(err) {
        setStatus('❌ ' + err.message, 'error');
        bar.style.width = '0%';
        btn.disabled = false;
      }
    }

    function humanSpeed(raw) {
      // Convierte "240.66KiB/s" → "240 KB/s", "1.2MiB/s" → "1.2 MB/s"
      if (!raw) return '';
      raw = raw.trim();
      if (raw.includes('MiB/s') || raw.includes('MB/s')) {
        const n = parseFloat(raw);
        return isNaN(n) ? '' : n.toFixed(1) + ' MB/s';
      }
      if (raw.includes('KiB/s') || raw.includes('KB/s') || raw.includes('k/s')) {
        const n = parseFloat(raw);
        return isNaN(n) ? '' : Math.round(n) + ' KB/s';
      }
      return '';
    }

    function humanETA(raw) {
      // Convierte "[0;33m00:10" o "00:10" → "10 segundos", "01:30" → "1 min 30 seg"
      if (!raw) return '';
      // limpiar códigos de escape ANSI
      raw = raw.replace(/\[\d+;?\d*m/g, '').trim();
      const match = raw.match(/(\d+):(\d+)(?::(\d+))?/);
      if (!match) return '';
      let h = 0, m = 0, s = 0;
      if (match[3] !== undefined) {
        h = parseInt(match[1]); m = parseInt(match[2]); s = parseInt(match[3]);
      } else {
        m = parseInt(match[1]); s = parseInt(match[2]);
      }
      const parts = [];
      if (h > 0) parts.push(h + ' h');
      if (m > 0) parts.push(m + ' min');
      if (s > 0 || parts.length === 0) parts.push(s + ' seg');
      return parts.join(' ');
    }

    function pollStatus(jobId, btn, bar) {
      const started = Date.now();
      const TIMEOUT_MS = 5 * 60 * 1000; // 5 minutos máximo

      const stateLabels = {
        'starting':    '⏳ Preparando descarga',
        'downloading': null,
        'cutting':     '✂️  Cortando el fragmento seleccionado',
        'converting':  '🔄 Convirtiendo al formato elegido',
      };

      const interval = setInterval(async () => {
        // Timeout de seguridad
        if (Date.now() - started > TIMEOUT_MS) {
          clearInterval(interval);
          setStatus('⏱ Tiempo de espera agotado. Intenta de nuevo.', 'error');
          btn.disabled = false;
          return;
        }

        try {
          const res  = await fetch('/status/' + jobId);
          const data = await res.json();

          if (data.percent !== undefined) {
            bar.style.width = data.percent + '%';
          }

          if (data.state === 'downloading') {
            const pct   = data.percent || 0;
            const speed = humanSpeed(data.speed || '');
            const eta   = humanETA(data.eta || '');
            const size  = data.size ? ` (${data.size})` : '';
            const etaStr = eta ? ` — queda ${eta}` : '';
            setStatus(`⬇ Descargando ${pct}%${size}${speed ? ' a ' + speed : ''}${etaStr}`);
          } else if (stateLabels[data.state]) {
            setStatus(stateLabels[data.state]);
          }

          if (data.state === 'done') {
            clearInterval(interval);
            bar.style.width = '100%';
            setStatus('✅ ¡Listo! Descargando archivo...', 'success');
            btn.disabled = false;
            window.location.href = '/file/' + jobId;
            setTimeout(() => {
              setStatus('🎵 Revisa tu carpeta de descargas', 'success');
            }, 1500);
          } else if (data.state === 'error') {
            clearInterval(interval);
            setStatus('❌ ' + (data.error || 'Error en la descarga'), 'error');
            btn.disabled = false;
          }
        } catch { clearInterval(interval); btn.disabled = false; }
      }, 1000);
    }

    // Enter en el input lanza la búsqueda
    document.getElementById('url').addEventListener('keydown', e => {
      if (e.key === 'Enter') fetchInfo();
    });
  </script>
</body>
</html>"""

# ─── API BACKEND ──────────────────────────────────────────────────────────────

def validar_url_youtube(url):
    if not url:
        return False, "URL vacía"
    if "youtube.com" not in url and "youtu.be" not in url:
        return False, "Solo se aceptan URLs de YouTube"
    if "playlist?list=" in url and "watch?v=" not in url:
        return False, "Las playlists no están soportadas. Usa la URL de un video específico"
    es_video  = "watch?v=" in url
    es_short  = "youtube.com/shorts/" in url
    es_youtu  = "youtu.be/" in url
    if not (es_video or es_short or es_youtu):
        return False, "No se detectó un video válido en la URL"
    return True, None


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/info", methods=["POST"])
def get_info():
    """Obtiene metadata del video. Si cambia la URL, limpia el caché."""
    data = request.json
    url = data.get("url", "").strip()

    valida, motivo = validar_url_youtube(url)
    if not valida:
        return jsonify({"error": motivo}), 400

    # Si el usuario busca un video diferente, limpiar ambos cachés
    current_urls = set(cc["url"] for cc in caches.values() if cc["url"])
    if current_urls and url not in current_urls:
        print(f"🔄 Nueva búsqueda — limpiando cachés")
        clear_cache()

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)

            if info.get("_type") == "playlist":
                return jsonify({"error": "Eso es una playlist. Pega la URL de un video individual"}), 400

            thumbnails = info.get("thumbnails", [])
            thumb = next((t["url"] for t in reversed(thumbnails) if t.get("url")), None)

            # Detectar orientación: width/height del video
            width  = info.get("width", 0) or 0
            height = info.get("height", 0) or 0
            # Algunos videos no traen w/h directamente, buscar en formats
            if not width or not height:
                for fmt in reversed(info.get("formats", [])):
                    if fmt.get("width") and fmt.get("height"):
                        width  = fmt["width"]
                        height = fmt["height"]
                        break
            orientation = "vertical" if height > width else "horizontal"

            return jsonify({
                "title":       info.get("title", "Sin título"),
                "channel":     info.get("uploader", "Desconocido"),
                "duration":    info.get("duration", 0),
                "thumbnail":   thumb,
                "width":       width,
                "height":      height,
                "orientation": orientation,
            })

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private video" in msg:
            return jsonify({"error": "Este video es privado"}), 400
        elif "unavailable" in msg.lower() or "removed" in msg.lower():
            return jsonify({"error": "Este video no está disponible o fue eliminado"}), 400
        elif "confirm your age" in msg.lower():
            return jsonify({"error": "Este video tiene restricción de edad"}), 400
        return jsonify({"error": "No se pudo acceder al video"}), 400
    except Exception:
        return jsonify({"error": "Error inesperado al obtener el video"}), 400


@app.route("/download", methods=["POST"])
def download():
    data        = request.json
    url         = data.get("url", "").strip()
    tipo        = data.get("tipo", "audio")           # "audio" o "video"
    formato     = data.get("formato", "mp3")          # audio
    calidad     = data.get("calidad", "192")          # audio kbps
    resolucion  = data.get("resolucion", "720")       # video height
    fmt_video   = data.get("formato_video", "mp4")    # video container
    orientacion = data.get("orientacion", "horizontal")
    crop_x_pct  = float(data.get("crop_x_pct", 50))  # 0-100, default centro
    start       = int(data.get("start", 0))
    end         = int(data.get("end", 0))

    valida, motivo = validar_url_youtube(url)
    if not valida:
        return jsonify({"error": motivo}), 400

    if tipo == "audio":
        if formato not in ["mp3", "m4a", "wav", "ogg"]: formato = "mp3"
        if str(calidad) not in ["128", "192", "256", "320"]: calidad = "192"
    else:
        if str(resolucion) not in ["1080", "720", "480", "360"]: resolucion = "720"
        if fmt_video not in ["mp4", "webm"]: fmt_video = "mp4"

    if start < 0: start = 0
    if end <= start:
        return jsonify({"error": "El rango de tiempo no es válido"}), 400

    job_id = str(uuid.uuid4())
    download_jobs[job_id] = {"state": "starting", "percent": 0, "title": ""}

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info  = ydl.extract_info(url, download=False)
            title = info.get("title", "Audio")
            download_jobs[job_id]["title"] = title
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    thread = threading.Thread(
        target=_run_download,
        args=(job_id, url, tipo, formato, calidad, resolucion, fmt_video, orientacion, crop_x_pct, start, end),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "title": title})


def _run_download(job_id, url, tipo, formato, calidad, resolucion, fmt_video, orientacion, crop_x_pct, start, end):
    output_path = os.path.join(DOWNLOAD_FOLDER, job_id)
    os.makedirs(output_path, exist_ok=True)

    try:
        # ── 1. Caché ───────────────────────────────────────────────────────────
        cc = caches[tipo]
        if cache_is_valid(url, tipo):
            print(f"⚡ Usando caché {tipo} para: {url[:50]}")
            download_jobs[job_id].update({"state": "cutting", "percent": 88})
            cc["last_used"] = time.time()
            raw_path = cc["filepath"]
        else:
            if cc["url"] and cc["url"] != url:
                clear_cache(tipo)
            with cc["lock"]:
                if not cache_is_valid(url, tipo):
                    cc["downloading"] = True
                    cc["url"] = url
            raw_path = download_raw_video(url, job_id, tipo)
            with cc["lock"]:
                cc["filepath"]    = raw_path
                cc["ready"]       = True
                cc["last_used"]   = time.time()
                cc["downloading"] = False
            print(f"✅ {tipo.capitalize()} cacheado en: {raw_path}")

        # ── 2. Cortar y convertir ──────────────────────────────────────────────
        download_jobs[job_id].update({"state": "cutting", "percent": 88})

        title     = download_jobs[job_id].get("title", "media")
        safe_title = "".join(ch for ch in title if ch.isalnum() or ch in " -_")[:60].strip()

        if tipo == "audio":
            cut_and_convert(raw_path, output_path, start, end, formato, calidad, job_id)
            ext = formato
        else:
            cut_video(raw_path, output_path, start, end, resolucion, fmt_video, orientacion, crop_x_pct, job_id)
            ext = fmt_video

        # Renombrar con título
        final_name = f"{safe_title}.{ext}" if safe_title else f"media.{ext}"
        src = os.path.join(output_path, f"output.{ext}")
        dst = os.path.join(output_path, final_name)
        if os.path.exists(src):
            os.rename(src, dst)
            download_jobs[job_id]["filename"] = dst

    except Exception as e:
        download_jobs[job_id]["state"]        = "error"
        download_jobs[job_id]["error"]        = str(e)
        download_jobs[job_id]["completed_at"] = time.time()
        caches[tipo]["downloading"] = False


@app.route("/status/<job_id>")
def status(job_id):
    job = download_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job no encontrado"}), 404
    return jsonify(job)


@app.route("/file/<job_id>")
def get_file(job_id):
    job = download_jobs.get(job_id)
    if not job or job.get("state") != "done":
        return "Archivo no disponible", 404
    filepath = job.get("filename")
    if not filepath or not os.path.exists(filepath):
        return "Archivo no encontrado", 404
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))


def cleanup_on_exit():
    """Limpia todos los archivos temporales al cerrar el servidor."""
    print("\n🧹 Limpiando archivos temporales...")
    try:
        if os.path.exists(DOWNLOAD_FOLDER):
            shutil.rmtree(DOWNLOAD_FOLDER)
            print("✅ Carpeta de descargas eliminada")
    except Exception as e:
        print(f"⚠️  No se pudo limpiar: {e}")


def cleanup_on_startup():
    """Limpia archivos residuales de sesiones anteriores al iniciar."""
    if os.path.exists(DOWNLOAD_FOLDER):
        try:
            shutil.rmtree(DOWNLOAD_FOLDER)
        except Exception:
            pass
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    print("🧹 Carpeta de descargas limpia")


if __name__ == "__main__":
    import signal
    import atexit

    cleanup_on_startup()

    # Registrar limpieza al salir (Ctrl+C, kill, etc.)
    atexit.register(cleanup_on_exit)

    def handle_signal(sig, frame):
        print("\n⛔ Señal de cierre recibida...")
        cleanup_on_exit()
        os._exit(0)

    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("\n🎵 YouTube Audio Downloader")
    print("─" * 35)
    print("✅ Servidor iniciado")
    print("🌐 Abre en tu browser: http://localhost:5000")
    print("─" * 35)
    print("Presiona Ctrl+C para detener\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
