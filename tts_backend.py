# ============================================================
# MÓDULO   : tts_backend.py   (Blueprint de Flask)
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Síntesis de VOZ con AWS Polly (voz "Mia", es-MX) O Fish Audio (voz "Ximena", clon de
# Loquendo). Endpoint /api/tts?texto=...&voz=... devuelve el audio (mp3) de una frase, con
# CACHÉ en disco por (voz|texto): cada frase se sintetiza UNA sola vez (los nombres de
# estación son finitos), así no se repite el costo -- esto importa el doble con Ximena,
# que se paga por uso (Mia/Polly tiene su propio costo pero ya está presupuestado).
#
# Requiere boto3 + credenciales AWS (rol IAM del EC2 o llaves en el entorno) para Mia, y
# FISH_AUDIO_API_KEY en el entorno para Ximena. Voz "experimental": la app la trae apagada
# por default (ajuste oculto) mientras se decide el modelo de costo/pago hacia el usuario.
# La voz la usa el "modo recorrido" de la app. Se registra como blueprint en app.py.
# ============================================================
"""
tts_backend.py — Síntesis de voz (AWS Polly "Mia" / Fish Audio "Ximena") para GeoMB, Blueprint Flask.

Rutas (regístralas en app.py: `from tts_backend import tts_bp; app.register_blueprint(tts_bp)`):
  GET  /api/tts?texto=...&voz=Mia|Ximena   -> audio/mpeg (mp3), con caché en disco por (voz|texto)
  POST /api/tts/paquete  {"voz": "Mia", "textos": [...]}  -> application/zip con los audios de
       TODAS esas frases en un solo archivo (descarga manual de una línea en la app): sale de la
       misma caché; solo se sintetizan las que falten (con tope de tiempo por petición, ver abajo).

Requisitos:
  pip install boto3 requests
  Para "Mia" (Polly) -- credenciales AWS con permiso polly:SynthesizeSpeech, vía:
    - Rol IAM de la instancia EC2 (recomendado), o
    - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY en el env del servicio.
  Para "Ximena" (Fish Audio) -- FISH_AUDIO_API_KEY en el env del servicio (clave propia del
  backend, NUNCA en la app ni en el repo -- mismo trato que las credenciales de Polly).
  AWS_REGION (opcional, default us-east-1).  TTS_CACHE (opcional, dir de caché).

El resultado se cachea, así que cada frase se sintetiza UNA vez (los nombres de estación son
finitos) y no se repite el costo -- en Ximena (Fish Audio, de pago por uso) esto es lo único
que mantiene el costo bajo mientras no hay un límite de uso propio.
"""
import hashlib
import io
import json
import os
import time
import zipfile

import boto3
import requests
from flask import Blueprint, Response, abort, jsonify, request

tts_bp = Blueprint("tts", __name__)

# Caché PERMANENTE en el disco del EC2 (EBS: sobrevive reinicios y apagados; solo se pierde si se
# elimina la instancia). Nunca se purga sola ni la borra deploy_backend.sh. TTS_CACHE permite
# moverla fuera del repo (p. ej. /var/lib/geomb/tts_cache) sin cambiar código.
CACHE_DIR = os.environ.get(
    "TTS_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_cache"))
os.makedirs(CACHE_DIR, exist_ok=True)

_polly = None

# ID del modelo de "Ximena" en Fish Audio. Clonado del anuncio REAL del Metrobús CDMX (locución
# original de abordo, no una voz genérica tipo Loquendo) a partir de un clip limpio de la
# grabación "Antiguo Anuncio Metrobús CDMX Línea 1" que proporcionó el usuario -- modelo propio
# (visibility=private), creado vía POST /model, no un modelo público del catálogo. Solo se
# mapean aquí las voces que realmente corren por Fish Audio; cualquier otro valor de "voz" sigue
# yendo por Polly.
FISH_AUDIO_VOICES = {
    "Ximena": "883dfc99945b47bd83efaaa87f99925b",
}


def _cliente_polly():
    global _polly
    if _polly is None:
        _polly = boto3.client("polly", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _polly


def _sintetizar_polly(texto, voz):
    r = _cliente_polly().synthesize_speech(
        Text=texto, VoiceId=voz, OutputFormat="mp3", LanguageCode="es-MX")
    return r["AudioStream"].read()


def _sintetizar_fish_audio(texto, model_id):
    api_key = os.environ["FISH_AUDIO_API_KEY"]
    r = requests.post(
        "https://api.fish.audio/v1/tts",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"text": texto, "reference_id": model_id, "format": "mp3"},
        timeout=30)
    r.raise_for_status()
    return r.content


