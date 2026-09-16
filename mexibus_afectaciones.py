# -*- coding: utf-8 -*-
"""
mexibus_afectaciones.py  —  backend GeoMB (FastAPI / EC2)
---------------------------------------------------------
Sondea la página oficial de SITRAMYTEM en Facebook, interpreta las
publicaciones de afectaciones del Mexibús y las empuja por Firebase Cloud
Messaging (topic "afectaciones") en EXACTAMENTE el formato que consume la app
Android (MensajesService.notificarAfectacion):

    data = {
        "tipo":   "afectacion",     # opcional; la app usa afectacion por defecto
        "linea":  "102",            # código de línea de la app (ver LINEA_COD)
        "estado": "Sin servicio",   # razón general (contiene 'restablec' = restablecido)
        "lugar":  "Tultitlán",      # zona/estación afectada (puede ir vacío)
        "info":   "debido al alto nivel de agua..."  # detalle visible
    }

La app arma el texto visible como:  "Línea N · {lugar}\n{info}"  y deduplica por
"linea|estado|lugar" (el 'estado' NO se muestra: sirve para dedup y para detectar
'restablecido'). Por eso el detalle legible va en 'info'.

Facebook: leer posts de una página que NO administras requiere "Page Public
Content Access" (revisión de app) o un token de página con permisos. Si no lo
tienes, deja `fetch_posts` como punto de inyección: aliméntalo con textos de
posts desde cualquier fuente — el parser y el envío FCM funcionan igual.

Fuentes (env AFECT_SOURCE):
  · "rss"       → lee RSS_URL (p. ej. RSS.app de la cuenta de X/FB). Recomendado: sin claves ni review.
  · "x"         → API de X v2 con X_BEARER (resuelve @X_USER y usa since_id para pagar solo lo nuevo).
  · "facebook"  → Graph API (requiere Page Public Content Access; poco viable si no administras la página).
  · webhook     → además, un endpoint FastAPI POST /webhook/afectacion para que Zapier/Make/IFTTT te
                  manden el texto del post (lo más "en vivo", sin polling ni costo de lectura).

Deps:  pip install requests firebase-admin          # + fastapi uvicorn  (solo para el webhook)
Uso:   python mexibus_afectaciones.py --test         # prueba el parser (sin red)
       python mexibus_afectaciones.py --once         # un sondeo y salir
       python mexibus_afectaciones.py                # loop continuo (usa AFECT_SOURCE)
       python mexibus_afectaciones.py --serve        # levanta el webhook (o: uvicorn mexibus_afectaciones:app)
"""

import os, re, json, time, unicodedata
from datetime import datetime, timezone

def _int_env(nombre, defecto):
    """int() tolerante: ignora comentarios en línea/espacios (systemd no los limpia)."""
    v = os.getenv(nombre, str(defecto)).split("#", 1)[0].strip()
    try: return int(v)
    except ValueError: return int(defecto)

# ------------------------------------------------------------------------ CONFIG (env)
# Fuente de posts: "rss" (recomendado), "x" (API de X) o "facebook" (Graph API).
AFECT_SOURCE     = os.getenv("AFECT_SOURCE", "rss").lower()
# RSS (p. ej. RSS.app de la cuenta de X/FB de SITRAMYTEM):
RSS_URL          = os.getenv("RSS_URL", "")
# X (Twitter) API v2:
X_BEARER         = os.getenv("X_BEARER", "")
X_USER           = os.getenv("X_USER", "sitramytem")
# Facebook Graph API (solo si tienes Page Public Content Access):
FB_PAGE_ID       = os.getenv("FB_PAGE_ID", "sitramytem")
FB_TOKEN         = os.getenv("FB_PAGE_TOKEN", "")
FB_API_VER       = os.getenv("FB_API_VERSION", "v21.0")
# FCM / estado:
FCM_TOPIC        = os.getenv("FCM_TOPIC", "afectaciones")
GOOGLE_CREDS     = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
ESTADO_PATH      = os.getenv("MXB_AFECT_STATE", "/tmp/mxb_afect_state.json")
CONTENIDO_PATH   = os.getenv("MXB_CONTENIDO_STATE", "/tmp/mxb_contenido_state.json")   # dedup por línea+estado+lugar
XCACHE_PATH      = os.getenv("MXB_XCACHE", "/tmp/mxb_x_cache.json")   # since_id + user_id de X
POLL_SECONDS     = _int_env("MXB_AFECT_POLL", 60)
MAX_POST_AGE_MIN = _int_env("MXB_AFECT_MAX_AGE", 90)
DRY_RUN          = os.getenv("MXB_AFECT_DRYRUN", "0").split("#", 1)[0].strip() == "1"
# Estado actual (para el panel de la app): JSON que sirve tu FastAPI en /data/.
AFECT_MXB_OUT    = os.getenv("AFECT_MXB_OUT", "").split("#", 1)[0].strip()
MXB_ESTADO_TTL   = _int_env("MXB_ESTADO_TTL", 420)   # min. que dura un aviso sin novedad (def. 7 h; usa 300–540 = 5–9 h)

