# ============================================================
# PRUEBAS : test_account_deletion.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# Pruebas de account_deletion.py (política de privacidad + eliminación de cuenta) y de la
# limpieza añadida a device_alertas.py. NO usa credenciales reales de Firebase ni manda correos
# reales: firebase_admin.auth y el envío SMTP se sustituyen por dobles de prueba (monkeypatch),
# y las bases SQLite se redirigen a archivos temporales (tmp_path) para no tocar ningún dato
# real ni dejar artefactos en el repo. Ejecutar con: pytest test_account_deletion.py -v
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


def _arbol_usuario_de_prueba(uid, deleted):
    """usuarios/{uid} con el esquema real confirmado: telemetria/recorridos/items/r1."""
    item = FakeDocRef(f"usuarios/{uid}/telemetria/recorridos/items/r1", {}, deleted)
    recorridos_doc = FakeDocRef(f"usuarios/{uid}/telemetria/recorridos", {"items": {"r1": item}}, deleted)
    return FakeDocRef(f"usuarios/{uid}", {"telemetria": {"recorridos": recorridos_doc}}, deleted)


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


# ---------------------------------------------------------------- confirmación por token (un solo uso)

def _emitir_token(uid):
    import hashlib, secrets
    token = secrets.token_urlsafe(16)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ahora = int(time.time())
    with account_deletion._conexion() as con:
        con.execute(
            "INSERT INTO deletion_tokens (token_hash, uid, created_ts, expires_ts, used_ts) "
            "VALUES (?, ?, ?, ?, NULL)",
            (token_hash, uid, ahora, ahora + account_deletion.TOKEN_TTL_SEGUNDOS),
        )
        con.commit()
    return token


def test_confirmar_get_token_valido_muestra_pantalla_sin_borrar(client, monkeypatch):
    borrado = {"llamado": False}
    monkeypatch.setattr(account_deletion, "delete_account_core",
                         lambda uid, device_id=None: borrado.update(llamado=True))
    token = _emitir_token("uid-1")
    r = client.get(f"/delete-account/confirmar?token={token}")
    assert r.status_code == 200
    assert b"Confirmar eliminaci" in r.data
    assert borrado["llamado"] is False   # el GET nunca ejecuta el borrado


def test_confirmar_post_token_valido_ejecuta_borrado_y_lo_consume(client, monkeypatch):
    llamadas = []
    monkeypatch.setattr(account_deletion, "delete_account_core",
                         lambda uid, device_id=None: llamadas.append(uid))
    token = _emitir_token("uid-2")
    r = client.post("/delete-account/confirmar", data={"token": token})
    assert r.status_code == 200
    assert b"eliminada" in r.data
    assert llamadas == ["uid-2"]


def test_confirmar_token_reutilizado_falla_la_segunda_vez(client, monkeypatch):
    monkeypatch.setattr(account_deletion, "delete_account_core", lambda uid, device_id=None: None)
    token = _emitir_token("uid-3")
    r1 = client.post("/delete-account/confirmar", data={"token": token})
    assert b"eliminada" in r1.data
    r2 = client.post("/delete-account/confirmar", data={"token": token})
    assert b"no es v" in r2.data or b"ya se us" in r2.data.lower() or b"usado" in r2.data.lower()


def test_confirmar_token_vencido(client, monkeypatch):
    monkeypatch.setattr(account_deletion, "delete_account_core", lambda uid, device_id=None: None)
    import hashlib, secrets
    token = secrets.token_urlsafe(16)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ahora = int(time.time())
    with account_deletion._conexion() as con:
        con.execute(
            "INSERT INTO deletion_tokens (token_hash, uid, created_ts, expires_ts, used_ts) "
            "VALUES (?, ?, ?, ?, NULL)",
            (token_hash, "uid-4", ahora - 10000, ahora - 1, ),
        )
        con.commit()
    r = client.get(f"/delete-account/confirmar?token={token}")
    assert b"venci" in r.data.lower()


