#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rellena saltos grandes en las polilíneas de data/routes.json usando el trazado real
de GeoMB (segmentos.json) como fuente confiable.

Por qué: index.html dibuja cada ruta['shape'] como una sola L.polyline — Leaflet une
puntos consecutivos con una línea recta sin importar qué tan lejos estén, así que un
salto grande entre dos puntos se ve como un "corte" atravesando manzanas en diagonal en
vez de seguir la calle real (reportado por el usuario cerca de La Raza L3, Buenavista
L1/L3/L4 y Etiopía L2/L3). La causa es que el shape original (derivado del GTFS oficial)
trae puntos muy espaciados en esos tramos.

Cómo lo arregla: por cada salto > `UMBRAL` metros en una ruta de Metrobús (líneas 1, 2,
3, 4 por ahora), busca en los `segmentos` de esa línea en GeoMB el tramo que pase cerca
(<150m) de AMBOS extremos del salto, prefiriendo el que aporte más camino real entre
ellos (para no quedarse con un tramo degenerado que solo roza los mismos dos puntos), y
lo inserta en medio. Los saltos que ningún tramo de GeoMB cubre se dejan igual — no hay
de dónde tomar el relleno.

Uso:
    python3 tools/rellenar_saltos_rutas.py [ruta_al_checkout_de_GeoMB]

Si no se pasa ruta, usa la variable de entorno GEOMB_REPO o, en su defecto, "../GeoMB"
relativo a este repo. Vuelve a correr este script si se detectan más cortes en el mapa
del panel, o si el trazado de GeoMB (segmentos.json) se actualiza.
"""
import json
import math
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES_PATH = os.path.join(APP_DIR, "data", "routes.json")
LINEAS_A_REVISAR = {1, 2, 3, 4}
UMBRAL_SALTO_M = 250
RADIO_MATCH_M = 150


def haversine(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def nearest_idx(tramo, punto):
    best_i, best_d = None, float("inf")
    for i, p in enumerate(tramo):
        d = haversine(p, punto)
        if d < best_d:
            best_d, best_i = d, i
    return best_i, best_d


def path_len(pts):
    return sum(haversine(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def segmentos_por_linea(geomb_segm):
    return {e["numero"]: e["segmentos"] for e in geomb_segm}


def relleno_para(tramos, p1, p2):
    """Entre todos los tramos de una línea, el que pase cerca de p1 Y p2 aportando
    MÁS camino real entre ambos (no solo el más cercano a los extremos)."""
    candidatos = []
    for tramo in tramos:
        i1, d1 = nearest_idx(tramo, p1)
        i2, d2 = nearest_idx(tramo, p2)
        if d1 > RADIO_MATCH_M or d2 > RADIO_MATCH_M or i1 == i2:
            continue
        a, b = (i1, i2) if i1 <= i2 else (i2, i1)
        candidato = tramo[a:b + 1]
        if i1 > i2:
            candidato = list(reversed(candidato))
        candidatos.append(candidato)
    if not candidatos:
        return None
    candidatos.sort(key=lambda c: -path_len(c))
    return candidatos[0]


def reparar_ruta(r, tramos):
    shape = r.get("shape") or []
    if len(shape) < 2:
        return shape, 0, 0
    nueva = [shape[0]]
    parches = sin_relleno = 0
    for i in range(1, len(shape)):
        p1, p2 = shape[i - 1], shape[i]
        if haversine(p1, p2) > UMBRAL_SALTO_M:
            relleno = relleno_para(tramos, p1, p2)
            if relleno:
                nueva.extend(relleno)
                parches += 1
            else:
                sin_relleno += 1
        nueva.append(p2)
    return nueva, parches, sin_relleno


def main():
    if len(sys.argv) > 1:
        geomb = sys.argv[1]
    else:
        geomb = os.environ.get("GEOMB_REPO") or os.path.join(APP_DIR, "..", "GeoMB")
    geomb = os.path.abspath(geomb)
    segm_path = os.path.join(geomb, "app", "src", "main", "assets", "segmentos.json")
    with open(segm_path, encoding="utf-8") as f:
        segmentos = segmentos_por_linea(json.load(f))

    with open(ROUTES_PATH, encoding="utf-8") as f:
        routes = json.load(f)

    total_parches = total_sin = 0
    tocadas = []
    for r in routes:
        try:
            numero = int(r["line"])
        except (KeyError, ValueError):
            continue
        if numero not in LINEAS_A_REVISAR or numero not in segmentos:
            continue
        nueva, parches, sin_relleno = reparar_ruta(r, segmentos[numero])
        if parches or sin_relleno:
            r["shape"] = nueva
            total_parches += parches
            total_sin += sin_relleno
            tocadas.append((r["route_id"], r["line"], parches, sin_relleno))

    with open(ROUTES_PATH, "w", encoding="utf-8") as f:
        json.dump(routes, f, ensure_ascii=False)

    print(f"OK: {len(tocadas)} rutas modificadas, {total_parches} saltos rellenados, "
          f"{total_sin} sin relleno (sin tramo de GeoMB cerca) -> {ROUTES_PATH}")
    for route_id, linea, parches, sin_relleno in tocadas:
        print(f"  {route_id} (L{linea}): +{parches} rellenos, {sin_relleno} sin cubrir")


if __name__ == "__main__":
    main()
