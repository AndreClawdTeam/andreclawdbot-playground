#!/usr/bin/env python3
"""
transcribe.py — Transcrição de áudio gratuita via Whisper local.
Uso: python3 transcribe.py <arquivo_de_audio>
     python3 transcribe.py <arquivo_de_audio> --model small --lang pt
Suporta: ogg, mp3, wav, m4a, flac, webm, etc. (qualquer coisa que o ffmpeg aceitar)
"""
import sys
import argparse
import pathlib

def transcribe(audio_path: str, model_size: str = "small", language: str = None) -> str:
    from faster_whisper import WhisperModel

    path = pathlib.Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {audio_path}")

    # Carrega o modelo (baixa automaticamente na primeira vez, fica em cache)
    # compute_type="int8" é o mais leve para CPU
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    segments, info = model.transcribe(
        str(path),
        language=language,       # None = detecção automática
        beam_size=5,
        vad_filter=True,         # ignora silêncio
    )

    text = " ".join(seg.text.strip() for seg in segments).strip()
    return text, info.language, info.duration


def main():
    parser = argparse.ArgumentParser(description="Transcreve áudio com Whisper local")
    parser.add_argument("audio", help="Caminho para o arquivo de áudio")
    parser.add_argument("--model", default="small", choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="Tamanho do modelo (default: small)")
    parser.add_argument("--lang", default=None,
                        help="Idioma (ex: pt, en). Padrão: detecção automática")
    parser.add_argument("--json", action="store_true", help="Saída em JSON")
    args = parser.parse_args()

    try:
        text, detected_lang, duration = transcribe(args.audio, args.model, args.lang)
        if args.json:
            import json
            print(json.dumps({
                "text": text,
                "language": detected_lang,
                "duration_seconds": round(duration, 2),
                "model": args.model,
            }, ensure_ascii=False))
        else:
            print(text)
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
