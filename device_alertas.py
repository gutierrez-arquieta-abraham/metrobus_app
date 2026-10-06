# ============================================================
# MÓDULO   : device_alertas.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Almacenamiento SQLite propio (mismo criterio que ratelimit.py del asistente Gemini: archivo
# junto al módulo, sin servicio/infra nueva) para las ALERTAS DE PROXIMIDAD de unidades
# guardadas. Identificado por X-Device-ID (NUNCA por uid de Firebase Auth -- un mismo usuario
# puede tener varios dispositivos, y esta función debe funcionar sin depender de sesión).
#
# Dos tablas, separadas a propósito:
#   - device_tokens: el token FCM es un dato POR DISPOSITIVO, no por unidad guardada -- una
#     fila por device_id, nunca repetido en cada economico (mismo principio que ya se exige
#     para la ubicación: "el dispositivo tiene una última ubicación/token, independientemente
#     de cuántas unidades tenga guardadas").
#   - device_alertas: preferencia + estado de histéresis por (device_id, economico). Esta
#     etapa solo implementa el registro/actualización/baja de filas -- el detector de
#     proximidad (que lee dentro_del_radio/ultima_notificacion_ts) llega en una etapa posterior
#     (NO se implementa aquí todavía).
#   - device_ubicacion: ÚLTIMA ubicación conocida del dispositivo, UNA fila por device_id (ver
#     actualizar_ubicacion) -- nunca un historial. Independiente de cuántas unidades tenga
#     guardadas: el dispositivo tiene una sola ubicación vigente a la vez, igual criterio que
#     device_tokens para el token FCM.
#
# No construye historial: cada upsert REEMPLAZA el valor anterior, nunca se insertan filas
# nuevas para el mismo (device_id, economico) ni se conserva un registro de cambios.
# ============================================================
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent / "device_alertas.db"
_lock = threading.Lock()

RADIOS_VALIDOS = (250, 500, 1000)
RADIO_DEFAULT = 500


