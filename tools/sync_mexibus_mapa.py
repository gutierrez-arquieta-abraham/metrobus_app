#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Genera data/mexibus_mapa.json (rutas + estaciones de Mexibús y Mexicable, para el mapa
web) a partir de mexibus.json de GeoMB (repo de Android) -- mismo checkout/convención que
tools/extraer_estaciones_geomb.py.

Solo incluye las líneas "reales" (ordinario 101-104, ramales 111-113, Mexicable 201-202):
las variantes exprés (121-124) comparten corredor y estaciones con su ordinario, así que
agregarlas al mapa solo duplicaría trazos sin aportar nada nuevo.

Uso:
    python3 tools/sync_mexibus_mapa.py [ruta_al_checkout_de_GeoMB]

Si no se pasa ruta, usa la variable de entorno GEOMB_REPO o, en su defecto, "../GeoMB"
relativo a este repo. Volver a correr este script cuando mexibus.json cambie en GeoMB
(estación nueva, ruta corregida, etc.) para mantener el mapa alineado.
"""
import json
import os
import re
import sys

LINEAS_INCLUIDAS = (101, 102, 103, 104, 111, 112, 113, 201, 202)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sin_prefijo(nombre):
    """Quita 'MXB '/'MXC ' del nombre (la app los guarda pero no los muestra) -- mismo
    criterio que extraer_estaciones_geomb.sin_prefijo(), sin el recorte del sufijo
    "(conexión ...)": aquí sí queremos distinguir esas paradas en el mapa."""
    return re.sub(r"^(MXB|MXC)\s+", "", nombre or "").strip()


def main():
    if len(sys.argv) > 1:
        geomb = sys.argv[1]
    else:
        geomb = os.environ.get("GEOMB_REPO") or os.path.join(APP_DIR, "..", "GeoMB")
    geomb = os.path.abspath(geomb)
    ruta_json = os.path.join(geomb, "app", "src", "main", "assets", "mexibus.json")

    with open(ruta_json, encoding="utf-8") as f:
        data = json.load(f)

    lineas = []
    for l in data.get("lineas", []):
        if l.get("numero") not in LINEAS_INCLUIDAS:
            continue
        lineas.append({
            "numero": l["numero"],
            "nombre": l.get("nombre", ""),
            "color": l.get("color", "#888888"),
            "estaciones": [
                {"nombre": sin_prefijo(e.get("n", "")), "lat": e["lat"], "lon": e["lon"],
                 "icono": e.get("icono", "")}
                for e in l.get("estaciones", [])
            ],
            "ruta": l.get("ruta", []),
        })

    faltantes = [n for n in LINEAS_INCLUIDAS if n not in {l["numero"] for l in lineas}]
    if faltantes:
        sys.exit(f"[sync_mexibus_mapa] faltan líneas en mexibus.json: {faltantes}")

    destino = os.path.join(APP_DIR, "data", "mexibus_mapa.json")
    with open(destino, "w", encoding="utf-8") as f:
        json.dump({"lineas": lineas}, f, ensure_ascii=False)

    total_est = sum(len(l["estaciones"]) for l in lineas)
    total_pts = sum(len(l["ruta"]) for l in lineas)
    print(f"OK: {len(lineas)} líneas, {total_est} estaciones, {total_pts} puntos de ruta -> {destino}")


if __name__ == "__main__":
    main()
