"""
Metrobús CDMX — mapa en tiempo real (proceso único, listo para desplegar)
==========================================================================

Un solo proceso que hace TODO, pensado para hosting tipo Render / Railway /
Fly.io con auto-deploy desde GitHub:

  1. Sirve la web (index.html + data/) en el puerto que indique la plataforma.
  2. Cada POLL_SECONDS descarga el feed GTFS-RT vigente, lo convierte a JSON
     y lo mantiene en memoria (se sirve en /data/vehicles.json).
  3. Cada RENEW_EVERY_SECONDS (11.5 h) renueva solo el link de 12 h:
     dispara el "Reenviar" del portal, lee el correo por IMAP y extrae el
     link nuevo de "Realtime". No hay que tocar nada a mano.

TODOS los secretos se leen de variables de entorno (NO de archivos en el
repo). Configúralas en el panel de tu hosting:

  PORT               (lo pone la plataforma sola; local usa 8000)
  MB_RT_URL          (opcional) link de 12 h inicial, para arrancar ya con datos
  GMAIL_ADDRESS      correo que recibe los links de Metrobús
  GMAIL_APP_PASSWORD contraseña de aplicación de Gmail (16 caracteres)
  MB_RESEND_URL      endpoint de reenvío del portal
                     (https://metrobus-gtfs.sinopticoplus.com/gtfs-api/senderEmailGtfs/1339/<correo>)

Si NO defines las variables de Gmail/reenvío, el bot de renovación se
desactiva y la app funciona igual con MB_RT_URL (o con el snapshot incluido
en data/vehicles.json) — útil para probar el despliegue antes de conectar
el correo.

Ejecutar:  python app.py
"""

import email
import imaplib
import json
import os
import re
import threading
import time
from datetime import datetime

import requests
from flask import Flask, Response, send_from_directory
from google.transit import gtfs_realtime_pb2

# ---------------------------------------------------------------------------
# Rutas y configuración
# ---------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, 'data')

POLL_SECONDS = int(os.environ.get('POLL_SECONDS', '25'))
RENEW_EVERY_SECONDS = int(float(os.environ.get('RENEW_HOURS', '11.5')) * 3600)
WAIT_FOR_EMAIL_SECONDS = 180
CHECK_EVERY_SECONDS = 10

IMAP_HOST = 'imap.gmail.com'
SENDER_FILTER = 'sinopticoplus.com'   # el correo llega de noreply@sinopticoplus.com

# Reintento rápido si la renovación falla (en vez de esperar 11.5 h)
RETRY_ON_ERROR_SECONDS = int(os.environ.get('RETRY_SECONDS', '600'))

# Cabeceras que imitan al navegador/portal para evitar el 403 del "Reenviar"
BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
    'Referer': 'https://www.metrobus.cdmx.gob.mx/',
    'Origin': 'https://www.metrobus.cdmx.gob.mx',
}

# Estado compartido entre hilos
_state_lock = threading.Lock()
_rt_url = os.environ.get('MB_RT_URL', '').strip() or None
_vehicles_json = None          # último JSON servido (string). None => usar fallback en disco
_last_update_ts = None         # timestamp del feed de la última actualización correcta

app = Flask(__name__, static_folder=None)


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


def get_rt_url():
    with _state_lock:
        return _rt_url


def set_rt_url(url):
    global _rt_url
    with _state_lock:
        _rt_url = url


# ---------------------------------------------------------------------------
# Polling del feed GTFS-RT  ->  vehicles.json en memoria
# ---------------------------------------------------------------------------

def load_routes_lookup():
    with open(os.path.join(DATA_DIR, 'routes.json'), encoding='utf-8') as f:
        routes = json.load(f)
    return {r['route_id']: r for r in routes}


def fetch_and_update(routes_by_id):
    global _vehicles_json, _last_update_ts

    url = get_rt_url()
    if not url:
        log('[!] Sin MB_RT_URL todavia. Sirviendo el snapshot incluido. '
            'Define MB_RT_URL o conecta el bot de renovacion.')
        return False

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log(f'[!] Error descargando el feed RT: {e}')
        return False

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(resp.content)
    except Exception as e:
        log(f'[!] Error parseando protobuf (el link caduco o es invalido?): {e}')
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

    payload = json.dumps(vehicles, ensure_ascii=False)
    with _state_lock:
        _vehicles_json = payload
        _last_update_ts = feed.header.timestamp

    log(f'[OK] {len(vehicles)} unidades (feed timestamp {feed.header.timestamp})')
    return True


def poll_loop():
    routes_by_id = load_routes_lookup()
    while True:
        try:
            fetch_and_update(routes_by_id)
        except Exception as e:
            log(f'[!] Error inesperado en poll_loop: {e}')
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# Bot de renovación del link (IMAP + reenvío)
# ---------------------------------------------------------------------------

def trigger_resend(resend_url):
    log('Solicitando reenvio del correo con el link nuevo...')
    resp = requests.get(resend_url, headers=BROWSER_HEADERS, timeout=20)
    resp.raise_for_status()
    log(f'Solicitud de reenvio enviada (status {resp.status_code}).')


