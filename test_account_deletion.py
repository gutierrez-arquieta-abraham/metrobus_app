# ============================================================
# PRUEBAS : test_account_deletion.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# Pruebas de account_deletion.py (política de privacidad + eliminación de cuenta) y de la
# limpieza/vínculo de propiedad añadidos a device_alertas.py. NO usa credenciales reales de
# Firebase ni manda correos reales: firebase_admin.auth y el envío SMTP se sustituyen por dobles
# de prueba (monkeypatch), y las bases SQLite se redirigen a archivos temporales (tmp_path) para
# no tocar ningún dato real ni dejar artefactos en el repo. Ejecutar con:
#   pytest test_account_deletion.py -v
import hashlib
import secrets
import threading
import time

import pytest
from flask import Flask
from firebase_admin import auth as real_auth

import account_deletion
import device_alertas


# ---------------------------------------------------------------- infraestructura de prueba

class FakeUser:
    def __init__(self, uid):
        self.uid = uid


def _resultado(completo, **extra):
    """Dict mínimo con la forma real de delete_account_core, para los tests que mockean la
    función en vez de ejercitarla de verdad (tests de rutas/orquestación HTTP)."""
    base = {"uid": extra.get("uid"), "firestore": completo, "kyc": completo,
            "device": extra.get("device"), "auth": completo, "completo": completo}
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _db_temporal(tmp_path, monkeypatch):
    """Redirige las bases SQLite de account_deletion y device_alertas a archivos temporales:
    ninguna prueba toca account_deletion.db / device_alertas.db reales del repo."""
    monkeypatch.setattr(account_deletion, "_DB_PATH", tmp_path / "account_deletion_test.db")
    monkeypatch.setattr(device_alertas, "_DB_PATH", tmp_path / "device_alertas_test.db")