def test_confirmar_token_invalido(client):
    r = client.get("/delete-account/confirmar?token=esto-no-existe")
    texto = r.get_data(as_text=True).lower()
    assert "no es v" in texto or "invalido" in texto


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
    monkeypatch.setattr(account_deletion, "delete_account_core",
                         lambda uid, device_id=None: llamadas.append((uid, device_id)))
    r = client.post("/api/account/delete",
                     headers={"Authorization": "Bearer valido", "X-Device-ID": "dev-123"},
                     json={"uid": "uid-de-otra-victima"})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
    assert llamadas == [("uid-propio", "dev-123")]   # NUNCA "uid-de-otra-victima"


def test_api_delete_rate_limit_por_ip(client, monkeypatch):
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.auth, "verify_id_token",
                         lambda token, check_revoked=True: {"uid": "uid-rl"})
    monkeypatch.setattr(account_deletion, "delete_account_core", lambda uid, device_id=None: None)
    maximo, _ = account_deletion.LIM_API_IP
    status = None
    for _ in range(maximo + 3):
        resp = client.post("/api/account/delete", headers={"Authorization": "Bearer x"})
        status = resp.status_code
    assert status == 429


# ---------------------------------------------------------------- delete_account_core (idempotencia + alcance)

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
    assert resultado1 == {"uid": "uid-core", "firestore": True, "kyc": True, "auth": True, "device": None}
    assert deleted == {
        "usuarios/uid-core",
        "usuarios/uid-core/telemetria/recorridos",
        "usuarios/uid-core/telemetria/recorridos/items/r1",
    }
    assert "uid-core" not in didit_backend._load()

    # Repetir la eliminación (idempotencia): auth.delete_user ahora lanza UserNotFoundError,
    # Firestore/KYC ya están vacíos -- todo debe seguir devolviendo éxito, nunca un error.
    deleted.clear()
    resultado2 = account_deletion.delete_account_core("uid-core", device_id=None)
    assert resultado2["auth"] is True
    assert resultado2["firestore"] is True
    assert resultado2["kyc"] is True


def test_delete_account_core_sin_device_id_no_toca_device_alertas(monkeypatch):
    deleted = set()
    arbol = _arbol_usuario_de_prueba("uid-sin-device", deleted)
    fake_client = FakeFirestoreClient({"uid-sin-device": arbol}, deleted)
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.firestore, "client", lambda: fake_client)
    monkeypatch.setattr(account_deletion.auth, "delete_user", lambda uid: None)

    def _no_debe_llamarse(device_id):
        raise AssertionError("no debió tocar device_alertas sin X-Device-ID")
    monkeypatch.setattr(device_alertas, "eliminar_todo_device", _no_debe_llamarse)

    resultado = account_deletion.delete_account_core("uid-sin-device", device_id=None)
    assert resultado["device"] is None


def test_delete_account_core_con_device_id_limpia_device_alertas(monkeypatch):
    deleted = set()
    arbol = _arbol_usuario_de_prueba("uid-con-device", deleted)
    fake_client = FakeFirestoreClient({"uid-con-device": arbol}, deleted)
    monkeypatch.setattr(account_deletion, "_init_firebase", lambda: None)
    monkeypatch.setattr(account_deletion.firestore, "client", lambda: fake_client)
    monkeypatch.setattr(account_deletion.auth, "delete_user", lambda uid: None)

    device_alertas.registrar_token("dev-xyz", "token-fcm-1")
    device_alertas.actualizar_alerta("dev-xyz", "1234", True, 500)
    device_alertas.actualizar_ubicacion("dev-xyz", 19.4, -99.1, int(time.time()))

    resultado = account_deletion.delete_account_core("uid-con-device", device_id="dev-xyz")
    assert resultado["device"] is True
    assert device_alertas.token_de("dev-xyz") is None
    assert device_alertas.alertas_activas_de("dev-xyz") == []
    assert device_alertas.ubicacion_de("dev-xyz") is None


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
