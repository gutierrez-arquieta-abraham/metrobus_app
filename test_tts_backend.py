# ============================================================
# MÓDULO   : test_tts_backend.py
# PROYECTO : GeoMB — Backend (EC2)
# ============================================================
#
# DESCRIPCIÓN:
#
# Pruebas de /api/tts (ver tts_backend.py): que "voz=Ximena" (y solo esa, por ahora) vaya
# por Fish Audio y cualquier otro valor (incluido el default "Mia") siga yendo por Polly, que
# la caché en disco evite repetir la llamada externa, y que un error de cualquiera de los dos
# motores responda 502 en vez de tronar. Usa un directorio de caché TEMPORAL propio (nunca
# tts_cache/ real) y mockea boto3/requests -- no pega contra AWS ni Fish Audio de verdad.
#
# Ejecutar:  python3 -m unittest test_tts_backend -v
# ============================================================
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

import tts_backend


def _app_con_cliente(tmpdir):
    app = Flask(__name__)
    app.register_blueprint(tts_backend.tts_bp)
    tts_backend.CACHE_DIR = tmpdir
    tts_backend._polly = None   # fuerza reconstruir el cliente mockeado en cada prueba
    return app.test_client()


class TtsBackendTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._cache_original = tts_backend.CACHE_DIR
        self.client = _app_con_cliente(self._tmpdir.name)

    def tearDown(self):
        tts_backend.CACHE_DIR = self._cache_original
        self._tmpdir.cleanup()

    @patch("tts_backend._cliente_polly")
    def test_vozPorDefecto_mia_vaPorPolly(self, cliente_polly):
        polly = MagicMock()
        polly.synthesize_speech.return_value = {"AudioStream": MagicMock(read=lambda: b"AUDIO_POLLY")}
        cliente_polly.return_value = polly

        r = self.client.get("/api/tts?texto=Llegando+a+Indios+Verdes")

        self.assertEqual(200, r.status_code)
        self.assertEqual(b"AUDIO_POLLY", r.data)
        polly.synthesize_speech.assert_called_once_with(
            Text="Llegando a Indios Verdes", VoiceId="Mia", OutputFormat="mp3", LanguageCode="es-MX")

    @patch("tts_backend._cliente_polly")
    def test_vozXimena_vaPorFishAudio_noPorPolly(self, cliente_polly):
        with patch.dict(os.environ, {"FISH_AUDIO_API_KEY": "clave-de-prueba"}), \
             patch("tts_backend.requests.post") as post:
            post.return_value = MagicMock(status_code=200, content=b"AUDIO_FISH",
                                            raise_for_status=lambda: None)

            r = self.client.get("/api/tts?texto=Llegando+a+Indios+Verdes&voz=Ximena")

            self.assertEqual(200, r.status_code)
            self.assertEqual(b"AUDIO_FISH", r.data)
            cliente_polly.assert_not_called()
            post.assert_called_once()
            args, kwargs = post.call_args
            self.assertEqual("https://api.fish.audio/v1/tts", args[0])
            self.assertEqual("Bearer clave-de-prueba", kwargs["headers"]["Authorization"])
            self.assertEqual(tts_backend.FISH_AUDIO_VOICES["Ximena"], kwargs["json"]["reference_id"])
            self.assertEqual("Llegando a Indios Verdes", kwargs["json"]["text"])

    @patch("tts_backend._cliente_polly")
    def test_otraVozPolly_distintaDeMia_sigueYendoPorPolly(self, cliente_polly):
        # Cualquier VoiceId de Polly (no solo "Mia") debe seguir funcionando igual que antes --
        # el mapeo a Fish Audio es SOLO por los nombres que estén en FISH_AUDIO_VOICES.
        polly = MagicMock()
        polly.synthesize_speech.return_value = {"AudioStream": MagicMock(read=lambda: b"AUDIO_OTRA")}
        cliente_polly.return_value = polly

        r = self.client.get("/api/tts?texto=Hola&voz=Lupe")

        self.assertEqual(200, r.status_code)
        polly.synthesize_speech.assert_called_once_with(
            Text="Hola", VoiceId="Lupe", OutputFormat="mp3", LanguageCode="es-MX")

    @patch("tts_backend._cliente_polly")
    def test_cacheEnDisco_evitaSegundaLlamadaExterna(self, cliente_polly):
        with patch.dict(os.environ, {"FISH_AUDIO_API_KEY": "clave-de-prueba"}), \
             patch("tts_backend.requests.post") as post:
            post.return_value = MagicMock(status_code=200, content=b"AUDIO_FISH",
                                            raise_for_status=lambda: None)

            self.client.get("/api/tts?texto=Hola&voz=Ximena")
            self.client.get("/api/tts?texto=Hola&voz=Ximena")

            post.assert_called_once()   # la segunda vino de la caché, no de otra llamada real

    @patch("tts_backend._cliente_polly")
    def test_errorFishAudio_responde502_noTronar(self, cliente_polly):
        with patch.dict(os.environ, {"FISH_AUDIO_API_KEY": "clave-de-prueba"}), \
             patch("tts_backend.requests.post", side_effect=RuntimeError("502 upstream")):
            r = self.client.get("/api/tts?texto=Hola&voz=Ximena")
            self.assertEqual(502, r.status_code)

    def test_errorPolly_responde502_noTronar(self):
        with patch("tts_backend._cliente_polly", side_effect=RuntimeError("AWS caído")):
            r = self.client.get("/api/tts?texto=Hola")
            self.assertEqual(502, r.status_code)

    def test_sinTexto_responde400(self):
        r = self.client.get("/api/tts")
        self.assertEqual(400, r.status_code)


if __name__ == "__main__":
    unittest.main()