@pytest.fixture(autouse=True)
def _sin_smtp_real(monkeypatch):
    """Nunca se manda un correo real: smtplib.SMTP_SSL se sustituye por un doble que solo
    registra qué se habría mandado."""
    enviados = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def login(self, *a, **k):
            pass
        def sendmail(self, remitente, destinatarios, cuerpo):
            enviados.append((remitente, destinatarios, cuerpo))

    monkeypatch.setattr(account_deletion.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("GMAIL_ADDRESS", "pruebas@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "clave-de-prueba")
    return enviados


@pytest.fixture
def app():
    app = Flask(__name__, template_folder="templates")
    app.register_blueprint(account_deletion.account_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class FakeFirestoreSnapshot:
    def __init__(self, ref):
        self.reference = ref


class FakeDocRef:
    """Doble mínimo de google.cloud.firestore.DocumentReference: 'children' es un dict
    {nombre_subcoleccion: {doc_id: FakeDocRef}}. 'deleted' acumula los paths borrados para que
    la prueba pueda verificar el recorrido recursivo completo."""

    def __init__(self, path, children, deleted):
        self.path = path
        self.children = children
        self.deleted = deleted

    def collections(self):
        return [FakeCollRef(self.path, name, docs, self.deleted)
                for name, docs in self.children.items()]

    def delete(self):
        self.deleted.add(self.path)


class FakeCollRef:
    def __init__(self, parent_path, name, docs, deleted):
        self.parent_path = parent_path
        self.name = name
        self.docs = docs
        self.deleted = deleted

    def stream(self):
        return [FakeFirestoreSnapshot(ref) for ref in self.docs.values()]

    def document(self, doc_id):
        return self.docs.get(doc_id, FakeDocRef(f"{self.parent_path}/{self.name}/{doc_id}", {}, self.deleted))


class FakeFirestoreClient:
    """Solo conoce 'usuarios': si delete_account_core intentara tocar cualquier otra colección
    (p. ej. 'reportes'), esta aserción hace fallar la prueba de inmediato."""

    def __init__(self, usuarios_docs, deleted):
        self.usuarios_docs = usuarios_docs
        self.deleted = deleted

    def collection(self, name):
        assert name == "usuarios", f"delete_account_core tocó una colección inesperada: {name}"
        return FakeCollRef("", "usuarios", self.usuarios_docs, self.deleted)


class FakeFirestoreClientRompe:
    """Simula un fallo real de Firestore (red, cuota, lo que sea) en cuanto se le pide la
    colección -- para probar que un fallo aquí bloquea el borrado de Firebase Auth."""

    def collection(self, name):
        raise RuntimeError("firestore no disponible (prueba)")


def _arbol_usuario_de_prueba(uid, deleted):
    """usuarios/{uid} con el esquema real confirmado: telemetria/recorridos/items/r1."""
    item = FakeDocRef(f"usuarios/{uid}/telemetria/recorridos/items/r1", {}, deleted)
    recorridos_doc = FakeDocRef(f"usuarios/{uid}/telemetria/recorridos", {"items": {"r1": item}}, deleted)
    return FakeDocRef(f"usuarios/{uid}", {"telemetria": {"recorridos": recorridos_doc}}, deleted)


def _exito_trivial(monkeypatch, uid):
    """Deja que _borrar_firestore/_borrar_kyc/_borrar_auth tengan éxito trivial, para aislar en
    cada prueba SOLO lo que realmente se quiere ejercitar (p. ej. la propiedad de un device)."""
    deleted = set()
    arbol = _arbol_usuario_de_prueba(uid, deleted)
    fake_client = FakeFirestoreClient({uid: arbol}, deleted)
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.firestore, "client", lambda: fake_client)
    monkeypatch.setattr(account_deletion.auth, "delete_user", lambda u: None)
    monkeypatch.setattr(account_deletion, "_borrar_kyc", lambda u: True)


# ---------------------------------------------------------------- páginas públicas

def test_privacy_reachable_sin_login(client):
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "Política de privacidad".encode("utf-8") in r.data


def test_delete_account_form_reachable_sin_login(client):
    r = client.get("/delete-account")
    assert r.status_code == 200
    assert b"Eliminar" in r.data


# ---------------------------------------------------------------- solicitud por correo (anti-enumeracion)

def test_solicitud_email_valido_responde_generico_y_manda_correo(client, monkeypatch, _sin_smtp_real):
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.auth, "get_user_by_email", lambda correo: FakeUser("uid-real"))
    r = client.post("/delete-account", data={"email": "real@example.com"})
    assert r.status_code == 200
    assert b"Revisa tu correo" in r.data
    assert len(_sin_smtp_real) == 1
    with account_deletion._conexion() as con:
        fila = con.execute("SELECT uid FROM deletion_tokens").fetchone()
    assert fila is not None and fila[0] == "uid-real"


def test_solicitud_email_inexistente_mismo_mensaje_generico_sin_token(client, monkeypatch, _sin_smtp_real):
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    def _no_existe(correo):
        raise real_auth.UserNotFoundError("no existe")
    monkeypatch.setattr(account_deletion.auth, "get_user_by_email", _no_existe)
    r = client.post("/delete-account", data={"email": "nadie@example.com"})
    assert r.status_code == 200
    assert b"Revisa tu correo" in r.data   # MISMO mensaje que si la cuenta sí existiera
    assert len(_sin_smtp_real) == 0
    with account_deletion._conexion() as con:
        fila = con.execute("SELECT uid FROM deletion_tokens").fetchone()
    assert fila is None


def test_solicitud_correo_mal_formado_rechazada(client):
    r = client.post("/delete-account", data={"email": "no-es-un-correo"})
    assert r.status_code == 400


# ---------------------------------------------------------------- confirmación por token (reclamo/liberación)

def _emitir_token(uid):
    token = secrets.token_urlsafe(16)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ahora = int(time.time())
    with account_deletion._conexion() as con:
        con.execute(
            "INSERT INTO deletion_tokens (token_hash, uid, created_ts, expires_ts, lock_ts, used_ts) "
            "VALUES (?, ?, ?, ?, NULL, NULL)",
            (token_hash, uid, ahora, ahora + account_deletion.TOKEN_TTL_SEGUNDOS),
        )
        con.commit()
    return token


def test_confirmar_get_token_valido_muestra_pantalla_sin_borrar(client, monkeypatch):
    llamado = {"si": False}
    def _no_debe_llamarse(uid, device_id=None):
        llamado["si"] = True
        return _resultado(True, uid=uid)
    monkeypatch.setattr(account_deletion, "delete_account_core", _no_debe_llamarse)
    token = _emitir_token("uid-1")
    r = client.get(f"/delete-account/confirmar?token={token}")
    assert r.status_code == 200
    assert b"Confirmar eliminaci" in r.data
    assert llamado["si"] is False   # el GET nunca ejecuta el borrado
    assert account_deletion._estado_token(token) == "valido"   # tampoco lo reclama/consume


def test_confirmar_post_token_valido_ejecuta_borrado_y_lo_consume(client, monkeypatch):
    llamadas = []
    monkeypatch.setattr(account_deletion, "delete_account_core",
                         lambda uid, device_id=None: (llamadas.append(uid), _resultado(True, uid=uid))[1])
    token = _emitir_token("uid-2")
    r = client.post("/delete-account/confirmar", data={"token": token})
    assert r.status_code == 200
    assert b"eliminada" in r.data
    assert llamadas == ["uid-2"]
    assert account_deletion._estado_token(token) == "token_usado"


def test_confirmar_token_reutilizado_falla_la_segunda_vez(client, monkeypatch):
    monkeypatch.setattr(account_deletion, "delete_account_core",
                         lambda uid, device_id=None: _resultado(True, uid=uid))
    token = _emitir_token("uid-3")
    r1 = client.post("/delete-account/confirmar", data={"token": token})
    assert b"eliminada" in r1.data
    r2 = client.post("/delete-account/confirmar", data={"token": token})
    texto2 = r2.get_data(as_text=True).lower()
    assert "no es v" in texto2 or "ya se us" in texto2


def test_confirmar_token_vencido(client, monkeypatch):
    monkeypatch.setattr(account_deletion, "delete_account_core",
                         lambda uid, device_id=None: _resultado(True, uid=uid))
    token = secrets.token_urlsafe(16)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ahora = int(time.time())
    with account_deletion._conexion() as con:
        con.execute(
            "INSERT INTO deletion_tokens (token_hash, uid, created_ts, expires_ts, lock_ts, used_ts) "
            "VALUES (?, ?, ?, ?, NULL, NULL)",
            (token_hash, "uid-4", ahora - 10000, ahora - 1),
        )
        con.commit()
    r = client.get(f"/delete-account/confirmar?token={token}")
    assert b"venci" in r.data.lower()


def test_confirmar_token_invalido(client):
    r = client.get("/delete-account/confirmar?token=esto-no-existe")
    texto = r.get_data(as_text=True).lower()
    assert "no es v" in texto or "invalido" in texto


# --- 9D: éxito consume, fallo libera (reintentable), concurrencia solo una ejecución real ---

def test_confirmar_fallo_no_consume_token_y_permite_reintentar(client, monkeypatch):
    """Sección 2 y 9D: si delete_account_core devuelve incompleto, el token NO debe quedar
    usado -- el mismo enlace debe poder reintentarse (y esta vez sí completar)."""
    llamadas = {"n": 0}
    def _core(uid, device_id=None):
        llamadas["n"] += 1
        return _resultado(llamadas["n"] > 1, uid=uid)   # falla la 1a vez, completa la 2a
    monkeypatch.setattr(account_deletion, "delete_account_core", _core)
    token = _emitir_token("uid-reintento")

    r1 = client.post("/delete-account/confirmar", data={"token": token})
    assert r1.status_code == 500
    assert b"fallo" in r1.data.lower() or b"problema" in r1.data.lower()
    assert account_deletion._estado_token(token) == "valido"   # NUNCA se marcó usado

    r2 = client.post("/delete-account/confirmar", data={"token": token})
    assert r2.status_code == 200
    assert b"eliminada" in r2.data
    assert llamadas["n"] == 2
    assert account_deletion._estado_token(token) == "token_usado"   # ahora sí, tras el éxito


def test_confirmar_dos_post_concurrentes_solo_uno_ejecuta_el_borrado(app, monkeypatch):
    """Sección 2 y 9D: dos POST con el MISMO token casi al mismo tiempo -- solo uno debe llegar a
    ejecutar delete_account_core; el otro debe rechazarse (token en proceso / ya usado), nunca
    ejecutar una segunda eliminación en paralelo. Concurrencia real con threading, determinista:
    el invariante que se verifica (len(llamadas) == 1) no depende de CUÁL hilo gane la carrera,
    solo de que nunca gane más de uno."""
    llamadas = []
    llamadas_lock = threading.Lock()

    def _core_lento(uid, device_id=None):
        with llamadas_lock:
            llamadas.append(uid)
        time.sleep(0.25)   # amplía a propósito la ventana en la que un segundo intento podría colarse
        return _resultado(True, uid=uid)

    monkeypatch.setattr(account_deletion, "delete_account_core", _core_lento)
    token = _emitir_token("uid-concurrente")

    barrera = threading.Barrier(2)
    resultados = []
    resultados_lock = threading.Lock()

    def _hacer_post():
        cliente = app.test_client()
        barrera.wait()
        r = cliente.post("/delete-account/confirmar", data={"token": token})
        with resultados_lock:
            resultados.append(r.status_code)

    t1 = threading.Thread(target=_hacer_post)
    t2 = threading.Thread(target=_hacer_post)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert len(llamadas) == 1, f"delete_account_core se ejecutó {len(llamadas)} veces, debía ser 1"
    assert 200 in resultados
    assert account_deletion._estado_token(token) == "token_usado"


# ---------------------------------------------------------------- API desde la app (Bearer + X-Device-ID)

def test_api_delete_sin_authorization_header(client):
    r = client.post("/api/account/delete")
    assert r.status_code == 401


def test_api_delete_token_invalido(client, monkeypatch):
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    def _invalido(token, check_revoked=True):
        raise real_auth.InvalidIdTokenError("token invalido")
    monkeypatch.setattr(account_deletion.auth, "verify_id_token", _invalido)
    r = client.post("/api/account/delete", headers={"Authorization": "Bearer lo-que-sea"})
    assert r.status_code == 401


def test_api_delete_token_valido_borra_solo_el_uid_del_token_no_el_del_cuerpo(client, monkeypatch):
    """Intento de eliminar OTRA cuenta: aunque el cuerpo mande un uid ajeno, el uid real de la
    eliminación debe salir SIEMPRE del token verificado, nunca del cuerpo de la petición."""
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.auth, "verify_id_token",
                         lambda token, check_revoked=True: {"uid": "uid-propio"})
    llamadas = []
    def _core(uid, device_id=None):
        llamadas.append((uid, device_id))
        return _resultado(True, uid=uid, device=True if device_id else None)
    monkeypatch.setattr(account_deletion, "delete_account_core", _core)
    r = client.post("/api/account/delete",
                     headers={"Authorization": "Bearer valido", "X-Device-ID": "dev-123"},
                     json={"uid": "uid-de-otra-victima"})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
    assert llamadas == [("uid-propio", "dev-123")]   # NUNCA "uid-de-otra-victima"