def _conexion() -> sqlite3.Connection:
    con = sqlite3.connect(_DB_PATH, timeout=5)
    con.execute(
        "CREATE TABLE IF NOT EXISTS device_tokens ("
        "  device_id TEXT PRIMARY KEY,"
        "  fcm_token TEXT NOT NULL,"
        "  actualizado_ts INTEGER NOT NULL"
        ")"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS device_alertas ("
        "  device_id TEXT NOT NULL,"
        "  economico TEXT NOT NULL,"
        "  alerta_activa INTEGER NOT NULL DEFAULT 0,"
        "  radio_alerta_m INTEGER NOT NULL DEFAULT 500,"
        "  dentro_del_radio INTEGER NOT NULL DEFAULT 0,"
        "  ultima_notificacion_ts INTEGER,"
        "  PRIMARY KEY (device_id, economico)"
        ")"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS device_ubicacion ("
        "  device_id TEXT PRIMARY KEY,"
        "  lat REAL NOT NULL,"
        "  lon REAL NOT NULL,"
        "  timestamp INTEGER NOT NULL"
        ")"
    )
    return con


def _radio_valido(radio_m) -> int:
    try:
        r = int(radio_m)
    except (TypeError, ValueError):
        return RADIO_DEFAULT
    return r if r in RADIOS_VALIDOS else RADIO_DEFAULT


# ---------------------------------------------------------------- token FCM (por dispositivo)

def registrar_token(device_id: str, fcm_token: str) -> None:
    """Upsert del token FCM de este dispositivo. Reemplaza el anterior (un dispositivo tiene UN
    token vigente a la vez -- igual que ya documenta DeviceUtils.java del lado Android)."""
    if not device_id or not fcm_token:
        return
    with _lock, _conexion() as con:
        con.execute(
            "INSERT INTO device_tokens (device_id, fcm_token, actualizado_ts) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET fcm_token = excluded.fcm_token, "
            "actualizado_ts = excluded.actualizado_ts",
            (device_id, fcm_token, int(time.time())),
        )
        con.commit()


def token_de(device_id: str) -> str | None:
    with _lock, _conexion() as con:
        row = con.execute(
            "SELECT fcm_token FROM device_tokens WHERE device_id = ?", (device_id,)
        ).fetchone()
        return row[0] if row else None


def eliminar_token(device_id: str) -> None:
    """Se llama cuando FCM reporta que el token ya no es válido (UNREGISTERED/NOT_FOUND) -- ver
    la etapa de envío. No borra las filas de device_alertas: la preferencia del usuario se
    conserva, solo deja de poder recibir el push hasta que la app registre un token nuevo."""
    with _lock, _conexion() as con:
        con.execute("DELETE FROM device_tokens WHERE device_id = ?", (device_id,))
        con.commit()


# ---------------------------------------------------------------- alertas por unidad

def actualizar_alerta(device_id: str, economico: str, alerta_activa: bool, radio_alerta_m) -> None:
    """Upsert de la preferencia de UNA unidad guardada. Si la fila no existía, la crea con
    dentro_del_radio=0 (arranca "fuera": la primera evaluación del detector decide de verdad si
    ya está dentro, nunca se asume un estado de histéresis previo que no existió)."""
    if not device_id or not economico:
        return
    radio = _radio_valido(radio_alerta_m)
    with _lock, _conexion() as con:
        con.execute(
            "INSERT INTO device_alertas "
            "(device_id, economico, alerta_activa, radio_alerta_m, dentro_del_radio, ultima_notificacion_ts) "
            "VALUES (?, ?, ?, ?, 0, NULL) "
            "ON CONFLICT(device_id, economico) DO UPDATE SET "
            "alerta_activa = excluded.alerta_activa, radio_alerta_m = excluded.radio_alerta_m",
            (device_id, economico, 1 if alerta_activa else 0, radio),
        )
        con.commit()


def eliminar_alerta(device_id: str, economico: str) -> None:
    """Baja completa de la fila -- se llama cuando la unidad se quita de 'Guardadas' por completo
    (no solo cuando se apaga la alerta, eso es actualizar_alerta con alerta_activa=False): evita
    que queden filas huérfanas de unidades que el usuario ya ni siquiera tiene guardadas."""
    if not device_id or not economico:
        return
    with _lock, _conexion() as con:
        con.execute(
            "DELETE FROM device_alertas WHERE device_id = ? AND economico = ?",
            (device_id, economico),
        )
        con.commit()


def alertas_activas_de(device_id: str) -> list[dict]:
    """Filas con alerta_activa=1 de este dispositivo -- para que Android pueda, si lo necesita,
    confirmar el estado real del backend (no se usa todavía, queda lista para la UI)."""
    with _lock, _conexion() as con:
        rows = con.execute(
            "SELECT economico, radio_alerta_m, dentro_del_radio, ultima_notificacion_ts "
            "FROM device_alertas WHERE device_id = ? AND alerta_activa = 1",
            (device_id,),
        ).fetchall()
        return [
            {
                "economico": r[0],
                "radioAlertaM": r[1],
                "dentroDelRadio": bool(r[2]),
                "ultimaNotificacionTs": r[3],
            }
            for r in rows
        ]


def hay_alguna_alerta_activa(device_id: str) -> bool:
    with _lock, _conexion() as con:
        row = con.execute(
            "SELECT EXISTS(SELECT 1 FROM device_alertas WHERE device_id = ? AND alerta_activa = 1)",
            (device_id,),
        ).fetchone()
        return bool(row[0]) if row else False


# ---------------------------------------------------------------- ubicación (una por dispositivo)

def actualizar_ubicacion(device_id: str, lat: float, lon: float, timestamp: int) -> None:
    """Upsert de la ÚLTIMA ubicación conocida de este dispositivo -- REEMPLAZA la anterior, nunca
    agrega una fila nueva ni conserva historial (ver el javadoc/comentario del módulo). Android
    solo llama esto mientras tiene al menos una alerta activa (AlertasUnidadesService); aquí no
    se valida eso de nuevo -- esta función solo persiste lo que se le pasa."""
    if not device_id:
        return
    with _lock, _conexion() as con:
        con.execute(
            "INSERT INTO device_ubicacion (device_id, lat, lon, timestamp) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET lat = excluded.lat, lon = excluded.lon, "
            "timestamp = excluded.timestamp",
            (device_id, float(lat), float(lon), int(timestamp)),
        )
        con.commit()


def ubicacion_de(device_id: str) -> dict | None:
    """La última ubicación conocida de este dispositivo, o None si nunca mandó una -- para que
    el futuro detector de proximidad (push_metrobus.py, etapa posterior) sepa contra qué comparar
    cada unidad con alerta activa. No se usa todavía en esta etapa."""
    with _lock, _conexion() as con:
        row = con.execute(
            "SELECT lat, lon, timestamp FROM device_ubicacion WHERE device_id = ?", (device_id,)
        ).fetchone()
        return {"lat": row[0], "lon": row[1], "timestamp": row[2]} if row else None
