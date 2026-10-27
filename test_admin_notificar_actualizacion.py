# ============================================================
# PRUEBAS : test_admin_notificar_actualizacion.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# Prueba que POST /admin/notificar_actualizacion acepte el MISMO mTLS (certificado cliente,
# vía nginx :8443 -> X-Client-Verify) que ya usa /admin/afectacion (admin_afect._token_ok),
# además del ADMIN_TOKEN de siempre como respaldo. No manda FCM real: enviar_actualizacion()
# se sustituye por un doble de prueba (monkeypatch) para no requerir credenciales de Firebase.
# Ejecutar con:
#   pytest test_admin_notificar_actualizacion.py -v
import os

os.environ.setdefault("ADMIN_TOKEN", "test-token")

import app


def _client(monkeypatch):
    llamadas = []
    monkeypatch.setattr(app, "enviar_actualizacion", lambda titulo, texto: llamadas.append((titulo, texto)))
    return app.app.test_client(), llamadas


def test_sin_credenciales_rechaza(monkeypatch):
    client, llamadas = _client(monkeypatch)
    r = client.post("/admin/notificar_actualizacion", json={"titulo": "x", "texto": "y"})
    assert r.status_code == 403
    assert llamadas == []


def test_mtls_valido_acepta(monkeypatch):
    client, llamadas = _client(monkeypatch)
    r = client.post("/admin/notificar_actualizacion", json={"titulo": "x", "texto": "y"},
                     headers={"X-Client-Verify": "SUCCESS"})
    assert r.status_code == 200
    assert llamadas == [("x", "y")]


def test_token_correcto_acepta_como_respaldo(monkeypatch):
    client, llamadas = _client(monkeypatch)
    r = client.post("/admin/notificar_actualizacion", json={"titulo": "x", "texto": "y"},
                     headers={"X-Client-Verify": "FAILED", "X-Admin-Token": "test-token"})
    assert r.status_code == 200
    assert llamadas == [("x", "y")]


def test_token_incorrecto_rechaza(monkeypatch):
    client, llamadas = _client(monkeypatch)
    r = client.post("/admin/notificar_actualizacion", json={"titulo": "x", "texto": "y"},
                     headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 403
    assert llamadas == []
