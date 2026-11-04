# -*- coding: utf-8 -*-
# ============================================================
# MÓDULO   : admin_trazos.py   (Blueprint de Flask)
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Editor de trazos sobre un mapa para desvíos/servicios de apoyo TEMPORALES
# (p. ej. Mexibús cubriendo una estación de Metro cerrada por afectación):
# formulario con mapa en /admin/trazos donde se colocan estaciones nuevas y
# se traza la ruta pegada a la calle a mano (igual que un editor GTFS, pero
# sin dependencia externa). Al guardar escribe un parche en rutas_temporales.json
# con caducidad ('expira'); /data/rutas_temporales.json (en app.py) lo sirve
# y GeoMB lo fusiona en tiempo de ejecución sin recompilar.
#
# Protegido con el MISMO certificado cliente (mTLS) que /admin/afectacion;
# reusa admin_afect._token_ok() en vez de duplicar el criterio.
# ============================================================
"""
admin_trazos.py — Editor de trazos temporales sobre un mapa (GeoMB / EC2)
---------------------------------------------------------------------------
Blueprint Flask que se registra en app.py. Sirve un editor mobile-first en
  GET  /admin/trazos            -> la página (mapa Leaflet/OSM, sin API key).
  POST /admin/trazos/guardar    -> valida token, guarda o elimina un parche
                                    en rutas_temporales.json ('accion': guardar|eliminar).

Un parche reemplaza POR COMPLETO, mientras esté vigente, la lista de estaciones
y el trazado de la línea/ramal 'numero' que indica (puede ser un número ya
existente -como 112- o uno nuevo para un servicio que no tiene línea base).
GeoMB lo toma tal cual: no hace falta que el trazado nuevo "empalme" con nada
existente en el JSON, eso lo decide quien dibuja en el mapa.

No pretende sustituir un editor GTFS de verdad (no hay snap a vialidad: quien
dibuja traza punto por punto guiándose por las calles del mapa de fondo), pero
cubre el caso de un desvío de unos días sin tocar el repo de la app ni recompilar.
"""

import json
import os
import time
from datetime import datetime

from flask import Blueprint, Response, request

from admin_afect import LINEAS, _token_ok   # mismo catálogo y mismo criterio mTLS que /admin/afectacion

