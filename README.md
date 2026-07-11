# Metrobús CDMX — Mapa en tiempo real

Mapa web de las 7 líneas del Metrobús con las unidades en vivo: iconos por
unidad, filtros (línea / destino / ruta / empresa), buscador por número
económico y listado de unidades en servicio.

Los datos vienen del feed oficial **GTFS-RT** de Metrobús (portal de datos
abiertos). El link oficial dura 12 h; la app lo **renueva sola** cada 11.5 h.

---

## Opción A — Desplegar en un hosting (GitHub → Render / Railway / Fly.io)

Todo corre en **un solo proceso** (`app.py`): sirve la web, descarga el feed
cada 25 s y renueva el link cada 11.5 h. Los secretos van en **variables de
entorno**, nunca en el repo.

### 1. Sube el proyecto a GitHub

```bash
cd metrobus_app
git init
git add .
git commit -m "Metrobus tiempo real"
git branch -M main
git remote add origin https://github.com/<tu-usuario>/<tu-repo>.git
git push -u origin main
```

`.gitignore` ya excluye los archivos con secretos, así que no se sube nada
sensible.

### 2. Crea el servicio en el hosting

En **Render** (ejemplo): New → Web Service → conecta el repo. Detecta
`render.yaml` solo. Si lo pide a mano:

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python app.py`

> Importante: usa **un solo worker/instancia**. La app trae sus propios hilos
> de fondo; con varios workers se dispararían correos de renovación duplicados.
> Por eso el arranque es `python app.py` y no `gunicorn` multi-worker.

### 3. Define las variables de entorno (en el panel del hosting)

| Variable | Valor |
|---|---|
| `GMAIL_ADDRESS` | `geometrobus@gmail.com` |
| `GMAIL_APP_PASSWORD` | tu contraseña de aplicación de Gmail (16 caracteres, sin espacios) |
| `MB_RESEND_URL` | `https://metrobus-gtfs.sinopticoplus.com/gtfs-api/senderEmailGtfs/1339/geometrobus@gmail.com` |
| `MB_RT_URL` | *(opcional)* un link de 12 h para arrancar ya con datos, sin esperar la primera renovación |
| `PORT` | *(no la pongas: la plataforma la inyecta sola)* |

Con eso, al desplegar la app arranca, pide el primer link, lo recibe por
correo y empieza a servir posiciones frescas. El estado se puede revisar en
`/health`.

### Modo sin correo (para probar el deploy rápido)

Si dejas vacías `GMAIL_*` y `MB_RESEND_URL`, el bot de renovación se apaga y
la app usa `MB_RT_URL` (o el snapshot incluido en `data/vehicles.json`).
Útil para confirmar que el despliegue funciona antes de conectar Gmail.

---

## Opción B — Correr en tu PC (local)

Sin hosting, con `app.py` igual:

```bash
cd metrobus_app
pip install -r requirements.txt

# define las variables (ejemplo en PowerShell de Windows):
$env:GMAIL_ADDRESS="geometrobus@gmail.com"
$env:GMAIL_APP_PASSWORD="tu_app_password"
$env:MB_RESEND_URL="https://metrobus-gtfs.sinopticoplus.com/gtfs-api/senderEmailGtfs/1339/geometrobus@gmail.com"

python app.py
```

Abre `http://localhost:8000`.

En Linux/Mac usa `export VAR=valor` en vez de `$env:`. También puedes copiar
`.env.example` a `.env` y cargarlo con tu herramienta preferida.

> Los scripts antiguos `backend/server.py` + `backend/auto_renew.py` (dos
> terminales, secretos en archivos `.txt`) siguen ahí como alternativa, pero
> `app.py` los reemplaza a ambos en un solo proceso. Si usas los viejos,
> copia los `*.example` de `backend/` quitándoles el `.example`.

---

## Empresas / concesionarios

`data/empresas.csv` mapea `numero_economico → empresa` (746 de 836 unidades
del snapshot ya asignadas). Edítalo y recarga la página; no hace falta
reiniciar. Formato:

```
numero_economico,empresa
1015,RTP
2658,CORENSA
...
```

---

## Estructura

```
metrobus_app/
├── app.py              ← proceso único (web + polling + renovación)
├── requirements.txt
├── Procfile            ← start command para el hosting
├── render.yaml         ← deploy declarativo en Render
├── runtime.txt         ← versión de Python
├── .env.example        ← plantilla de variables (sin valores)
├── .gitignore
├── index.html          ← mapa + panel (hace polling a /data/vehicles.json)
├── data/
│   ├── routes.json     ← 7 líneas: trazado, color, origen/destino
│   ├── stops.json      ← estaciones
│   ├── vehicles.json   ← snapshot de arranque (se sobrescribe en vivo)
│   └── empresas.csv    ← número económico → concesionario
└── backend/            ← versión antigua de 2 scripts (opcional)
```

---

## Notas

- **Seguridad:** genera una contraseña de aplicación de Gmail *nueva* si la
  anterior quedó expuesta, y ponla solo en las variables de entorno.
- **Límite del feed:** el link dura 12 h; por eso la renovación corre a 11.5 h.
- **Diagnóstico:** `GET /health` indica si hay link configurado, el timestamp
  del último feed y si el bot de renovación está activo.