# ---------------------------------------------------------------- normalización de texto
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()

# --------------------------------------------------- detección de líneas → código de app
# Códigos de la app (GtfsRepository): Mexibús 101-104, ramales 111-113, Mexicable 201-202.
# Tolerante a "línea" mal escrita (linia/lnea/line) y a formas cortas ("L1", "MexibúsL1",
# "mexibus l1"), SIN falsos positivos (exige 'mexibus'+L pegado, o un límite de palabra antes
# del L/linea). Orden: ramales/alias ANTES que troncales para que "1a" no caiga en "1".
_LW = r"(?:linea|linia|lnea|line|l)"   # "línea" y variantes + "L"
def _pat(n, ramal=False):
    suf = r"a\b" if ramal else r"\b"   # ramal: "1a" pegado (no "1 a", que es "…1 a la altura…")
    # pegado a "mexibus" (mexibuslinea4 / mexibusl1)  ó  con límite de palabra antes (linea 4 / l1)
    return (r"(?:mexibus\s*" + _LW + r"\s*" + str(n) + suf
            + r"|\b" + _LW + r"\s*" + str(n) + suf + r")")

LINEA_COD = [
    (r"aifa|afia|aeropuerto|felipe angeles|terminal de pasajeros", 111, "L1A"),
    (_pat(1, ramal=True),                                     111, "L1A"),
    (r"servicio\s*electric\w*|servicioelectric\w*",          112, "L2A"),   # L2A = "Servicio Eléctrico" (tolera "electrici", etc.)
    (_pat(2, ramal=True),                                     112, "L2A"),
    (_pat(3, ramal=True),                                     113, "L3A"),   # (normalmente la etiquetan como L3)
    (r"mexicable\s*" + _LW + r"\s*1|linea roja",             201, "MXC L1"),
    (r"mexicable\s*" + _LW + r"\s*2",                        202, "MXC L2"),
    (_pat(1),                                                 101, "L1"),
    (_pat(2),                                                 102, "L2"),
    (_pat(3),                                                 103, "L3"),
    (_pat(4),                                                 104, "L4"),
]

def lineas_en_texto(tn: str):
    enc = {}
    for rx, cod, lab in LINEA_COD:
        if re.search(rx, tn):
            enc.setdefault(cod, lab)
    # "Servicio Eléctrico" = L2A: su "#MexibusLinea2" es solo el corredor troncal → deja L2A, quita L2.
    if re.search(r"servicio\s*electric|servicioelectric", tn) and 112 in enc:
        enc.pop(102, None)
    # "Ampliación" de L3 = ramal L3A: sus posts la etiquetan "#MexibusLinea3" (troncal) + "#ampliación"
    # (sin el "3a" pegado). Si aparece "ampliacion" junto a L3, es la L3A → deja L3A y quita L3.
    if re.search(r"ampliacion", tn) and 103 in enc:
        enc[113] = "L3A"
        enc.pop(103, None)
    return enc

