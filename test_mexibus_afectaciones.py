# ============================================================
# PRUEBAS : test_mexibus_afectaciones.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# Cubre parse_post() / estado_de(), en particular el default por feed (RSS_URL_LINEAS /
# parámetro linea_feed): las cuentas POR LÍNEA (@MexibusL2, @Mexibuslll, @MexiBus_4, "Alerta
# de Tránsito Mexibús L1 Page") casi nunca repiten "Mexibús"/"línea N" en el texto -- antes de
# este fix esos posts se descartaban en silencio (nunca pasaban el filtro de palabras clave).
# Ejecutar con:
#   pytest test_mexibus_afectaciones.py -v
import mexibus_afectaciones as m


def test_autotest_ejemplos_no_regresion():
    # Mismos 6 ejemplos de _autotest(): deben seguir detectando línea/estado sin linea_feed.
    assert m.parse_post(
        "#MexibúsLínea1, #AIFA, #MexibúsLínea3 y #MexibúsLínea4 debido a la presencia de lluvia "
        "en los corredores , se presenta avance lento. #MexibusInforma ⚠Tome sus precauciones⚠"
    ) and len(m.parse_post(
        "#MexibúsLínea1, #AIFA, #MexibúsLínea3 y #MexibúsLínea4 debido a la presencia de lluvia "
        "en los corredores , se presenta avance lento. #MexibusInforma ⚠Tome sus precauciones⚠"
    )) == 3

    r = m.parse_post(
        "#MexibusLinea2, debido al alto nivel d agua en la zona de Tultitlán, se suspende el "
        "servicio. #MexibusInforma ⚠Tome sus precauciones⚠"
    )
    assert r == [{"tipo": "afectacion", "linea": "102", "etiqueta": "L2",
                  "estado": "Sin servicio", "lugar": "Tultitlán",
                  "info": "debido al alto nivel d agua en la zona de Tultitlán, se suspende el servicio",
                  "circuito": []}]


def test_texto_generico_sin_mencion_de_linea_se_descarta():
    # Sin linea_feed (feed genérico: MexibusInforma/Mexicable), un texto que no diga nada de
    # Mexibús/línea se sigue descartando -- NO queremos falsos positivos en esos feeds.
    assert m.parse_post("Hoy tuvimos una gran jornada de trabajo con el equipo") == []
    assert m.parse_post("17:58 Bloqueo en Adolfo López") == []   # sin linea_feed


def test_cuenta_mexibus_4_post_real_sin_mencionar_mexibus():
    # Caso real reportado: @MexiBus_4 nunca dice "Mexibús"/"línea 4" -- antes se perdía.
    texto = ("Se suspende el servicio continuo debido a un bloqueo de manifestantes a altura de "
             "La Viga (dirección UMB), contaremos con los siguientes circuitos:\n"
             "UMB-Puente de Fierro (Servicio ordinario)\n"
             "La Raza- Nuevo Laredo (Servicio express y ordinario)")
    r = m.parse_post(texto, linea_feed=104)
    assert len(r) == 1
    assert r[0]["linea"] == "104"
    assert r[0]["estado"] == "Sin servicio"


def test_feed_l1_facebook_bloqueo_y_transito_lento():
    # "Alerta de Tránsito Mexibús L1 Page": formato telegráfico, nunca dice Mexibús/L1.
    r = m.parse_post("17:58 Bloqueo en Adolfo López", linea_feed=101)
    assert len(r) == 1 and r[0]["linea"] == "101"
    assert r[0]["estado"] == "Afectación en el servicio"

    r2 = m.parse_post("11:57 UNITEC Tránsito lento debido a percance entre particulares", linea_feed=101)
    assert r2[0]["estado"] == "Retraso en el servicio"


def test_feed_l1_facebook_liberado_es_restablecido():
    # "Se libera el paso de nuestras unidades" debe leerse como reanudación, no como afectación.
    r = m.parse_post("20:29 Se libera el paso de nuestras unidades", linea_feed=101)
    assert len(r) == 1
    assert r[0]["estado"] == "Servicio restablecido"
    # computar_estado() excluye las líneas cuya última noticia es "restablecido".
    posts = [{"message": "20:29 Se libera el paso de nuestras unidades",
              "created_time": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
              "linea_feed": 101}]
    assert m.computar_estado(posts) == []


def test_resolver_linea_feed_acepta_entero_legacy_y_etiqueta():
    # RSS_URL_LINEAS acepta el entero interno (compat) o la etiqueta real -- no hay que saber
    # la convención numérica (ver LineaId) para configurar un feed nuevo.
    assert m._resolver_linea_feed("104") == 104
    assert m._resolver_linea_feed("L4") == 104
    assert m._resolver_linea_feed("l4") == 104
    assert m._resolver_linea_feed("3A") == 113
    assert m._resolver_linea_feed("MXC L1") == 201
    assert m._resolver_linea_feed("") is None
    try:
        m._resolver_linea_feed("no-existe")
        assert False, "debió lanzar ValueError"
    except ValueError:
        pass


def test_lineaid_codigo_coincide_con_la_convencion_legacy():
    # LineaId.codigo debe reproducir exactamente la convención de CLAUDE.md (Mexibús
    # 10X ordinario/11X ramal/12X exprés, Mexicable 20X) para no romper el formato de cable.
    assert m.LineaId(m.Sistema.MEXIBUS, 4).codigo == 104
    assert m.LineaId(m.Sistema.MEXIBUS, 1, m.Variante.RAMAL).codigo == 111
    assert m.LineaId(m.Sistema.MEXIBUS, 2, m.Variante.EXPRES).codigo == 122
    assert m.LineaId(m.Sistema.MEXICABLE, 2).codigo == 202
    assert m.LineaId(m.Sistema.MEXIBUS, 4).etiqueta == "L4"
    assert m.LineaId(m.Sistema.MEXIBUS, 1, m.Variante.RAMAL).etiqueta == "L1A"
    assert m.LineaId(m.Sistema.MEXICABLE, 1).etiqueta == "MXC L1"


def test_linea_explicita_en_texto_gana_sobre_el_default_del_feed():
    # Si el propio texto YA menciona otra línea, esa manda (el default del feed es solo respaldo).
    r = m.parse_post("Afectación en Mexibús línea 2 por agua en la vía", linea_feed=104)
    assert [a["linea"] for a in r] == ["102"]
