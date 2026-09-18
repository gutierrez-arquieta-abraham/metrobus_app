# ============================================================
# MÓDULO   : tts_backend.py   (Blueprint de Flask)
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Síntesis de VOZ con AWS Polly (voz "Mia", es-MX). Endpoint /api/tts?texto=
# devuelve el audio (mp3) de una frase, con CACHÉ en disco por (voz|texto):
# cada frase se sintetiza UNA sola vez (los nombres de estación son finitos),
# así no se repite el costo.
#
# Requiere boto3 + credenciales AWS (rol IAM del EC2 o llaves en el entorno).
# La voz la usa el "modo recorrido" de la app. Se registra como blueprint en app.py.
# ============================================================
"""
tts_backend.py — Síntesis de voz con AWS Polly (voz "Mia", es-MX) para GeoMB, como Blueprint Flask.

Ruta (regístrala en app.py: `from tts_backend import tts_bp; app.register_blueprint(tts_bp)`):
  GET /api/tts?texto=...&voz=Mia   -> audio/mpeg (mp3), con caché en disco por (voz|texto)

Requisitos:
  pip install boto3
  Credenciales AWS con permiso polly:SynthesizeSpeech, vía:
    - Rol IAM de la instancia EC2 (recomendado), o
    - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY en el env del servicio.
  AWS_REGION (opcional, default us-east-1).  TTS_CACHE (opcional, dir de caché).

Mia es voz estándar (es-MX). El resultado se cachea, así que cada frase se sintetiza UNA vez
(los nombres de estación son finitos) y no se repite el costo.
"""
import hashlib
import os

import boto3
from flask import Blueprint, Response, abort, request

tts_bp = Blueprint("tts", __name__)

CACHE_DIR = os.environ.get(
    "TTS_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_cache"))
os.makedirs(CACHE_DIR, exist_ok=True)

_polly = None


def _cliente():
    global _polly
    if _polly is None:
        _polly = boto3.client("polly", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _polly


@tts_bp.route("/api/tts")
def tts():
    texto = (request.args.get("texto") or "").strip()
    voz = (request.args.get("voz") or "Mia").strip()
    if not texto:
        abort(400)
    if len(texto) > 800:
        texto = texto[:800]

    key = hashlib.sha256((voz + "|" + texto).encode("utf-8")).hexdigest()
    path = os.path.join(CACHE_DIR, key + ".mp3")

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        try:
            r = _cliente().synthesize_speech(
                Text=texto, VoiceId=voz, OutputFormat="mp3", LanguageCode="es-MX")
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(r["AudioStream"].read())
            os.replace(tmp, path)
        except Exception as e:
            print(f"[tts] Polly error: {e}", flush=True)
            abort(502)

    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=604800"})
