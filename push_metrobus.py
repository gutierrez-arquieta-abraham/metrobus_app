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

URL_ESTADO = ("https://incidentesmovilidad.cdmx.gob.mx/public/"
              "bandejaEstadoServicio.xhtml?idMedioTransporte=mb")
URL_SERVICIOMB = "https://www.metrobus.cdmx.gob.mx/ServicioMB"
INTERVALO_SEG = 60

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


def _get(url):
    # Los sitios del gobierno CDMX bloquean IPs fuera de México (Railway sale por EE.UU.).
    # MB_PROXY = uno o VARIOS proxies con salida en México, separados por coma. Se prueban en
    # orden y se usa el primero que responda (así un proxy gratuito muerto no rompe el monitor).
    # SOLO estas peticiones pasan por proxy; Firebase y el feed de unidades siguen directos.
    # Ej.: MB_PROXY=http://host1:puerto,http://user:pass@host2:puerto
    lista = [p.strip() for p in os.environ.get("MB_PROXY", "").split(",") if p.strip()]
    if not lista:
        return requests.get(url, timeout=20, headers=HEADERS).text
    ultimo = None
    for p in lista:
        try:
            return requests.get(url, timeout=20, headers=HEADERS,
                                proxies={"http": p, "https": p}).text
        except Exception as e:
            ultimo = e
    raise ultimo


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
    for f in leer_estado():
        ln = f["linea"]
        if ln <= 0 or _es_normal(f["estado"], f["estaciones"]):
            continue
        clave = f"{ln}|{_norm(f['estado'])}|{_norm(f['estaciones'])}"
        est_actual[ln] = clave
        if _prev["estado"].get(ln) != clave:
            _push(TEMA_AFECTA, "afectacion", ln, f["estado"], f["estaciones"], f["info"])
    for ln in list(_prev["estado"].keys()):
        if ln not in est_actual:
            _push(TEMA_AFECTA, "afectacion", ln, "Servicio restablecido", "", "")

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
            time.sleep(INTERVALO_SEG)
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    iniciar_monitor()
    while True:
        time.sleep(3600)
