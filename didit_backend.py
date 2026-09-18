# ============================================================
# MÓDULO   : didit_backend.py   (Blueprint de Flask)
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Endpoints de verificación de identidad (KYC) con Didit:
#   - /api/didit/session : crea una sesión y devuelve la URL de verificación;
#   - /api/didit/webhook : recibe el resultado firmado (verifica la firma) y
#     marca al usuario como aprobado;
#   - /api/didit/status  : consulta si un usuario quedó verificado.
#
# Las llaves (API key, secreto del webhook) van en variables de entorno del
# servidor, NUNCA en el código. Se registra como blueprint en app.py.
# ============================================================
"""
didit_backend.py — Endpoints KYC (Didit, API v3) para GeoMB, como Blueprint de Flask.

Rutas (regístralo en app.py: `from didit_backend import didit_bp; app.register_blueprint(didit_bp)`):
  GET|POST  /api/didit/session   -> crea una sesión (POST /v3/session/) y devuelve {"url": ...}
  POST      /api/didit/webhook   -> verifica X-Signature-V2 y, si "Approved", marca verificado
  GET       /api/didit/status    -> {"verificado": bool, "nombre": str} para que la app consulte

Variables de entorno (NUNCA hardcodear las llaves; van en el systemd/.env del servidor):
  DIDIT_API_KEY         (obligatoria) API key secreta de Didit  (header x-api-key)
  DIDIT_WEBHOOK_SECRET  (obligatoria) secreto para verificar el HMAC del webhook
  DIDIT_WORKFLOW_ID     (opcional) UUID del flujo; default = el de "Free KYC"
  DIDIT_API_URL         (opcional) default https://verification.didit.me/v3/session/
  DIDIT_STORE           (opcional) archivo de estado, default /tmp/geomb_kyc.json

Webhook a registrar en Didit:  https://geomb.duckdns.org/api/didit/webhook  (HTTPS público)
"""
import hashlib
import hmac
import json
import os
import threading
import time

import requests
from flask import Blueprint, jsonify, request

didit_bp = Blueprint("didit", __name__)

