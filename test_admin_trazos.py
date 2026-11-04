# ============================================================
# PRUEBAS : test_admin_trazos.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# Prueba que POST /admin/trazos/guardar acepte el MISMO mTLS (X-Client-Verify) y el
# MISMO ADMIN_TOKEN de respaldo que /admin/afectacion (admin_afect._token_ok, reusado
# tal cual en admin_trazos.py), que valide lo mínimo (nombre, >=2 estaciones, >=2 puntos
# de ruta) y que /data/rutas_temporales.json sirva exactamente lo que se guardó/eliminó.
# Usa un archivo temporal para no tocar datos reales -- se fuerza como ATRIBUTO del módulo
# (no solo la env var RUTAS_TEMP_FILE) porque admin_trazos la lee UNA vez al importarse, y si
# otro test_*.py ya importó 'app' antes (pytest importa todos los módulos de prueba antes de
# correr ninguno), la env var puesta aquí llega tarde y no tiene efecto.
# Ejecutar con:
#   pytest test_admin_trazos.py -v
import os
import tempfile

os.environ.setdefault("ADMIN_TOKEN", "test-token")

import app
import admin_trazos

admin_trazos.RUTAS_TEMP_FILE = os.path.join(tempfile.mkdtemp(), "rutas_temporales.json")


def _client():
    return app.app.test_client()


def _cuerpo(numero=11290):
    return {
        "numero": numero, "nombre": "Desvío de prueba", "nota": "nota",
        "duracion_h": 2,
        "estaciones": [{"nombre": "A", "lat": 19.1, "lon": -99.1},
                        {"nombre": "B", "lat": 19.2, "lon": -99.2}],
        "ruta": [[19.1, -99.1], [19.15, -99.15], [19.2, -99.2]],
    }


def test_sin_credenciales_rechaza():
    r = _client().post("/admin/trazos/guardar", json=_cuerpo())
    assert r.status_code == 403


def test_mtls_valido_acepta_y_publica():
    client = _client()
    r = client.post("/admin/trazos/guardar", json=_cuerpo(11291),
                     headers={"X-Client-Verify": "SUCCESS"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    pub = client.get("/data/rutas_temporales.json").get_json()
    assert any(p["numero"] == 11291 for p in pub["rutas"])


def test_token_correcto_acepta_como_respaldo():
    r = _client().post("/admin/trazos/guardar", json=_cuerpo(11292),
                        headers={"X-Admin-Token": "test-token"})
    assert r.status_code == 200


def test_token_incorrecto_rechaza():
    r = _client().post("/admin/trazos/guardar", json=_cuerpo(11293),
                        headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 403


def test_valida_minimo_dos_estaciones_y_dos_puntos():
    client = _client()
    cuerpo = _cuerpo(11294)
    cuerpo["estaciones"] = cuerpo["estaciones"][:1]
    r = client.post("/admin/trazos/guardar", json=cuerpo, headers={"X-Admin-Token": "test-token"})
    assert r.status_code == 400

    cuerpo = _cuerpo(11295)
    cuerpo["ruta"] = cuerpo["ruta"][:1]
    r = client.post("/admin/trazos/guardar", json=cuerpo, headers={"X-Admin-Token": "test-token"})
    assert r.status_code == 400


def test_eliminar_quita_del_endpoint_publico():
    client = _client()
    client.post("/admin/trazos/guardar", json=_cuerpo(11296), headers={"X-Admin-Token": "test-token"})
    r = client.post("/admin/trazos/guardar", json={"numero": 11296, "accion": "eliminar"},
                     headers={"X-Admin-Token": "test-token"})
    assert r.status_code == 200
    assert r.get_json()["eliminado"] is True

    pub = client.get("/data/rutas_temporales.json").get_json()
    assert not any(p["numero"] == 11296 for p in pub["rutas"])


def test_upsert_reemplaza_el_mismo_numero():
    client = _client()
    client.post("/admin/trazos/guardar", json=_cuerpo(11297), headers={"X-Admin-Token": "test-token"})
    otro = _cuerpo(11297)
    otro["nombre"] = "Versión 2"
    client.post("/admin/trazos/guardar", json=otro, headers={"X-Admin-Token": "test-token"})

    pub = client.get("/data/rutas_temporales.json").get_json()
    coincidencias = [p for p in pub["rutas"] if p["numero"] == 11297]
    assert len(coincidencias) == 1
    assert coincidencias[0]["nombre"] == "Versión 2"


def test_pagina_get_no_requiere_credenciales():
    r = _client().get("/admin/trazos")
    assert r.status_code == 200
    assert "Editor de trazos" in r.get_data(as_text=True)
