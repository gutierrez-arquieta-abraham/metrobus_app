"""
Bot de renovación automática del link de Metrobús GTFS-RT
===========================================================

Qué hace, en bucle infinito:
1. Llama al endpoint de "Reenviar" del portal de Metrobús -> dispara
   un correo nuevo con un link de 12h.
2. Se conecta a Gmail por IMAP y espera a que llegue ese correo.
3. Extrae el link de "Realtime" (no el de "Estaticos") del cuerpo HTML.
4. Sobrescribe backend/config.txt con RT_URL=<link nuevo>.
   server.py ya relee ese archivo solo antes de cada descarga, así que
   no hace falta reiniciar nada.
5. Duerme ~11.5 horas y repite (antes de que el link de 12h expire).

Requisitos (una sola vez):
    pip install requests

Uso:
    cd backend
    python auto_renew.py

Déjalo corriendo en su propia terminal, junto con server.py en otra.

Credenciales: se leen de backend/email_config.txt (EMAIL_ADDRESS,
EMAIL_APP_PASSWORD, RESEND_URL). La contraseña debe ser una
"contraseña de aplicación" de Gmail (16 caracteres), no la contraseña
normal de la cuenta.
"""

import email
import imaplib
import os
import re
import time
from datetime import datetime, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EMAIL_CONFIG_PATH = os.path.join(BASE_DIR, 'email_config.txt')
RT_CONFIG_PATH = os.path.join(BASE_DIR, 'config.txt')

IMAP_HOST = 'imap.gmail.com'
SENDER_FILTER = 'sinopticoplus.com'   # el correo llega de noreply@sinopticoplus.com

RENEW_EVERY_SECONDS = int(11.5 * 3600)   # 11.5 horas, antes de que expiren las 12h
WAIT_FOR_EMAIL_SECONDS = 180              # cuánto esperar a que llegue el correo tras pedirlo
CHECK_EVERY_SECONDS = 10                  # cada cuánto revisar la bandeja mientras espera


def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


def read_email_config():
    cfg = {}
    with open(EMAIL_CONFIG_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            cfg[k.strip()] = v.strip()
    for required in ('EMAIL_ADDRESS', 'EMAIL_APP_PASSWORD', 'RESEND_URL'):
        if required not in cfg or not cfg[required]:
            raise RuntimeError(f'Falta {required} en {EMAIL_CONFIG_PATH}')
    return cfg


def trigger_resend(resend_url):
    log('Solicitando reenvío del correo con el link nuevo...')
    resp = requests.get(resend_url, timeout=20)
    resp.raise_for_status()
    log(f'Solicitud enviada (status {resp.status_code}).')


def extract_realtime_link(html_body):
    """
    Busca todos los enlaces <a href="...">texto</a> y devuelve el href
    del que contenga 'Realtime' en su texto visible.
    """
    # Captura pares (href, texto_interno) de forma no muy estricta
    anchors = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_body, re.IGNORECASE | re.DOTALL)
    for href, text in anchors:
        clean_text = re.sub('<[^<]+?>', '', text)  # quita tags anidados dentro del <a>
        if 'realtime' in clean_text.lower():
            return href.replace('&amp;', '&')
    return None


def fetch_latest_realtime_link(cfg, since_uid):
    """
    Conecta por IMAP, busca el correo más reciente de sinopticoplus.com
    con UID mayor a since_uid, y extrae el link de Realtime.
    Devuelve (link, uid) o (None, since_uid) si no hay nada nuevo aún.
    """
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(cfg['EMAIL_ADDRESS'], cfg['EMAIL_APP_PASSWORD'])
        imap.select('INBOX')

        status, data = imap.uid('search', None, f'(FROM "{SENDER_FILTER}")')
        if status != 'OK' or not data or not data[0]:
            return None, since_uid

        uids = [int(u) for u in data[0].split()]
        uids.sort()
        new_uids = [u for u in uids if since_uid is None or u > since_uid]
        if not new_uids:
            return None, since_uid

        latest_uid = new_uids[-1]
        status, msg_data = imap.uid('fetch', str(latest_uid), '(RFC822)')
        if status != 'OK' or not msg_data or not msg_data[0]:
            return None, since_uid

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        html_body = None
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == 'text/html':
                    charset = part.get_content_charset() or 'utf-8'
                    html_body = part.get_payload(decode=True).decode(charset, errors='ignore')
                    break
        else:
            if msg.get_content_type() == 'text/html':
                charset = msg.get_content_charset() or 'utf-8'
                html_body = msg.get_payload(decode=True).decode(charset, errors='ignore')

        if not html_body:
            log('El correo más reciente no tiene cuerpo HTML legible.')
            return None, latest_uid

        link = extract_realtime_link(html_body)
        return link, latest_uid
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def write_rt_url(link):
    with open(RT_CONFIG_PATH, 'w', encoding='utf-8') as f:
        f.write(f'RT_URL={link}\n')
    log(f'config.txt actualizado con el nuevo link.')


def renew_once(cfg, last_uid):
    trigger_resend(cfg['RESEND_URL'])

    waited = 0
    while waited < WAIT_FOR_EMAIL_SECONDS:
        time.sleep(CHECK_EVERY_SECONDS)
        waited += CHECK_EVERY_SECONDS
        try:
            link, uid = fetch_latest_realtime_link(cfg, last_uid)
        except imaplib.IMAP4.error as e:
            log(f'Error IMAP (revisa EMAIL_APP_PASSWORD): {e}')
            return last_uid
        if link:
            write_rt_url(link)
            return uid
        log(f'Correo nuevo aún no llega... ({waited}s)')

    log('No llegó el correo nuevo dentro del tiempo de espera. '
        'Se reintentará en el próximo ciclo.')
    return last_uid


def main():
    cfg = read_email_config()
    log(f'Bot de renovación iniciado para {cfg["EMAIL_ADDRESS"]}.')
    last_uid = None

    while True:
        try:
            last_uid = renew_once(cfg, last_uid)
        except Exception as e:
            log(f'Error inesperado en el ciclo de renovación: {e}')

        log(f'Durmiendo {RENEW_EVERY_SECONDS/3600:.1f} horas hasta la próxima renovación...')
        time.sleep(RENEW_EVERY_SECONDS)


if __name__ == '__main__':
    main()
