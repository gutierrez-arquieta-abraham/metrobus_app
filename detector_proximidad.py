# ============================================================
# MÓDULO   : detector_proximidad.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Detector de proximidad de unidades guardadas con alerta activa: compara la última ubicación
# conocida de cada dispositivo (device_alertas.actualizar_ubicacion, ver AlertasUnidadesService
# en GeoMB) contra la posición EN VIVO de la unidad correspondiente en /data/vehicles.json, y
# aplica histéresis para decidir cuándo "entra" al radio configurado de cada alerta.
#
# ESTA ETAPA NO ENVÍA FCM: evaluar_alertas() solo actualiza dentro_del_radio y devuelve/loguea
# los eventos "unidad_cerca" nuevos -- el envío real (firebase_admin.messaging.send) es una
# etapa posterior que todavía no existe. No se llama a Firebase Admin desde este módulo.
# ============================================================
"""
Reutilizado desde push_metrobus.py (mismo ciclo de ~60s, ver iniciar_monitor): llamar a
evaluar_alertas() una vez por ciclo basta -- hace UNA sola descarga del feed y evalúa TODAS las
alertas activas de TODOS los dispositivos contra ella, nunca una petición por unidad ni por
dispositivo.
"""
from __future__ import annotations

import logging
import math
import os
import time

import requests

import device_alertas

logger = logging.getLogger("geomb.detector_proximidad")

# Mismo host que ya sirve /data/vehicles.json en este mismo EC2 (ver app.py, metrobus-web.service
# en 127.0.0.1:8000 detrás de nginx) -- loopback, no el dominio público: este módulo corre en el
# proceso de metrobus-push.service (otro proceso, mismo servidor), así que no hay memoria
# compartida con el _vehicles_json de app.py y hay que pedirlo por HTTP, igual que ya hace
# Android. Configurable por si el despliegue cambia.
VEHICLES_URL = os.environ.get("VEHICLES_URL", "http://127.0.0.1:8000/data/vehicles.json")
HTTP_TIMEOUT_S = 10

# Margen de histéresis (ver el flujo FUERA -> entra -> DENTRO -> ... -> sale -> FUERA): un valor
# ABSOLUTO, no proporcional al radio -- estable en los tres radios que ofrece GeoMB (250/500/
# 1000 m) sin ser desproporcionado en el más chico. Configurable.
MARGEN_HISTERESIS_M = int(os.environ.get("ALERTA_HISTERESIS_M", "100"))

# No existe (todavía) un criterio de "feed obsoleto" activo en metrobus_app ni en GeoMB para
# reutilizar tal cual: el único intento (RealtimeRepository.filtrarFantasmas en Android, relativo
# a la unidad más fresca del lote, ventana de 240s) está DESACTIVADO -- su propio comentario en
# RealtimeRepository.java documenta que descartó la mayoría de la flota real (~845 -> ~36)
# porque la distribución real de timestamps de ESTE feed no es la esperada (no hay un cúmulo
# fresco + pocos outliers viejos). Reusar ese mismo criterio relativo aquí repetiría el mismo
# problema -- arriesgaría nunca disparar una alerta real. En su lugar se usa un umbral ABSOLUTO
# (reloj de pared, no relativo al lote) deliberadamente generoso: solo descarta datos
# inequívocamente muertos (15 min sin reportar), nunca una unidad activa con un timestamp
# simplemente irregular. Recalibrar aquí si en el futuro se analiza una muestra real del feed
# (ver el comentario de RealtimeRepository.parsear()).
UMBRAL_OBSOLETO_S = int(os.environ.get("ALERTA_UMBRAL_OBSOLETO_S", "900"))


