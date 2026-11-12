# ============================================================
# PRUEBAS : test_internal_metrobus_scrape.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
# Prueba POST /internal/metrobus_scrape: rechaza sin token/con token incorrecto, acepta con
# el MB_SCRAPE_TOKEN correcto y guarda en el caché (metrobus_scrape_cache), y rechaza un
# payload sin 'scraped_at'. Ejecutar con:
#   pytest test_internal_metrobus_scrape.py -v
import os
import tempfile
import time

import pytest

os.environ.setdefault("MB_SCRAPE_TOKEN", "test-scrape-token")

import app
import metrobus_scrape_cache


@pytest.fixture(autouse=True)
def _cache_path_aislado(monkeypatch):
    # Ver el mismo comentario en test_metrobus_scrape_cache.py: CACHE_PATH es compartido
    # entre ambos archivos de prueba, monkeypatch por prueba evita que se pisen.
    monkeypatch.setattr(metrobus_scrape_cache, "CACHE_PATH",
                         os.path.join(tempfile.mkdtemp(), "mb_scrape_cache.json"))


def _client():
    return app.app.test_client()


def _payload():
    return {"scraped_at": int(time.time()), "estado": [{"linea": 4, "estado": "x"}], "eleva": [], "manten": []}


def test_sin_token_rechaza():
    r = _client().post("/internal/metrobus_scrape", json=_payload())
    assert r.status_code == 401


def test_token_incorrecto_rechaza():
    r = _client().post("/internal/metrobus_scrape", json=_payload(),
                        headers={"Authorization": "Bearer lo-que-sea"})
    assert r.status_code == 401


def test_token_correcto_acepta_y_guarda():
    r = _client().post("/internal/metrobus_scrape", json=_payload(),
                        headers={"Authorization": "Bearer test-scrape-token"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert metrobus_scrape_cache.leer()["estado"][0]["linea"] == 4


def test_payload_sin_scraped_at_se_rechaza():
    r = _client().post("/internal/metrobus_scrape", json={"estado": []},
                        headers={"Authorization": "Bearer test-scrape-token"})
    assert r.status_code == 400
