# ============================================================
# MÓDULO   : push_metrobus.py   (servicio metrobus-push)
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Vigila el estado del servicio Metrobús y MANDA notificaciones push (FCM):
#   - Estado del servicio (afectaciones) → topic "afectaciones";
#   - Elevadores fuera de servicio → topic "elevadores";
#   - Mantenimiento de estaciones (vigente hoy) → topic "afectaciones".
#
# Además ESCRIBE el estado raspado a afect_metrobus.json para que el panel de
# la app persista (no dependa de cachar el push). Lee las páginas del gobierno
# con requests + BeautifulSoup (sin navegador). Corre como servicio aparte.
# ============================================================
"""
push_metrobus.py — Notificaciones push (FCM) desde el backend (Railway/Flask) para GeoMB.

Sondea (una sola vez para todos los usuarios) y difunde por push:
  - Estado del Servicio  -> tema "afectaciones"  (obstrucción, sin servicio, retraso, etc.)
  - Estaciones en mantenimiento (solo las vigentes hoy) -> tema "afectaciones"
  - Elevadores fuera de servicio -> tema "elevadores"  (la app solo se suscribe si el
    usuario marcó movilidad reducida)
  - enviar_actualizacion(titulo, texto) -> tema "actualizaciones"

Fuentes (ambas se obtienen con requests + BeautifulSoup, sin navegador):
  - Estado: iframe server-rendered  .../bandejaEstadoServicio.xhtml?idMedioTransporte=mb
  - Elevadores y mantenimiento: HTML de https://www.metrobus.cdmx.gob.mx/ServicioMB
    (las 3 tablas vienen en el HTML del servidor; hay que mandar headers de navegador)

Requisitos:  pip install firebase-admin requests beautifulsoup4
Credenciales (variable de entorno de Railway, NO hardcodear):
    FIREBASE_CREDENTIALS_JSON  = contenido JSON del service account de Firebase
    (o) GOOGLE_APPLICATION_CREDENTIALS = ruta a un archivo de credenciales

Uso en tu app Flask:
    from push_metrobus import iniciar_monitor, enviar_actualizacion
    iniciar_monitor()
"""

import json
import os
import re
import threading
import time
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore, messaging

import detector_proximidad
import device_alertas

URL_ESTADO = ("https://incidentesmovilidad.cdmx.gob.mx/public/"
              "bandejaEstadoServicio.xhtml?idMedioTransporte=mb")
URL_SERVICIOMB = "https://www.metrobus.cdmx.gob.mx/ServicioMB"
INTERVALO_SEG = 60

# Estado de Metrobús para el PANEL persistente de la app: app.py lo mezcla en
# /data/afectaciones_mexibus.json (junto con Mexibús y los avisos manuales). Configurable.
AFECT_MTB_OUT = os.environ.get("AFECT_MTB_OUT", "").strip() \
    or "/home/ubuntu/metrobus_app/data/afect_metrobus.json"

TEMA_AFECTA = "afectaciones"
TEMA_ELEVA = "elevadores"
TEMA_ACTUALIZA = "actualizaciones"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "es-ES,es;q=0.9",
}

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
         "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

_prev = {"estado": {}, "eleva": set(), "manten": set()}
_lock = threading.Lock()


# ---------------------------------------------------------------- Firebase
def _init():
    if firebase_admin._apps:
        return
    j = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    cred = credentials.Certificate(json.loads(j)) if j else credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)


def _push(tema, tipo, linea, estado, lugar, info):
    _init()
    messaging.send(messaging.Message(
        topic=tema,
        data={"tipo": tipo, "linea": str(linea or ""), "estado": estado or "",
              "lugar": lugar or "", "info": info or ""},
        android=messaging.AndroidConfig(priority="high"),
    ))


