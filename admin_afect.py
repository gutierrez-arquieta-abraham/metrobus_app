# -*- coding: utf-8 -*-
# ============================================================
# MÓDULO   : admin_afect.py   (Blueprint de Flask)
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Panel WEB para mandar avisos de afectación A MANO (más rápido que el
# scraper). Formulario móvil en /admin/afectacion: eliges línea, estado,
# lugar, info y tramos de circuito.
#
# Al enviar: empuja por FCM (topic "afectaciones", reusa push_metrobus._push)
# y escribe un OVERRIDE en afect_manual.json con caducidad (expira). Protegido
# con certificado cliente (mTLS) que valida nginx; token solo de respaldo.
# ============================================================
"""
admin_afect.py — Panel web para mandar avisos de afectación A MANO (GeoMB / EC2)
--------------------------------------------------------------------------------
Blueprint Flask que se registra en app.py. Sirve un formulario mobile-first en
  GET  /admin/afectacion        -> la página (form). No revela secretos.
  POST /admin/afectacion        -> valida ADMIN_TOKEN, empuja por FCM al topic
                                   "afectaciones" (mismo formato que consume la
                                   app: MensajesService.notificarAfectacion) y
                                   refleja el aviso en el panel (afectaciones_mexibus.json).

Objetivo: informar MÁS RÁPIDO que el scraper. Escribes línea, estado, lugar,
info (y, si aplica, los tramos del circuito) y un tap lo difunde a todos.

Seguridad: se protege con ADMIN_TOKEN (el mismo de /admin/rt_url). El token NO
va en la URL: se manda en el cuerpo del POST y el navegador lo recuerda en
localStorage para no re-teclearlo. Reutiliza push_metrobus._push (Firebase con
FIREBASE_CREDENTIALS_JSON), así no duplica credenciales.
"""

import json
import os
import re
import time
from datetime import datetime

from flask import Blueprint, Response, request

admin_afect_bp = Blueprint("admin_afect", __name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Overrides manuales: app.py sirve /data/afectaciones_mexibus.json MEZCLANDO el archivo
# del feed con estos overrides (que ganan mientras no venzan). No escribimos el archivo
# del feed directamente (lo pisaría el servicio de afectaciones cada 60 s).
MANUAL_FILE = os.environ.get("AFECT_MANUAL_FILE", "").split("#", 1)[0].strip() \
    or os.path.join(APP_DIR, "data", "afect_manual.json")
# Horas que dura un aviso manual si no se actualiza (tope duro: 23:59 hrs CDMX del mismo día).
DUR_H = float(os.environ.get("AFECT_DUR_H", "7").split("#", 1)[0].strip() or "7")

# Catálogo de líneas (códigos que entiende la app). Metrobús 1-7, Mexibús 101-104,
# ramales 111-113. (Mexicable no se maneja desde el aviso web.)
LINEAS = [
    ("1", "Metrobús L1"), ("2", "Metrobús L2"), ("3", "Metrobús L3"),
    ("4", "Metrobús L4"), ("5", "Metrobús L5"), ("6", "Metrobús L6"),
    ("7", "Metrobús L7"),
    ("101", "Mexibús L1"), ("102", "Mexibús L2"), ("103", "Mexibús L3"),
    ("104", "Mexibús L4"),
    ("111", "Mexibús L1A (AIFA)"), ("112", "Mexibús L2A (Serv. Eléctrico)"),
    ("113", "Mexibús L3A"),
]

ESTADOS = [
    "Sin servicio", "Servicio parcial", "Retraso en el servicio",
    "Paso de largo", "Estación cerrada", "Estación en mantenimiento",
    "Afectación en el servicio", "Servicio restablecido",
]


def _token_ok():
    # 1) Certificado cliente verificado por nginx (mTLS en :8443). Es confiable porque gunicorn
    #    solo escucha en 127.0.0.1 y el :443 bloquea /admin, así que este header no es spoofeable
    #    desde afuera (nginx lo sobrescribe en cada request al :8443).
    if request.headers.get("X-Client-Verify", "") == "SUCCESS":
        return True
    # 2) Respaldo: token (útil para pruebas locales contra 127.0.0.1:8000).
    esperado = os.environ.get("ADMIN_TOKEN", "")
    if esperado:
        dado = request.form.get("token") or request.headers.get("X-Admin-Token", "")
        if dado == esperado:
            return True
    return False


def _segmentos_circuito(texto):
    """Cada línea 'A - B' del textarea -> [A, B] (tramos que SÍ operan en circuito)."""
    segs = []
    for ln in (texto or "").split("\n"):
        ln = ln.strip(" .")
        if not ln:
            continue
        m = re.match(r"^(.{2,40}?)\s*[-–]\s*(.{2,40}?)$", ln)
        if m:
            segs.append([m.group(1).strip(), m.group(2).strip()])
    return segs


def _empujar_fcm(linea, estado, lugar, info):
    """Difunde por FCM al topic 'afectaciones' (mismo esquema que enviar_fcm)."""
    from push_metrobus import _push   # reusa init de Firebase + messaging
    _push("afectaciones", "afectacion", linea, estado, lugar, info)


def _fin_del_dia_local(ts):
    """Epoch de las 23:59 (hora CDMX) del día de 'ts' (tope de vigencia del aviso)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Mexico_City")
    except Exception:
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=-6))
    d = datetime.fromtimestamp(ts, tz).replace(hour=23, minute=59, second=0, microsecond=0)
    return d.timestamp()


def _guardar_override(linea, estado, lugar, info, circuito):
    """Escribe/actualiza el override manual (con 'expira'). 'Servicio restablecido' quita
       la línea. app.py lo mezcla con el feed al servir el panel. Best-effort, atómico."""
    try:
        try:
            with open(MANUAL_FILE, encoding="utf-8") as f:
                manual = json.load(f)
        except Exception:
            manual = []
        ahora = time.time()
        # descarta vencidos y la línea que se re-escribe
        manual = [m for m in manual
                  if float(m.get("expira", 0)) > ahora
                  and int(m.get("linea", 0)) != int(linea)]
        if "restablec" not in estado.lower():
            expira = min(ahora + DUR_H * 3600, _fin_del_dia_local(ahora))
            e = {"linea": int(linea), "estado": estado, "lugar": lugar,
                 "info": info, "expira": int(expira)}
            if circuito:
                e["circuito"] = circuito
            manual.append(e)
        os.makedirs(os.path.dirname(MANUAL_FILE), exist_ok=True)
        tmp = MANUAL_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manual, f, ensure_ascii=False)
        os.replace(tmp, MANUAL_FILE)
        return True
    except Exception as e:
        print("[admin_afect] no se pudo escribir override:", e, flush=True)
        return False


@admin_afect_bp.route("/admin/afectacion", methods=["GET"])
def form():
    return Response(_HTML, mimetype="text/html")


@admin_afect_bp.route("/admin/afectacion", methods=["POST"])
def enviar():
    if not _token_ok():
        return {"ok": False, "error": "token inválido"}, 403
    linea = (request.form.get("linea") or "").strip()
    estado = (request.form.get("estado") or "").strip()
    lugar = (request.form.get("lugar") or "").strip()
    info = (request.form.get("info") or "").strip()
    circuito = _segmentos_circuito(request.form.get("circuito", ""))
    if not linea or not estado:
        return {"ok": False, "error": "faltan línea o estado"}, 400
    try:
        _empujar_fcm(linea, estado, lugar, info)
    except Exception as e:
        return {"ok": False, "error": f"FCM: {e}"}, 500
    panel = _guardar_override(linea, estado, lugar, info, circuito)
    return {"ok": True, "linea": linea, "estado": estado,
            "panel": panel, "circuito": circuito}


# ------------------------------------------------------------------ HTML (una sola página)
_OP_LINEAS = "".join(f'<option value="{v}">{t}</option>' for v, t in LINEAS)
_OP_ESTADOS = "".join(f'<option value="{e}">{e}</option>' for e in ESTADOS)

_HTML = """<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GeoMB · Enviar afectación</title>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;font:16px/1.4 system-ui,Segoe UI,Roboto,sans-serif;background:#111;color:#eee;padding:16px}
 h1{font-size:19px;margin:.2em 0 .6em}
 label{display:block;margin:.7em 0 .2em;font-size:13px;color:#bbb}
 input,select,textarea{width:100%;padding:12px;border-radius:10px;border:1px solid #333;background:#1c1c1c;color:#eee;font-size:16px}
 textarea{min-height:76px;resize:vertical}
 .row{display:flex;gap:10px}.row>*{flex:1}
 button{margin-top:18px;width:100%;padding:15px;border:0;border-radius:12px;background:#D40D0D;color:#fff;font-size:17px;font-weight:600}
 button:active{opacity:.85}
 #msg{margin-top:14px;padding:12px;border-radius:10px;display:none;font-size:14px}
 .ok{background:#123d1a;color:#9be7a6}.err{background:#3d1212;color:#e79b9b}
 .hint{font-size:12px;color:#888;margin-top:2px}
 details{margin-top:10px}summary{color:#bbb;font-size:14px}
</style></head><body>
<h1>Enviar afectación</h1>
<form id="f">
 <label>Token de admin (opcional)</label>
 <input id="token" name="token" type="password" autocomplete="off" placeholder="solo si no usas certificado">
 <div class="hint">Con el certificado (.p12) importado no hace falta. Solo respaldo; se recuerda en este teléfono.</div>

 <div class="row">
  <div><label>Línea</label><select name="linea">__LINEAS__</select></div>
  <div><label>Estado</label><select name="estado">__ESTADOS__</select></div>
 </div>

 <label>Lugar / estación (opcional)</label>
 <input name="lugar" placeholder="p. ej. ODAPAS y Santa Elena">

 <label>Detalle (info)</label>
 <textarea name="info" placeholder="debido a inundación a la altura de ..., se presenta retraso en el servicio"></textarea>

 <details><summary>Circuito (tramos que SÍ operan) — opcional</summary>
  <label>Un tramo por línea, formato "A - B"</label>
  <textarea name="circuito" placeholder="Chimalhuacán - Sor Juana Inés
Pantitlán - López Mateos"></textarea>
 </details>

 <button type="submit">Difundir aviso</button>
</form>
<div id="msg"></div>
<script>
 var tk=document.getElementById('token');
 tk.value=localStorage.getItem('geomb_admin_token')||'';
 tk.addEventListener('change',function(){localStorage.setItem('geomb_admin_token',tk.value)});
 document.getElementById('f').addEventListener('submit',function(ev){
  ev.preventDefault();
  localStorage.setItem('geomb_admin_token',tk.value);
  var m=document.getElementById('msg');m.style.display='none';
  fetch('/admin/afectacion',{method:'POST',body:new FormData(ev.target)})
   .then(function(r){return r.json().then(function(j){return {s:r.status,j:j}})})
   .then(function(x){
     m.style.display='block';
     if(x.j.ok){m.className='ok';m.textContent='✅ Enviado: '+x.j.estado+' (línea '+x.j.linea+')'+(x.j.panel?' · panel actualizado':' · panel no escrito');}
     else{m.className='err';m.textContent='⚠ '+(x.j.error||('error '+x.s));}
   })
   .catch(function(e){m.style.display='block';m.className='err';m.textContent='⚠ '+e;});
 });
</script>
</body></html>""".replace("__LINEAS__", _OP_LINEAS).replace("__ESTADOS__", _OP_ESTADOS)