admin_trazos_bp = Blueprint("admin_trazos", __name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
# Mismo nombre de env var en app.py (que sirve /data/rutas_temporales.json) y aquí (que escribe):
# por convención, no por import -- así ya trabaja el resto del panel (ver MANUAL_AFECT/MANUAL_FILE).
RUTAS_TEMP_FILE = os.environ.get("RUTAS_TEMP_FILE", "").split("#", 1)[0].strip() \
    or os.path.join(DATA_DIR, "rutas_temporales.json")
# Horas que dura un parche si no se especifica (tope razonable: 10 días, puede durar más que
# un aviso de afectación porque un desvío de servicio suele cubrir varios días, no solo hoy).
DUR_H = float(os.environ.get("RUTAS_TEMP_DUR_H", "24").split("#", 1)[0].strip() or "24")
DUR_MIN_H, DUR_MAX_H = 0.5, 240.0

# Mapa de referencia (de fondo, solo lectura) para ubicar las estaciones existentes al trazar
# un desvío: copia que ya mantiene este repo para el mapa web (data/mexibus_mapa.json).
try:
    with open(os.path.join(DATA_DIR, "mexibus_mapa.json"), encoding="utf-8") as _f:
        _MAPA_REF = json.load(_f)
except Exception:
    _MAPA_REF = {"lineas": []}


def _cargar_patches():
    """Todos los parches guardados, vencidos incluidos (para listarlos/editarlos tal cual)."""
    try:
        with open(RUTAS_TEMP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def cargar_patches_vigentes():
    """Parches NO vencidos -- lo que usa /data/rutas_temporales.json (importado desde app.py,
       igual que app.py reusa admin_afect._token_ok())."""
    ahora = time.time()
    return [p for p in _cargar_patches() if float(p.get("expira", 0)) > ahora]


def _guardar(lista):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RUTAS_TEMP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lista, f, ensure_ascii=False)
    os.replace(tmp, RUTAS_TEMP_FILE)


def _upsert(numero, nombre, nota, estaciones, ruta, duracion_h):
    """Reemplaza el parche de 'numero' (si había uno) y agrega el nuevo, con 'expira'."""
    ahora = time.time()
    horas = duracion_h if duracion_h and duracion_h > 0 else DUR_H
    horas = max(DUR_MIN_H, min(DUR_MAX_H, horas))
    lista = [p for p in _cargar_patches()
             if float(p.get("expira", 0)) > ahora and int(p.get("numero", 0)) != int(numero)]
    lista.append({
        "numero": int(numero), "nombre": nombre, "nota": nota,
        "estaciones": estaciones, "ruta": ruta,
        "creado": int(ahora), "expira": int(ahora + horas * 3600),
    })
    _guardar(lista)
    return horas


def _eliminar(numero):
    ahora = time.time()
    lista = [p for p in _cargar_patches()
             if float(p.get("expira", 0)) > ahora and int(p.get("numero", 0)) != int(numero)]
    _guardar(lista)


@admin_trazos_bp.route("/admin/trazos", methods=["GET"])
def form():
    return Response(_html(), mimetype="text/html")


@admin_trazos_bp.route("/admin/trazos/guardar", methods=["POST"])
def guardar():
    if not _token_ok():
        return {"ok": False, "error": "token inválido"}, 403
    body = request.get_json(silent=True) or {}
    try:
        numero = int(body.get("numero", 0))
    except (TypeError, ValueError):
        numero = 0
    if numero <= 0:
        return {"ok": False, "error": "falta número de línea/ramal"}, 400

    if body.get("accion") == "eliminar":
        _eliminar(numero)
        return {"ok": True, "numero": numero, "eliminado": True}

    nombre = (body.get("nombre") or "").strip()
    nota = (body.get("nota") or "").strip()
    estaciones = body.get("estaciones") or []
    ruta = body.get("ruta") or []
    try:
        duracion_h = float(body.get("duracion_h") or 0)
    except (TypeError, ValueError):
        duracion_h = 0

    if not nombre:
        return {"ok": False, "error": "falta el nombre del servicio"}, 400
    if len(estaciones) < 2:
        return {"ok": False, "error": "hacen falta al menos 2 estaciones"}, 400
    if len(ruta) < 2:
        return {"ok": False, "error": "hace falta trazar la ruta (mínimo 2 puntos)"}, 400

    estaciones_limpias = []
    for e in estaciones:
        try:
            estaciones_limpias.append({
                "nombre": str(e["nombre"]).strip(),
                "lat": float(e["lat"]), "lon": float(e["lon"]),
            })
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "estación con datos inválidos"}, 400
    ruta_limpia = []
    for p in ruta:
        try:
            ruta_limpia.append([float(p[0]), float(p[1])])
        except (IndexError, TypeError, ValueError):
            return {"ok": False, "error": "punto de ruta inválido"}, 400

    horas = _upsert(numero, nombre, nota, estaciones_limpias, ruta_limpia, duracion_h)
    return {"ok": True, "numero": numero, "duracion_h": horas,
            "expira_legible": datetime.fromtimestamp(time.time() + horas * 3600)
            .strftime("%d/%m %H:%M")}


# ------------------------------------------------------------------ HTML (una sola página)
_OP_LINEAS = "".join(f'<option value="{v}">{t}</option>' for v, t in LINEAS)
_JS_REF = json.dumps(_MAPA_REF, ensure_ascii=False)


def _html():
    # Los parches activos se leen AL VUELO en cada GET (cambian por POST, no son estáticos
    # como el catálogo de líneas), por eso van en una función y no en el bloque de módulo.
    activos_js = json.dumps(cargar_patches_vigentes(), ensure_ascii=False)
    return _HTML_TPL.replace("__LINEAS__", _OP_LINEAS) \
        .replace("__REF__", _JS_REF).replace("__ACTIVOS__", activos_js)


