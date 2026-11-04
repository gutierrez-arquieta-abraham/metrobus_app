# ============================================================
# PRUEBAS : test_admin_version.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# Prueba la versión mínima obligatoria: POST /admin/version/guardar (mismo mTLS/token que
# /admin/afectacion), GET /data/version.json (lo que lee ActualizacionObligatoria en GeoMB al
# arrancar) y el 'before_request' gate_version_minima (426 para clientes con X-App-Version
# atrasado). Usa un VERSION_APP_FILE temporal para no tocar datos reales.
# Ejecutar con:
#   pytest test_admin_version.py -v
import os
import tempfile

os.environ.setdefault("ADMIN_TOKEN", "test-token")

import app
import admin_version

admin_version.VERSION_FILE = os.path.join(tempfile.mkdtemp(), "version_app.json")


def _client():
    return app.app.test_client()


def test_sin_configurar_minima_es_cero():
    r = _client().get("/data/version.json")
    assert r.status_code == 200
    assert r.get_json()["minima"] == 0


def test_guardar_sin_credenciales_rechaza():
    r = _client().post("/admin/version/guardar", json={"minima": 30})
    assert r.status_code == 403


def test_guardar_con_token_y_se_refleja_en_data_version():
    client = _client()
    r = client.post("/admin/version/guardar", json={"minima": 30}, headers={"X-Admin-Token": "test-token"})
    assert r.status_code == 200
    assert r.get_json()["minima"] == 30

    r = client.get("/data/version.json")
    assert r.get_json()["minima"] == 30


def test_llamada_sin_header_nunca_se_bloquea():
    client = _client()
    client.post("/admin/version/guardar", json={"minima": 30}, headers={"X-Admin-Token": "test-token"})
    r = client.get("/data/vehicles.json")
    assert r.status_code != 426


def test_llamada_con_version_vieja_responde_426():
    client = _client()
    client.post("/admin/version/guardar", json={"minima": 30}, headers={"X-Admin-Token": "test-token"})
    r = client.get("/data/vehicles.json", headers={"X-App-Version": "10"})
    assert r.status_code == 426
    j = r.get_json()
    assert j["error"] == "actualizacion_requerida"
    assert j["minima"] == 30
    assert j["actual"] == 10


def test_llamada_con_version_igual_o_mayor_no_se_bloquea():
    client = _client()
    client.post("/admin/version/guardar", json={"minima": 30}, headers={"X-Admin-Token": "test-token"})
    assert client.get("/data/vehicles.json", headers={"X-App-Version": "30"}).status_code != 426
    assert client.get("/data/vehicles.json", headers={"X-App-Version": "31"}).status_code != 426


def test_data_version_json_exento_del_gate_aunque_venga_vieja():
    client = _client()
    client.post("/admin/version/guardar", json={"minima": 30}, headers={"X-Admin-Token": "test-token"})
    r = client.get("/data/version.json", headers={"X-App-Version": "1"})
    assert r.status_code == 200


def test_admin_exento_del_gate_aunque_venga_vieja():
    client = _client()
    client.post("/admin/version/guardar", json={"minima": 30}, headers={"X-Admin-Token": "test-token"})
    r = client.get("/admin/version", headers={"X-App-Version": "1"})
    assert r.status_code == 200


def test_minima_cero_desactiva_el_bloqueo():
    client = _client()
    client.post("/admin/version/guardar", json={"minima": 30}, headers={"X-Admin-Token": "test-token"})
    client.post("/admin/version/guardar", json={"minima": 0}, headers={"X-Admin-Token": "test-token"})
    r = client.get("/data/vehicles.json", headers={"X-App-Version": "1"})
    assert r.status_code != 426


def test_header_no_numerico_se_ignora_sin_romper():
    client = _client()
    client.post("/admin/version/guardar", json={"minima": 30}, headers={"X-Admin-Token": "test-token"})
    r = client.get("/data/vehicles.json", headers={"X-App-Version": "no-es-un-numero"})
    assert r.status_code != 426
