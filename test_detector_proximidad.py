# ============================================================
# MÓDULO   : test_detector_proximidad.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Pruebas del detector de proximidad (ver detector_proximidad.py). Usa un archivo SQLite
# TEMPORAL propio (nunca el device_alertas.db real) y pasa el feed directo a evaluar_alertas()
# (parámetro `feed`, pensado exactamente para esto) para no depender de red ni de que
# /data/vehicles.json esté sirviendo algo en este entorno.
#
# Ejecutar:  python3 -m unittest test_detector_proximidad -v
# ============================================================
from __future__ import annotations

import math
import os
import tempfile
import unittest

import device_alertas
import detector_proximidad

# Punto fijo de referencia (CDMX, cerca de Indios Verdes) + un segundo punto a ~500 m hacia el
# norte, para construir vehículos a distancias conocidas sin tener que recalcular Haversine a
# mano en cada caso -- se generan con un desplazamiento en metros (ver _mover_norte).
LAT0, LON0 = 19.4900, -99.1200


_METROS_POR_GRADO_LAT = 6371000.0 * math.pi / 180.0   # mismo R que usa _haversine_m: exacto
                                                        # para un desplazamiento puro norte-sur
                                                        # (va sobre un meridiano = círculo máximo)


def _mover_norte(lat, metros):
    """Desplaza `metros` al norte desde `lat`, con la MISMA constante que usa internamente
    detector_proximidad._haversine_m (R=6371000) -- así la distancia Haversine que calcula el
    propio detector coincide con `metros` casi exactamente, sin arrastrar un error de
    redondeo de una constante "aproximada" distinta a la que usa el código bajo prueba."""
    return lat + metros / _METROS_POR_GRADO_LAT


class DetectorProximidadTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_original = device_alertas._DB_PATH
        device_alertas._DB_PATH = os.path.join(self._tmpdir.name, "test_device_alertas.db")

    def tearDown(self):
        device_alertas._DB_PATH = self._db_original
        self._tmpdir.cleanup()

    def _vehiculo(self, lat, lon, linea="1", ts=None, eco="1057"):
        return {
            "id": "crudo-" + eco, "label": eco, "line": linea,
            "lat": lat, "lon": lon, "timestamp": ts if ts is not None else _ahora(),
        }

    def _feed(self, *vehiculos):
        idx = {}
        for v in vehiculos:
            idx[v["label"]] = v
        return idx

    # ---------------------------------------------------------------- caso 1: entrada

    def test_caso1_entrada(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)

        # 700 m: fuera, sin evento
        v_lejos = self._vehiculo(_mover_norte(LAT0, 700), LON0)
        eventos = detector_proximidad.evaluar_alertas(self._feed(v_lejos))
        self.assertEqual(eventos, [])
        fila = device_alertas.todas_las_alertas_activas()[0]
        self.assertFalse(fila["dentro_del_radio"])

        # 450 m: entra, UN evento
        v_cerca = self._vehiculo(_mover_norte(LAT0, 450), LON0)
        eventos = detector_proximidad.evaluar_alertas(self._feed(v_cerca))
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["tipo"], "unidad_cerca")
        self.assertEqual(eventos[0]["device_id"], "devA")
        self.assertEqual(eventos[0]["economico"], "1057")
        self.assertEqual(eventos[0]["radio_m"], 500)
        self.assertLessEqual(eventos[0]["distancia_m"], 500)
        fila = device_alertas.todas_las_alertas_activas()[0]
        self.assertTrue(fila["dentro_del_radio"])

    # ---------------------------------------------------------------- caso 2: permanece dentro

    def test_caso2_permanece_dentro_sin_eventos_nuevos(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        detector_proximidad.evaluar_alertas(self._feed(self._vehiculo(_mover_norte(LAT0, 450), LON0)))

        for metros in (400, 300, 200):
            eventos = detector_proximidad.evaluar_alertas(
                self._feed(self._vehiculo(_mover_norte(LAT0, metros), LON0)))
            self.assertEqual(eventos, [], f"no debe haber evento a {metros} m (sigue dentro)")

    # ---------------------------------------------------------------- caso 3: salida (histéresis)

    def test_caso3_salida_requiere_radio_mas_margen(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        detector_proximidad.evaluar_alertas(self._feed(self._vehiculo(_mover_norte(LAT0, 450), LON0)))

        # 550 m: todavía dentro de la histéresis (radio+margen = 600) -> sigue "dentro"
        detector_proximidad.evaluar_alertas(self._feed(self._vehiculo(_mover_norte(LAT0, 550), LON0)))
        fila = device_alertas.todas_las_alertas_activas()[0]
        self.assertTrue(fila["dentro_del_radio"], "550 m no debe rearmar todavía (margen=100)")

        # claramente por encima de radio+margen (600): rearma. Se usa 615 m, no 600 m exactos --
        # una distancia Haversine real (senos/cosenos) nunca coincide EXACTO con el valor usado
        # para fabricar el punto de prueba, así que comparar justo en el límite es frágil por
        # redondeo de punto flotante; 15 m de margen lo deja inequívoco sin perder el sentido del
        # caso (sigue probando "por encima del límite", no un valor arbitrariamente lejano).
        detector_proximidad.evaluar_alertas(self._feed(self._vehiculo(_mover_norte(LAT0, 615), LON0)))
        fila = device_alertas.todas_las_alertas_activas()[0]
        self.assertFalse(fila["dentro_del_radio"], "615 m (por encima de radio+margen) debe rearmar")

    # ---------------------------------------------------------------- caso 4: nueva entrada

    def test_caso4_nueva_entrada_tras_rearme(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        detector_proximidad.evaluar_alertas(self._feed(self._vehiculo(_mover_norte(LAT0, 450), LON0)))
        detector_proximidad.evaluar_alertas(self._feed(self._vehiculo(_mover_norte(LAT0, 615), LON0)))  # rearma

        eventos = detector_proximidad.evaluar_alertas(
            self._feed(self._vehiculo(_mover_norte(LAT0, 480), LON0)))
        self.assertEqual(len(eventos), 1, "debe generar un nuevo evento tras el rearme")

    # ---------------------------------------------------------------- caso 5: unidad desaparece

    def test_caso5_unidad_desaparece_no_cambia_estado(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        detector_proximidad.evaluar_alertas(self._feed(self._vehiculo(_mover_norte(LAT0, 450), LON0)))
        fila_antes = device_alertas.todas_las_alertas_activas()[0]
        self.assertTrue(fila_antes["dentro_del_radio"])

        # feed sin esa unidad (p. ej. otra economico distinta, o vacío)
        eventos = detector_proximidad.evaluar_alertas(self._feed())
        self.assertEqual(eventos, [])
        fila_despues = device_alertas.todas_las_alertas_activas()[0]
        self.assertTrue(fila_despues["dentro_del_radio"], "no debe cambiar a false solo por desaparecer")

    # ---------------------------------------------------------------- caso 6: alerta desactivada

    def test_caso6_alerta_desactivada_no_se_procesa(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", False, 500)   # activa=False

        eventos = detector_proximidad.evaluar_alertas(
            self._feed(self._vehiculo(_mover_norte(LAT0, 10), LON0)))   # prácticamente encima
        self.assertEqual(eventos, [], "una alerta desactivada nunca debe generar evento")

    def test_caso6b_alerta_eliminada_deja_de_procesarse(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        device_alertas.eliminar_alerta("devA", "1057")

        eventos = detector_proximidad.evaluar_alertas(
            self._feed(self._vehiculo(_mover_norte(LAT0, 10), LON0)))
        self.assertEqual(eventos, [], "una alerta eliminada no debe seguir evaluándose")

    # ---------------------------------------------------------------- caso 7: dos unidades, mismo device

    def test_caso7_dos_unidades_independientes(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        device_alertas.actualizar_alerta("devA", "1065", True, 500)

        feed = self._feed(
            self._vehiculo(_mover_norte(LAT0, 300), LON0, eco="1057"),   # dentro
            self._vehiculo(_mover_norte(LAT0, 900), LON0, eco="1065"),   # fuera
        )
        eventos = detector_proximidad.evaluar_alertas(feed)
        self.assertEqual({e["economico"] for e in eventos}, {"1057"})

        estados = {f["economico"]: f["dentro_del_radio"] for f in device_alertas.todas_las_alertas_activas()}
        self.assertTrue(estados["1057"])
        self.assertFalse(estados["1065"])

    # ---------------------------------------------------------------- caso 8: dos dispositivos

    def test_caso8_dos_dispositivos_independientes(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_ubicacion("devB", _mover_norte(LAT0, 2000), LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        device_alertas.actualizar_alerta("devB", "1057", True, 500)

        # la unidad está cerca de devA, lejos de devB (están a 2 km uno del otro)
        feed = self._feed(self._vehiculo(_mover_norte(LAT0, 300), LON0, eco="1057"))
        eventos = detector_proximidad.evaluar_alertas(feed)
        self.assertEqual({e["device_id"] for e in eventos}, {"devA"})

    # ---------------------------------------------------------------- caso 9: radios diferentes

    def test_caso9_radios_diferentes_misma_distancia(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_ubicacion("devB", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 250)
        device_alertas.actualizar_alerta("devB", "1057", True, 1000)

        # 600 m: fuera del radio de A (250), dentro del radio de B (1000)
        feed = self._feed(self._vehiculo(_mover_norte(LAT0, 600), LON0))
        eventos = detector_proximidad.evaluar_alertas(feed)
        self.assertEqual({e["device_id"] for e in eventos}, {"devB"})

    # ---------------------------------------------------------------- caso 10: feed obsoleto

    def test_caso10_feed_obsoleto_no_genera_alerta(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)

        ts_viejo = _ahora() - (detector_proximidad.UMBRAL_OBSOLETO_S + 60)
        v = self._vehiculo(_mover_norte(LAT0, 10), LON0, ts=ts_viejo)   # encima, pero viejo
        eventos = detector_proximidad.evaluar_alertas(self._feed(v))
        self.assertEqual(eventos, [], "un timestamp obsoleto nunca debe disparar una alerta")
        fila = device_alertas.todas_las_alertas_activas()[0]
        self.assertFalse(fila["dentro_del_radio"])

    # ---------------------------------------------------------------- extra: sin ubicación todavía

    def test_sin_ubicacion_del_dispositivo_no_crashea(self):
        device_alertas.actualizar_alerta("devSinUbicacion", "1057", True, 500)
        eventos = detector_proximidad.evaluar_alertas(
            self._feed(self._vehiculo(LAT0, LON0)))
        self.assertEqual(eventos, [])

    # ---------------------------------------------------------------- extra: feed vacío/fallido

    def test_descarga_fallida_devuelve_lista_vacia_sin_romper(self):
        device_alertas.actualizar_ubicacion("devA", LAT0, LON0, _ahora())
        device_alertas.actualizar_alerta("devA", "1057", True, 500)
        # feed=None (el valor por defecto real) intenta la descarga HTTP de verdad -- se apunta
        # a un puerto que a propósito no tiene nada escuchando, para ejercitar de forma
        # determinista la ruta de "la descarga falla": no debe tronar, solo devolver [].
        url_original = detector_proximidad.VEHICLES_URL
        detector_proximidad.VEHICLES_URL = "http://127.0.0.1:1/data/vehicles.json"
        try:
            eventos = detector_proximidad.evaluar_alertas()
        finally:
            detector_proximidad.VEHICLES_URL = url_original
        self.assertEqual(eventos, [])

    # ---------------------------------------------------------------- fórmula de distancia

    def test_haversine_distancia_conocida(self):
        # ~500 m hacia el norte: Haversine debe acercarse mucho al desplazamiento usado para
        # fabricar el punto (tolerancia generosa, no se busca precisión al milímetro).
        d = detector_proximidad._haversine_m(LAT0, LON0, _mover_norte(LAT0, 500), LON0)
        self.assertAlmostEqual(d, 500, delta=5)


def _ahora():
    import time
    return int(time.time())


class RegresionPushMetrobusTest(unittest.TestCase):
    """No debe romperse nada de lo existente en push_metrobus.py por este cambio."""

    def test_import_no_rompe_nada(self):
        import importlib
        import push_metrobus
        importlib.reload(push_metrobus)
        # Las funciones/temas preexistentes siguen ahí, sin cambios.
        self.assertTrue(callable(push_metrobus.iniciar_monitor))
        self.assertTrue(callable(push_metrobus.enviar_actualizacion))
        self.assertTrue(callable(push_metrobus._ciclo))
        self.assertEqual(push_metrobus.TEMA_AFECTA, "afectaciones")
        self.assertEqual(push_metrobus.TEMA_ELEVA, "elevadores")
        self.assertEqual(push_metrobus.TEMA_ACTUALIZA, "actualizaciones")
        self.assertEqual(push_metrobus.INTERVALO_SEG, 60)


if __name__ == "__main__":
    unittest.main()
