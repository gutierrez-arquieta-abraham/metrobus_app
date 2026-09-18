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
# ramales 111-113, Mexicable 201-202.
LINEAS = [
    ("1", "Metrobús L1"), ("2", "Metrobús L2"), ("3", "Metrobús L3"),
    ("4", "Metrobús L4"), ("5", "Metrobús L5"), ("6", "Metrobús L6"),
    ("7", "Metrobús L7"),
    ("101", "Mexibús L1"), ("102", "Mexibús L2"), ("103", "Mexibús L3"),
    ("104", "Mexibús L4"),
    ("111", "Mexibús L1A (AIFA)"), ("112", "Mexibús L2A (Serv. Eléctrico)"),
    ("113", "Mexibús L3A"),
    ("201", "Mexicable L1"), ("202", "Mexicable L2"),
]

ESTADOS = [
    "Sin servicio", "Servicio parcial", "Retraso en el servicio",
    "Obstrucción de carril", "Manifestación",
    "Paso de largo", "Estación cerrada", "Estación en mantenimiento",
    "Afectación en el servicio", "Servicio restablecido",
]

# Catálogo de estaciones por línea (extraído de GeoMB: lineas.json + mexibus.json), para
# poblar el <select> de "Estaciones afectadas"/"Circuito" según la línea elegida — evita
# tener que teclear el nombre exacto de la estación a mano.
try:
    with open(os.path.join(APP_DIR, "data", "estaciones_por_linea.json"), encoding="utf-8") as _f:
        ESTACIONES_POR_LINEA = json.load(_f)
except Exception:
    ESTACIONES_POR_LINEA = {}


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


def _guardar_override(linea, estado, lugar, info, circuito, duracion_h=None):
    """Escribe/actualiza el override manual (con 'expira'). 'Servicio restablecido' quita
       la línea. app.py lo mezcla con el feed al servir el panel. Best-effort, atómico.
       duracion_h: horas que dura ESTE aviso en particular; si no se manda, usa DUR_H (env)."""
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
            horas = duracion_h if duracion_h and duracion_h > 0 else DUR_H
            expira = min(ahora + horas * 3600, _fin_del_dia_local(ahora))
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
    duracion_h = None
    try:
        crudo = (request.form.get("duracion_h") or "").strip()
        if crudo:
            duracion_h = max(0.5, min(48.0, float(crudo)))   # tope razonable: 30 min a 48 h
    except ValueError:
        pass
    if not linea or not estado:
        return {"ok": False, "error": "faltan línea o estado"}, 400
    try:
        _empujar_fcm(linea, estado, lugar, info)
    except Exception as e:
        return {"ok": False, "error": f"FCM: {e}"}, 500
    panel = _guardar_override(linea, estado, lugar, info, circuito, duracion_h)
    return {"ok": True, "linea": linea, "estado": estado,
            "panel": panel, "circuito": circuito,
            "duracion_h": duracion_h or DUR_H}