def _clave(texto, voz):
    """Nombre en caché de (voz, texto). Incluye el ID del modelo de Fish Audio (no solo "voz"): así,
    si alguna vez se cambia el reference_id de una voz (como pasó con Ximena, de un clon genérico al
    anuncio real del Metrobús), la caché vieja queda automáticamente huérfana -- nunca sirve audio de
    un modelo distinto al que está configurado ahora, sin tener que purgar el directorio a mano."""
    fish_model = FISH_AUDIO_VOICES.get(voz)
    return hashlib.sha256((voz + "|" + (fish_model or "") + "|" + texto).encode("utf-8")).hexdigest()


def _ruta_en_cache(texto, voz):
    """Ruta del mp3 de (voz, texto) si YA está en caché; None si no."""
    path = os.path.join(CACHE_DIR, _clave(texto, voz) + ".mp3")
    return path if os.path.exists(path) and os.path.getsize(path) > 0 else None


def _obtener(texto, voz):
    """Ruta del mp3 de (voz, texto): de la caché o, si falta, sintetizándolo UNA vez y guardándolo.
    Lanza la excepción del motor si falla."""
    path = _ruta_en_cache(texto, voz)
    if path:
        return path
    fish_model = FISH_AUDIO_VOICES.get(voz)
    path = os.path.join(CACHE_DIR, _clave(texto, voz) + ".mp3")
    audio = (_sintetizar_fish_audio(texto, fish_model) if fish_model
             else _sintetizar_polly(texto, voz))
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(audio)
    os.replace(tmp, path)
    return path


@tts_bp.route("/api/tts")
def tts():
    texto = (request.args.get("texto") or "").strip()
    voz = (request.args.get("voz") or "Mia").strip()
    if not texto:
        abort(400)
    if len(texto) > 800:
        texto = texto[:800]
    try:
        path = _obtener(texto, voz)
    except Exception as e:
        motor = "Fish Audio" if FISH_AUDIO_VOICES.get(voz) else "Polly"
        print(f"[tts] {motor} error: {e}", flush=True)
        abort(502)
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=604800"})


# --- Paquete .zip (descarga manual de una línea) ---------------------------------------------
# Límites: frases por petición y segundos que una petición puede pasar SINTETIZANDO frases nuevas
# (gunicorn corta un worker a los 30 s por default). Las que no alcanzaron a sintetizarse salen en
# "pendientes" del manifiesto y la app las vuelve a pedir en otra petición -- para entonces las
# anteriores ya están en caché y salen al instante.
PAQUETE_MAX_TEXTOS = int(os.environ.get("TTS_PAQUETE_MAX_TEXTOS", "600"))
PAQUETE_SEG_SINTESIS = float(os.environ.get("TTS_PAQUETE_SEG", "18"))


@tts_bp.route("/api/tts/paquete", methods=["POST"])
def tts_paquete():
    """Body JSON {"voz": "Mia", "textos": ["...", ...]}. Responde un .zip con "NNNN.mp3" (NNNN =
    índice del texto en la lista pedida) y "manifest.json" {"voz", "ok": [...], "pendientes": [...],
    "fallidos": [...]} -- índices. Sin compresión (mp3 ya viene comprimido)."""
    datos = request.get_json(silent=True) or {}
    voz = str(datos.get("voz") or "Mia").strip()
    textos = datos.get("textos")
    if not isinstance(textos, list) or not textos:
        return jsonify(error="textos vacío"), 400
    if len(textos) > PAQUETE_MAX_TEXTOS:
        return jsonify(error=f"máximo {PAQUETE_MAX_TEXTOS} textos por petición"), 413

    inicio = time.monotonic()
    ok, pendientes, fallidos = [], [], []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i, t in enumerate(textos):
            texto = str(t or "").strip()[:800]
            if not texto:
                fallidos.append(i)
                continue
            path = _ruta_en_cache(texto, voz)
            if path is None:
                if time.monotonic() - inicio > PAQUETE_SEG_SINTESIS:
                    pendientes.append(i)     # se pedirá en la siguiente petición
                    continue
                try:
                    path = _obtener(texto, voz)
                except Exception as e:
                    print(f"[tts/paquete] error ({voz}): {e}", flush=True)
                    fallidos.append(i)
                    continue
            z.write(path, "%04d.mp3" % i)
            ok.append(i)
        z.writestr("manifest.json", json.dumps(
            {"voz": voz, "ok": ok, "pendientes": pendientes, "fallidos": fallidos}))
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": "attachment; filename=voz.zip"})
