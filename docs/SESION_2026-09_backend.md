# Registro de sesión — Backend GeoMB (sept 2026)

Bitácora detallada de los cambios de backend de esta sesión, de inicio a fin, con decisiones, bugs y gotchas.
Fin: mudanza a Claude Code (trabajar desde el repo `metrobus_app`). Producción: `https://geomb.duckdns.org` (EC2, IP 78.14.99.249).

---

## 0. Contexto de infra durante la sesión
- El **workspace de bash del asistente estuvo caído** (una actualización de Windows del 8-sep rompió el montaje virtiofs/Plan9).
  Por eso NO se pudo ejecutar python/bash/git desde el asistente; se trabajó con herramientas de archivo + `web_fetch`, y todos los
  comandos de servidor/git los corrió el usuario en su Git Bash (MINGW64).
- Gotcha recurrente: al pegar en la terminal se colaba un carácter invisible antes de `cd`/`ssh` (`$'\302\226'`) → "command not found".
  Solución: reescribir el comando a mano.
- Gotcha MSYS: en Git Bash, `openssl req -subj "/CN=..."` se mutila (convierte `/CN` en ruta de Windows) y `sudo` no existe →
  los `openssl` de generación de certificados hay que correrlos **dentro del servidor** por SSH.

## 1. Layout REAL del servidor (descubierto en la sesión)
Tres servicios systemd, en rutas distintas:
- **`metrobus-web.service`** → `app.py` con `gunicorn app:app`, `WorkingDirectory=/home/ubuntu/metrobus_app`,
  `EnvironmentFile=/home/ubuntu/geomb.env` (+ drop-in `metrobus-web.service.d/kyc.conf` con llaves Didit). gunicorn en `127.0.0.1:8000`.
- **`metrobus-push.service`** → `python /home/ubuntu/push_metrobus.py`, `WorkingDirectory=/home/ubuntu`,
  `Environment=GOOGLE_APPLICATION_CREDENTIALS=/home/ubuntu/firebase.json`.
- **`mexibus-afectaciones.service`** → `python /opt/geomb-afect/mexibus_afectaciones.py`, env `/opt/geomb-afect/.env`.
- venv en `/home/ubuntu/venv` (trae Flask 3.1, gunicorn, gtfs-realtime-bindings, firebase-admin, beautifulsoup4, boto3).
  **PEP 668:** el pip del sistema está bloqueado; usar `/home/ubuntu/venv/bin/pip`.