def enviar_alerta_unidad_cerca(evento):
    """Envía el evento "unidad_cerca" de detector_proximidad.evaluar_alertas() por FCM -- a UN
    SOLO dispositivo (el token registrado para evento["device_id"], ver device_tokens), NUNCA a
    un topic ni broadcast: a diferencia de _push() de arriba (afectaciones, para TODOS los
    suscritos al tema), esto es inherentemente personal a quien configuró esa alerta.

    Reusa la MISMA inicialización de Firebase Admin que ya usa el resto de este archivo (_init())
    -- no crea una app ni credenciales aparte.

    El payload (data message, todo como string: así lo exige FCM/Android) NUNCA lleva ubicación
    del usuario ni del dispositivo -- solo lo que la notificación necesita para mostrarse:
    económico, línea, distancia aproximada y el radio configurado.

    Si FCM indica que el token ya no sirve (UnregisteredError: desinstalado, token rotado sin
    que este dispositivo lo haya vuelto a registrar, etc.), se elimina de device_tokens para no
    reintentarlo cada ciclo -- NUNCA se toca device_alertas: la preferencia del usuario se
    conserva, solo deja de poder recibir el push hasta que la app registre un token nuevo (ver
    MensajesService.onNewToken en GeoMB).

    Cualquier otro fallo (red, cuota, credencial temporalmente inválida, etc.) se trata como
    transitorio: solo se registra. NUNCA se toca dentro_del_radio ni se genera otro evento --
    detector_proximidad ya decidió que la unidad entró al radio; un fallo de ENTREGA no debe
    rearmar nada (ver su docstring: es la única autoridad sobre la histéresis).

    Devuelve True si FCM aceptó el envío, False en cualquier otro caso (sin token registrado,
    token eliminado, fallo transitorio) -- nunca lanza, para que el llamador pueda seguir con el
    siguiente evento sin que un fallo aquí detenga a los demás (ver
    _procesar_eventos_proximidad)."""
    device_id = evento.get("device_id")
    token = device_alertas.token_de(device_id)
    if not token:
        print(f"push: unidad_cerca sin token registrado para device_id={device_id}, no se envia")
        return False

    _init()
    linea = evento.get("linea")
    data = {
        "tipo": "unidad_cerca",
        "economico": str(evento.get("economico") or ""),
        "linea": str(linea) if linea not in (None, "") else "",
        "distancia_m": str(evento.get("distancia_m") if evento.get("distancia_m") is not None else ""),
        "radio_m": str(evento.get("radio_m") if evento.get("radio_m") is not None else ""),
    }
    try:
        # token= (no fid=): firebase-admin>=7 marca token= como deprecado en favor de fid=, pero
        # requirements.txt solo fija >=6.5 -- no hay forma de saber desde aquí qué versión corre
        # de verdad el EC2, y token= sigue totalmente soportado en todas ellas. Usar fid=
        # arriesgaría romper el envío si el servidor todavía no tiene la versión que lo introdujo.
        # Revisar si vale la pena migrar una vez que se confirme la versión real en producción.
        messaging.send(messaging.Message(
            token=token,
            data=data,
            android=messaging.AndroidConfig(priority="high"),
        ))
        return True
    except messaging.UnregisteredError:
        device_alertas.eliminar_token(device_id)
        print(f"push: token invalido/expirado para device_id={device_id}, eliminado")
        return False
    except Exception as e:
        print(f"push: fallo transitorio enviando unidad_cerca a device_id={device_id}: {e}")
        return False


def _procesar_eventos_proximidad(eventos):
    """Manda cada evento "unidad_cerca" por su cuenta -- un fallo en UNO (red, token inválido,
    lo que sea: ver enviar_alerta_unidad_cerca) nunca debe impedir que se intenten los demás."""
    for evento in eventos:
        try:
            enviar_alerta_unidad_cerca(evento)
        except Exception as e:
            print("push: error enviando unidad_cerca:", e)


def _escribir_estado_metrobus(filas):
    """Snapshot del estado por línea (solo las afectadas) para el panel persistente. app.py
       lo mezcla al servir /data/afectaciones_mexibus.json. Escritura atómica, best-effort."""
    try:
        data = {"actualizado": int(time.time()), "afectaciones": filas}
        os.makedirs(os.path.dirname(AFECT_MTB_OUT), exist_ok=True)
        tmp = AFECT_MTB_OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, AFECT_MTB_OUT)
    except Exception as e:
        print("push: no se pudo escribir estado metrobus:", e)


def enviar_actualizacion(titulo, texto, version_code=""):
    _init()
    messaging.send(messaging.Message(
        topic=TEMA_ACTUALIZA,
        data={"tipo": "actualizacion", "titulo": titulo or "", "texto": texto or "",
              "version_code": str(version_code or "")},
        android=messaging.AndroidConfig(priority="high"),
    ))


def revisar_actualizacion():
    """
    Difunde el aviso de 'actualización disponible' UNA sola vez cuando sube APP_VERSION_CODE.
    Persiste el último valor notificado en Firestore (config/app) para no repetirlo en cada
    redeploy. La app solo mostrará el aviso si su versión es menor a version_code.
    Flujo: publicas nueva versión -> subes APP_VERSION_CODE en Railway -> redeploy -> se envía.
    """
    vc = os.environ.get("APP_VERSION_CODE", "").strip()
    if not vc.isdigit():
        return
    vc = int(vc)
    _init()
    try:
        db = firestore.client()
        ref = db.collection("config").document("app")
        snap = ref.get()
        prev = int((snap.to_dict() or {}).get("version_notificada", 0)) if snap.exists else 0
    except Exception as e:
        print("push: firestore lectura:", e)
        return
    if vc <= prev:
        return
    nombre = os.environ.get("APP_VERSION_NAME", "").strip()
    titulo = "Actualización disponible" + (f" ({nombre})" if nombre else "")
    texto = os.environ.get("APP_UPDATE_TEXT", "").strip() \
        or "Hay una nueva versión de GeoMB. Toca para actualizar."
    enviar_actualizacion(titulo, texto, vc)
    try:
        ref.set({"version_notificada": vc}, merge=True)
    except Exception as e:
        print("push: firestore escritura:", e)


