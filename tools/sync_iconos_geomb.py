#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Copia al mapa web los elementos gráficos de la app Android (logo + iconos de línea de
Metrobús + pictogramas de estación de Mexibús/Mexicable), desde el checkout de GeoMB --
mismo checkout/convención que tools/sync_mexibus_mapa.py.

Los pictogramas de estación de Metrobús (ic_est_<línea>_<n>) NO se copian: esa numeración la
arma en vivo Iconos.java a partir del índice de la estación en la RUTA (no hay un campo
"icono" fijo por estación como en mexibus.json), y el stops.json que ya usa este mapa viene
de una fuente distinta (GTFS oficial de Sonda, no de los assets de GeoMB) -- no hay forma
confiable de emparejar un stop de ahí con su pictograma de GeoMB. Metrobús usa en su lugar el
logo de línea (icons/lineas/linea_N.png) como elemento gráfico, no un pictograma por estación.

Uso:
    python3 tools/sync_iconos_geomb.py [ruta_al_checkout_de_GeoMB]

Si no se pasa ruta, usa la variable de entorno GEOMB_REPO o, en su defecto, "../GeoMB"
relativo a este repo. Volver a correr este script si cambian esos drawables en GeoMB, o si
data/mexibus_mapa.json (sync_mexibus_mapa.py) empieza a referenciar pictogramas nuevos.
"""
import json
import os
import shutil
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINEAS_METROBUS = range(1, 8)


def main():
    if len(sys.argv) > 1:
        geomb = sys.argv[1]
    else:
        geomb = os.environ.get("GEOMB_REPO") or os.path.join(APP_DIR, "..", "GeoMB")
    geomb = os.path.abspath(geomb)
    res = os.path.join(geomb, "app", "src", "main", "res")

    destino_app = os.path.join(APP_DIR, "icons", "app")
    destino_lineas = os.path.join(APP_DIR, "icons", "lineas")
    destino_est = os.path.join(APP_DIR, "icons", "estaciones")
    for d in (destino_app, destino_lineas, destino_est):
        os.makedirs(d, exist_ok=True)

    # Logo de la app (ícono de lanzador, resolución más alta disponible).
    shutil.copy(os.path.join(res, "mipmap-xxxhdpi", "ic_launcher.webp"),
                os.path.join(destino_app, "ic_launcher.webp"))

    # Logos de línea de Metrobús (1-7), resolución más alta disponible.
    n_lineas = 0
    for n in LINEAS_METROBUS:
        origen = os.path.join(res, "drawable-xxxhdpi", f"linea_{n}.png")
        if os.path.exists(origen):
            shutil.copy(origen, os.path.join(destino_lineas, f"linea_{n}.png"))
            n_lineas += 1

    # Pictogramas de estación de Mexibús/Mexicable -- solo los que data/mexibus_mapa.json
    # realmente referencia (evita copiar los ~349 que existen en GeoMB sin usarse aquí).
    mapa_path = os.path.join(APP_DIR, "data", "mexibus_mapa.json")
    n_est = 0
    if os.path.exists(mapa_path):
        with open(mapa_path, encoding="utf-8") as f:
            mapa = json.load(f)
        iconos = {e["icono"] for l in mapa.get("lineas", []) for e in l.get("estaciones", [])
                  if e.get("icono")}
        faltantes = []
        for icono in sorted(iconos):
            origen = os.path.join(res, "drawable", f"{icono}.png")
            if os.path.exists(origen):
                shutil.copy(origen, os.path.join(destino_est, f"{icono}.png"))
                n_est += 1
            else:
                faltantes.append(icono)
        if faltantes:
            print(f"[!] {len(faltantes)} pictogramas referenciados sin archivo en GeoMB: {faltantes[:10]}")
    else:
        print(f"[!] No existe {mapa_path} -- corre sync_mexibus_mapa.py primero para saber qué pictogramas copiar.")

    print(f"OK: logo app, {n_lineas} logos de línea, {n_est} pictogramas de estación -> {APP_DIR}/icons/")


if __name__ == "__main__":
    main()
