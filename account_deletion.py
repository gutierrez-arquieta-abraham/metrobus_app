# ============================================================
# MÓDULO   : account_deletion.py   (Blueprint de Flask)
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Política de privacidad pública y eliminación de cuenta de GeoMB, para cumplir con la sección
# "Seguridad y recopilación de datos" de Google Play (Política de privacidad + URL de eliminación
# de cuenta funcional, no un simple formulario informativo).
#
# Dos caminos, un solo núcleo (delete_account_core): NUNCA se acepta un uid/correo tecleado como
# prueba de identidad -- el uid siempre sale de una credencial verificada:
#
#   1) Dentro de la app (ConfiguracionFragment -> "Eliminar mi cuenta"), con sesión iniciada:
#      POST /api/account/delete con Authorization: Bearer <ID token de Firebase> + X-Device-ID.
#      El uid sale de auth.verify_id_token() (Admin SDK) -- nunca de un campo del cliente. El
#      X-Device-ID (mismo header que ya usan /device/*) SOLO permite borrar el token FCM, las
#      alertas de proximidad y la última ubicación de ESE dispositivo si el backend puede
#      demostrar que ese device_id pertenece de verdad a este uid (ver
#      _verificar_propiedad_device/device_alertas.vincular_device) -- nunca por una simple
#      comparación de lo que mande el cliente.
#
#   2) Página pública GET/POST /delete-account (para Play Console, sin abrir la app): solo pide
#      el correo. Si existe una cuenta con ese correo (auth.get_user_by_email), se genera un
#      token de un solo uso (no se guarda en claro, solo su sha256) y se manda por correo
#      (reusando GMAIL_ADDRESS/GMAIL_APP_PASSWORD, ya existentes para el bot de renovación) un
#      enlace a /delete-account/confirmar?token=... La respuesta del formulario es SIEMPRE el
#      mismo mensaje genérico, exista o no la cuenta (anti-enumeración). GET /confirmar solo
#      MUESTRA una pantalla de confirmación (nunca borra en un GET, para no ejecutar el borrado
#      si un escáner de correo pre-visita el enlace); la eliminación real ocurre en el POST de
#      esa misma pantalla. El token se RECLAMA (lock_ts) antes de intentar el borrado y solo se
#      marca usado/inservible si la eliminación quedó realmente completa -- si falla, se libera
#      para poder reintentar con el MISMO enlace (ver _reclamar_token/_marcar_token_usado/
#      _liberar_token). Nunca se puede ejecutar dos veces en paralelo con el mismo token.
#
# Qué se borra (ver delete_account_core) y EN QUÉ ORDEN -- sin transacción distribuida (Firestore
# y Firebase Auth son sistemas distintos, eso no existe): primero Firestore (usuarios/{uid} y
# TODO su subárbol), la entrada KYC en kyc_store.json y, si corresponde, el dispositivo; Firebase
# Authentication (auth.delete_user) se borra AL FINAL y SOLO si todo lo anterior se completó de
# verdad -- es la única credencial que le permite al usuario volver a autenticarse y reintentar
# si algo falló a medias. resultado['completo'] es la única señal de éxito real: ni la API ni el
# flujo por correo reportan éxito si viene en False. NUNCA se toca reportes/{reportId}: esa
# colección jamás guarda uid, correo ni deviceId (ver TelemetriaSync.subirReportes en el
# cliente), así que no hay nada que vincular.
#
# Nada de esto introduce credenciales nuevas: Firebase Admin ya se inicializa igual que en
# push_metrobus.py (GOOGLE_APPLICATION_CREDENTIALS ya está en el geomb.env de metrobus-web.service)
# y el envío de correo reusa el mismo GMAIL_ADDRESS/GMAIL_APP_PASSWORD que ya existe.
# ============================================================
"""
account_deletion.py — Política de privacidad (GET /privacy) y eliminación de cuenta para GeoMB.

Rutas (regístralo en app.py: `from account_deletion import account_bp; app.register_blueprint(account_bp)`):
  GET   /privacy                        -> política de privacidad pública
  GET   /delete-account                 -> formulario (solo correo) para pedir la eliminación
  POST  /delete-account                 -> procesa la solicitud, manda correo si la cuenta existe
  GET   /delete-account/confirmar       -> pantalla de confirmación (no borra nada todavía)
  POST  /delete-account/confirmar       -> ejecuta el borrado si el token es válido y no se usó
  POST  /api/account/delete             -> eliminación desde la app (Authorization: Bearer <idToken>)

Variables de entorno (ninguna nueva/secreta obligatoria más allá de lo que ya usa el resto del
backend): FIREBASE_CREDENTIALS_JSON o GOOGLE_APPLICATION_CREDENTIALS (ya configurado, ver
push_metrobus.py), GMAIL_ADDRESS/GMAIL_APP_PASSWORD (ya configurado, ver app.py renew_loop).
"""
import hashlib
import json
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from email.mime.text import MIMEText
from pathlib import Path