def test_api_delete_incompleto_responde_500_sin_filtrar_detalles(client, monkeypatch):
    """Sección 5 y 9F: si la eliminación queda incompleta, la API NUNCA responde ok=true, usa un
    código distinto de 200 y el cuerpo no expone uid/PII/detalles internos."""
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.auth, "verify_id_token",
                         lambda token, check_revoked=True: {"uid": "uid-incompleto"})
    monkeypatch.setattr(account_deletion, "delete_account_core",
                         lambda uid, device_id=None: _resultado(False, uid=uid))
    r = client.post("/api/account/delete", headers={"Authorization": "Bearer x"})
    assert r.status_code == 500
    cuerpo = r.get_json()
    assert cuerpo["ok"] is False
    assert "uid-incompleto" not in r.get_data(as_text=True)
    assert "token" not in cuerpo.get("error", "").lower()


def test_api_delete_rate_limit_por_ip(client, monkeypatch):
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.auth, "verify_id_token",
                         lambda token, check_revoked=True: {"uid": "uid-rl"})
    monkeypatch.setattr(account_deletion, "delete_account_core",
                         lambda uid, device_id=None: _resultado(True, uid=uid))
    maximo, _ = account_deletion.LIM_API_IP
    status = None
    for _ in range(maximo + 3):
        resp = client.post("/api/account/delete", headers={"Authorization": "Bearer x"})
        status = resp.status_code
    assert status == 429