# ----------------------------------------------------------------- estado (razón general)
def estado_de(tn: str) -> str:
    if re.search(r"restablec|reanud|normaliz|opera con normalidad", tn):
        return "Servicio restablecido"
    if re.search(r"realiza circuito|se realiza circuito|\bcircuito\b", tn):
        return "Servicio parcial"   # el circuito manda sobre 'suspende': la línea corre parcial
    if re.search(r"suspend|sin servicio|cierre total|se cierra", tn):
        return "Sin servicio"
    if re.search(r"retras|avance lento|servicio lento|marcha lenta|demora|\blento\b", tn):
        return "Retraso en el servicio"
    if re.search(r"omite acople|pasa de largo|sin parada|no se detiene", tn):
        return "Paso de largo"     # estación suelta que no se atiende (la línea sigue)
    if re.search(r"estacion(es)? cerrad|cerrad", tn):
        return "Estación cerrada"
    return "Afectación en el servicio"

# ------------------------------------------------------------------- lugar (zona/estación)
# Antes el corte era solo por [,.;\n] con un tope duro de 35 caracteres: si la coma quedaba más
# lejos que eso (p. ej. "a la altura de la Estación Laureles y Clínica 93 POR TRABAJOS DE
# reencarpetamiento, ...") el lugar salía cortado a la mitad de una palabra ("...93 po"). Ahora
# la captura es perezosa y también se detiene en conectores comunes ("por", "debido a", etc.)
# aunque la coma esté lejos, y se le quita el artículo inicial ("la Estación..." -> "Estación...").
_LUGAR_FIN = r"(?:[,.;\n]|\s+(?:por|debido|ya que|mientras|hasta|se\s)\b)"

def lugar_de(texto: str) -> str:
    for rx in [r"a la altura de ([A-Za-zÁÉÍÓÚÑáéíóúñ0-9][\wáéíóúñ.\- ]{2,35}?)(?=" + _LUGAR_FIN + r"|$)",
               r"zona de ([A-ZÁÉÍÓÚÑ][\wáéíóúñ.\- ]{2,35}?)(?=" + _LUGAR_FIN + r"|$)",
               r"estaci[oó]n(?:es)? ([A-ZÁÉÍÓÚÑ][\wáéíóúñ.\- ]{2,35}?)(?=" + _LUGAR_FIN + r"|$)"]:
        m = re.search(rx, texto)
        if m:
            return re.sub(r"^(el|la|los|las)\s+", "", m.group(1).strip(), flags=re.I)
    return ""

