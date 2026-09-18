# CLAUDE.md — Backend GeoMB (metrobus_app)

Backend del proyecto **GeoMB** (app Android de transporte CDMX/Edomex: Metrobús L1–L7, Mexibús, Mexicable).
Sirve la web + datos en vivo + afectaciones + push FCM. Producción: `https://geomb.duckdns.org` (EC2 Ubuntu, IP 78.14.99.249).

## Arquitectura (3 servicios systemd en el EC2)
- **`metrobus-web.service`** → `app.py` (Flask, corre con `gunicorn app:app` en `127.0.0.1:8000`).
  - Ubicación en el server: `/home/ubuntu/metrobus_app` · env: `/home/ubuntu/geomb.env` (+ drop-in `kyc.conf` con llaves Didit).
  - Sirve: `/` (index.html), `/data/vehicles.json` (posiciones en vivo, feed Sonda GTFS-rt), `/data/routes.json`,
    `/data/afectaciones_mexibus.json` (panel, ver abajo), `/data/modelos.csv`, `/health`.
  - Blueprints: `didit_backend.py` (KYC `/api/didit/*`), `tts_backend.py` (voz Polly `/api/tts`), `admin_afect.py` (panel afectaciones).
- **`metrobus-push.service`** → `push_metrobus.py` en `/home/ubuntu`. Raspa el estado de Metrobús (gov) + elevadores/mantenimiento,
  empuja por FCM (topic `afectaciones`) **y escribe `metrobus_app/data/afect_metrobus.json`** (estado persistente para el panel).
- **`mexibus-afectaciones.service`** → `mexibus_afectaciones.py` en `/opt/geomb-afect`. Sondea 3 feeds RSS.app de SITRAMYTEM →
  FCM + escribe `data/afectaciones_mexibus.json`. poll 60 s. Regla: `#ampliación` + línea 3 ⇒ **L3A (113)**.

nginx (Certbot/Let's Encrypt) termina el TLS y hace proxy a gunicorn. `firebase.json` y llaves viven **solo en el EC2**.

## Panel de afectaciones = fusión al vuelo (no archivo estático)
`app.py` calcula `/data/afectaciones_mexibus.json` en **cada request**, mezclando por línea 3 fuentes:
1. Feed Mexibús — `AFECT_MXB_OUT` (= `data/afectaciones_mexibus.json`, lo escribe `mexibus_afectaciones.py`).
2. Estado Metrobús — `data/afect_metrobus.json` (lo escribe `push_metrobus.py`).
3. **Overrides manuales** — `data/afect_manual.json` (los escribe `admin_afect.py`).

El manual **gana** mientras no venza. Si una línea trae varias afectaciones a la vez, se **combinan** en una fila
(junta estado/lugar/info con ` / ` y ` · `). La app Android (`AfectacionesMexibus`) ingiere cualquier línea (Metrobús 1–7 y Mexibús 10X+).

## Aviso manual (admin) + seguridad mTLS
- `admin_afect.py`: formulario móvil `GET/POST /admin/afectacion`. Empuja FCM (reusa `push_metrobus._push`) y escribe el override
  con `expira = min(now + AFECT_DUR_H h, 23:59 CDMX)` (default 7 h). Cubre Metrobús L1–L7 y Mexibús/ramales (Mexicable no).
- **Auth por certificado cliente (mTLS)**, no token: el panel vive en `https://geomb.duckdns.org:8443/admin/afectacion`
  (nginx `ssl_verify_client on`, CA propia en `/etc/nginx/certs/geomb-admin-ca.crt`). `/admin` está **bloqueado (404) en el `:443`**
  para que no se pueda falsear el header. El cliente importa `geomb-admin.p12` (llave dedicada, NO la .jks de firma del APK).
  Flask confía en `X-Client-Verify: SUCCESS` (lo pone nginx). `ADMIN_TOKEN` solo como respaldo local. Puerto 8443 abierto en el SG de AWS.

## Caducidad de afectaciones
`MXB_ESTADO_TTL` (min, default 420 = 7 h; usar 300–540 para 5–9 h) **y tope duro a las 23:59 CDMX** del día del post
(`_fin_del_dia_local`, `zoneinfo America/Mexico_City`). Aplica al feed Mexibús, al estado Metrobús y a los overrides manuales.

## Deploy
`deploy_backend.sh` (correr desde una PC con la llave `.pem`): scp a las 3 rutas + reinicia los 3 servicios + `--test` del parser + verificación HTTP.
- El **venv** `/home/ubuntu/venv` ya trae todas las deps. **No** usar el pip del sistema (Ubuntu lo bloquea, PEP 668). Usar `/home/ubuntu/venv/bin/pip`.
- `.env` de systemd: comentarios **en su propia línea** (systemd no ignora comentarios en línea).

## Seguridad — NO commitear
`.gitignore` ya excluye: `*.pem`, `*.p12`, `*.key`, `*.jks`, `firebase.json`, `*.env` (salvo `.env.example`), `kyc_store.json`.
Nunca poner llaves/tokens en el repo ni en claro. Los secretos viven en `geomb.env` / drop-in systemd / `firebase.json`, solo en el EC2.

## Pendientes backend
- L2 Express: capturar patrón de paradas.
- Integrar TripUpdates GTFS-rt de Metrobús (arribos por estación, unidades sin GPS).
- Limpiar unidades fantasma también en el server (hoy el filtro es del cliente).
