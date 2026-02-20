# whisper-server

Servidor HTTP local que imita a API do Deepgram, usando Whisper local (faster-whisper). Permite que o OpenClaw transcreva áudios de voz gratuitamente, sem enviar nada para a nuvem.

---

## Objetivo

O OpenClaw suporta transcrição de áudio de mensagens de voz via Deepgram. O problema: Deepgram é pago e envia o áudio para servidores externos.

A solução: este servidor implementa exatamente o mesmo endpoint que o Deepgram (`POST /v1/listen`) e retorna o mesmo formato JSON que o OpenClaw espera — mas toda a transcrição acontece localmente com o Whisper.

Do ponto de vista do OpenClaw, parece que está falando com o Deepgram. Na prática, está falando com esse servidor local que usa faster-whisper.

---

## Stack

- **Python 3** — linguagem principal
- **Flask** — framework web leve para o servidor HTTP
- **faster-whisper** — transcrição local com CTranslate2
- **tempfile** — biblioteca padrão para criar arquivos temporários
- **ffmpeg** — usado internamente pelo faster-whisper

---

## Como foi feito

### O "fake Deepgram"
O OpenClaw manda um `POST` com o áudio bruto no body para `https://api.deepgram.com/v1/listen`. Ao configurar o `baseUrl` para `http://127.0.0.1:8765`, o OpenClaw passa a mandar para esse servidor local em vez da API real.

O servidor recebe os bytes do áudio, salva num arquivo temporário, transcreve com o faster-whisper e retorna um JSON no exato formato que o Deepgram retornaria.

### Carregamento do modelo na inicialização
O modelo Whisper é carregado **uma vez** quando o servidor sobe, não a cada requisição. Isso é crucial para performance: carregar o modelo leva alguns segundos, mas a transcrição em si é rápida. Se fosse recarregado a cada requisição, cada transcrição demoraria muito mais.

### Arquivo temporário
O Flask recebe o áudio como bytes crus (`request.get_data()`). O faster-whisper precisa de um arquivo em disco. O fluxo é:
1. Salva os bytes num arquivo temporário (`.ogg`)
2. Transcreve o arquivo
3. Deleta o arquivo temporário (no bloco `finally`)

### Formato de resposta (Deepgram)
O Deepgram retorna um JSON com estrutura específica que o OpenClaw parseia. O servidor retorna a mesma estrutura, populada com os dados do Whisper:
```json
{
  "results": {
    "channels": [{
      "alternatives": [{
        "transcript": "texto transcrito aqui",
        "confidence": 0.95,
        "words": []
      }]
    }]
  }
}
```

---

## Passo a passo do código

### 1. Inicialização do modelo (nível de módulo)
```python
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
_model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
```
O modelo é carregado quando o módulo é importado (i.e., quando o servidor sobe). Fica em memória para toda a vida do processo. `WHISPER_MODEL` pode ser configurado via variável de ambiente para mudar o modelo sem alterar o código.

### 2. `transcribe_bytes(data, language)` — Transcrição
```python
with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
    tmp.write(data)
    tmp_path = tmp.name
try:
    segments, info = _model.transcribe(tmp_path, ...)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    confidence = 0.95
    return text, confidence, info.language
finally:
    os.unlink(tmp_path)
```
Salva os bytes em disco, transcreve, junta os segmentos e deleta o arquivo temporário. O `finally` garante que o arquivo é deletado mesmo se a transcrição falhar.

> **Nota:** `confidence = 0.95` é um valor fixo. O faster-whisper não expõe uma confidence média por transcrição (só por palavra, e de forma indireta). O valor 0.95 é suficiente para satisfazer o parser do OpenClaw.

### 3. `@app.route("/v1/listen", methods=["POST"])` — Endpoint principal
```python
language = request.args.get("language", None)
audio_data = request.get_data()
```
Lê o parâmetro `language` da query string (ex: `?language=pt`) e os bytes do áudio do corpo da requisição.

```python
if not audio_data:
    return jsonify({"error": "No audio data received"}), 400
```
Validação básica: rejeita requisições sem áudio com 400 Bad Request.

Depois chama `transcribe_bytes` e monta a resposta no formato Deepgram.

### 4. `@app.route("/health", methods=["GET"])` — Health check
```python
return jsonify({"status": "ok", "model": MODEL_SIZE})
```
Endpoint simples para verificar se o servidor está rodando e qual modelo está carregado. Útil para debugging e monitoramento.

### 5. `if __name__ == "__main__"` — Inicialização
```python
port = int(os.environ.get("WHISPER_PORT", 8765))
app.run(host="127.0.0.1", port=port, debug=False)
```
Só aceita conexões locais (`127.0.0.1`), nunca expõe para a internet. A porta pode ser configurada via `WHISPER_PORT`.

---

## Como usar

### Instalação das dependências
```bash
pip install flask faster-whisper
sudo apt install ffmpeg
```

### Iniciar o servidor
```bash
python3 whisper_server.py
```
Saída esperada:
```
2026-02-20 10:00:00 INFO Carregando modelo Whisper 'small'...
2026-02-20 10:00:05 INFO Modelo carregado!
2026-02-20 10:00:05 INFO Servidor iniciando em http://127.0.0.1:8765
```

### Usar modelo diferente
```bash
WHISPER_MODEL=medium python3 whisper_server.py
```

### Testar manualmente
```bash
curl -X POST http://127.0.0.1:8765/v1/listen \
  --data-binary @audio.ogg \
  -H "Content-Type: audio/ogg"
```

### Health check
```bash
curl http://127.0.0.1:8765/health
# {"model":"small","status":"ok"}
```

### Configurar no OpenClaw

No arquivo `openclaw.json`:
```json
{
  "tools": {
    "media": {
      "audio": {
        "enabled": true,
        "models": [{ "provider": "deepgram", "model": "nova-3" }],
        "baseUrl": "http://127.0.0.1:8765"
      }
    }
  }
}
```

### Rodar como serviço (systemd)
```ini
[Unit]
Description=Whisper Local Server (Fake Deepgram API)
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/whisper-server/whisper_server.py
Restart=always
RestartSec=10
Environment=WHISPER_MODEL=small

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable whisper-server
sudo systemctl start whisper-server
sudo systemctl status whisper-server
```

---

## Comparação: whisper-cli vs whisper-server

| Característica   | `whisper-cli`             | `whisper-server`                  |
|------------------|---------------------------|-----------------------------------|
| Interface        | CLI (linha de comando)    | HTTP (servidor web)               |
| Carregamento     | A cada execução           | Uma vez na inicialização          |
| Uso principal    | Transcrição ad-hoc manual | Integração com OpenClaw           |
| Input            | Arquivo em disco          | Bytes via HTTP POST               |
| Output           | Texto no stdout (ou JSON) | JSON no formato Deepgram          |
| Dependências     | faster-whisper            | faster-whisper + Flask            |
