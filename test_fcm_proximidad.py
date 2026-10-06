# ============================================================
# MÓDULO   : test_fcm_proximidad.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Pruebas de enviar_alerta_unidad_cerca()/_procesar_eventos_proximidad() (ver push_metrobus.py):
# el transporte evento -> FCM. Solo se parchea messaging.send (NUNCA manda un mensaje real) --
# messaging.Message/messaging.UnregisteredError se usan REALES, para que el payload que se
# inspecciona en las pruebas sea el que de verdad construiría el código, y para que
# "except messaging.UnregisteredError" en el código bajo prueba siga funcionando igual que en
# producción. Un SQLite temporal propio (nunca device_alertas.db real).
#
# Ejecutar:  python3 -m unittest test_fcm_proximidad -v
# ============================================================
from __future__ import annotations

import math
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from firebase_admin.messaging import UnregisteredError

import device_alertas
import detector_proximidad
import push_metrobus


def _ahora():
    return int(time.time())


class EnviarAlertaUnidadCercaTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_original = device_alertas._DB_PATH
        device_alertas._DB_PATH = os.path.join(self._tmpdir.name, "test_device_alertas.db")
        # _init() real intentaría credenciales reales (ApplicationDefault) -- no hay nada de eso
        # en este entorno de pruebas, así que se vuelve no-op; lo único que de verdad se prueba
        # aquí es QUÉ se manda y a quién, nunca la inicialización real de Firebase Admin.
        self._init_patch = patch.object(push_metrobus, "_init", lambda: None)
        self._init_patch.start()

    def tearDown(self):
        self._init_patch.stop()
        device_alertas._DB_PATH = self._db_original
        self._tmpdir.cleanup()

    def _evento(self, device_id="devA", economico="1057", linea="1", distancia_m=327, radio_m=500):
        return {
            "tipo": "unidad_cerca", "device_id": device_id, "economico": economico,
            "linea": linea, "distancia_m": distancia_m, "radio_m": radio_m,
            "timestamp": 1700000000,
        }

    # ---------------------------------------------------------------- caso 1

    def test_caso1_token_valido_envia_fcm(self):
        device_alertas.registrar_token("devA", "tokenA")
        with patch.object(push_metrobus.messaging, "send") as mock_send:
            ok = push_metrobus.enviar_alerta_unidad_cerca(self._evento())
        self.assertTrue(ok)
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        self.assertEqual(msg.token, "tokenA")
        self.assertIsNone(msg.topic, "debe mandarse por token, NUNCA por topic")
        self.assertEqual(msg.data["tipo"], "unidad_cerca")
        self.assertEqual(msg.data["economico"], "1057")
        self.assertEqual(msg.data["linea"], "1")
        self.assertEqual(msg.data["distancia_m"], "327")
        self.assertEqual(msg.data["radio_m"], "500")
        # nunca ubicacion del usuario en el payload
        for clave in msg.data:
            self.assertNotIn("lat", clave.lower())
            self.assertNotIn("lon", clave.lower())

    def test_payload_son_strings(self):
        device_alertas.registrar_token("devA", "tokenA")
        with patch.object(push_metrobus.messaging, "send") as mock_send:
            push_metrobus.enviar_alerta_unidad_cerca(self._evento())
            msg = mock_send.call_args[0][0]
            for v in msg.data.values():
                self.assertIsInstance(v, str)

    # ---------------------------------------------------------------- caso 2

    def test_caso2_sin_token_no_rompe(self):
        # "devSinToken" nunca registro ningun token.
        with patch.object(push_metrobus.messaging, "send") as mock_send:
            ok = push_metrobus.enviar_alerta_unidad_cerca(self._evento(device_id="devSinToken"))
        self.assertFalse(ok)
        mock_send.assert_not_called()

    # ---------------------------------------------------------------- caso 3

    def test_caso3_token_invalido_se_elimina(self):
        device_alertas.registrar_token("devA", "tokenViejo")
        with patch.object(push_metrobus.messaging, "send", side_effect=UnregisteredError("ya no sirve")):
            ok = push_metrobus.enviar_alerta_unidad_cerca(self._evento())
        self.assertFalse(ok)
        self.assertIsNone(device_alertas.token_de("devA"), "el token invalido debe eliminarse")

    def test_caso3b_token_invalido_no_borra_device_alertas(self):
        device_alertas.registrar_token("devA", "tokenViejo")
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        with patch.object(push_metrobus.messaging, "send", side_effect=UnregisteredError("ya no sirve")):
            push_metrobus.enviar_alerta_unidad_cerca(self._evento())
        alertas = device_alertas.todas_las_alertas_activas()
        self.assertEqual(len(alertas), 1, "la preferencia de alerta debe conservarse")
        self.assertEqual(alertas[0]["economico"], "1057")

    # ---------------------------------------------------------------- caso 4

    def test_caso4_error_transitorio_no_altera_alerta(self):
        device_alertas.registrar_token("devA", "tokenA")
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        device_alertas.actualizar_estado_radio("devA", "1057", True)   # como si el detector ya la marco "dentro"
        with patch.object(push_metrobus.messaging, "send", side_effect=Exception("timeout de red")):
            ok = push_metrobus.enviar_alerta_unidad_cerca(self._evento())
        self.assertFalse(ok)
        # el token NO se elimina (no es UnregisteredError) y dentro_del_radio no se toca
        self.assertEqual(device_alertas.token_de("devA"), "tokenA")
        fila = device_alertas.todas_las_alertas_activas()[0]
        self.assertTrue(fila["dentro_del_radio"], "un fallo transitorio de FCM nunca debe rearmar la alerta")

    # ---------------------------------------------------------------- caso 5

    def test_caso5_dos_dispositivos_mismo_economico_mensajes_propios(self):
        device_alertas.registrar_token("devA", "tokenA")
        device_alertas.registrar_token("devB", "tokenB")
        with patch.object(push_metrobus.messaging, "send") as mock_send:
            push_metrobus.enviar_alerta_unidad_cerca(self._evento(device_id="devA", economico="1057"))
            push_metrobus.enviar_alerta_unidad_cerca(self._evento(device_id="devB", economico="1057"))
        self.assertEqual(mock_send.call_count, 2)
        tokens_usados = {c.args[0].token for c in mock_send.call_args_list}
        self.assertEqual(tokens_usados, {"tokenA", "tokenB"})

    # ---------------------------------------------------------------- caso 6

    def test_caso6_dos_unidades_mismo_dispositivo_mensajes_independientes(self):
        device_alertas.registrar_token("devA", "tokenA")
        with patch.object(push_metrobus.messaging, "send") as mock_send:
            push_metrobus.enviar_alerta_unidad_cerca(self._evento(device_id="devA", economico="1057"))
            push_metrobus.enviar_alerta_unidad_cerca(self._evento(device_id="devA", economico="1065"))
        self.assertEqual(mock_send.call_count, 2)
        economicos = {c.args[0].data["economico"] for c in mock_send.call_args_list}
        self.assertEqual(economicos, {"1057", "1065"})

    # ---------------------------------------------------------------- caso 7

    def test_caso7_evento_de_device_no_registrado_no_envia(self):
        device_alertas.registrar_token("devA", "tokenA")
        # el evento es de un device_id que NUNCA registro token (p.ej. desinstalo la app)
        with patch.object(push_metrobus.messaging, "send") as mock_send:
            ok = push_metrobus.enviar_alerta_unidad_cerca(self._evento(device_id="devZ"))
        self.assertFalse(ok)
        mock_send.assert_not_called()

    # ---------------------------------------------------------------- caso 8

    def test_caso8_fallo_en_uno_no_detiene_los_demas(self):
        device_alertas.registrar_token("devA", "tokenA")
        device_alertas.registrar_token("devC", "tokenC")
        eventos = [
            self._evento(device_id="devA", economico="1057"),
            self._evento(device_id="devB", economico="1065"),   # sin token -> "falla" (no revienta)
            self._evento(device_id="devC", economico="1146"),
        ]
        with patch.object(push_metrobus.messaging, "send") as mock_send:
            push_metrobus._procesar_eventos_proximidad(eventos)
        # 1057 y 1146 SÍ se intentan mandar aunque 1065 no tenga token.
        self.assertEqual(mock_send.call_count, 2)
        economicos = {c.args[0].data["economico"] for c in mock_send.call_args_list}
        self.assertEqual(economicos, {"1057", "1146"})

    def test_caso8b_excepcion_inesperada_no_detiene_los_demas(self):
        eventos = [
            self._evento(device_id="devA", economico="1057"),
            self._evento(device_id="devC", economico="1146"),
        ]
        llamadas = []

        def falla_la_primera(evento):
            llamadas.append(evento["economico"])
            if evento["economico"] == "1057":
                raise RuntimeError("algo totalmente inesperado")
            return True

        with patch.object(push_metrobus, "enviar_alerta_unidad_cerca", side_effect=falla_la_primera):
            push_metrobus._procesar_eventos_proximidad(eventos)
        self.assertEqual(llamadas, ["1057", "1146"], "debe intentar el segundo aunque el primero reviente")

    # ---------------------------------------------------------------- caso 9 (integracion con el detector)

    def test_caso9_sin_envios_adicionales_mientras_dentro_del_radio(self):
        lat0, lon0 = 19.4900, -99.1200
        device_alertas.registrar_token("devA", "tokenA")
        device_alertas.actualizar_ubicacion("devA", lat0, lon0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)

        metros_por_grado = 6371000.0 * math.pi / 180.0

        def vehiculo(metros):
            return {"id": "x", "label": "1057", "line": "1",
                    "lat": lat0 + metros / metros_por_grado, "lon": lon0, "timestamp": _ahora()}

        with patch.object(push_metrobus.messaging, "send") as mock_send:
            eventos = detector_proximidad.evaluar_alertas({"1057": vehiculo(450)})   # entra
            push_metrobus._procesar_eventos_proximidad(eventos)
            for m in (400, 300, 200, 450):   # permanece dentro, varias lecturas
                eventos = detector_proximidad.evaluar_alertas({"1057": vehiculo(m)})
                push_metrobus._procesar_eventos_proximidad(eventos)

        self.assertEqual(mock_send.call_count, 1,
                          "solo UN envio mientras la unidad sigue dentro del radio")


if __name__ == "__main__":
    unittest.main()