# ---------------------------------------------------------------- delete_account_core: orden y fallos parciales

def test_delete_account_core_borra_firestore_kyc_auth_y_es_idempotente(monkeypatch, tmp_path):
    deleted = set()
    arbol = _arbol_usuario_de_prueba("uid-core", deleted)
    fake_client = FakeFirestoreClient({"uid-core": arbol}, deleted)
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.firestore, "client", lambda: fake_client)

    llamadas_auth = {"n": 0}
    def _delete_user(uid):
        llamadas_auth["n"] += 1
        if llamadas_auth["n"] > 1:
            raise real_auth.UserNotFoundError("ya no existe")
    monkeypatch.setattr(account_deletion.auth, "delete_user", _delete_user)

    # KYC: redirige didit_backend a un store temporal con una entrada para este uid.
    kyc_store = tmp_path / "kyc_store.json"
    monkeypatch.setenv("DIDIT_STORE", str(kyc_store))
    import importlib
    import didit_backend
    importlib.reload(didit_backend)   # recoge el DIDIT_STORE nuevo
    didit_backend._save({"uid-core": {"verificado": True, "nombre": "Prueba", "ts": 1}})

    resultado1 = account_deletion.delete_account_core("uid-core", device_id=None)
    assert resultado1 == {"uid": "uid-core", "firestore": True, "kyc": True, "auth": True,
                           "device": None, "completo": True}
    assert deleted == {
        "usuarios/uid-core",
        "usuarios/uid-core/telemetria/recorridos",
        "usuarios/uid-core/telemetria/recorridos/items/r1",
    }
    assert "uid-core" not in didit_backend._load()

    # Repetir la eliminación (idempotencia / 9G): auth.delete_user ahora lanza UserNotFoundError,
    # Firestore/KYC ya están vacíos -- todo debe seguir devolviendo éxito, nunca un error.
    deleted.clear()
    resultado2 = account_deletion.delete_account_core("uid-core", device_id=None)
    assert resultado2["completo"] is True
    assert resultado2["auth"] is True
    assert resultado2["firestore"] is True
    assert resultado2["kyc"] is True