# ---------------------------------------------------------------- Utilidades
def _norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


def _txt(td):
    for sp in td.select("span.ui-column-title"):   # etiqueta responsiva de PrimeFaces
        sp.extract()
    return " ".join(td.get_text().split()).strip()


def _num(s):
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else 0


# --- Proxies con salida en México (los sitios del gobierno bloquean la IP de Railway/EE.UU.) ---
# Fuente auto-descargable de proxies MX en TEXTO PLANO (ip:puerto por línea). ProxyScrape da los
# mismos proxies gratis que ProxyNova pero legibles (ProxyNova ofusca las IPs con JS). Configurable.
PROXY_SRC = os.environ.get(
    "MB_PROXY_SRC",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=MX")

_proxy_ok = None          # último proxy que funcionó: se prueba primero
_proxy_cache = []         # lista auto-descargada
_proxy_cache_ts = 0.0
_RE_IPPORT = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}$")


def _descargar_proxies():
    """Baja una lista fresca de proxies MX (texto ip:puerto). Se pide directo, sin proxy."""
    global _proxy_cache, _proxy_cache_ts
    try:
        txt = requests.get(PROXY_SRC, timeout=15, headers=HEADERS).text
        _proxy_cache = ["http://" + ln.strip() for ln in txt.splitlines()
                        if _RE_IPPORT.match(ln.strip())]
        _proxy_cache_ts = time.time()
    except Exception as e:
        print("push: no se pudo bajar lista de proxies:", e)
    return _proxy_cache


def _candidatos():
    """Orden de prueba: MB_PROXY (manuales) -> el que funcionó antes -> lista auto (refresca 30 min)."""
    cand = [p.strip() for p in os.environ.get("MB_PROXY", "").split(",") if p.strip()]
    if _proxy_ok and _proxy_ok not in cand:
        cand.insert(0, _proxy_ok)
    if not _proxy_cache or time.time() - _proxy_cache_ts > 1800:
        _descargar_proxies()
    for p in _proxy_cache:
        if p not in cand:
            cand.append(p)
    return cand


def _get(url):
    """Lee la página del gobierno. Intenta DIRECTO primero (funciona desde México, p. ej. AWS
    mx-central-1: rápido y confiable). Solo si el directo falla (IP fuera de México, como Railway)
    cae a proxies MX (los de MB_PROXY y luego la lista auto-descargada). Firebase y el feed de
    unidades siempre van directos."""
    global _proxy_ok
    err = None
    try:
        return requests.get(url, timeout=20, headers=HEADERS).text
    except Exception as e:
        err = e
    for p in _candidatos()[:25]:             # tope: no gastar el ciclo probando cientos de muertos
        try:
            txt = requests.get(url, timeout=(6, 15), headers=HEADERS,
                               proxies={"http": p, "https": p}).text
            _proxy_ok = p                    # este sirvió: úsalo primero la próxima vez
            return txt
        except Exception:
            pass
    _proxy_ok = None
    raise err


def _vigente_hoy(periodo):
    """True si el 'Periodo de Cierre' (p. ej. '8 y 9 agosto') incluye hoy."""
    if not periodo:
        return True
    p = _norm(periodo)
    mes = next((i for i, m in enumerate(MESES) if m in p), -1)
    if mes < 0:
        return True
    hoy = date.today()
    if mes + 1 != hoy.month:
        return False
    dias = [int(x) for x in re.findall(r"\d{1,2}", p)]
    return not dias or (min(dias) <= hoy.day <= max(dias))


# ---------------------------------------------------------------- Estado
def leer_estado():
    filas = []
    try:
        soup = BeautifulSoup(_get(URL_ESTADO), "html.parser")
        for tr in soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            linea = _num(_txt(tds[0]))
            if not linea:
                img = tds[0].find("img")
                if img and img.get("src"):
                    m = re.search(r"MB(\d)", img["src"])
                    linea = int(m.group(1)) if m else 0
            filas.append({"linea": linea, "estado": _txt(tds[1]),
                          "estaciones": _txt(tds[2]), "info": _txt(tds[3])})
    except Exception as e:
        print("push: error estado:", e)
    return filas


def _es_normal(estado, estaciones):
    e, s = _norm(estado), _norm(estaciones)
    return (not e) or ("servicio regular" in e) or (e == "estado") \
        or ("estaciones afectadas" in s) or (s == "ninguna")