from flask import Blueprint, render_template, request

import firebase_admin
from firebase_admin import auth, credentials, firestore

import device_alertas

account_bp = Blueprint("account", __name__)

_DB_PATH = Path(__file__).resolve().parent / "account_deletion.db"
_lock = threading.Lock()

TOKEN_TTL_SEGUNDOS = 30 * 60   # enlace de confirmación por correo: válido 30 minutos
LOCK_TTL_SEGUNDOS = 120        # cuánto dura el "reclamo" de un token mientras se procesa (ver
                               # _reclamar_token) antes de poder reintentarse si el proceso se
                               # cayó a medias -- nunca se marca usado hasta un éxito completo.
_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# (clave de limite) -> (maximo de peticiones, ventana en segundos)
LIM_SOLICITUD_IP = (5, 3600)        # pedir el enlace por correo: 5/hora por IP
LIM_SOLICITUD_EMAIL = (3, 86400)    # 3 correos/día a la MISMA dirección (evita "email bombing")
LIM_CONFIRMAR_IP = (20, 3600)       # ver/confirmar el enlace: 20/hora por IP
LIM_API_IP = (10, 3600)             # POST /api/account/delete: 10/hora por IP
LIM_API_UID = (5, 3600)             # ... y 5/hora por cuenta ya autenticada


