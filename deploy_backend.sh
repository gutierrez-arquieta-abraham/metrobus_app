#!/usr/bin/env bash
# =============================================================================
# deploy_backend.sh  —  despliega el backend GeoMB (web + feed) y reinicia.
# Correr desde MINGW64 (Git Bash) en Escritorio/GeoMB:  bash deploy_backend.sh
#
# Layout real del server (confirmado):
#   WEB : /home/ubuntu/metrobus_app   -> app.py + admin_afect.py   (svc metrobus-web)
#         env: /home/ubuntu/geomb.env      venv: /home/ubuntu/venv
#   FEED: /opt/geomb-afect            -> mexibus_afectaciones.py    (svc mexibus-afectaciones)
#         env: /opt/geomb-afect/.env
# Requiere sudo SIN password para ubuntu (default en EC2). NO toca secretos ni metrobus-push.
# =============================================================================
set -euo pipefail

KEY="/c/Users/abrah/OneDrive/Escritorio/GeoMB/tu-clave.pem"
HOST="ubuntu@geomb.duckdns.org"
WEBDIR="/home/ubuntu/metrobus_app"
FEEDDIR="/opt/geomb-afect"
TTL_MIN="420"        # ventana "sin novedad" del feed (min): 300-540 = 5-9 h

cd "$(dirname "$0")"
chmod 600 "$KEY" 2>/dev/null || true
TS="$(date +%Y%m%d_%H%M%S)"

echo ">> 1/5  Env del servicio web (ADMIN_TOKEN + Firebase) ..."
ssh -i "$KEY" "$HOST" '
  GE=/home/ubuntu/geomb.env
  grep -q "^GOOGLE_APPLICATION_CREDENTIALS=" $GE || echo "GOOGLE_APPLICATION_CREDENTIALS=/home/ubuntu/firebase.json" >> $GE
  grep -q "^ADMIN_TOKEN=" $GE || echo "ADMIN_TOKEN=$(openssl rand -hex 24)" >> $GE
  echo "   claves en geomb.env:"; grep -oE "^[A-Za-z_]+=" $GE | sort -u | sed "s/^/     /"
'

echo ">> 2/5  TTL del feed a ${TTL_MIN} min ..."
ssh -i "$KEY" "$HOST" "sudo sed -i 's/^MXB_ESTADO_TTL=.*/MXB_ESTADO_TTL=${TTL_MIN}/' ${FEEDDIR}/.env; grep '^MXB_ESTADO_TTL=' ${FEEDDIR}/.env | sed 's/^/     /'"

echo ">> 3/5  Subiendo archivos (con respaldo) ..."
ssh -i "$KEY" "$HOST" "cp ${WEBDIR}/app.py ${WEBDIR}/app.py.bak.${TS}; cp /home/ubuntu/push_metrobus.py /home/ubuntu/push_metrobus.py.bak.${TS}; sudo cp ${FEEDDIR}/mexibus_afectaciones.py ${FEEDDIR}/mexibus_afectaciones.py.bak.${TS}"
# Web (metrobus_app): app.py + admin_afect.py + push_metrobus.py (este ultimo por el _push del panel)
scp -i "$KEY" app.py admin_afect.py push_metrobus.py "$HOST:${WEBDIR}/"
# Scraper (metrobus-push, /home/ubuntu): push_metrobus.py (escribe el estado Metrobus)
scp -i "$KEY" push_metrobus.py "$HOST:/home/ubuntu/push_metrobus.py"
# Feed Mexibus (/opt/geomb-afect): via /tmp + sudo
scp -i "$KEY" mexibus_afectaciones.py "$HOST:/tmp/mexibus_afectaciones.py"
ssh -i "$KEY" "$HOST" "sudo cp /tmp/mexibus_afectaciones.py ${FEEDDIR}/mexibus_afectaciones.py && rm -f /tmp/mexibus_afectaciones.py"

echo ">> 4/5  Prueba del parser (#ampliacion -> L3A) ..."
ssh -i "$KEY" "$HOST" "/home/ubuntu/venv/bin/python ${FEEDDIR}/mexibus_afectaciones.py --test | sed 's/^/     /'" || true

echo ">> 5/5  Reiniciando servicios ..."
ssh -i "$KEY" "$HOST" "sudo systemctl restart metrobus-web.service metrobus-push.service mexibus-afectaciones.service && sleep 2 && systemctl is-active metrobus-web.service metrobus-push.service mexibus-afectaciones.service | sed 's/^/     /'"

echo ">> Verificacion:"
curl -s -o /dev/null -w "     /admin/afectacion -> HTTP %{http_code}\n" "https://geomb.duckdns.org/admin/afectacion" || true
echo "     panel actual:"; curl -s "https://geomb.duckdns.org/data/afectaciones_mexibus.json" | sed 's/^/       /'; echo
echo ">> Tu ADMIN_TOKEN (para el celular):"
ssh -i "$KEY" "$HOST" 'grep "^ADMIN_TOKEN=" /home/ubuntu/geomb.env | sed "s/^/     /"'
echo ">> Limpiando carpeta suelta /home/ubuntu/backend ..."
ssh -i "$KEY" "$HOST" 'rm -rf /home/ubuntu/backend'
echo ">> Listo. Abre en el celular:  https://geomb.duckdns.org/admin/afectacion"
