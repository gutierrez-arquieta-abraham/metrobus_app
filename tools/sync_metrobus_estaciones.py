#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Genera data/metrobus_estaciones.json (estaciones de Metrobús L1-L7, CON su pictograma real
ic_est_<línea>_<n>) a partir de estaciones.json de GeoMB -- mismo checkout/convención que
tools/sync_mexibus_mapa.py.

A diferencia de data/stops.json (viene del GTFS oficial de Sonda, sin pictograma), este archivo
sí trae el campo "icono" porque GeoMB lo guarda explícito por estación en estaciones.json (no
hace falta adivinar/emparejar nada). Reemplaza los puntos blancos genéricos del mapa por los
pictogramas reales -- lo que pidió el usuario para que la web se vea "casi idéntica" a la app.

Uso:
    python3 tools/sync_metrobus_estaciones.py [ruta_al_checkout_de_GeoMB]

Si no se pasa ruta, usa la variable de entorno GEOMB_REPO o, en su defecto, "../GeoMB"
relativo a este repo. Volver a correr este script cuando estaciones.json cambie en GeoMB.
"""
import json
import os
import re
import sys

LINEAS_METROBUS = range(1, 8)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sin_prefijo(nombre):
    return re.sub(r"^(MXB|MXC)\s+", "", nombre or "").strip()


def main():
    if len(sys.argv) > 1:
        geomb = sys.argv[1]
    else:
        geomb = os.environ.get("GEOMB_REPO") or os.path.join(APP_DIR, "..", "GeoMB")
    geomb = os.path.abspath(geomb)
    ruta_json = os.path.join(geomb, "app", "src", "main", "assets", "estaciones.json")

    with open(ruta_json, encoding="utf-8") as f:
        data = json.load(f)

    lineas = []
    for l in data.get("lineas", []):
        if l.get("numero") not in LINEAS_METROBUS:
            continue
        lineas.append({
            "numero": l["numero"],
            "estaciones": [
                {"nombre": sin_prefijo(e.get("n", "")), "lat": e["lat"], "lon": e["lon"],
                 "icono": e.get("icono", "")}
                for e in l.get("estaciones", []) if not e.get("soloMapa")
            ],
        })

    faltantes = [n for n in LINEAS_METROBUS if n not in {l["numero"] for l in lineas}]
    if faltantes:
        sys.exit(f"[sync_metrobus_estaciones] faltan líneas en estaciones.json: {faltantes}")

    destino = os.path.join(APP_DIR, "data", "metrobus_estaciones.json")
    with open(destino, "w", encoding="utf-8") as f:
        json.dump({"lineas": lineas}, f, ensure_ascii=False)

    total = sum(len(l["estaciones"]) for l in lineas)
    print(f"OK: {len(lineas)} líneas, {total} estaciones -> {destino}")


if __name__ == "__main__":
    main()
