# -*- coding: utf-8 -*-
# ============================================================
# MÓDULO   : admin_version.py   (Blueprint de Flask)
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Panel para fijar la versión MÍNIMA de la app (versionCode) que se acepta.
# GET/POST /admin/version (mismo mTLS que /admin/afectacion). minima_vigente()
# la usan: GET /data/version.json (para que la app se autobloquee al arrancar,
# ver ActualizacionObligatoria en GeoMB) y el 'before_request' de app.py (para
# rechazar con 426 las llamadas de clientes atrasados vía el header
# X-App-Version -- cinturón y tirantes, por si alguien manipula el APK).
# ============================================================
"""
admin_version.py — Versión mínima requerida de la app (GeoMB / EC2)
---------------------------------------------------------------------
  GET  /admin/version         -> form web (mínima actual + campo para cambiarla).
  POST /admin/version/guardar -> valida token, guarda {"minima": int} en version_app.json.

0 = sin mínimo configurado -- por defecto, NADIE se bloquea (ante cualquier duda, como
archivo ausente o corrupto, minima_vigente() también devuelve 0: un error aquí jamás debe
bloquear a todos los usuarios).
"""

import json
import os
import time

from flask import Blueprint, Response, request

from admin_afect import _token_ok   # mismo criterio mTLS/token que /admin/afectacion

admin_version_bp = Blueprint("admin_version", __name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.environ.get("VERSION_APP_FILE", "").split("#", 1)[0].strip() \
    or os.path.join(APP_DIR, "data", "version_app.json")


def minima_vigente():
    """versionCode mínimo vigente, o 0 (sin mínimo / archivo ausente o inválido -- nunca bloquea
       por error)."""
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return int(json.load(f).get("minima", 0))
    except Exception:
        return 0


def _guardar(minima):
    os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
    tmp = VERSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"minima": int(minima), "actualizado": int(time.time())}, f)
    os.replace(tmp, VERSION_FILE)


@admin_version_bp.route("/admin/version", methods=["GET"])
def form():
    return Response(_html(), mimetype="text/html")


@admin_version_bp.route("/admin/version/guardar", methods=["POST"])
def guardar():
    if not _token_ok():
        return {"ok": False, "error": "token inválido"}, 403
    body = request.get_json(silent=True) or {}
    try:
        minima = int(body.get("minima", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "número inválido"}, 400
    if minima < 0:
        return {"ok": False, "error": "número inválido"}, 400
    _guardar(minima)
    return {"ok": True, "minima": minima}


def _html():
    return _HTML_TPL.replace("__MINIMA__", str(minima_vigente()))


_HTML_TPL = """<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GeoMB · Versión mínima</title>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;font:16px/1.4 system-ui,Segoe UI,Roboto,sans-serif;background:#111;color:#eee;padding:16px}
 h1{font-size:19px;margin:.2em 0 .6em}
 label{display:block;margin:.7em 0 .2em;font-size:13px;color:#bbb}
 input{width:100%;padding:12px;border-radius:10px;border:1px solid #333;background:#1c1c1c;color:#eee;font-size:16px}
 button{margin-top:18px;width:100%;padding:15px;border:0;border-radius:12px;background:#D40D0D;color:#fff;font-size:17px;font-weight:600}
 #msg{margin-top:14px;padding:12px;border-radius:10px;display:none;font-size:14px}
 .ok{background:#123d1a;color:#9be7a6}.err{background:#3d1212;color:#e79b9b}
 .hint{font-size:12px;color:#888;margin-top:2px}
</style></head><body>
<h1>Versión mínima requerida</h1>
<div class="hint">Cualquier instalación con versionCode MENOR al que pongas aquí se bloquea:
la app muestra una pantalla de "actualiza para continuar" (no se puede cerrar) y el backend
rechaza (426) sus llamadas. 0 = sin mínimo, nadie se bloquea. El versionCode actual está en
app/build.gradle.kts del repo GeoMB.</div>

<label>Token de admin (opcional, con certificado no hace falta)</label>
<input id="token" type="password" autocomplete="off" placeholder="solo si no usas certificado">

<label>versionCode mínimo</label>
<input id="minima" type="number" min="0" step="1" value="__MINIMA__">

<button id="btnGuardar">Guardar</button>
<div id="msg"></div>
<script>
 var tk = document.getElementById('token');
 tk.value = localStorage.getItem('geomb_admin_token') || '';
 tk.addEventListener('change', function(){ localStorage.setItem('geomb_admin_token', tk.value); });
 document.getElementById('btnGuardar').addEventListener('click', function(){
  var m = document.getElementById('msg'); m.style.display = 'block';
  fetch('/admin/version/guardar', {method:'POST', headers:{'Content-Type':'application/json',
    'X-Admin-Token': tk.value}, body: JSON.stringify({minima: parseInt(document.getElementById('minima').value || '0', 10)})})
   .then(function(r){ return r.json().then(function(j){ return {s:r.status, j:j}; }); })
   .then(function(x){
    if (x.j.ok) { m.className = 'ok'; m.textContent = '✅ Guardado: mínima = ' + x.j.minima; }
    else { m.className = 'err'; m.textContent = '⚠ ' + (x.j.error || ('error ' + x.s)); }
   })
   .catch(function(e){ m.className = 'err'; m.textContent = '⚠ ' + e; });
 });
</script>
</body></html>"""
