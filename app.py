"""
Metrobús CDMX — mapa en tiempo real (proceso único, listo para desplegar)
==========================================================================

Sirve la web (index.html + data/) y expone /data/vehicles.json con las
posiciones en vivo de las unidades.

Fuente de datos en vivo — API oficial de Sonda:
  Cada REFRESH_SECONDS hace POST a partnerValidation con usuario/senha y
  obtiene una urlRealTime fresca (el .proto, válido 10 min). Luego el poll
  descarga ese .proto cada POLL_SECONDS. Totalmente automático en la nube.

Catálogo de modelos:
  /data/modelos.csv sirve el catálogo (economico,marca,modelo). Si defines
  MODELOS_SHEET_URL (CSV publicado de un Google Sheet) lo proxea; si no,
  sirve data/modelos.csv del repo.

Variables de entorno:
  PORT               (lo pone la plataforma; local 8000)
  PARTNER_USER       usuario de la API GTFS de Sonda
  PARTNER_PASS       contraseña (senha) de la API GTFS de Sonda
  MODELOS_SHEET_URL  (opcional) CSV publicado de un Google Sheet con modelos
  MB_RT_URL          (opcional/fallback) link .proto de 12 h ya firmado
  ADMIN_TOKEN        (opcional/fallback) token para POST /admin/rt_url

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
from flask import Flask, Response, request, send_from_directory
from google.transit import gtfs_realtime_pb2

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, 'data')

POLL_SECONDS = int(os.environ.get('POLL_SECONDS', '15'))
RENEW_EVERY_SECONDS = int(float(os.environ.get('RENEW_HOURS', '11.5')) * 3600)
WAIT_FOR_EMAIL_SECONDS = 180
CHECK_EVERY_SECONDS = 10

IMAP_HOST = 'imap.gmail.com'
SENDER_FILTER = 'sinopticoplus.com'
RETRY_ON_ERROR_SECONDS = int(os.environ.get('RETRY_SECONDS', '600'))

PARTNER_URL = os.environ.get('PARTNER_URL',
                             'https://metrobus-gtfs.sinopticoplus.com/gtfs-api/partnerValidation')
REFRESH_SECONDS = int(os.environ.get('REFRESH_SECONDS', '540'))

MODELOS_SHEET_URL = os.environ.get('MODELOS_SHEET_URL', '').strip()

BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
    'Referer': 'https://metrobus-gtfs.sinopticoplus.com/',
    'Origin': 'https://metrobus-gtfs.sinopticoplus.com',
}

_state_lock = threading.Lock()
_rt_url = os.environ.get('MB_RT_URL', '').strip() or None
_vehicles_json = None
_last_update_ts = None

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


def partner_config():
    u = os.environ.get('PARTNER_USER', '').strip()
    p = os.environ.get('PARTNER_PASS', '').strip()
    return (u, p) if u and p else None


def refresh_rt_url_from_api():
    creds = partner_config()
    if not creds:
        return False
    usuario, senha = creds
    try:
        resp = requests.post(PARTNER_URL, json={'usuario': usuario, 'senha': senha},
                             headers=BROWSER_HEADERS, timeout=20)
    except Exception as e:
        log(f'[!] Error llamando partnerValidation: {e}')
        return False
    if resp.status_code != 200:
        log(f'[!] partnerValidation status {resp.status_code}: {resp.text[:120]}')
        return False
    try:
        data = resp.json()
    except Exception:
        log('[!] partnerValidation no devolvio JSON')
        return False
    url = (data.get('urlRealTime') or '').strip()
    if not url:
        log('[!] partnerValidation sin urlRealTime')
        return False
    set_rt_url(url)
    log(f'[OK] urlRealTime renovada (expira {data.get("expirationDateTime")})')
    return True


def api_refresh_loop():
    while True:
        try:
            refresh_rt_url_from_api()
        except Exception as e:
            log(f'[!] Error inesperado en api_refresh_loop: {e}')
        time.sleep(REFRESH_SECONDS)


def load_routes_lookup():
    with open(os.path.join(DATA_DIR, 'routes.json'), encoding='utf-8') as f:
        routes = json.load(f)
    return {r['route_id']: r for r in routes}


def fetch_and_update(routes_by_id):
    global _vehicles_json, _last_update_ts

    url = get_rt_url()
    if not url:
        log('[!] Sin urlRealTime todavia. Define PARTNER_USER/PASS, MB_RT_URL o /admin/rt_url.')
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
        log(f'[!] Error parseando protobuf (link caduco?): {e}')
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
        ok = False
        try:
            ok = fetch_and_update(routes_by_id)
        except Exception as e:
            log(f'[!] Error inesperado en poll_loop: {e}')
        if not ok and partner_config():
            refresh_rt_url_from_api()
        time.sleep(POLL_SECONDS)


def trigger_resend(resend_url):
    log('Solicitando reenvio del correo con el link nuevo...')
    resp = requests.get(resend_url, headers=BROWSER_HEADERS, timeout=20)
    resp.raise_for_status()
    log(f'Solicitud de reenvio enviada (status {resp.status_code}).')


def extract_realtime_link(html_body):
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
            log(f'Error IMAP: {e}')
            return last_uid
        if link:
            set_rt_url(link)
            log('Link de Realtime renovado en memoria.')
            return uid
    return last_uid


def renew_loop(cfg):
    log(f'Bot de renovacion por correo activo para {cfg["GMAIL_ADDRESS"]}.')
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
    cfg = {
        'GMAIL_ADDRESS': os.environ.get('GMAIL_ADDRESS', '').strip(),
        'GMAIL_APP_PASSWORD': os.environ.get('GMAIL_APP_PASSWORD', '').replace(' ', '').strip(),
        'MB_RESEND_URL': os.environ.get('MB_RESEND_URL', '').strip(),
    }
    if all(cfg.values()):
        return cfg
    return None


@app.route('/')
def index():
    return send_from_directory(APP_DIR, 'index.html')


@app.route('/data/vehicles.json')
def vehicles():
    with _state_lock:
        payload = _vehicles_json
    if payload is None:
        return send_from_directory(DATA_DIR, 'vehicles.json', mimetype='application/json')
    return Response(payload, mimetype='application/json',
                    headers={'Cache-Control': 'no-store'})


@app.route('/data/modelos.csv')
def modelos_csv():
    if MODELOS_SHEET_URL:
        try:
            resp = requests.get(MODELOS_SHEET_URL, timeout=15)
            if resp.status_code == 200 and resp.text.strip():
                return Response(resp.text, mimetype='text/csv',
                                headers={'Cache-Control': 'no-store'})
            log(f'[!] MODELOS_SHEET_URL status {resp.status_code}')
        except Exception as e:
            log(f'[!] Error leyendo MODELOS_SHEET_URL: {e}')
    try:
        return send_from_directory(DATA_DIR, 'modelos.csv', mimetype='text/csv')
    except Exception:
        return Response('economico,marca,modelo\n', mimetype='text/csv',
                        headers={'Cache-Control': 'no-store'})


@app.route('/health')
def health():
    with _state_lock:
        return {
            'rt_url_configurada': _rt_url is not None,
            'ultimo_feed_timestamp': _last_update_ts,
            'modo': 'api' if partner_config() else 'manual',
            'modelos': 'sheet' if MODELOS_SHEET_URL else 'archivo',
        }


@app.route('/admin/rt_url', methods=['POST'])
def admin_set_rt_url():
    token = os.environ.get('ADMIN_TOKEN', '')
    if not token or request.headers.get('X-Admin-Token', '') != token:
        return {'ok': False, 'error': 'token invalido'}, 403
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return {'ok': False, 'error': 'url vacio'}, 400
    set_rt_url(url)
    log('MB_RT_URL actualizada via /admin/rt_url')
    return {'ok': True}


@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(APP_DIR, path)


def start_background_workers():
    threading.Thread(target=poll_loop, daemon=True).start()

    if partner_config():
        refresh_rt_url_from_api()
        threading.Thread(target=api_refresh_loop, daemon=True).start()
        log('[i] Modo API oficial activo (partnerValidation cada %ds).' % REFRESH_SECONDS)
        return

    cfg = read_renew_config()
    if cfg:
        threading.Thread(target=renew_loop, args=(cfg,), daemon=True).start()
    else:
        log('[i] Sin PARTNER_USER/PASS ni bot de correo. Usa MB_RT_URL o /admin/rt_url.')


start_background_workers()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    log(f'Servidor escuchando en http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port)
