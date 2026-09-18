#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extrae el catálogo de estaciones por línea desde los assets de GeoMB (repo de
Android) y regenera data/estaciones_por_linea.json, que admin_afect.py usa para
poblar el <select> de estaciones del panel /admin/afectacion.

Uso:
    python3 tools/extraer_estaciones_geomb.py [ruta_al_checkout_de_GeoMB]

Si no se pasa ruta, usa la variable de entorno GEOMB_REPO o, en su defecto,
"../GeoMB" relativo a este repo. Lee lineas.json (Metrobús 1-7) y mexibus.json
(Mexibús 101-104 ordinario y 111-113 ramales) — las mismas líneas que
LINEAS en admin_afect.py; no incluye exprés (12X) ni Mexicable (20X), que el
panel web no maneja.

Volver a correr este script cuando esos assets cambien en GeoMB (estación
nueva, renombre, etc.) para mantener el selector del panel alineado.
"""
import json
import os
import re
import sys

LINEAS_METROBUS = range(1, 8)
LINEAS_MEXIBUS = (101, 102, 103, 104, 111, 112, 113)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sin_prefijo(nombre):
    """Quita 'MXB '/'MXC ' del nombre (la app los guarda pero no los muestra) y el sufijo
    "(conexión ...)" de estaciones compartidas entre líneas (ej. "Pantitlán (conexión
    Metrobús L4)" -> "Pantitlán"): el selector queda más limpio y el matching en
    AfectacionesMexibus.indiceEstacion() ya hace fallback por substring, así que el nombre
    corto sigue emparejando igual contra la estación real."""
    s = re.sub(r"^(MXB|MXC)\s+", "", nombre or "").strip()
    return re.sub(r"\s*\(conexión[^)]*\)\s*$", "", s, flags=re.IGNORECASE).strip()


def cargar_lineas(ruta_json, permitidas):
    with open(ruta_json, encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for l in data.get("lineas", []):
        n = l.get("numero")
        if n not in permitidas:
            continue
        vistos = set()
        nombres = []
        for e in l.get("estaciones", []):
            nombre = sin_prefijo(e.get("n", ""))
            if nombre and nombre not in vistos:
                vistos.add(nombre)
                nombres.append(nombre)
        out[str(n)] = nombres
    return out


def main():
    if len(sys.argv) > 1:
        geomb = sys.argv[1]
    else:
        geomb = os.environ.get("GEOMB_REPO") or os.path.join(APP_DIR, "..", "GeoMB")
    geomb = os.path.abspath(geomb)
    assets = os.path.join(geomb, "app", "src", "main", "assets")

    estaciones = {}
    estaciones.update(cargar_lineas(os.path.join(assets, "lineas.json"), set(LINEAS_METROBUS)))
    estaciones.update(cargar_lineas(os.path.join(assets, "mexibus.json"), set(LINEAS_MEXIBUS)))

    orden = [str(n) for n in list(LINEAS_METROBUS) + list(LINEAS_MEXIBUS)]
    faltantes = [k for k in orden if k not in estaciones]
    if faltantes:
        sys.exit(f"[extraer_estaciones_geomb] faltan líneas en los assets de GeoMB: {faltantes}")
    estaciones = {k: estaciones[k] for k in orden}

    destino = os.path.join(APP_DIR, "data", "estaciones_por_linea.json")
    with open(destino, "w", encoding="utf-8") as f:
        json.dump(estaciones, f, ensure_ascii=False)

    total = sum(len(v) for v in estaciones.values())
    print(f"OK: {len(estaciones)} líneas, {total} estaciones -> {destino}")


if __name__ == "__main__":
    main()