def test_9a_fallo_firestore_bloquea_auth_y_no_reporta_completo(monkeypatch):
    """9A: Firestore falla -> Auth NO se borra -> completo=False -> se puede reintentar."""
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.firestore, "client", lambda: FakeFirestoreClientRompe())
    monkeypatch.setattr(account_deletion, "_borrar_kyc", lambda uid: True)
    llamado_auth = {"si": False}
    def _delete_user(uid):
        llamado_auth["si"] = True
    monkeypatch.setattr(account_deletion.auth, "delete_user", _delete_user)

    resultado = account_deletion.delete_account_core("uid-firestore-roto", device_id=None)
    assert resultado["firestore"] is False
    assert resultado["auth"] is None   # nunca se INTENTÓ, no solo "falló"
    assert resultado["completo"] is False
    assert llamado_auth["si"] is False


def test_9b_fallo_kyc_bloquea_auth_y_no_reporta_completo(monkeypatch):
    """9B: KYC falla -> Auth NO se borra -> completo=False."""
    _exito_trivial(monkeypatch, "uid-kyc-roto")
    monkeypatch.setattr(account_deletion, "_borrar_kyc", lambda uid: False)
    llamado_auth = {"si": False}
    def _delete_user(uid):
        llamado_auth["si"] = True
    monkeypatch.setattr(account_deletion.auth, "delete_user", _delete_user)

    resultado = account_deletion.delete_account_core("uid-kyc-roto", device_id=None)
    assert resultado["kyc"] is False
    assert resultado["auth"] is None
    assert resultado["completo"] is False
    assert llamado_auth["si"] is False