_HTML_TPL = """<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GeoMB · Editor de trazos</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;font:16px/1.4 system-ui,Segoe UI,Roboto,sans-serif;background:#111;color:#eee}
 h1{font-size:17px;margin:0;padding:10px 14px;background:#1a1a1a;border-bottom:1px solid #2a2a2a}
 #mapa{height:46vh;min-height:260px}
 #panel{padding:12px 14px}
 label{display:block;margin:.7em 0 .2em;font-size:13px;color:#bbb}
 input,select,textarea{width:100%;padding:11px;border-radius:10px;border:1px solid #333;background:#1c1c1c;color:#eee;font-size:15px}
 .row{display:flex;gap:10px}.row>*{flex:1}
 .modos{display:flex;gap:8px;margin-top:10px}
 .modos button{flex:1;padding:12px 6px;border-radius:10px;border:1px solid #333;background:#1c1c1c;color:#ddd;font-size:14px}
 .modos button.activo{background:#0d3d5c;border-color:#1f6ea0;color:#bfe3ff}
 .acciones{display:flex;gap:8px;margin-top:8px}
 .acciones button{flex:1;padding:10px 6px;border-radius:10px;border:1px solid #333;background:#1c1c1c;color:#ccc;font-size:13px}
 #lista{margin-top:6px;font-size:13px;color:#bbb;max-height:110px;overflow-y:auto}
 #lista div{padding:4px 0;border-bottom:1px solid #222}
 button[type=submit]{margin-top:16px;width:100%;padding:15px;border:0;border-radius:12px;background:#D40D0D;color:#fff;font-size:16px;font-weight:600}
 #msg{margin-top:12px;padding:12px;border-radius:10px;display:none;font-size:14px}
 .ok{background:#123d1a;color:#9be7a6}.err{background:#3d1212;color:#e79b9b}
 .hint{font-size:12px;color:#888;margin-top:2px}
 .activo-row{display:flex;justify-content:space-between;align-items:center;gap:8px;
             padding:8px 0;border-bottom:1px solid #222;font-size:13px}
 .activo-row button{flex:0 0 auto;padding:6px 10px;border-radius:8px;border:1px solid #444;
                     background:#2a1414;color:#e79b9b;font-size:12px}
</style></head><body>
<h1>Editor de trazos temporales</h1>
<div id="mapa"></div>
<div id="panel">
 <label>Línea/ramal que cubre este servicio</label>
 <select id="numero">__LINEAS__
  <option value="">Otro número (servicio sin línea base)…</option>
 </select>
 <input id="numeroOtro" placeholder="Número (p. ej. 11290)" style="display:none;margin-top:6px">

 <label>Nombre del servicio</label>
 <input id="nombre" placeholder="p. ej. Mexibús L2A — desvío a Metro Nezahualcóyotl">

 <label>Nota (opcional, se muestra en el panel de la app)</label>
 <textarea id="nota" placeholder="Servicio de apoyo por afectación en Línea B del Metro"></textarea>

 <label>Duración</label>
 <input id="duracion" type="number" min="0.5" max="240" step="0.5" placeholder="horas (por defecto 24)">
 <div class="hint">Entre 0.5 y 240 h (10 días). Puedes terminarlo antes desde la lista de abajo.</div>

 <div class="modos">
  <button type="button" class="activo" data-modo="estacion">📍 Agregar estación</button>
  <button type="button" data-modo="trazo">✏️ Solo punto de trazo</button>
 </div>
 <div class="hint">Click en el mapa para agregar en el orden del recorrido. Guíate por las calles
  del fondo (OSM) — no hay snap automático a vialidad.</div>
 <div class="acciones">
  <button type="button" id="btnDeshacer">↩️ Deshacer último punto</button>
  <button type="button" id="btnLimpiar">🗑️ Limpiar todo</button>
 </div>
 <div id="lista"></div>

 <button type="button" id="btnGuardar">Guardar desvío</button>
 <div id="msg"></div>

 <label style="margin-top:22px">Desvíos activos</label>
 <div id="activos"></div>
</div>
<script>
 var REF = __REF__;
 var ACTIVOS = __ACTIVOS__;

 var mapa = L.map('mapa').setView([19.49, -99.05], 11);
 L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
   attribution: '&copy; OpenStreetMap', maxZoom: 19
 }).addTo(mapa);

 // Fondo de referencia: red existente (solo lectura), para ubicar dónde empalma el desvío.
 (REF.lineas || []).forEach(function(l){
  var pts = (l.estaciones || []).map(function(e){ return [e.lat, e.lon]; });
  if (pts.length > 1) L.polyline(pts, {color:'#555', weight:2, opacity:.6}).addTo(mapa);
  (l.estaciones || []).forEach(function(e){
   L.circleMarker([e.lat, e.lon], {radius:3, color:'#777', fillOpacity:.8})
    .bindTooltip(e.nombre, {direction:'top'}).addTo(mapa);
  });
 });

 var modo = 'estacion';
 var puntos = [];        // [[lat,lon], ...] en orden -- ES el trazo.
 var estaciones = [];    // [{nombre,lat,lon}, ...] subconjunto de 'puntos' con nombre.
 var trazoLayer = L.polyline([], {color:'#D40D0D', weight:4}).addTo(mapa);
 var marcadores = [];

 document.querySelectorAll('.modos button').forEach(function(b){
  b.addEventListener('click', function(){
   document.querySelectorAll('.modos button').forEach(function(x){x.classList.remove('activo')});
   b.classList.add('activo'); modo = b.dataset.modo;
  });
 });

 function repintar(){
  trazoLayer.setLatLngs(puntos);
  marcadores.forEach(function(m){ mapa.removeLayer(m); });
  marcadores = estaciones.map(function(e){
   return L.marker([e.lat, e.lon]).bindTooltip(e.nombre, {permanent:true, direction:'right'}).addTo(mapa);
  });
  document.getElementById('lista').innerHTML = estaciones.map(function(e, i){
   return '<div>' + (i+1) + '. ' + e.nombre + '</div>';
  }).join('') || '<div style="color:#666">(sin estaciones todavía)</div>';
 }

 mapa.on('click', function(ev){
  var p = [ev.latlng.lat, ev.latlng.lng];
  puntos.push(p);
  if (modo === 'estacion') {
   var nombre = prompt('Nombre de la estación:');
   if (!nombre) { puntos.pop(); return; }
   estaciones.push({nombre: nombre.trim(), lat: p[0], lon: p[1]});
  }
  repintar();
 });

 document.getElementById('btnDeshacer').addEventListener('click', function(){
  if (!puntos.length) return;
  var ultimo = puntos.pop();
  if (estaciones.length && estaciones[estaciones.length-1].lat === ultimo[0]
      && estaciones[estaciones.length-1].lon === ultimo[1]) estaciones.pop();
  repintar();
 });
 document.getElementById('btnLimpiar').addEventListener('click', function(){
  puntos = []; estaciones = []; repintar();
 });

 var selNumero = document.getElementById('numero');
 var numeroOtro = document.getElementById('numeroOtro');
 selNumero.addEventListener('change', function(){
  numeroOtro.style.display = selNumero.value === '' ? 'block' : 'none';
 });

 function pintarActivos(){
  var d = document.getElementById('activos');
  if (!ACTIVOS.length) { d.innerHTML = '<div class="hint">(ninguno)</div>'; return; }
  d.innerHTML = ACTIVOS.map(function(p){
   var exp = new Date(p.expira * 1000).toLocaleString('es-MX', {dateStyle:'short', timeStyle:'short'});
   return '<div class="activo-row"><span>' + p.numero + ' · ' + p.nombre +
    '<br><span class="hint">hasta ' + exp + '</span></span>' +
    '<button data-numero="' + p.numero + '">Terminar</button></div>';
  }).join('');
  d.querySelectorAll('button').forEach(function(b){
   b.addEventListener('click', function(){
    if (!confirm('¿Terminar este desvío ahora?')) return;
    fetch('/admin/trazos/guardar', {method:'POST', headers:{'Content-Type':'application/json',
      'X-Admin-Token': localStorage.getItem('geomb_admin_token') || ''},
      body: JSON.stringify({numero: b.dataset.numero, accion: 'eliminar'})})
     .then(function(){ location.reload(); });
   });
  });
 }
 pintarActivos();

 document.getElementById('btnGuardar').addEventListener('click', function(){
  var numero = selNumero.value || numeroOtro.value.trim();
  var m = document.getElementById('msg'); m.style.display = 'block';
  if (!numero) { m.className = 'err'; m.textContent = '⚠ falta el número de línea/ramal'; return; }
  if (estaciones.length < 2) { m.className = 'err'; m.textContent = '⚠ hacen falta al menos 2 estaciones'; return; }
  var body = {
   numero: numero,
   nombre: document.getElementById('nombre').value.trim(),
   nota: document.getElementById('nota').value.trim(),
   duracion_h: parseFloat(document.getElementById('duracion').value) || null,
   estaciones: estaciones, ruta: puntos,
  };
  fetch('/admin/trazos/guardar', {method:'POST', headers:{'Content-Type':'application/json',
    'X-Admin-Token': localStorage.getItem('geomb_admin_token') || ''}, body: JSON.stringify(body)})
   .then(function(r){ return r.json().then(function(j){ return {s:r.status, j:j}; }); })
   .then(function(x){
    if (x.j.ok) { m.className = 'ok'; m.textContent = '✅ Guardado, vigente hasta ' + x.j.expira_legible; }
    else { m.className = 'err'; m.textContent = '⚠ ' + (x.j.error || ('error ' + x.s)); }
   })
   .catch(function(e){ m.className = 'err'; m.textContent = '⚠ ' + e; });
 });
</script>
</body></html>"""