# ---------------------------------------------------------------- Elevadores / mantenimiento
def leer_tablas():
    """Del HTML de ServicioMB: elevadores (Línea N …) y mantenimiento (Periodo …)."""
    eleva, manten = [], []
    try:
        soup = BeautifulSoup(_get(URL_SERVICIOMB), "html.parser")
        for tb in soup.find_all("table"):
            es_mant = "periodo de cierre" in _norm(tb.get_text())
            for tr in tb.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue
                c0 = _txt(tds[0])
                if es_mant:
                    ln = _num(_txt(tds[1]))
                    if not ln:
                        continue
                    manten.append({"linea": ln, "estacion": _txt(tds[2]),
                                   "direccion": _txt(tds[3]),
                                   "motivo": _txt(tds[4]) if len(tds) > 4 else "",
                                   "periodo": c0})
                elif re.search(r"l[ií]nea\s*\d", c0, re.I):
                    eleva.append({"linea": _num(c0), "estacion": _txt(tds[1]),
                                  "direccion": _txt(tds[2]), "motivo": _txt(tds[3]),
                                  "fecha": _txt(tds[4]) if len(tds) > 4 else ""})
    except Exception as e:
        print("push: error tablas:", e)
    return eleva, manten


# ---------------------------------------------------------------- Ciclo
def _ciclo():
    global _prev

    # 1) Estado del Servicio -> tema afectaciones (nueva/cambiada/restablecida)
    est_actual = {}
    estado_filas = []   # snapshot del estado actual para el panel persistente (lo sirve app.py)
    for f in leer_estado():
        ln = f["linea"]
        if ln <= 0 or _es_normal(f["estado"], f["estaciones"]):
            continue
        clave = f"{ln}|{_norm(f['estado'])}|{_norm(f['estaciones'])}"
        est_actual[ln] = clave
        estado_filas.append({"linea": ln, "estado": f["estado"],
                             "lugar": f["estaciones"], "info": f["info"]})
        if _prev["estado"].get(ln) != clave:
            _push(TEMA_AFECTA, "afectacion", ln, f["estado"], f["estaciones"], f["info"])
    for ln in list(_prev["estado"].keys()):
        if ln not in est_actual:
            _push(TEMA_AFECTA, "afectacion", ln, "Servicio restablecido", "", "")
    _escribir_estado_metrobus(estado_filas)   # persiste el estado (afectadas) para el panel

    # 2) Elevadores y mantenimiento
    eleva, manten = leer_tablas()

    eleva_actual = set()
    for e in eleva:
        clave = f"{e['linea']}|{_norm(e['estacion'])}|{_norm(e['direccion'])}"
        eleva_actual.add(clave)
        if clave not in _prev["eleva"]:
            info = e["motivo"]
            if e["fecha"] and _norm(e["fecha"]) != "por definir":
                info = (info + " · " + e["fecha"]).strip(" ·")
            _push(TEMA_ELEVA, "afectacion", e["linea"], "Elevador sin servicio",
                  e["estacion"], info)

    manten_actual = set()
    for m in manten:
        if not _vigente_hoy(m["periodo"]):
            continue
        clave = f"{m['linea']}|{_norm(m['estacion'])}|{_norm(m['periodo'])}"
        manten_actual.add(clave)
        if clave not in _prev["manten"]:
            info = m["motivo"]
            if m["periodo"]:
                info = (info + " · " + m["periodo"]).strip(" ·")
            _push(TEMA_AFECTA, "afectacion", m["linea"], "Estación en mantenimiento",
                  m["estacion"], info)

    with _lock:
        _prev = {"estado": est_actual, "eleva": eleva_actual, "manten": manten_actual}


def iniciar_monitor():
    def loop():
        while True:
            try:
                revisar_actualizacion()
            except Exception as e:
                print("push: error version:", e)
            try:
                _ciclo()
            except Exception as e:
                print("push: error ciclo:", e)
            # Detector de proximidad de unidades guardadas (ver detector_proximidad.py): reusa
            # este mismo ciclo de ~60s en vez de otro daemon/temporizador aparte. Paso
            # independiente (su propio try/except) a propósito: un fallo aquí nunca debe afectar
            # el push de afectaciones de arriba, ni viceversa. Cada evento "unidad_cerca" se manda
            # por FCM a SU dispositivo (ver enviar_alerta_unidad_cerca/_procesar_eventos_proximidad);
            # un fallo mandando uno nunca debe impedir que se intenten los demás.
            try:
                eventos = detector_proximidad.evaluar_alertas()
                _procesar_eventos_proximidad(eventos)
            except Exception as e:
                print("push: error detector proximidad:", e)
            time.sleep(INTERVALO_SEG)
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    iniciar_monitor()
    while True:
        time.sleep(3600)
