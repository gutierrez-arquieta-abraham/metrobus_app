# ============================================================
# PRUEBAS : test_push_metrobus_scrape_cache.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
# Prueba que leer_estado()/leer_tablas() de push_metrobus.py prefieran el caché del Lambda
# externo (metrobus_scrape_cache) y caigan a raspar directo (_leer_estado_directo/
# _leer_tablas_directo) solo cuando el caché no existe o está vencido.
# Ejecutar con:
#   pytest test_push_metrobus_scrape_cache.py -v
import push_metrobus as pm


def test_leer_estado_usa_cache_si_esta_fresco(monkeypatch):
    monkeypatch.setattr(pm.metrobus_scrape_cache, "leer",
                         lambda: {"estado": [{"linea": 4}], "eleva": [], "manten": []})
    llamado = {"directo": False}
    def _directo():
        llamado["directo"] = True
        return []
    monkeypatch.setattr(pm, "_leer_estado_directo", _directo)

    assert pm.leer_estado() == [{"linea": 4}]
    assert llamado["directo"] is False


def test_leer_estado_cae_a_directo_si_no_hay_cache(monkeypatch):
    monkeypatch.setattr(pm.metrobus_scrape_cache, "leer", lambda: None)
    monkeypatch.setattr(pm, "_leer_estado_directo", lambda: [{"linea": 7}])

    assert pm.leer_estado() == [{"linea": 7}]


def test_leer_tablas_usa_cache_si_esta_fresco(monkeypatch):
    monkeypatch.setattr(pm.metrobus_scrape_cache, "leer",
                         lambda: {"estado": [], "eleva": [{"linea": 2}], "manten": [{"linea": 3}]})
    llamado = {"directo": False}
    def _directo():
        llamado["directo"] = True
        return [], []
    monkeypatch.setattr(pm, "_leer_tablas_directo", _directo)

    eleva, manten = pm.leer_tablas()
    assert eleva == [{"linea": 2}]
    assert manten == [{"linea": 3}]
    assert llamado["directo"] is False


def test_leer_tablas_cae_a_directo_si_cache_vencido(monkeypatch):
    monkeypatch.setattr(pm.metrobus_scrape_cache, "leer", lambda: None)
    monkeypatch.setattr(pm, "_leer_tablas_directo", lambda: ([{"linea": 5}], []))

    eleva, manten = pm.leer_tablas()
    assert eleva == [{"linea": 5}]
    assert manten == []