# ------------------------------------------------------------------- info (detalle limpio)
def info_de(texto: str) -> str:
    t = re.sub(r"#\w+", "", texto)                     # quita hashtags
    t = re.sub(r"[⚠️]", "", t)               # quita ⚠ / variation selector
    t = re.sub(r"(?i)tome sus precauciones|ver menos|ver mas", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", " · ", t.strip())          # multilínea → " · " (p. ej. tramos de circuito)
    t = t.strip(" ·,.-")                                # limpia comas/puntos/bullets colados de los hashtags
    t = re.sub(r"^(y|e)\s+", "", t, flags=re.I)         # quita conjunción inicial ("y debido...")
    return t.strip(" ·,.-")

# ------------------------------------------------------- tramos del circuito ("A - B")
def segmentos_circuito(texto: str):
    """Pares [terminal, estacion] de los tramos que SÍ operan cuando hay circuito."""
    if "circuito" not in _norm(texto):
        return []
    segs = []
    for ln in texto.split("\n"):
        ln = ln.strip(" .")
        if not ln or ln.startswith("#") or "circuito" in ln.lower():
            continue
        m = re.match(r"^([A-Za-zÁÉÍÓÚÑáéíóúñ0-9.\s]{3,32}?)\s+-\s+([A-Za-zÁÉÍÓÚÑáéíóúñ0-9.\s]{3,32}?)$", ln)
        if m:
            segs.append([m.group(1).strip(), m.group(2).strip()])
    return segs

# --------------------------------------------------------------------- parser de un post
def parse_post(texto: str):
    tn = _norm(texto)
    if not any(k in tn for k in ("mexibus", "mexicable", "aifa", "afia",
                                 "servicio electrico", "servicioelectrico")):
        return []
    lineas = lineas_en_texto(tn)
    if not lineas:
        return []
    estado = estado_de(tn)
    lugar  = lugar_de(texto)
    info   = info_de(texto)
    circ   = segmentos_circuito(texto)
    return [{"tipo": "afectacion", "linea": str(cod), "etiqueta": lab,
             "estado": estado, "lugar": lugar, "info": info, "circuito": circ}
            for cod, lab in lineas.items()]

# ------------------------------------------------------------------------ FUENTES DE POSTS
def fetch_posts():
    """Despacha según AFECT_SOURCE. Devuelve [{'id','message','created_time':datetime}]."""
    if AFECT_SOURCE == "x":        return fetch_posts_x()
    if AFECT_SOURCE == "facebook": return fetch_posts_facebook()
    return fetch_posts_rss()       # por defecto: RSS

# --- RSS (RSS.app u otro puente de la cuenta de X/FB) -------------------------------------
# Si un feed falla (p. ej. RSS.app devolviendo 402 "Payment Required" porque la cuenta se quedó
# sin plan/cupo), reintentar cada POLL_SECONDS lo único que logra es llenar el log de warnings
# idénticos y gastar peticiones contra un servicio que ya sabemos que va a fallar. Cada URL entra
# en un "cooldown" que crece (backoff exponencial, tope RSS_BACKOFF_MAX_S) mientras siga fallando,
# y se resetea en cuanto vuelve a responder bien.
RSS_BACKOFF_MAX_S = 1800   # tope de espera entre reintentos por feed (30 min)
_rss_estado = {}           # url -> {"proximo": epoch del próximo intento permitido, "fallos": int}

def fetch_posts_rss():
    """Lee uno o VARIOS feeds RSS (RSS_URL separados por coma) y usa el título como texto."""
    import requests, xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    urls = [u.strip() for u in RSS_URL.split(",") if u.strip()]
    if not urls:
        raise RuntimeError("Falta RSS_URL")
    out = []
    ahora = time.time()
    for url in urls:
        st = _rss_estado.get(url, {"proximo": 0, "fallos": 0})
        if ahora < st["proximo"]:
            continue   # en cooldown tras fallos repetidos: no insiste este ciclo
        try:
            r = requests.get(url, timeout=20); r.raise_for_status()
            root = ET.fromstring(r.content)
            _rss_estado[url] = {"proximo": 0, "fallos": 0}   # se recuperó
        except Exception as e:
            fallos = st["fallos"] + 1
            espera = min(RSS_BACKOFF_MAX_S, POLL_SECONDS * (2 ** min(fallos, 6)))
            _rss_estado[url] = {"proximo": ahora + espera, "fallos": fallos}
            print(f"[warn] RSS {url}: {e} (reintenta en {int(espera)}s)")
            continue
        for item in root.iter("item"):
            titulo = item.findtext("title") or ""
            # La DESCRIPCIÓN conserva los saltos de línea (<br>) → tramos del circuito en líneas aparte.
            desc = item.findtext("description") or ""
            desc = re.sub(r"(?i)<br\s*/?>", "\n", desc)          # <br> → salto de línea
            desc = re.sub(r"<[^>]+>", " ", desc)                 # quita el resto de HTML
            # Quita el pie de firma que RSS.app agrega a TODOS los embeds de X, no solo los de
            # Mexibús Informa: "— <Nombre de la cuenta> (@handle) <fecha>". Antes solo se quitaba
            # el de "Mexibús Informa" específicamente; al agregar cuentas por línea (@MexibusL2,
            # @Mexibuslll, @MexiBus_4) su propio nombre ("Mexibus L2", etc.) se quedaba pegado al
            # texto, y como ese nombre por sí mismo hace match con el patrón de línea (p. ej.
            # "mexibus l2" cae en el patrón de "mexibus linea 2"), un tuit sin ningún texto real
            # (solo un video) se interpretaba como afectación real de esa línea.
            desc = re.sub(r"—\s*.+?\(@[\w.]+\).*$", "", desc, flags=re.S)
            texto = desc.strip() or titulo.strip()
            if not texto:
                continue
            pub = item.findtext("pubDate")
            try:    dt = parsedate_to_datetime(pub) if pub else datetime.now(timezone.utc)
            except Exception: dt = datetime.now(timezone.utc)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            # id estable = guid o link (evita reprocesar el mismo post entre feeds/rondas)
            out.append({"id": item.findtext("guid") or item.findtext("link") or titulo,
                        "message": texto, "created_time": dt})
    return out

# --- X (Twitter) API v2 ------------------------------------------------------------------
def _xcache():
    try:
        with open(XCACHE_PATH, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def _xcache_save(c):
    try:
        with open(XCACHE_PATH, "w", encoding="utf-8") as f: json.dump(c, f)
    except Exception as e: print("[warn] xcache:", e)

def fetch_posts_x():
    """Tuits nuevos de @X_USER usando el Bearer (env X_BEARER) y since_id persistido."""
    import requests
    if not X_BEARER:
        raise RuntimeError("Falta X_BEARER")
    h = {"Authorization": "Bearer " + X_BEARER}
    c = _xcache()
    uid = c.get("uid")
    if not uid:   # resuelve username → id una sola vez
        ru = requests.get(f"https://api.x.com/2/users/by/username/{X_USER}", headers=h, timeout=20)
        ru.raise_for_status()
        uid = ru.json()["data"]["id"]; c["uid"] = uid; _xcache_save(c)
    params = {"max_results": 10, "tweet.fields": "created_at"}
    if c.get("since_id"): params["since_id"] = c["since_id"]
    r = requests.get(f"https://api.x.com/2/users/{uid}/tweets", headers=h, params=params, timeout=20)
    r.raise_for_status()
    data = r.json().get("data", [])
    out, maxid = [], c.get("since_id")
    for t in data:
        ct = t.get("created_at")
        dt = datetime.fromisoformat(ct.replace("Z", "+00:00")) if ct else datetime.now(timezone.utc)
        out.append({"id": t["id"], "message": t.get("text", ""), "created_time": dt})
        if maxid is None or int(t["id"]) > int(maxid): maxid = t["id"]
    if maxid: c["since_id"] = maxid; _xcache_save(c)
    return out

# --- Facebook Graph API (requiere Page Public Content Access) -----------------------------
def fetch_posts_facebook():
    import requests
    url = f"https://graph.facebook.com/{FB_API_VER}/{FB_PAGE_ID}/posts"
    params = {"fields": "id,message,created_time", "limit": 15, "access_token": FB_TOKEN}
    r = requests.get(url, params=params, timeout=20); r.raise_for_status()
    out = []
    for p in r.json().get("data", []):
        if not p.get("message"):
            continue
        ct = p.get("created_time")
        dt = datetime.fromisoformat(ct.replace("Z", "+00:00")) if ct else datetime.now(timezone.utc)
        out.append({"id": p.get("id"), "message": p["message"], "created_time": dt})
    return out

# ---------------------------------------------------------------- estado persistente (dedup)
def _cargar_estado():
    try:
        with open(ESTADO_PATH, encoding="utf-8") as f: return set(json.load(f))
    except Exception: return set()

def _guardar_estado(claves):
    try:
        with open(ESTADO_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(claves), f, ensure_ascii=False)
    except Exception as e: print("[warn] no se pudo guardar estado:", e)

def _clave(a):  # misma clave de dedup que la app
    return f"{a['linea']}|{a['estado'].lower()}|{a['lugar'].lower()}"

# ------------------------------------------- dedup por CONTENIDO (varias cuentas, mismo aviso)
def _cargar_contenido():
    try:
        with open(CONTENIDO_PATH, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def _guardar_contenido(mapa):
    try:
        with open(CONTENIDO_PATH, "w", encoding="utf-8") as f:
            json.dump(mapa, f, ensure_ascii=False)
    except Exception as e: print("[warn] no se pudo guardar estado de contenido:", e)

# ------------------------------------------------------------------------------ envío FCM
_fcm_ready = False
def _init_fcm():
    global _fcm_ready
    if _fcm_ready: return
    import firebase_admin
    from firebase_admin import credentials
    if not firebase_admin._apps:
        cred = credentials.Certificate(GOOGLE_CREDS) if GOOGLE_CREDS else credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    _fcm_ready = True

def enviar_fcm(a):
    data = {"tipo": "afectacion", "linea": a["linea"],
            "estado": a["estado"], "lugar": a["lugar"], "info": a["info"]}
    if DRY_RUN:
        print("  [DRY] FCM →", json.dumps(data, ensure_ascii=False)); return
    _init_fcm()
    from firebase_admin import messaging
    messaging.send(messaging.Message(
        data=data, topic=FCM_TOPIC,
        android=messaging.AndroidConfig(priority="high")))

# ---------------------------------------------- estado actual (para el panel de la app)
def _fin_del_dia_local(ts):
    """Epoch de las 23:59 (hora CDMX) del día de 'ts' (tope duro de vigencia de un aviso)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Mexico_City")
    except Exception:
        from datetime import timedelta
        tz = timezone(timedelta(hours=-6))
    d = datetime.fromtimestamp(ts, tz).replace(hour=23, minute=59, second=0, microsecond=0)
    return d.timestamp()


def computar_estado(posts):
    """Situación vigente por línea: el post MÁS RECIENTE (dentro del TTL y del mismo día)
       manda; si es 'reanudado' o no hay, la línea está OK. Devuelve lista para el JSON."""
    ahora = datetime.now(timezone.utc)
    reciente = {}   # linea -> afectación más reciente
    for post in sorted(posts, key=lambda p: p["created_time"], reverse=True):
        if (ahora - post["created_time"]).total_seconds() / 60 > MXB_ESTADO_TTL:
            continue
        # tope duro: nunca pasado las 23:59 (CDMX) del día en que se publicó
        if ahora.timestamp() > _fin_del_dia_local(post["created_time"].timestamp()):
            continue
        for a in parse_post(post["message"]):
            reciente.setdefault(a["linea"], a)   # el primero (más reciente) por línea gana
    out = []
    for ln, a in reciente.items():
        if "restablec" in a["estado"].lower():
            continue   # su última noticia es que se reanudó → no está afectada
        e = {"linea": int(ln), "estado": a["estado"], "lugar": a["lugar"], "info": a["info"]}
        if a.get("circuito"):
            e["circuito"] = a["circuito"]   # tramos que SÍ operan (la app habilita solo esos)
        out.append(e)
    out.sort(key=lambda x: x["linea"])
    return out

def escribir_estado(posts):
    """Escribe afectaciones_mexibus.json (estado actual) de forma atómica."""
    if not AFECT_MXB_OUT:
        return
    data = {"actualizado": int(time.time()), "afectaciones": computar_estado(posts)}
    try:
        tmp = AFECT_MXB_OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, AFECT_MXB_OUT)   # atómico
    except Exception as e:
        print("[warn] escribir_estado:", e)

# --------------------------------------------------------------------------- ciclo principal
def procesar_una_vez():
    vistos = _cargar_estado(); enviados = 0
    contenido = _cargar_contenido()
    try:
        posts = fetch_posts()
    except Exception as e:
        print("[error] fetch_posts:", e); return 0
    ahora = datetime.now(timezone.utc)
    ahora_ts = ahora.timestamp()
    ids_actuales = {str(p.get("id")) for p in posts}
    for post in sorted(posts, key=lambda p: p["created_time"]):
        if (ahora - post["created_time"]).total_seconds() / 60 > MAX_POST_AGE_MIN:
            continue
        for a in parse_post(post["message"]):
            # Dedup POR PUBLICACIÓN + línea: cada post se empuja UNA sola vez (incl. "restablecido").
            k = str(post.get("id")) + "|" + a["linea"]
            if k in vistos:
                continue
            # Dedup POR CONTENIDO (línea+estado+lugar): con varias cuentas vigilando la misma línea
            # (SITRAMYTEM/MexibusInforma + la cuenta específica de esa línea, p. ej. @MexibusL2), el
            # MISMO aviso real llega como posts DISTINTOS (id distinto) desde cuentas distintas. Sin
            # esto se mandaba un push duplicado por cada cuenta que republicara el mismo aviso. Vigente
            # mientras el aviso lo esté (MXB_ESTADO_TTL), igual que el panel de la app.
            ck = _clave(a)
            ya_enviado = ck in contenido and (ahora_ts - contenido[ck]) / 60 <= MXB_ESTADO_TTL
            vistos.add(k)   # no reevaluar este post de nuevo, se haya mandado o no
            if ya_enviado:
                continue
            enviar_fcm(a); enviados += 1
            contenido[ck] = ahora_ts
    # Poda: conserva solo claves de posts aún presentes en el feed (evita crecer sin límite; los
    # que salen del feed ya no se reprocesan, así que no se reenviarán aunque se olviden).
    vistos = {k for k in vistos if k.split("|", 1)[0] in ids_actuales}
    contenido = {k: ts for k, ts in contenido.items() if (ahora_ts - ts) / 60 <= MXB_ESTADO_TTL}
    _guardar_estado(vistos)
    _guardar_contenido(contenido)
    escribir_estado(posts)   # actualiza el estado actual para el panel de la app
    return enviados

def loop():
    print(f"[mexibus_afectaciones] topic={FCM_TOPIC} dry={DRY_RUN} cada {POLL_SECONDS}s")
    while True:
        try:
            n = procesar_una_vez()
            if n: print(f"[{datetime.now():%H:%M:%S}] {n} afectación(es) enviada(s)")
        except Exception as e:
            print("[error] loop:", e)
        time.sleep(POLL_SECONDS)

# ------------------------------------------------ procesar un texto suelto (para webhook)
def procesar_texto(texto: str):
    """Interpreta un texto de post, deduplica y empuja por FCM. Devuelve cuántas envió."""
    vistos = _cargar_estado(); enviados = 0
    for a in parse_post(texto or ""):
        k = _clave(a)
        if "restablec" in a["estado"].lower():
            vistos = {c for c in vistos if not c.startswith(a["linea"] + "|")}
        elif k in vistos:
            continue
        enviar_fcm(a); enviados += 1
        if "restablec" not in a["estado"].lower():
            vistos.add(k)
    _guardar_estado(vistos)
    return enviados

# ------------------------------------------------ webhook FastAPI (Zapier / Make / IFTTT)
# Corre con:  uvicorn mexibus_afectaciones:app --host 0.0.0.0 --port 8000
# Zapier/Make: disparador "nuevo tuit de @SITRAMYTEM" → POST JSON {"text": "<contenido>"}.
try:
    from fastapi import FastAPI, Request
    app = FastAPI(title="GeoMB · afectaciones Mexibús")

    @app.get("/health")
    def _health():
        return {"ok": True, "source": AFECT_SOURCE}

    @app.post("/webhook/afectacion")
    async def _webhook(req: Request):
        texto = ""
        try:
            body = await req.json()
            texto = body.get("text") or body.get("message") or body.get("post") or ""
        except Exception:
            texto = (await req.body()).decode("utf-8", "ignore")
        return {"enviadas": procesar_texto(texto)}
except ImportError:
    app = None   # FastAPI no instalado: el resto (polling) sigue funcionando

# ----------------------------------------------------------------------------- autotest
def _autotest():
    ejemplos = [
        "#MexibúsLínea1, #AIFA, #MexibúsLínea3 y #MexibúsLínea4 debido a la presencia de lluvia "
        "en los corredores , se presenta avance lento. #MexibusInforma ⚠Tome sus precauciones⚠",
        "#MexibusLinea2, debido al intenso tráfico y al alto nivel d agua en la zona de Tultitlán, "
        "se presenta retraso en el servicio. #MexibusInforma ⚠Tome sus precauciones⚠",
        "#MexibusLinea2, debido al alto nivel d agua en la zona de Tultitlán, se suspende el "
        "servicio. #MexibusInforma ⚠Tome sus precauciones⚠",
        "#MexibusLinea1, a partir de este momento se reanuda el servicio, te solicitamos paciencia "
        "en lo que se restablece por completo. #MexibusInforma",
        "#MexibúslÍnea1, debido a bloqueo de colonos a la altura de Aquiles Serdán, se suspende el "
        "servicio.\nSe realiza circuito.\nCiudad Azteca - 1ro de Mayo\nCentral de Abastos - Ojo de Agua\n"
        "#MexibúsInforma ⚠Tome sus precauciones⚠",
        "#MexibúsLínea4 debido a choque de camión de transporte público que invade carril confinado "
        "a la altura de periférico, se realiza circuito.\nClínica 76 - UMB\n#MexibusInforma",
    ]
    for i, e in enumerate(ejemplos, 1):
        print(f"--- Ejemplo {i} ---")
        for a in parse_post(e):
            print(f"  linea={a['linea']} ({a['etiqueta']}) | estado={a['estado']} | "
                  f"lugar={a['lugar']!r} | info={a['info']!r}")

if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        _autotest()
    elif "--once" in sys.argv:
        procesar_una_vez()
    elif "--serve" in sys.argv:            # levanta el webhook FastAPI
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    else:
        loop()                             # polling continuo (RSS / X / Facebook)
