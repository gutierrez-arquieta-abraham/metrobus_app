#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Copia al mapa web los elementos gráficos de la app Android: logo, logos de línea de
Metrobús, y los pictogramas reales de estación de los 3 sistemas (Metrobús/Mexibús/
Mexicable), desde el checkout de GeoMB -- mismo checkout/convención que
tools/sync_mexibus_mapa.py y tools/sync_metrobus_estaciones.py.

Los pictogramas de Metrobús (ic_est_<línea>_<n>) salen de data/metrobus_estaciones.json (ver
sync_metrobus_estaciones.py), que a su vez viene de estaciones.json de GeoMB -- ESE archivo
(no lineas.json, que no trae el campo "icono") es la fuente real, con nombre/posición/icono
explícitos por estación; no hace falta adivinar ni emparejar contra el stops.json del GTFS
oficial de Sonda que ya usa este mapa (son dos cosas separadas: ese sigue sirviendo para las
rutas trazadas con datos de Sonda en vivo).

Uso:
    python3 tools/sync_iconos_geomb.py [ruta_al_checkout_de_GeoMB]

Si no se pasa ruta, usa la variable de entorno GEOMB_REPO o, en su defecto, "../GeoMB"
relativo a este repo. Volver a correr este script si cambian esos drawables en GeoMB, o si
data/mexibus_mapa.json / data/metrobus_estaciones.json empiezan a referenciar pictogramas
nuevos (corre antes sync_mexibus_mapa.py y sync_metrobus_estaciones.py para regenerarlos).
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

    # Pictogramas de estación -- solo los que data/mexibus_mapa.json y
    # data/metrobus_estaciones.json REALMENTE referencian (evita copiar los ~700 que existen
    # en GeoMB entre los 3 sistemas sin usarse todos aquí).
    n_est = 0
    for nombre_json in ("mexibus_mapa.json", "metrobus_estaciones.json"):
        mapa_path = os.path.join(APP_DIR, "data", nombre_json)
        if not os.path.exists(mapa_path):
            print(f"[!] No existe {mapa_path} -- corre sync_mexibus_mapa.py / "
                  f"sync_metrobus_estaciones.py primero para saber qué pictogramas copiar.")
            continue
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
            print(f"[!] {nombre_json}: {len(faltantes)} pictogramas sin archivo en GeoMB: {faltantes[:10]}")

    print(f"OK: logo app, {n_lineas} logos de línea, {n_est} pictogramas de estación -> {APP_DIR}/icons/")


if __name__ == "__main__":
    main()
