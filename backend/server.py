"""
Servidor local para Metrobús CDMX en tiempo real.

Qué hace:
1. Sirve toda la app (index.html + data/) en http://localhost:8000
2. Cada 25 segundos, descarga el feed GTFS-RT desde la URL que pusiste
   en config.txt, lo convierte a JSON y sobrescribe data/vehicles.json.
3. Cuando el link de 12h caduque, solo edita config.txt con el nuevo
   link (te lo manda Metrobús por correo al darle "Reenviar" en el
   portal) y guarda el archivo — no hace falta reiniciar el servidor,
   se relee automáticamente antes de cada descarga.

Requisitos (una sola vez):
    pip install flask requests gtfs-realtime-bindings protobuf

Uso:
    cd backend
    python server.py
    Abre http://localhost:8000
"""

import json
import os
import threading
import time
import sys

import requests
from flask import Flask, send_from_directory
from google.transit import gtfs_realtime_pb2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(BASE_DIR)                 # metrobus_app/
DATA_DIR = os.path.join(APP_DIR, 'data')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.txt')

POLL_SECONDS = 25

app = Flask(__name__, static_folder=None)


def read_rt_url():
    """Lee la URL vigente desde config.txt (se relee en cada ciclo)."""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('RT_URL='):
                    url = line.split('=', 1)[1].strip()
                    if url and url != 'PEGA_AQUI_TU_LINK_DE_12H':
                        return url
    except FileNotFoundError:
        pass
    return None


def load_routes_lookup():
    with open(os.path.join(DATA_DIR, 'routes.json'), encoding='utf-8') as f:
        routes = json.load(f)
    return {r['route_id']: r for r in routes}


def fetch_and_update(routes_by_id):
    url = read_rt_url()
    if not url:
        print('[!] No hay RT_URL configurada en backend/config.txt todavía. Esperando...')
        return False

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f'[!] Error descargando el feed RT: {e}')
        return False

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(resp.content)
    except Exception as e:
        print(f'[!] Error parseando protobuf (¿el link caducó o es inválido?): {e}')
        return False

    vehicles = []
    for entity in feed.entity:
        if not entity.HasField('vehicle'):
            continue
        v = entity.vehicle
        rid = v.trip.route_id
        r = routes_by_id.get(rid)
        vehicles.append({
            'id': v.vehicle.id,
            'label': v.vehicle.label,
            'plate': v.vehicle.license_plate,
            'route_id': rid,
            'line': r['line'] if r else None,
            'destino': r['destino'] if r else None,
            'origen': r['origen'] if r else None,
            'direction_id': v.trip.direction_id,
            'lat': round(v.position.latitude, 6),
            'lon': round(v.position.longitude, 6),
            'bearing': v.position.bearing,
            'speed': v.position.speed,
            'timestamp': v.timestamp,
        })

    tmp_path = os.path.join(DATA_DIR, 'vehicles.json.tmp')
    final_path = os.path.join(DATA_DIR, 'vehicles.json')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(vehicles, f, ensure_ascii=False)
    os.replace(tmp_path, final_path)  # escritura atómica, evita leer un archivo a medias

    print(f'[OK] vehicles.json actualizado — {len(vehicles)} unidades '
          f'(feed timestamp {feed.header.timestamp})')
    return True


def poll_loop():
    routes_by_id = load_routes_lookup()
    while True:
        fetch_and_update(routes_by_id)
        time.sleep(POLL_SECONDS)


@app.route('/')
def index():
    return send_from_directory(APP_DIR, 'index.html')


@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(APP_DIR, path)


if __name__ == '__main__':
    if not read_rt_url():
        print('=' * 70)
        print('AVISO: todavía no configuraste el link en backend/config.txt')
        print('Abre ese archivo, reemplaza PEGA_AQUI_TU_LINK_DE_12H con el link')
        print('real del correo de Metrobús, y guarda. El servidor lo detectará solo.')
        print('=' * 70)

    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    print('Servidor corriendo en http://localhost:8000')
    app.run(host='0.0.0.0', port=8000)
