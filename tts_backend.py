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

Ruta (regístrala en app.py: `from tts_backend import tts_bp; app.register_blueprint(tts_bp)`):
  GET /api/tts?texto=...&voz=Mia|Ximena   -> audio/mpeg (mp3), con caché en disco por (voz|texto)

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
import os

import boto3
import requests
from flask import Blueprint, Response, abort, request

tts_bp = Blueprint("tts", __name__)

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


@tts_bp.route("/api/tts")
def tts():
    texto = (request.args.get("texto") or "").strip()
    voz = (request.args.get("voz") or "Mia").strip()
    if not texto:
        abort(400)
    if len(texto) > 800:
        texto = texto[:800]

    # La clave incluye el ID del modelo de Fish Audio (no solo "voz"): así, si alguna vez se
    # cambia el reference_id de una voz (como pasó con Ximena, de un clon genérico al anuncio
    # real del Metrobús), la caché vieja queda automáticamente huérfana -- nunca sirve audio de
    # un modelo distinto al que está configurado ahora, sin tener que purgar el directorio a mano.
    fish_model = FISH_AUDIO_VOICES.get(voz)
    key = hashlib.sha256((voz + "|" + (fish_model or "") + "|" + texto).encode("utf-8")).hexdigest()
    path = os.path.join(CACHE_DIR, key + ".mp3")

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        try:
            audio = (_sintetizar_fish_audio(texto, fish_model) if fish_model
                      else _sintetizar_polly(texto, voz))
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(audio)
            os.replace(tmp, path)
        except Exception as e:
            motor = "Fish Audio" if fish_model else "Polly"
            print(f"[tts] {motor} error: {e}", flush=True)
            abort(502)

    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=604800"})