- nginx (Certbot/Let's Encrypt) al frente; `sites-available/geomb` (server `:443` + redirect `:80`). Certs en `/etc/letsencrypt/live/geomb.duckdns.org/`.
- **Importante:** el `app.py` que corre es `/home/ubuntu/metrobus_app/app.py` (repo con `.git`), NO `/home/ubuntu/app.py` (que no existe).
  `geomb.env` solo tenía `PARTNER_USER/PARTNER_PASS`; se le agregó `ADMIN_TOKEN` y `GOOGLE_APPLICATION_CREDENTIALS`.

## 2. requirements.txt completo
El original solo listaba `requests` y `firebase-admin`, pero el código importa más. Se completó con:
`flask`, `requests`, `gtfs-realtime-bindings` (provee `google.transit`), `firebase-admin`, `beautifulsoup4` (bs4), `boto3` (Polly TTS);
`fastapi`/`uvicorn` comentados (solo si se usa el webhook de `mexibus_afectaciones`). (En el server el venv ya las tenía, así que no hubo pip.)

## 3. Aviso manual de afectaciones — `admin_afect.py` (NUEVO)
Blueprint Flask registrado en `app.py`. Formulario móvil `GET/POST /admin/afectacion` (campos a mano: línea, estado, lugar, info,
tramos de circuito opcional). Cubre **Metrobús L1–L7 y Mexibús/ramales** (Mexicable se quitó a pedido del usuario).
- Empuja FCM al topic `afectaciones` reusando `push_metrobus._push` (misma credencial Firebase, no se duplica).
- Escribe un **override** en `data/afect_manual.json` con `expira` (ver §5). "Servicio restablecido" quita la línea.
- Al inicio se protegía con `ADMIN_TOKEN`; luego se cambió a **mTLS** (ver §7), quedando el token solo como respaldo local.

## 4. Estado de Metrobús persistente — `push_metrobus.py`
Antes solo empujaba FCM (transitorio) → el panel de Metrobús "no actualizaba". Se agregó `_escribir_estado_metrobus()` que
escribe un snapshot del estado raspado (solo líneas afectadas) a `data/afect_metrobus.json` cada ciclo. Así el panel de Metrobús
**persiste** igual que Mexibús. (La app Android `AfectacionesMexibus` NO filtra por rango de línea, así que ingiere 1–7 y 10X+.)

## 5. Panel = fusión al vuelo + caducidad — `app.py`
`/data/afectaciones_mexibus.json` dejó de ser archivo estático: ahora se **calcula en cada request** mezclando por línea:
1. Feed Mexibús (`AFECT_MXB_OUT` = `data/afectaciones_mexibus.json`).
2. Estado Metrobús (`data/afect_metrobus.json`).
3. Overrides manuales (`data/afect_manual.json`).
Reglas: el **manual gana** mientras no venza; si una línea trae **varias afectaciones a la vez, se combinan** en una fila
(junta estado/lugar/info con ` / ` y ` · `; helper `_unicos`). No se escribe archivo, así el feed no pisa lo manual.
- **Caducidad:** override manual `expira = min(now + AFECT_DUR_H h, 23:59 CDMX)` (default 7 h). Feed y estado Metrobús caducan con
  `MXB_ESTADO_TTL` (min; se bajó de 720 a **420 = 7 h**; rango 300–540 = 5–9 h) **y tope duro a las 23:59 CDMX** del día del post
  (`_fin_del_dia_local`, `zoneinfo America/Mexico_City`). Confirmado en `mexibus_afectaciones.computar_estado`.

## 6. Ramal L3A por hashtag — `mexibus_afectaciones.py`
En `lineas_en_texto`, un post con `#ampliación` + línea 3 ⇒ **L3A (113)** (el `#MexibusLinea3` es la troncal). Espejo de la
regla "Servicio Eléctrico"→L2A. Motivo: los posts de SITRAMYTEM etiquetan el ramal como `#ampliación`, no como "3a".
(Ojo: algunos posts del mismo incidente omiten `#ampliación` y caen en L3 troncal; se aceptó esa limitación.)

## 7. Seguridad del panel — mTLS con certificado cliente
En vez de token tecleado, se protegió `/admin` con **certificado cliente (mTLS)**:
- Se generó (en el server) una **CA propia** + cert cliente y se empaquetó en `geomb-admin.p12` (llave **dedicada**, NO la .jks de firma del APK).
  CA en `/etc/nginx/certs/geomb-admin-ca.crt`.
- **nuevo server block nginx `:8443`** (`sites-available/geomb-admin`) con `ssl_verify_client on` → exige el cert; `location /admin/` hace proxy a `:8000`
  y pasa `X-Client-Verify $ssl_client_verify`; todo lo demás `404`.
- En el `:443` público se agregó `location /admin/ { return 404; }` para que **no** se pueda alcanzar el panel (ni falsear el header) por ahí.
- Flask (`admin_afect._token_ok`) confía en `X-Client-Verify: SUCCESS`; `ADMIN_TOKEN` queda como respaldo local.
- Panel: `https://geomb.duckdns.org:8443/admin/afectacion`. Requiere **abrir el puerto 8443 en el Security Group de AWS** e importar el `.p12` al teléfono
  (Ajustes → Seguridad → Instalar certificado → *certificado de VPN y apps*). Un `400 "No required SSL certificate was sent"` = mTLS OK, falta presentar el cert.

## 8. Deploy — `deploy_backend.sh`
Script (se corre desde la PC con la llave `.pem`) que: agrega env al web (ADMIN_TOKEN + Firebase), baja el TTL del feed a 420,
respalda y sube `app.py`+`admin_afect.py`+`push_metrobus.py` a `metrobus_app/`, `push_metrobus.py` a `/home/ubuntu`, `mexibus_afectaciones.py`
a `/opt/geomb-afect` (vía /tmp + sudo), corre `--test` del parser, reinicia los **3** servicios y verifica HTTP + imprime el token.
Asume sudo sin contraseña (default EC2). Un error inicial apuntó a `/home/ubuntu/backend` (carpeta inexistente) → se corrigió a las rutas reales.

## 9. Git
Repo backend: **github.com/gutierrez-arquieta-abraham/metrobus_app** (rama `master`). Los archivos de trabajo estaban en la RAÍZ de
`Escritorio/GeoMB`, pero el repo vive en `metrobus_app/` → se copian ahí antes de commitear. Se reforzó `.gitignore` (excluye
`*.pem`, `*.p12`, `*.key`, `*.jks`, `firebase.json`, `*.env` salvo `.env.example`, `kyc_store.json`). Un push rebotó por `non-fast-forward`
(origin adelante) → se resolvió con `git pull --rebase origin master` + `git push`.

## 10. Verificación (por `web_fetch`, endpoints públicos)
- `/data/afect_metrobus.json` existe y con datos → `push_metrobus` desplegado y escribiendo. ✔
- `/data/afectaciones_mexibus.json` sirve la fusión (aparecen líneas Metrobús 4/6/7). ✔ Tras el fix de combinación, la línea 4 muestra
  sus **dos** segmentos juntos (antes se perdía uno). ✔
- `/admin/afectacion` en `:443` responde 404 (bloqueo). ✔  `/health`: `ultimo_feed_timestamp` poblado. ✔

## 11. Documentación / memoria
- `MEMORIA.md` (repo Android): sección Backend reescrita con todo lo anterior.
- `CLAUDE.md` (repo `metrobus_app`): contexto para Claude Code.
- Memoria del proyecto: nota durable de GeoMB (app + backend + repo + mudanza a Claude Code).

## 12. Secretos
Durante la sesión se **expusieron en el chat** (al pegar logs) llaves de **Didit** y el `ADMIN_TOKEN`. **El usuario ya los rotó**;
los valores anteriores quedaron inválidos. Regla permanente: secretos (firebase.json, .pem, tokens) solo en el EC2 (`geomb.env` /
drop-in systemd), nunca al repo ni en claro.

## 13. Pendientes
- L2 Express: capturar patrón de paradas para rutearlo como variante.
- Integrar TripUpdates GTFS-rt de Metrobús (arribos por estación, unidades sin GPS).
- Limpiar unidades fantasma también en el server (hoy el filtro es del cliente).
- `mexibus_afectaciones.py` es un servicio aparte (`/opt/geomb-afect`); si se desea, versionarlo en su propio repo.
- L2A (Mexibús 112): el trazo del circuito seguía sin quedar; el usuario iba a mandar un KML/GeoJSON correcto (app Android, no backend).
