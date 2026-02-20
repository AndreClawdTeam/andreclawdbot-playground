#!/usr/bin/env python3
"""
whisper_server.py — Fake Deepgram API usando Whisper local (faster-whisper).
Roda em http://127.0.0.1:8765
OpenClaw envia áudio → aqui → transcreve grátis → retorna JSON no formato Deepgram.

Config no openclaw.json:
  tools.media.audio.enabled = true
  tools.media.audio.models  = [{ provider: "deepgram", model: "nova-3" }]
  tools.media.audio.baseUrl = "http://127.0.0.1:8765"
"""
import os
import json
import tempfile
import logging
from flask import Flask, request, jsonify
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("whisper-server")

app = Flask(__name__)

# Modelo carregado uma vez (baixa ~244MB na primeira execução, fica em cache)
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
log.info(f"Carregando modelo Whisper '{MODEL_SIZE}'...")
_model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
log.info("Modelo carregado!")


def transcribe_bytes(data: bytes, language: str = None) -> tuple[str, float]:
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        segments, info = _model.transcribe(
            tmp_path,
            language=language or None,
            beam_size=5,
            vad_filter=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        confidence = 0.95  # faster-whisper não expõe confidence média simples
        return text, confidence, info.language
    finally:
        os.unlink(tmp_path)


@app.route("/v1/listen", methods=["POST"])
def listen():
    """Endpoint principal — imita POST https://api.deepgram.com/v1/listen"""
    language = request.args.get("language", None)

    audio_data = request.get_data()
    if not audio_data:
        return jsonify({"error": "No audio data received"}), 400

    log.info(f"Recebido áudio: {len(audio_data)} bytes | lang={language or 'auto'}")

    try:
        text, confidence, detected_lang = transcribe_bytes(audio_data, language)
        log.info(f"Transcrição: [{detected_lang}] {text[:80]}...")
    except Exception as e:
        log.error(f"Erro na transcrição: {e}")
        return jsonify({"error": str(e)}), 500

    # Resposta no formato Deepgram (o que OpenClaw espera)
    response = {
        "metadata": {
            "transaction_key": "local-whisper",
            "request_id": "local",
            "sha256": "",
            "created": "",
            "duration": 0,
            "channels": 1,
            "models": [MODEL_SIZE],
        },
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": text,
                            "confidence": confidence,
                            "words": [],
                        }
                    ]
                }
            ]
        },
    }
    return jsonify(response)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": MODEL_SIZE})


if __name__ == "__main__":
    port = int(os.environ.get("WHISPER_PORT", 8765))
    log.info(f"Servidor iniciando em http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