def _conexion() -> sqlite3.Connection:
    con = sqlite3.connect(_DB_PATH, timeout=5)
    con.execute(
        "CREATE TABLE IF NOT EXISTS deletion_tokens ("
        "  token_hash TEXT PRIMARY KEY,"
        "  uid TEXT NOT NULL,"
        "  created_ts INTEGER NOT NULL,"
        "  expires_ts INTEGER NOT NULL,"
        "  lock_ts INTEGER,"
        "  used_ts INTEGER"
        ")"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS rate_limit ("
        "  clave TEXT PRIMARY KEY,"
        "  ventana_inicio INTEGER NOT NULL,"
        "  conteo INTEGER NOT NULL"
        ")"
    )
    return con


# ---------------------------------------------------------------- utilidades comunes

def _ip_cliente() -> str:
    """nginx es el único frente (ver CLAUDE.md del backend); si manda X-Forwarded-For se usa el
    primer salto, si no se cae a remote_addr (en ese caso, mismo límite para todos detrás del
    proxy -- verificar que nginx mande XFF para que el límite por IP sea efectivo de verdad)."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "desconocida"


def _limitar(clave: str, maximo: int, ventana_seg: int) -> bool:
    """Ventana fija (no deslizante) en SQLite junto al módulo -- mismo criterio que
    device_alertas.py: sin infraestructura nueva. Devuelve True si la petición se permite (y
    queda contada); False si esta ventana ya se agotó para esta clave."""
    ahora = int(time.time())
    with _lock, _conexion() as con:
        row = con.execute(
            "SELECT ventana_inicio, conteo FROM rate_limit WHERE clave = ?", (clave,)
        ).fetchone()
        if row is None or ahora - row[0] >= ventana_seg:
            con.execute(
                "INSERT INTO rate_limit (clave, ventana_inicio, conteo) VALUES (?, ?, 1) "
                "ON CONFLICT(clave) DO UPDATE SET ventana_inicio = excluded.ventana_inicio, conteo = 1",
                (clave, ahora),
            )
            con.commit()
            return True
        if row[1] >= maximo:
            return False
        con.execute("UPDATE rate_limit SET conteo = conteo + 1 WHERE clave = ?", (clave,))
        con.commit()
        return True


def _init_firebase():
    if firebase_admin._apps:
        return
    j = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    cred = credentials.Certificate(json.loads(j)) if j else credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)


def _uid_desde_bearer() -> str | None:
    """Verifica el ID token de Firebase del header 'Authorization: Bearer <token>' y devuelve el
    uid que Firebase certifica -- NUNCA se acepta un uid que venga como campo del cuerpo/query
    (eso permitiría a cualquiera borrar la cuenta de otro con solo conocer su uid). check_revoked
    obliga a validar contra el registro actual del usuario en Firebase, así un token todavía no
    vencido pero de una cuenta ya eliminada/revocada se rechaza igual."""
    cab = request.headers.get("Authorization", "")
    if not cab.startswith("Bearer "):
        return None
    token = cab[7:].strip()
    if not token:
        return None
    try:
        _init_firebase()
        decoded = auth.verify_id_token(token, check_revoked=True)
        return decoded.get("uid")
    except Exception as e:
        # Nunca se registra el token ni el motivo detallado (podría incluir PII) -- solo el tipo
        # de excepción, para poder diagnosticar sin exponer credenciales en los logs.
        print(f"[account] token invalido: {type(e).__name__}", flush=True)
        return None


# ---------------------------------------------------------------- borrado (núcleo, idempotente)

def _borrar_doc_recursivo(doc_ref) -> None:
    """Firestore no borra subcolecciones al borrar un documento -- hay que bajar primero. Un
    documento que solo existe como "padre" de una subcolección (nunca se le hizo .set(), p. ej.
    usuarios/{uid}/telemetria/recorridos) no existe de verdad como documento; borrarlo es un
    no-op, no un error -- por eso esta función ya es idempotente sin cuidado extra."""
    for coll_ref in doc_ref.collections():
        for doc in coll_ref.stream():
            _borrar_doc_recursivo(doc.reference)
    doc_ref.delete()


def _borrar_firestore(uid: str) -> bool:
    try:
        _init_firebase()
        db = firestore.client()
        _borrar_doc_recursivo(db.collection("usuarios").document(uid))
        return True
    except Exception as e:
        print(f"[account] error borrando firestore uid={uid}: {e}", flush=True)
        return False


def _borrar_kyc(uid: str) -> bool:
    """kyc_store.json vive en didit_backend.py (solo si ese blueprint cargó -- ver app.py). Si no
    está disponible en este deploy, no hay nada que borrar: se trata como éxito, no como error.

    IMPORTANTE: didit_backend._save() atrapa sus PROPIAS excepciones de escritura a disco
    ('except Exception: pass') y nunca las deja subir -- así que un try/except aquí alrededor de
    kyc_save() NUNCA vería un fallo real de escritura (disco lleno, permisos, etc.), y
    reportaríamos éxito aunque el archivo no se haya actualizado de verdad. Por eso se VERIFICA
    el resultado releyendo el archivo después de guardar, en vez de confiar en que "no lanzó"
    signifique "se guardó"."""
    try:
        from didit_backend import _load as kyc_load, _save as kyc_save, _lock as kyc_lock
    except Exception:
        return True
    try:
        with kyc_lock:
            d = kyc_load()
            if str(uid) not in d:
                return True   # nada que borrar: éxito trivial, idempotente
            del d[str(uid)]
            kyc_save(d)
            releido = kyc_load()
            return str(uid) not in releido
    except Exception as e:
        print(f"[account] error borrando kyc uid={uid}: {e}", flush=True)
        return False


def _borrar_auth(uid: str) -> bool:
    try:
        _init_firebase()
        auth.delete_user(uid)
        return True
    except auth.UserNotFoundError:
        return True   # ya no existia: idempotente, no es un fallo
    except Exception as e:
        print(f"[account] error borrando auth uid={uid}: {e}", flush=True)
        return False


def _borrar_device(device_id: str) -> bool:
    try:
        device_alertas.eliminar_todo_device(device_id)
        return True
    except Exception as e:
        print(f"[account] error borrando device_id (prefijo {device_id[:8]}...): {e}", flush=True)
        return False


def _verificar_propiedad_device(uid: str, device_id: str) -> str:
    """Decide si 'uid' puede borrar los datos de 'device_id'. El backend es la ÚNICA autoridad --
    NUNCA se confía en que el cliente mande un device_id "suyo": solo se actúa sobre lo que
    device_alertas.propietario_de() (vínculo registrado por app.py al validar un ID token, ver
    vincular_device) diga de verdad. Devuelve:

      'propio'       -- hay un vínculo device_owner y coincide con uid: se puede borrar.
      'sin_datos'    -- el device NO tiene ninguna fila en ninguna tabla (ni vínculo): no hay
                        nada que proteger, borrar es un no-op seguro sin importar quién lo pida.
      'otro_usuario' -- hay un vínculo device_owner, pero es de OTRO uid: se rechaza, NUNCA se
                        toca -- esto es precisamente lo que impide que un usuario autenticado
                        borre el dispositivo de otro con solo conocer/adivinar su device_id.
      'no_vinculado' -- el device SÍ tiene datos pero nunca se vinculó a ningún uid (dispositivo
                        histórico, de antes de esta función, o que aún no mandó un ID token junto
                        con su X-Device-ID): se rechaza por prudencia. No se "reconstruye" esa
                        propiedad de forma mágica (ver migración en device_alertas.py)."""
    propietario = device_alertas.propietario_de(device_id)
    if propietario is not None:
        return "propio" if propietario == uid else "otro_usuario"
    return "sin_datos" if not device_alertas.tiene_datos(device_id) else "no_vinculado"


def delete_account_core(uid: str, device_id: str | None = None) -> dict:
    """Elimina TODO lo asociado a esta cuenta. 'uid' SIEMPRE debe venir de una credencial ya
    verificada por el llamador (_uid_desde_bearer o _reclamar_token) -- esta función en sí misma
    no vuelve a autenticar nada, confía en que quien la llama ya probó la identidad.

    ORDEN Y POLÍTICA DE FALLOS PARCIALES (sin transacción distribuida -- Firestore y Firebase
    Auth son sistemas distintos, no hay forma real de que esto sea atómico):

      1. Firestore (usuarios/{uid} + subárbol), KYC y -- si 'device_id' pertenece de verdad a
         este uid (ver _verificar_propiedad_device) -- los datos de ese dispositivo.
      2. Firebase Authentication, AL FINAL, y SOLO si TODO lo anterior quedó realmente completo.

    Por qué Auth se borra al final y nunca si algo falló antes: es la ÚNICA credencial que le
    permite al usuario volver a autenticarse (conseguir un ID token nuevo) y reintentar la
    eliminación. Si se borrara Auth primero y luego fallara Firestore/KYC/device, el usuario se
    quedaría sin cuenta pero con datos huérfanos, y SIN FORMA de volver a probar su identidad
    para reintentar por la vía de la app (el flujo por correo tampoco podría resolver un uid que
    ya no existe en Auth). Conservar Auth hasta el final convierte cualquier fallo parcial en
    algo RETOMABLE: la próxima llamada (misma app, o el mismo enlace de correo si no se marcó
    usado -- ver _liberar_token) vuelve a intentar exactamente los pasos que ya son idempotentes
    por construcción (ver cada _borrar_*), nunca duplica ni dejó nada a medias de forma invisible.

    resultado['completo'] es la única señal de verdad: los llamadores (rutas) NUNCA deben
    reportar éxito si viene en False, sin importar qué subcampo individual diga True.

    NO toca reportes/{reportId}: esa colección nunca guarda uid/correo/deviceId (ver
    TelemetriaSync.subirReportes en el cliente), no existe vínculo que borrar ahí."""
    resultado = {
        "uid": uid,
        "firestore": _borrar_firestore(uid),
        "kyc": _borrar_kyc(uid),
        "device": None,
        "auth": None,
        "completo": False,
    }

    device_bloquea = False
    if device_id:
        propiedad = _verificar_propiedad_device(uid, device_id)
        if propiedad in ("propio", "sin_datos"):
            ok_device = _borrar_device(device_id)
            resultado["device"] = ok_device
            device_bloquea = not ok_device
        else:
            # 'otro_usuario' / 'no_vinculado': rechazo de seguridad -- nunca se toca ese
            # dispositivo, y nunca bloquea el resto de la eliminación de ESTA cuenta (ese
            # dispositivo no es suyo, o no se puede demostrar que lo sea; en cualquier caso no
            # hay nada de ESTA cuenta ahí que deje de borrarse).
            resultado["device"] = propiedad

    completo_datos = bool(resultado["firestore"]) and bool(resultado["kyc"]) and not device_bloquea
    if completo_datos:
        resultado["auth"] = _borrar_auth(uid)
    resultado["completo"] = completo_datos and bool(resultado["auth"])

    print(f"[account] eliminacion ejecutada uid={uid} device={'si' if device_id else 'no'} "
          f"resultado={resultado}", flush=True)
    return resultado


# ---------------------------------------------------------------- flujo por correo (token de un solo uso)

def _enviar_correo_confirmacion(destinatario: str, token: str) -> None:
    remitente = os.environ.get("GMAIL_ADDRESS", "").strip()
    clave_app = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not (remitente and clave_app):
        print("[account] GMAIL_ADDRESS/GMAIL_APP_PASSWORD no configurados: "
              "no se pudo enviar el correo de confirmacion", flush=True)
        return
    base = os.environ.get("ACCOUNT_DELETE_BASE_URL", "https://geomb.duckdns.org").strip()
    enlace = f"{base}/delete-account/confirmar?token={token}"
    minutos = TOKEN_TTL_SEGUNDOS // 60
    cuerpo = (
        "Recibimos una solicitud para eliminar tu cuenta de GeoMB.\n\n"
        f"Si fuiste tú, confirma aquí (enlace válido {minutos} minutos, un solo uso):\n"
        f"{enlace}\n\n"
        "Si no fuiste tú, ignora este correo: tu cuenta no sufrirá ningún cambio.\n"
    )
    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"] = "Confirma la eliminación de tu cuenta de GeoMB"
    msg["From"] = remitente
    msg["To"] = destinatario
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
            smtp.login(remitente, clave_app)
            smtp.sendmail(remitente, [destinatario], msg.as_string())
    except Exception as e:
        print(f"[account] error enviando correo de confirmacion: {e}", flush=True)


def _procesar_solicitud(correo: str) -> None:
    """Si existe una cuenta con este correo, emite un token de un solo uso (solo se guarda su
    sha256, nunca el token en claro) y manda el enlace. Si NO existe, no hace nada mas -- el
    llamador (eliminar_cuenta_solicitar) responde siempre el mismo mensaje generico, exista o no
    la cuenta, para no permitir enumerar correos registrados."""
    try:
        _init_firebase()
        user = auth.get_user_by_email(correo)
    except Exception:
        return
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ahora = int(time.time())
    with _lock, _conexion() as con:
        con.execute(
            "INSERT INTO deletion_tokens (token_hash, uid, created_ts, expires_ts, used_ts) "
            "VALUES (?, ?, ?, ?, NULL)",
            (token_hash, user.uid, ahora, ahora + TOKEN_TTL_SEGUNDOS),
        )
        con.commit()
    _enviar_correo_confirmacion(correo, token)


def _estado_token(token: str) -> str:
    """'valido' | 'token_invalido' | 'token_usado' | 'token_vencido' -- solo lectura, nunca
    reclama ni marca el token (eso lo hace _reclamar_token, en el POST de confirmación). El GET
    de confirmación usa esto; mostrar la pantalla de confirmación nunca tiene efectos
    destructivos, sin importar si hay un reclamo (lock_ts) en curso de otra petición."""
    if not token:
        return "token_invalido"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _lock, _conexion() as con:
        row = con.execute(
            "SELECT expires_ts, used_ts FROM deletion_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
    if row is None:
        return "token_invalido"
    expires_ts, used_ts = row
    if used_ts is not None:
        return "token_usado"
    if int(time.time()) > expires_ts:
        return "token_vencido"
    return "valido"


def _reclamar_token(token: str) -> tuple[str, str | None]:
    """Reserva este token para procesarlo -- NUNCA lo marca como usado todavía (eso lo decide el
    llamador según el resultado real de delete_account_core, ver _marcar_token_usado/
    _liberar_token). Devuelve (estado, uid):

      'reclamado'      -- se obtuvo el reclamo en exclusiva; procede a delete_account_core(uid).
      'en_proceso'     -- otra petición ya lo está procesando (lock_ts reciente, < LOCK_TTL_SEGUNDOS).
      'token_invalido' / 'token_usado' / 'token_vencido' -- igual que _estado_token.

    Concurrencia: el UPDATE de abajo con 'WHERE ... (lock_ts IS NULL OR vencido)' es atómico a
    nivel de SQLite (un solo escritor a la vez sobre el archivo) y además serializado dentro de
    este proceso por el Lock de Python -- si dos peticiones llegan con el MISMO token casi al
    mismo tiempo, solo UNA ve rowcount == 1 y pasa a 'reclamado'; la otra ve el lock ya puesto y
    recibe 'en_proceso'. Si el proceso que reclamó se cae a medias (nunca llama a
    _marcar_token_usado ni a _liberar_token), el lock expira solo tras LOCK_TTL_SEGUNDOS y el
    token vuelve a ser reclamable -- nunca queda inutilizable para siempre por un fallo a medias."""
    if not token:
        return "token_invalido", None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ahora = int(time.time())
    with _lock, _conexion() as con:
        row = con.execute(
            "SELECT uid, expires_ts, used_ts, lock_ts FROM deletion_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            return "token_invalido", None
        uid, expires_ts, used_ts, lock_ts = row
        if used_ts is not None:
            return "token_usado", None
        if ahora > expires_ts:
            return "token_vencido", None
        if lock_ts is not None and ahora - lock_ts < LOCK_TTL_SEGUNDOS:
            return "en_proceso", None
        cur = con.execute(
            "UPDATE deletion_tokens SET lock_ts = ? WHERE token_hash = ? AND used_ts IS NULL "
            "AND (lock_ts IS NULL OR ? - lock_ts >= ?)",
            (ahora, token_hash, ahora, LOCK_TTL_SEGUNDOS),
        )
        con.commit()
        if cur.rowcount != 1:
            return "en_proceso", None
        return "reclamado", uid


def _marcar_token_usado(token: str) -> None:
    """Se llama SOLO cuando delete_account_core devolvió resultado['completo'] == True -- deja
    el token definitivamente inservible (ver _estado_token/_reclamar_token: 'token_usado')."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _lock, _conexion() as con:
        con.execute(
            "UPDATE deletion_tokens SET used_ts = ? WHERE token_hash = ?", (int(time.time()), token_hash)
        )
        con.commit()


def _liberar_token(token: str) -> None:
    """Se llama cuando la eliminación quedó INCOMPLETA -- suelta el reclamo (lock_ts = NULL) SIN
    marcar como usado, para que el mismo enlace de correo pueda reintentarse. NUNCA se llama
    después de un éxito completo (eso es _marcar_token_usado)."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _lock, _conexion() as con:
        con.execute("UPDATE deletion_tokens SET lock_ts = NULL WHERE token_hash = ?", (token_hash,))
        con.commit()


# ---------------------------------------------------------------- rutas

@account_bp.route("/privacy")
def privacidad():
    return render_template("privacy.html")


@account_bp.route("/delete-account", methods=["GET"])
def eliminar_cuenta_formulario():
    return render_template("delete_account.html", error=False)


@account_bp.route("/delete-account", methods=["POST"])
def eliminar_cuenta_solicitar():
    ip = _ip_cliente()
    if not _limitar(f"solicitud_ip:{ip}", *LIM_SOLICITUD_IP):
        return render_template("delete_account_sent.html"), 429
    correo = (request.form.get("email") or "").strip().lower()
    if not _RE_EMAIL.match(correo):
        return render_template("delete_account.html", error=True), 400
    clave_correo = hashlib.sha256(correo.encode()).hexdigest()
    if _limitar(f"solicitud_email:{clave_correo}", *LIM_SOLICITUD_EMAIL):
        _procesar_solicitud(correo)
    # Mismo mensaje SIEMPRE (exista o no la cuenta, se haya mandado el correo o no por el límite
    # de la dirección): no revela si el correo está registrado.
    return render_template("delete_account_sent.html")


@account_bp.route("/delete-account/confirmar", methods=["GET"])
def eliminar_cuenta_confirmar_formulario():
    ip = _ip_cliente()
    if not _limitar(f"confirmar_ip:{ip}", *LIM_CONFIRMAR_IP):
        return render_template("delete_account_done.html", ok=False, motivo="limite"), 429
    token = (request.args.get("token") or "").strip()
    estado = _estado_token(token)
    if estado != "valido":
        return render_template("delete_account_done.html", ok=False, motivo=estado)
    # Solo MUESTRA la confirmación -- el borrado real ocurre en el POST de este mismo formulario,
    # nunca en este GET (un escáner de correo que pre-visite el enlace no debe disparar nada).
    return render_template("delete_account_confirm.html", token=token)


@account_bp.route("/delete-account/confirmar", methods=["POST"])
def eliminar_cuenta_confirmar():
    ip = _ip_cliente()
    if not _limitar(f"confirmar_ip:{ip}", *LIM_CONFIRMAR_IP):
        return render_template("delete_account_done.html", ok=False, motivo="limite"), 429
    token = (request.form.get("token") or "").strip()
    estado, uid = _reclamar_token(token)
    if estado == "en_proceso":
        return render_template("delete_account_done.html", ok=False, motivo="en_proceso"), 409
    if estado != "reclamado":
        return render_template("delete_account_done.html", ok=False, motivo=estado)
    resultado = delete_account_core(uid)
    if resultado["completo"]:
        _marcar_token_usado(token)
        return render_template("delete_account_done.html", ok=True)
    # Incompleto: el token se LIBERA (no se marca usado) para que el mismo enlace pueda
    # reintentarse -- nunca se deja al usuario sin token y sin cuenta eliminada.
    _liberar_token(token)
    return render_template("delete_account_done.html", ok=False, motivo="fallo_temporal"), 500


@account_bp.route("/api/account/delete", methods=["POST"])
def eliminar_cuenta_api():
    """Eliminación desde la app (ConfiguracionFragment): Authorization: Bearer <ID token de
    Firebase> + X-Device-ID opcional (si se manda y el backend puede demostrar que ese
    dispositivo pertenece a este uid -- ver _verificar_propiedad_device -- también se limpian
    sus datos en device_alertas.db). El uid nunca sale de un campo del cuerpo -- solo del token
    verificado (ver _uid_desde_bearer). Solo se responde ok=true con HTTP 200 cuando
    resultado['completo'] es True; cualquier otro caso es HTTP 500 con un mensaje genérico (sin
    uid, sin detalle de Firebase, sin stack trace) y puede reintentarse: cada paso de
    delete_account_core ya es idempotente."""
    ip = _ip_cliente()
    if not _limitar(f"api_ip:{ip}", *LIM_API_IP):
        return {"ok": False, "error": "demasiadas solicitudes"}, 429
    uid = _uid_desde_bearer()
    if uid is None:
        return {"ok": False, "error": "token invalido o ausente"}, 401
    if not _limitar(f"api_uid:{uid}", *LIM_API_UID):
        return {"ok": False, "error": "demasiadas solicitudes"}, 429
    device_id = (request.headers.get("X-Device-ID") or "").strip()[:128]
    resultado = delete_account_core(uid, device_id or None)
    if resultado["completo"]:
        return {"ok": True}
    return {"ok": False, "error": "no se pudo completar la eliminacion, intenta de nuevo"}, 500