def test_borrar_kyc_no_confia_en_que_save_no_lance_excepcion(monkeypatch, tmp_path):
    """Sección 8: didit_backend._save() atrapa sus propias excepciones de escritura ('except
    Exception: pass') -- si _borrar_kyc confiara en que 'no lanzó' == 'se guardó', reportaría
    éxito aunque el archivo NUNCA se haya actualizado. Se verifica releyendo el archivo."""
    kyc_store = tmp_path / "kyc_store.json"
    import importlib
    import didit_backend
    monkeypatch.setenv("DIDIT_STORE", str(kyc_store))
    importlib.reload(didit_backend)
    didit_backend._save({"uid-silencioso": {"verificado": True, "nombre": "X", "ts": 1}})

    # Simula un fallo de escritura que didit_backend._save() se traga en silencio (como hace de
    # verdad su 'except Exception: pass'): no actualiza el archivo, no lanza nada.
    monkeypatch.setattr(didit_backend, "_save", lambda d: None)

    assert account_deletion._borrar_kyc("uid-silencioso") is False   # NO es un éxito silencioso
    assert "uid-silencioso" in didit_backend._load()   # sigue ahí: el fix detectó el fallo real


def test_9c_fallo_device_propio_bloquea_auth_y_no_reporta_completo(monkeypatch):
    """9C, política elegida: si el device_id SÍ pertenece a esta cuenta pero su borrado falla de
    verdad (excepción en device_alertas), eso es dato real de la cuenta que quedó sin borrar --
    bloquea Auth igual que Firestore/KYC, nunca una falsa respuesta de eliminación completa."""
    _exito_trivial(monkeypatch, "uid-device-roto")
    device_alertas.vincular_device("dev-roto", "uid-device-roto")
    device_alertas.registrar_token("dev-roto", "token-x")

    def _rompe(device_id):
        raise RuntimeError("disco lleno (prueba)")
    monkeypatch.setattr(device_alertas, "eliminar_todo_device", _rompe)
    llamado_auth = {"si": False}
    monkeypatch.setattr(account_deletion.auth, "delete_user", lambda uid: llamado_auth.update(si=True))

    resultado = account_deletion.delete_account_core("uid-device-roto", device_id="dev-roto")
    assert resultado["device"] is False
    assert resultado["auth"] is None
    assert resultado["completo"] is False
    assert llamado_auth["si"] is False


# ---------------------------------------------------------------- 9E: propiedad de dispositivo (X-Device-ID)

def test_9e_usuario_a_mas_device_a_puede_borrarlo(monkeypatch):
    _exito_trivial(monkeypatch, "uid-A")
    device_alertas.vincular_device("device-A", "uid-A")
    device_alertas.registrar_token("device-A", "token-A")

    resultado = account_deletion.delete_account_core("uid-A", device_id="device-A")
    assert resultado["device"] is True
    assert resultado["completo"] is True
    assert device_alertas.token_de("device-A") is None