def _haversine_m(lat1, lon1, lat2, lon2):
    """Distancia entre dos puntos geográficos, en metros -- fórmula de Haversine, NUNCA
    distancia euclidiana sobre grados (un grado de longitud no mide lo mismo que uno de
    latitud, y menos aún a la latitud de la Ciudad de México)."""
    R = 6371000.0
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(df / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _obtener_feed():
    """Una sola descarga de /data/vehicles.json, indexada por económico. None si la descarga
    falla -- un fallo de red nunca debe generar falsos rearmes ni eventos; el ciclo siguiente
    (60s después) lo reintenta solo."""
    try:
        r = requests.get(VEHICLES_URL, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        vehiculos = r.json()
    except Exception as e:
        logger.warning("No se pudo obtener %s: %s", VEHICLES_URL, e)
        return None

    indice = {}
    for v in vehiculos:
        # Mismo criterio que RealtimeRepository.parsear() en Android: el económico real es
        # "label" (lo que trae pintado el camión); "id" es un identificador crudo del feed GTFS-RT
        # y solo se usa si "label" viene vacío. economicos_favoritos/device_alertas.economico se
        # guardan tal como los guarda Android, así que deben indexarse con el MISMO criterio.
        eco = str(v.get("label") or "").strip() or str(v.get("id") or "").strip()
        if not eco:
            continue
        indice[eco] = v
    return indice


def _vigente(vehiculo, ahora):
    """¿El timestamp de esta unidad es lo bastante reciente para confiar en su posición? Ver
    UMBRAL_OBSOLETO_S arriba para por qué es un umbral absoluto y no el criterio relativo
    desactivado en Android."""
    ts = vehiculo.get("timestamp") or 0
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return False
    if ts <= 0:
        return False
    return (ahora - ts) <= UMBRAL_OBSOLETO_S


def evaluar_alertas(feed=None):
    """Evalúa TODAS las alertas activas de TODOS los dispositivos contra UNA sola descarga del
    feed de vehículos.

    Para cada (device_id, economico) con alerta_activa=1:
      - si la unidad no aparece en el feed, su timestamp está obsoleto, o el dispositivo nunca
        mandó ubicación: NO se toca dentro_del_radio y no se genera evento -- se deja para el
        próximo ciclo (ver UMBRAL_OBSOLETO_S / "unidad desaparecida" en el diseño: una ausencia
        temporal del feed NUNCA se interpreta como "salió del radio");
      - si no, se calcula la distancia (Haversine) y se aplica histéresis:
          * dentro_del_radio False -> True (distancia <= radio_alerta_m): genera un evento
            "unidad_cerca" y actualiza el estado;
          * dentro_del_radio True -> False (distancia >= radio_alerta_m + MARGEN_HISTERESIS_M):
            solo rearma el estado, NUNCA genera evento;
          * cualquier otro caso (se mantiene dentro, se mantiene fuera, o está en la zona de
            histéresis sin cruzar ninguno de los dos umbrales): no se toca nada.

    Devuelve la lista de eventos NUEVOS de este ciclo -- cada uno con la forma exacta
    {"tipo": "unidad_cerca", "device_id", "economico", "linea", "distancia_m", "radio_m",
    "timestamp"}. NUNCA envía FCM ni importa firebase_admin: esta función solo detecta y deja el
    evento preparado (logueado + devuelto) para que una etapa posterior decida qué hacer con él.

    @param feed Para pruebas: un índice {economico: vehiculo} ya armado (salta la descarga HTTP).
                None (el valor por defecto en producción) hace la descarga real.
    """
    indice = feed if feed is not None else _obtener_feed()
    if indice is None:
        return []

    ahora = int(time.time())
    eventos = []

    for alerta in device_alertas.todas_las_alertas_activas():
        device_id = alerta["device_id"]
        economico = alerta["economico"]
        radio_m = alerta["radio_alerta_m"]
        dentro_antes = alerta["dentro_del_radio"]

        vehiculo = indice.get(economico)
        if vehiculo is None:
            continue   # no aparece este ciclo: se conserva el estado, ver docstring
        if not _vigente(vehiculo, ahora):
            continue   # dato obsoleto: igual, se conserva el estado

        ubicacion = device_alertas.ubicacion_de(device_id)
        if ubicacion is None:
            continue   # el dispositivo todavía no mandó ninguna ubicación

        try:
            lat_v = float(vehiculo.get("lat"))
            lon_v = float(vehiculo.get("lon"))
        except (TypeError, ValueError):
            continue

        distancia_m = _haversine_m(ubicacion["lat"], ubicacion["lon"], lat_v, lon_v)

        if not dentro_antes and distancia_m <= radio_m:
            device_alertas.actualizar_estado_radio(device_id, economico, True)
            evento = {
                "tipo": "unidad_cerca",
                "device_id": device_id,
                "economico": economico,
                "linea": vehiculo.get("line"),
                "distancia_m": round(distancia_m),
                "radio_m": radio_m,
                "timestamp": ahora,
            }
            eventos.append(evento)
            logger.info("unidad_cerca: %s", evento)
        elif dentro_antes and distancia_m >= radio_m + MARGEN_HISTERESIS_M:
            device_alertas.actualizar_estado_radio(device_id, economico, False)
        # cualquier otro caso: sin cambios (ver docstring).

    return eventos
