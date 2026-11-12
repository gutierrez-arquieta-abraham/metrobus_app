# -*- coding: utf-8 -*-
# ============================================================
# MÓDULO   : metrobus_scrape_cache.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Caché del último scraping de Metrobús entregado por el Lambda externo
# (ver repo privado geomb-metrobus-scraper-lambda, mx-central-1 -- IP
# mexicana real, el sitio del gobierno responde mal desde fuera de México).
#
# Escritura: internal_metrobus_scrape.py (el webhook que recibe el POST del
# Lambda). Lectura: push_metrobus.py (leer_estado()/leer_tablas()), que cae
# de vuelta a raspar ÉL MISMO (con su propio respaldo de proxies) si el
# caché no existe o está más viejo que MB_SCRAPE_MAX_AGE -- así un Lambda
# caído/sin presupuesto no deja ciego al backend, solo lo vuelve más lento.
# ============================================================
import json
import os
import time

# OJO: este módulo se despliega en DOS directorios (${WEBDIR}/metrobus_app y /home/ubuntu/,
# ver deploy_backend.sh) -- un default relativo a __file__ resolvería a una ruta DISTINTA según
# desde dónde se importe, y el servicio web (que escribe) y metrobus-push (que lee) nunca se
# verían entre sí. Por eso el default es una ruta ABSOLUTA fija al directorio del servicio web,
# igual que ya hace AFECT_MTB_OUT en push_metrobus.py para el mismo problema.
CACHE_PATH = os.environ.get("MB_SCRAPE_CACHE", "").split("#", 1)[0].strip() \
    or "/home/ubuntu/metrobus_app/data/mb_scrape_cache.json"
# Generoso a propósito: el Lambda se dispara cada 60 s (ver su README), 180 s = 3 ciclos
# perdidos antes de considerar el caché muerto y volver al scraping directo.
MAX_AGE_SEG = float(os.environ.get("MB_SCRAPE_MAX_AGE", "180").split("#", 1)[0].strip() or "180")


def guardar(payload: dict):
    """Escritura atómica (tmp + rename), igual que el resto de los cachés de este backend."""
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, CACHE_PATH)


def leer():
    """None si no hay caché o está vencido (más viejo que MAX_AGE_SEG) -- el llamador debe
    caer a su propio scraping directo en ese caso, no asumir que el dato vacío es válido."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    scraped_at = data.get("scraped_at")
    if not isinstance(scraped_at, (int, float)):
        return None
    if time.time() - scraped_at > MAX_AGE_SEG:
        return None
    return data
