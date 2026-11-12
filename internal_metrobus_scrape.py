# -*- coding: utf-8 -*-
# ============================================================
# MÓDULO   : internal_metrobus_scrape.py   (Blueprint de Flask)
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Webhook que recibe el scraping de metrobus.cdmx.gob.mx ya parseado, mandado
# por el Lambda externo (repo privado geomb-metrobus-scraper-lambda, corre en
# mx-central-1 por IP mexicana). Solo guarda el dato en el caché compartido
# (metrobus_scrape_cache.py); push_metrobus.py lo consume desde ahí.
#
# Seguridad: token Bearer propio (MB_SCRAPE_TOKEN), DISTINTO de ADMIN_TOKEN a
# propósito -- si este token se filtra, solo permite alimentar el caché de
# scraping (datos públicos del gobierno, de solo lectura para la app), nunca
# las acciones de /admin/* (avisos manuales, trazos, versión mínima).
# ============================================================
import os

from flask import Blueprint, jsonify, request

import metrobus_scrape_cache

internal_scrape_bp = Blueprint("internal_metrobus_scrape", __name__)


def _token_ok():
    esperado = os.environ.get("MB_SCRAPE_TOKEN", "")
    if not esperado:
        return False   # sin token configurado, el endpoint se queda cerrado por defecto
    auth = request.headers.get("Authorization", "")
    dado = auth[7:] if auth.startswith("Bearer ") else ""
    return dado == esperado


@internal_scrape_bp.route("/internal/metrobus_scrape", methods=["POST"])
def recibir_scrape():
    if not _token_ok():
        return jsonify({"error": "no autorizado"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or "scraped_at" not in payload:
        return jsonify({"error": "payload invalido"}), 400

    metrobus_scrape_cache.guardar(payload)
    return jsonify({"ok": True})