def extract_realtime_link(html_body):
    """Devuelve el href del <a> cuyo texto visible contiene 'Realtime'."""
    anchors = re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html_body, re.IGNORECASE | re.DOTALL)
    for href, text in anchors:
        clean_text = re.sub('<[^<]+?>', '', text)
        if 'realtime' in clean_text.lower():
            return href.replace('&amp;', '&')
    return None


def fetch_latest_realtime_link(cfg, since_uid):
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(cfg['GMAIL_ADDRESS'], cfg['GMAIL_APP_PASSWORD'])
        imap.select('INBOX')

        status, data = imap.uid('search', None, f'(FROM "{SENDER_FILTER}")')
        if status != 'OK' or not data or not data[0]:
            return None, since_uid

        uids = sorted(int(u) for u in data[0].split())
        new_uids = [u for u in uids if since_uid is None or u > since_uid]
        if not new_uids:
            return None, since_uid

        latest_uid = new_uids[-1]
        status, msg_data = imap.uid('fetch', str(latest_uid), '(RFC822)')
        if status != 'OK' or not msg_data or not msg_data[0]:
            return None, since_uid

        msg = email.message_from_bytes(msg_data[0][1])
        html_body = None
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == 'text/html':
                    charset = part.get_content_charset() or 'utf-8'
                    html_body = part.get_payload(decode=True).decode(charset, errors='ignore')
                    break
        elif msg.get_content_type() == 'text/html':
            charset = msg.get_content_charset() or 'utf-8'
            html_body = msg.get_payload(decode=True).decode(charset, errors='ignore')

        if not html_body:
            log('El correo mas reciente no tiene cuerpo HTML legible.')
            return None, latest_uid

        return extract_realtime_link(html_body), latest_uid
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def renew_once(cfg, last_uid):
    trigger_resend(cfg['MB_RESEND_URL'])
    waited = 0
    while waited < WAIT_FOR_EMAIL_SECONDS:
        time.sleep(CHECK_EVERY_SECONDS)
        waited += CHECK_EVERY_SECONDS
        try:
            link, uid = fetch_latest_realtime_link(cfg, last_uid)
        except imaplib.IMAP4.error as e:
            log(f'Error IMAP (revisa GMAIL_APP_PASSWORD): {e}')
            return last_uid
        if link:
            set_rt_url(link)
            log('Link de Realtime renovado en memoria.')
            return uid
        log(f'Correo nuevo aun no llega... ({waited}s)')
    log('No llego el correo nuevo dentro del tiempo de espera. Se reintenta al proximo ciclo.')
    return last_uid


def renew_loop(cfg):
    log(f'Bot de renovacion activo para {cfg["GMAIL_ADDRESS"]}.')
    last_uid = None
    while True:
        try:
            last_uid = renew_once(cfg, last_uid)
            dormir = RENEW_EVERY_SECONDS
        except Exception as e:
            log(f'Error inesperado en renew_loop: {e}')
            dormir = RETRY_ON_ERROR_SECONDS
        log(f'Durmiendo {dormir/3600:.2f} h hasta el proximo intento...')
        time.sleep(dormir)


def read_renew_config():
    """Devuelve el dict de config si estan las 3 variables; None si falta alguna."""
    cfg = {
        'GMAIL_ADDRESS': os.environ.get('GMAIL_ADDRESS', '').strip(),
        'GMAIL_APP_PASSWORD': os.environ.get('GMAIL_APP_PASSWORD', '').replace(' ', '').strip(),
        'MB_RESEND_URL': os.environ.get('MB_RESEND_URL', '').strip(),
    }
    if all(cfg.values()):
        return cfg
    return None


# ---------------------------------------------------------------------------
# Rutas HTTP
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_from_directory(APP_DIR, 'index.html')


@app.route('/data/vehicles.json')
def vehicles():
    """Sirve el JSON en memoria; si aun no hay, cae al snapshot en disco."""
    with _state_lock:
        payload = _vehicles_json
    if payload is None:
        return send_from_directory(DATA_DIR, 'vehicles.json',
                                   mimetype='application/json')
    return Response(payload, mimetype='application/json',
                    headers={'Cache-Control': 'no-store'})


@app.route('/health')
def health():
    with _state_lock:
        return {
            'rt_url_configurada': _rt_url is not None,
            'ultimo_feed_timestamp': _last_update_ts,
            'renovacion_activa': read_renew_config() is not None,
        }


@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(APP_DIR, path)


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------

def start_background_workers():
    threading.Thread(target=poll_loop, daemon=True).start()

    cfg = read_renew_config()
    if cfg:
        threading.Thread(target=renew_loop, args=(cfg,), daemon=True).start()
    else:
        log('[i] Bot de renovacion desactivado (faltan variables GMAIL_ADDRESS / '
            'GMAIL_APP_PASSWORD / MB_RESEND_URL). La app usara MB_RT_URL o el snapshot.')


# Los hilos se arrancan al importar el módulo, así funciona tanto con
# `python app.py` como bajo gunicorn (gunicorn app:app).
start_background_workers()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    log(f'Servidor escuchando en http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port)
