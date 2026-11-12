# ============================================================
# PRUEBAS : test_metrobus_scrape_cache.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
# Ejecutar con:
#   pytest test_metrobus_scrape_cache.py -v
import os
import tempfile
import time

import pytest

import metrobus_scrape_cache as cache


@pytest.fixture(autouse=True)
def _cache_path_aislado(monkeypatch):
    # CACHE_PATH es un atributo de MÓDULO compartido -- si dos archivos de prueba lo
    # sobrescriben a nivel de import, pytest importa todos ANTES de correr ninguno (ver
    # test_admin_trazos.py) y el último import gana para TODOS. monkeypatch por prueba
    # evita esa colisión entre test_metrobus_scrape_cache.py e
    # test_internal_metrobus_scrape.py (que también usa este módulo).
    monkeypatch.setattr(cache, "CACHE_PATH", os.path.join(tempfile.mkdtemp(), "mb_scrape_cache.json"))


def test_sin_archivo_devuelve_none():
    assert cache.leer() is None


def test_guardar_y_leer_redondo():
    payload = {"scraped_at": int(time.time()), "estado": [{"linea": 4}], "eleva": [], "manten": []}
    cache.guardar(payload)
    leido = cache.leer()
    assert leido == payload


def test_cache_vencido_devuelve_none():
    cache.guardar({"scraped_at": time.time() - cache.MAX_AGE_SEG - 1, "estado": [], "eleva": [], "manten": []})
    assert cache.leer() is None


def test_payload_sin_scraped_at_se_ignora():
    cache.guardar({"estado": []})
    assert cache.leer() is None