def test_9e_usuario_a_no_puede_borrar_device_de_usuario_b(monkeypatch):
    _exito_trivial(monkeypatch, "uid-A")
    device_alertas.vincular_device("device-B", "uid-B")
    device_alertas.registrar_token("device-B", "token-B-secreto")

    resultado = account_deletion.delete_account_core("uid-A", device_id="device-B")
    assert resultado["device"] == "otro_usuario"
    # El device de B queda INTACTO: A no pudo tocarlo con solo conocer su device_id.
    assert device_alertas.token_de("device-B") == "token-B-secreto"
    # Pero la eliminación de LA CUENTA DE A sigue completándose con normalidad.
    assert resultado["completo"] is True


def test_9e_usuario_a_sin_device_continua_la_eliminacion(monkeypatch):
    _exito_trivial(monkeypatch, "uid-A-sin-device")
    resultado = account_deletion.delete_account_core("uid-A-sin-device", device_id=None)
    assert resultado["device"] is None
    assert resultado["completo"] is True


def test_9e_device_inexistente_es_idempotente(monkeypatch):
    """Ni datos ni vínculo registrado para este device_id -- no hay nada que proteger, borrar es
    un no-op seguro para cualquiera que lo pida."""
    _exito_trivial(monkeypatch, "uid-cualquiera")
    resultado = account_deletion.delete_account_core("uid-cualquiera", device_id="device-que-no-existe")
    assert resultado["device"] is True
    assert resultado["completo"] is True


def test_9e_device_historico_sin_vinculo_se_rechaza_sin_bloquear(monkeypatch):
    """Dispositivo CON datos (de antes de que existiera device_owner) pero sin vínculo
    registrado: no se puede demostrar que sea de este uid -- se rechaza, pero NO bloquea la
    eliminación de la cuenta que sí se autenticó correctamente (ver sección 10: no se
    'reconstruye' la propiedad de un device antiguo)."""
    _exito_trivial(monkeypatch, "uid-historico")
    device_alertas.registrar_token("device-historico", "token-viejo")   # sin vincular_device

    resultado = account_deletion.delete_account_core("uid-historico", device_id="device-historico")
    assert resultado["device"] == "no_vinculado"
    assert device_alertas.token_de("device-historico") == "token-viejo"   # intacto
    assert resultado["completo"] is True


# ---------------------------------------------------------------- límite de peticiones (unitario)

def test_limitar_fija_ventana_y_resetea():
    assert account_deletion._limitar("clave-prueba", 2, 3600) is True
    assert account_deletion._limitar("clave-prueba", 2, 3600) is True
    assert account_deletion._limitar("clave-prueba", 2, 3600) is False   # 3ra: ya se agotó
    # Forzar que la ventana ya haya expirado y vuelva a permitir.
    with account_deletion._conexion() as con:
        con.execute("UPDATE rate_limit SET ventana_inicio = 0 WHERE clave = ?", ("clave-prueba",))
        con.commit()
    assert account_deletion._limitar("clave-prueba", 2, 3600) is True


# ---------------------------------------------------------------- vínculo device_id <-> uid (app.py)

def test_vincular_device_desde_token_fcm_con_sesion(monkeypatch):
    """app.py:_vincular_device_si_hay_sesion -- si la petición a /device/token trae un ID token
    válido, el device_id queda vinculado al uid (lo usará account_deletion más adelante)."""
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.auth, "verify_id_token",
                         lambda token, check_revoked=True: {"uid": "uid-del-token"})
    import app as backend_app
    with backend_app.app.test_request_context(headers={"Authorization": "Bearer x"}):
        backend_app._vincular_device_si_hay_sesion("device-recien-vinculado")
    assert device_alertas.propietario_de("device-recien-vinculado") == "uid-del-token"


def test_vincular_device_sin_authorization_no_hace_nada(monkeypatch):
    """Sin header Authorization (el caso de HOY, cliente Android sin actualizar): no vincula
    nada, y sobre todo NO rompe la petición -- comportamiento idéntico al de antes."""
    import app as backend_app
    with backend_app.app.test_request_context():
        backend_app._vincular_device_si_hay_sesion("device-sin-sesion")
    assert device_alertas.propietario_de("device-sin-sesion") is None
