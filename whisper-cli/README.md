# whisper-cli

CLI de transcrição de áudio usando Whisper local — 100% gratuito, sem API, sem enviar áudio para a nuvem.

---

## Objetivo

Transcrever arquivos de áudio (ogg, mp3, wav, m4a, flac, webm e qualquer formato que o ffmpeg aceite) diretamente no VPS, usando o modelo Whisper rodando localmente via `faster-whisper`.

O uso principal é fornecer uma ferramenta de linha de comando rápida para transcrição ad-hoc. A versão "servidor" desse mesmo conceito está em `whisper-server/`.

---

## Stack

- **Python 3** — linguagem principal
- **faster-whisper** — implementação otimizada do Whisper usando CTranslate2 (muito mais rápida e leve que a implementação original da OpenAI em CPU)
- **ffmpeg** — usado internamente pelo faster-whisper para decodificar o áudio
- **argparse** — biblioteca padrão para parsing de argumentos CLI

---

## Como foi feito

### Por que faster-whisper e não o Whisper original?
O Whisper original da OpenAI usa PyTorch, que é pesado (~1GB+ de dependências). O `faster-whisper` usa CTranslate2, que é muito mais eficiente em CPU, consome menos memória RAM e é significativamente mais rápido para inferência. Para um VPS sem GPU, essa diferença é enorme.

### Modelos disponíveis
O Whisper tem vários tamanhos de modelo com tradeoff qualidade × velocidade:

| Modelo    | Tamanho em disco | Velocidade (CPU) | Qualidade |
|-----------|-----------------|------------------|-----------|
| tiny      | ~39MB           | Muito rápido     | Básica    |
| base      | ~74MB           | Rápido           | Boa       |
| small     | ~244MB          | Moderado         | Muito boa |
| medium    | ~769MB          | Lento            | Excelente |
| large-v3  | ~1.5GB          | Muito lento      | Máxima    |

O padrão escolhido foi `small`: ótimo equilíbrio para uso em português no VPS.

### VAD Filter
O `vad_filter=True` ativa o filtro de detecção de atividade de voz (VAD). Ele ignora automaticamente trechos de silêncio, o que acelera a transcrição e reduz alucinações (o Whisper às vezes "inventa" texto em silêncios).

### Cache automático
Na primeira execução, o modelo é baixado automaticamente do HuggingFace Hub e fica em cache em `~/.cache/huggingface/`. Execuções subsequentes carregam do cache.

---

## Passo a passo do código

### 1. `transcribe(audio_path, model_size, language)` — Função principal
```python
model = WhisperModel(model_size, device="cpu", compute_type="int8")
```
Carrega o modelo. `compute_type="int8"` usa quantização de 8 bits — a mais leve para CPU, com qualidade praticamente idêntica ao float32.

```python
segments, info = model.transcribe(
    str(path),
    language=language,   # None = detecção automática
    beam_size=5,
    vad_filter=True,
)
```
Transcreve o áudio. Retorna um gerador de `segments` (cada segmento tem `.text`, `.start`, `.end`) e um objeto `info` com metadados (idioma detectado, duração).

```python
text = " ".join(seg.text.strip() for seg in segments).strip()
return text, info.language, info.duration
```
Junta todos os segmentos em uma string única e retorna junto com o idioma detectado e a duração.

### 2. `main()` — Parsing de argumentos e saída
```python
parser.add_argument("audio", help="Caminho para o arquivo de áudio")
parser.add_argument("--model", default="small", choices=[...])
parser.add_argument("--lang", default=None, ...)
parser.add_argument("--json", action="store_true", ...)
```
Define os argumentos:
- `audio` (posicional, obrigatório): caminho do arquivo
- `--model`: tamanho do modelo (padrão: `small`)
- `--lang`: idioma (padrão: detecção automática)
- `--json`: saída estruturada em JSON ao invés de texto puro

```python
if args.json:
    print(json.dumps({
        "text": text,
        "language": detected_lang,
        "duration_seconds": round(duration, 2),
        "model": args.model,
    }, ensure_ascii=False))
else:
    print(text)
```
Se `--json`, imprime um objeto JSON completo. Caso contrário, só o texto transcrito. O `ensure_ascii=False` preserva caracteres especiais (acentos, etc.).

---

## Como usar

### Instalação das dependências
```bash
pip install faster-whisper
# ffmpeg também precisa estar instalado no sistema:
sudo apt install ffmpeg
```

### Uso básico
```bash
python3 transcribe.py audio.ogg
```

### Com modelo específico
```bash
python3 transcribe.py audio.mp3 --model medium
```

### Forçar idioma (evita detecção automática)
```bash
python3 transcribe.py audio.wav --lang pt
```

### Saída em JSON (útil para scripts)
```bash
python3 transcribe.py audio.m4a --json
```
Saída exemplo:
```json
{
  "text": "Olá, isso é um teste de transcrição.",
  "language": "pt",
  "duration_seconds": 3.45,
  "model": "small"
}
```

### Processar em lote com bash
```bash
for f in *.ogg; do
  echo "=== $f ==="
  python3 transcribe.py "$f"
done
```

---

## Performance esperada (VPS com 2 vCPUs)

| Modelo | Áudio 30s | Áudio 5min |
|--------|-----------|------------|
| small  | ~15s      | ~2.5min    |
| medium | ~40s      | ~7min      |

Na primeira execução, adicione o tempo de download do modelo.