# ------------------------------------------------------------------ HTML (una sola página)
_OP_LINEAS = "".join(f'<option value="{v}">{t}</option>' for v, t in LINEAS)
_OP_ESTADOS = "".join(f'<option value="{e}">{e}</option>' for e in ESTADOS)
_JS_ESTACIONES = json.dumps(ESTACIONES_POR_LINEA, ensure_ascii=False)

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
 .chips{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;-webkit-overflow-scrolling:touch}
 .chip{flex:0 0 auto;width:auto;margin:0;padding:9px 14px;border-radius:20px;border:1px solid #444;
       background:#1c1c1c;color:#ddd;font-size:13px;font-weight:500;white-space:nowrap}
 .chip.ok{background:#123d1a;color:#9be7a6;border-color:#1f5c2a}
 .chip:active{opacity:.8}
 .multi-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;align-items:center}
 .multi-row input{margin:0}
 .multi-row .x{flex:0 0 auto;width:36px;height:36px;padding:0;margin:0;border-radius:8px;
               background:#2a1414;color:#e79b9b;font-size:16px;font-weight:700}
 .link-btn{width:auto;margin-top:8px;padding:8px 4px;background:none;color:#7fb2e5;
           font-size:13px;font-weight:600;text-align:left}
 .link-btn:active{opacity:.7}
 .multi-row select{margin:0}
 .multi-row .otra{flex:1 0 100%;display:none}
</style></head><body>
<h1>Enviar afectación</h1>

<div class="hint">Plantillas rápidas (llenan estado + detalle; edítalos si hace falta):</div>
<div class="chips" id="plantillas">
 <button type="button" class="chip" data-estado="Manifestación" data-info="Manifestación bloquea la circulación">🚩 Manifestación</button>
 <button type="button" class="chip" data-estado="Obstrucción de carril" data-info="Choque/incidente vial afecta la circulación">🚗 Choque vial</button>
 <button type="button" class="chip" data-estado="Sin servicio" data-info="Corte de energía eléctrica en la estación">⚡ Corte de luz</button>
 <button type="button" class="chip" data-estado="Servicio parcial" data-info="Encharcamiento/inundación a la altura de la estación">🌧 Inundación</button>
 <button type="button" class="chip" data-estado="Estación en mantenimiento" data-info="Mantenimiento correctivo">🔧 Mantenimiento</button>
 <button type="button" class="chip ok" data-estado="Servicio restablecido" data-info="">✅ Restablecido</button>
</div>

<form id="f">
 <label>Token de admin (opcional)</label>
 <input id="token" name="token" type="password" autocomplete="off" placeholder="solo si no usas certificado">
 <div class="hint">Con el certificado (.p12) importado no hace falta. Solo respaldo; se recuerda en este teléfono.</div>

 <div class="row">
  <div><label>Línea</label><select name="linea">__LINEAS__</select></div>
  <div><label>Estado</label><select name="estado">__ESTADOS__</select></div>
 </div>

 <label>Estaciones afectadas (opcional)</label>
 <div id="lugares"><div class="multi-row">
   <select class="lugar-item"></select>
   <input class="otra lugar-otra" placeholder="Nombre de la estación">
 </div></div>
 <button type="button" class="link-btn" id="btnLugar">+ agregar otra estación</button>
 <div class="hint">Elige la línea primero para ver su lista. Varias estaciones se juntan solas con "y" al enviar.</div>

 <label>Detalle (info)</label>
 <textarea name="info" placeholder="debido a inundación a la altura de ..., se presenta retraso en el servicio"></textarea>

 <label>Duración del aviso</label>
 <input name="duracion_h" type="number" min="0.5" max="48" step="0.5" placeholder="por defecto 7 h (o hasta las 23:59, lo que sea antes)">
 <div class="hint">Cuánto dura activo si no lo actualizas antes. Entre 0.5 y 48 h.</div>

 <details><summary>Circuito (tramos que SÍ operan) — opcional</summary>
  <div class="hint">Un tramo por fila: estación de un extremo y del otro.</div>
  <div id="tramos"><div class="multi-row">
    <select class="tramo-a"></select><select class="tramo-b"></select>
    <input class="otra tramo-a-otra" placeholder="Desde (nombre)">
    <input class="otra tramo-b-otra" placeholder="Hasta (nombre)">
  </div></div>
  <button type="button" class="link-btn" id="btnTramo">+ agregar tramo</button>
  <textarea name="circuito" id="circuitoRaw" style="display:none"></textarea>
 </details>

 <button type="submit">Difundir aviso</button>
</form>
<div id="msg"></div>
<script>
 var ESTACIONES_POR_LINEA = __ESTACIONES__;
 var selLinea = document.querySelector('select[name=linea]');

 function poblarSelect(sel, valorPrevio){
  var lista = ESTACIONES_POR_LINEA[selLinea.value] || [];
  sel.innerHTML = '<option value="">-- Estación --</option>' +
   lista.map(function(n){return '<option value="'+n.replace(/&/g,'&amp;').replace(/"/g,'&quot;')+'">'+n+'</option>'}).join('') +
   '<option value="__otra__">Otra (escribir)…</option>';
  if (valorPrevio === '__otra__') sel.value = '__otra__';
  else if (valorPrevio && lista.indexOf(valorPrevio) >= 0) sel.value = valorPrevio;
 }

 function ligarSelectOtra(sel, otra){
  poblarSelect(sel);
  sel.addEventListener('change', function(){
   var esOtra = sel.value === '__otra__';
   otra.style.display = esOtra ? 'block' : 'none';
   if (esOtra) otra.focus();
  });
 }

 function repoblarTodos(){
  [].slice.call(document.querySelectorAll('#lugares .lugar-item')).forEach(function(sel){
   poblarSelect(sel, sel.value);
  });
  [].slice.call(document.querySelectorAll('#tramos .tramo-a, #tramos .tramo-b')).forEach(function(sel){
   poblarSelect(sel, sel.value);
  });
 }
 selLinea.addEventListener('change', repoblarTodos);

 [].slice.call(document.querySelectorAll('#lugares .lugar-item')).forEach(function(sel){
  ligarSelectOtra(sel, sel.parentNode.querySelector('.lugar-otra'));
 });
 (function(){
  var row = document.querySelector('#tramos .multi-row');
  ligarSelectOtra(row.querySelector('.tramo-a'), row.querySelector('.tramo-a-otra'));
  ligarSelectOtra(row.querySelector('.tramo-b'), row.querySelector('.tramo-b-otra'));
 })();

 function valorFila(sel){
  var otra = sel.parentNode.querySelector(sel.classList.contains('tramo-a') ? '.tramo-a-otra'
    : sel.classList.contains('tramo-b') ? '.tramo-b-otra' : '.lugar-otra');
  if (sel.value === '__otra__') return otra ? otra.value.trim() : '';
  return sel.value;
 }

 var tk=document.getElementById('token');
 tk.value=localStorage.getItem('geomb_admin_token')||'';
 tk.addEventListener('change',function(){localStorage.setItem('geomb_admin_token',tk.value)});

 document.querySelectorAll('#plantillas .chip').forEach(function(b){
  b.addEventListener('click',function(){
   document.querySelector('select[name=estado]').value=b.dataset.estado;
   document.querySelector('textarea[name=info]').value=b.dataset.info;
  });
 });

 document.getElementById('btnLugar').addEventListener('click',function(){
  var d=document.getElementById('lugares');
  var row=document.createElement('div'); row.className='multi-row';
  var sel=document.createElement('select'); sel.className='lugar-item';
  var otra=document.createElement('input'); otra.className='otra lugar-otra'; otra.placeholder='Nombre de la estación';
  var x=document.createElement('button'); x.type='button'; x.className='x'; x.textContent='×';
  x.addEventListener('click',function(){row.remove()});
  row.appendChild(sel); row.appendChild(otra); row.appendChild(x); d.appendChild(row);
  ligarSelectOtra(sel, otra);
 });
 document.getElementById('btnTramo').addEventListener('click',function(){
  var d=document.getElementById('tramos');
  var row=document.createElement('div'); row.className='multi-row';
  var a=document.createElement('select'); a.className='tramo-a';
  var b=document.createElement('select'); b.className='tramo-b';
  var oa=document.createElement('input'); oa.className='otra tramo-a-otra'; oa.placeholder='Desde (nombre)';
  var ob=document.createElement('input'); ob.className='otra tramo-b-otra'; ob.placeholder='Hasta (nombre)';
  var x=document.createElement('button'); x.type='button'; x.className='x'; x.textContent='×';
  x.addEventListener('click',function(){row.remove()});
  row.appendChild(a); row.appendChild(oa); row.appendChild(b); row.appendChild(ob); row.appendChild(x); d.appendChild(row);
  ligarSelectOtra(a, oa); ligarSelectOtra(b, ob);
 });

 document.getElementById('f').addEventListener('submit',function(ev){
  ev.preventDefault();
  localStorage.setItem('geomb_admin_token',tk.value);

  var lugares=[].slice.call(document.querySelectorAll('#lugares .lugar-item'))
   .map(function(sel){return valorFila(sel).trim()}).filter(Boolean);
  var tramos=[].slice.call(document.querySelectorAll('#tramos .multi-row'))
   .map(function(row){
    var a=valorFila(row.querySelector('.tramo-a')).trim(), b=valorFila(row.querySelector('.tramo-b')).trim();
    return (a&&b) ? (a+' - '+b) : null;
   }).filter(Boolean);
  document.getElementById('circuitoRaw').value=tramos.join('\\n');

  var m=document.getElementById('msg');m.style.display='none';
  var fd=new FormData(ev.target);
  fd.set('lugar', lugares.join(' y '));
  fetch('/admin/afectacion',{method:'POST',body:fd})
   .then(function(r){return r.json().then(function(j){return {s:r.status,j:j}})})
   .then(function(x){
     m.style.display='block';
     if(x.j.ok){m.className='ok';m.textContent='✅ Enviado: '+x.j.estado+' (línea '+x.j.linea+')'+(x.j.panel?' · panel actualizado':' · panel no escrito')+' · dura '+x.j.duracion_h+' h';}
     else{m.className='err';m.textContent='⚠ '+(x.j.error||('error '+x.s));}
   })
   .catch(function(e){m.style.display='block';m.className='err';m.textContent='⚠ '+e;});
 });
</script>
</body></html>""".replace("__LINEAS__", _OP_LINEAS).replace("__ESTADOS__", _OP_ESTADOS) \
    .replace("__ESTACIONES__", _JS_ESTACIONES)