API_KEY = os.environ.get("DIDIT_API_KEY", "").strip()
WEBHOOK_SECRET = os.environ.get("DIDIT_WEBHOOK_SECRET", "").strip()
WORKFLOW_ID = os.environ.get("DIDIT_WORKFLOW_ID", "a74adce9-1c2d-4b71-8177-c6e798494aeb").strip()
API_URL = os.environ.get("DIDIT_API_URL", "https://verification.didit.me/v3/session/").strip()
STORE = os.environ.get(
    "DIDIT_STORE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "kyc_store.json")).strip()

_lock = threading.Lock()


def _load():
    try:
        with open(STORE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    try:
        tmp = STORE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, STORE)
    except Exception:
        pass


def _set_verificado(uid, nombre):
    if not uid:
        return
    with _lock:
        d = _load()
        d[str(uid)] = {"verificado": True, "nombre": nombre or "", "ts": int(time.time())}
        _save(d)


# ---- create session ----

@didit_bp.route("/api/didit/session", methods=["GET", "POST"])
def crear_sesion():
    if not (API_KEY and WORKFLOW_ID):
        return jsonify({"error": "KYC no configurado"}), 503
    uid = request.args.get("uid")
    if not uid and request.is_json:
        uid = (request.get_json(silent=True) or {}).get("uid")

    print(f"[didit] sesion solicitada vendor_data={uid!r}", flush=True)
    body = {"workflow_id": WORKFLOW_ID}
    if uid:
        body["vendor_data"] = str(uid)   # tu id interno; vuelve en el webhook
    try:
        r = requests.post(API_URL, json=body, timeout=20, headers={
            "accept": "application/json",
            "content-type": "application/json",
            "x-api-key": API_KEY,
        })
        if r.status_code // 100 != 2:
            return jsonify({"error": "didit", "status": r.status_code, "detail": r.text[:300]}), 502
        data = r.json()
        url = data.get("url") or data.get("session_url") or data.get("verification_url")
        if not url:
            return jsonify({"error": "sin url", "raw": data}), 502
        return jsonify({"url": url, "session_id": data.get("session_id")})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ---- webhook (X-Signature-V2) ----

def _shorten_floats(v):
    """Floats enteros (1.0) -> int (1), recursivo. Igual que la canonicalización de Didit."""
    if isinstance(v, list):
        return [_shorten_floats(x) for x in v]
    if isinstance(v, dict):
        return {k: _shorten_floats(x) for k, x in v.items()}
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _canonical(parsed):
    # shorten_floats -> claves ordenadas -> JSON compacto, Unicode sin escapar
    return json.dumps(_shorten_floats(parsed), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


@didit_bp.route("/api/didit/webhook", methods=["POST"])
def webhook():
    raw = request.get_data()
    ts = request.headers.get("x-timestamp", "")
    sig_raw = request.headers.get("x-signature", "")      # HMAC sobre los BYTES crudos
    sig_v2 = request.headers.get("x-signature-v2", "")    # HMAC sobre el cuerpo canonicalizado
    print(f"[didit] webhook recibido bytes={len(raw)} ts={ts!r} "
          f"xsig={'si' if sig_raw else 'no'} xsigv2={'si' if sig_v2 else 'no'}", flush=True)
    # 1) frescura (anti-replay): 300 s
    try:
        if abs(time.time() - float(ts)) > 300:
            print("[didit] webhook RECHAZADO: timestamp fuera de rango", flush=True)
            return jsonify({"error": "stale"}), 401
    except Exception:
        print("[didit] webhook RECHAZADO: timestamp inválido", flush=True)
        return jsonify({"error": "timestamp"}), 401
    if not WEBHOOK_SECRET:
        return jsonify({"error": "sin secreto"}), 500
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        print("[didit] webhook RECHAZADO: json", flush=True)
        return jsonify({"error": "json"}), 400
    # --------------------------------------------------------
    # PARTE "REBUSCADA": verificar la FIRMA del webhook (HMAC)
    # --------------------------------------------------------
    # ¿Cómo sabemos que este webhook lo mandó Didit y no un impostor? Con un
    # HMAC: Didit y nosotros compartimos un SECRETO (WEBHOOK_SECRET). Didit
    # calcula hash(secreto + cuerpo) y lo manda en una cabecera; aquí calculamos
    # lo MISMO y comparamos. Sin conocer el secreto, nadie puede falsificarlo.
    #
    # Hay DOS variantes de firma y aceptamos cualquiera de las dos:
    #   - X-Signature    : HMAC sobre los BYTES CRUDOS del cuerpo (tal cual llegan).
    #   - X-Signature-V2 : HMAC sobre el cuerpo CANONICALIZADO (ver _canonical):
    #     hay que reconstruir EXACTAMENTE los bytes que Didit firmó (mismos
    #     floats "1.0"->1, claves ordenadas, JSON compacto), o el hash no coincide.
    #
    # OJO: hmac.compare_digest compara en TIEMPO CONSTANTE (no corta al primer
    # byte distinto). Así evita los "timing attacks", que adivinarían la firma
    # byte por byte midiendo cuánto tarda la comparación.
    # 2) firma: acepta si coincide X-Signature (bytes crudos) O X-Signature-V2 (canonical).
    esperado_raw = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    esperado_v2 = hmac.new(WEBHOOK_SECRET.encode(), _canonical(parsed).encode("utf-8"),
                           hashlib.sha256).hexdigest()
    ok = (sig_raw and hmac.compare_digest(sig_raw, esperado_raw)) \
        or (sig_v2 and hmac.compare_digest(sig_v2, esperado_v2))
    if not ok:
        print(f"[didit] webhook RECHAZADO: firma NO coincide "
              f"(xsig={sig_raw[:12]}… esperado_raw={esperado_raw[:12]}… | "
              f"xsigv2={sig_v2[:12]}… esperado_v2={esperado_v2[:12]}…) "
              f"status={parsed.get('status')!r} vendor_data={parsed.get('vendor_data')!r}", flush=True)
        return jsonify({"error": "firma"}), 401

    # 3) aplica la decisión (status case-sensitive)
    status = parsed.get("status", "")
    uid = parsed.get("vendor_data")
    print(f"[didit] webhook OK status={status!r} vendor_data={uid!r}", flush=True)
    if status == "Approved":
        _set_verificado(uid, _nombre_de(parsed))
    elif status in ("Kyc Expired", "Declined"):
        with _lock:
            d = _load()
            if str(uid) in d:
                del d[str(uid)]
                _save(d)
    # 4) 2xx rápido
    return jsonify({"ok": True})


def _nombre_de(ev):
    """Extrae el nombre validado del decision.id_verifications[] (esquema v3)."""
    try:
        dec = ev.get("decision") or {}
        idv = dec.get("id_verifications") or []
        if idv:
            fn = idv[0].get("first_name") or ""
            ln = idv[0].get("last_name") or ""
            return (fn + " " + ln).strip()
    except Exception:
        pass
    return ""


# ---- status (la app consulta) ----

@didit_bp.route("/api/didit/status", methods=["GET"])
def status():
    uid = request.args.get("uid", "")
    with _lock:
        d = _load()
    e = d.get(str(uid)) or {}
    return jsonify({"verificado": bool(e.get("verificado")), "nombre": e.get("nombre", "")})
