# email-digest

Script de digest de e-mails: lê os e-mails não resumidos do SQLite, monta um sumário formatado e envia pelo Telegram em blocos, depois marca como resumidos.

---

## Objetivo

Enviar um resumo consolidado dos e-mails novos para o André no Telegram. Diferente do `monitor_email.py` (que notifica individualmente e em tempo real), o digest agrupa todos os e-mails ainda não resumidos numa única mensagem (ou série de mensagens, se necessário) e é chamado sob demanda — por exemplo, de hora em hora via cron, ou manualmente.

A ideia é ter dois níveis de notificação:
- **Monitor**: alerta imediato, um por e-mail novo
- **Digest**: sumário agrupado, chamado periodicamente, para revisão rápida

---

## Stack

- **Python 3** — linguagem principal
- **sqlite3** — biblioteca padrão para leitura do banco gerado pelo `monitor_email.py`
- **html** — biblioteca padrão para `html.unescape()`
- **subprocess** — para enviar mensagens via CLI do OpenClaw

---

## Como foi feito

### Idempotência
O script usa uma coluna `is_summarized` no banco SQLite. Ao rodar, ele só busca emails onde `is_summarized = 0`. Após envio com sucesso, marca todos como `is_summarized = 1`. Isso garante que nunca re-envia o mesmo e-mail, mesmo que o script seja chamado múltiplas vezes.

### Divisão em chunks
O Telegram tem limite de 4096 caracteres por mensagem. O script monta blocos por e-mail e vai agrupando numa fila. Quando o tamanho acumulado ultrapassa 3800 caracteres (headroom de segurança), fecha o chunk atual e começa um novo. Cada chunk é enviado como mensagem separada.

### Atomicidade do envio
Só marca os e-mails como `is_summarized = 1` se **todos** os chunks foram enviados com sucesso. Se qualquer envio falhar, o banco permanece intacto e o script pode ser rodado novamente.

### Silêncio se não houver nada
Se não houver e-mails novos (`WHERE is_summarized = 0` retorna vazio), o script simplesmente termina sem enviar nada. Não envia mensagem de "nenhum e-mail novo".

---

## Passo a passo do código

### 1. `DB_PATH` — Caminho do banco
```python
DB_PATH = pathlib.Path(__file__).with_name("emails.db")
```
Resolve o caminho do `emails.db` relativo ao próprio script. Isso significa que `digest.py` e `emails.db` precisam estar no mesmo diretório — o mesmo diretório onde o `monitor_email.py` salva.

### 2. Query dos e-mails não resumidos
```python
rows = con.execute(
    "SELECT ... FROM emails WHERE is_summarized = 0 ORDER BY uid ASC"
).fetchall()
```
Busca todos os e-mails pendentes em ordem crescente de UID (cronológica).

### 3. `send_message(text)` — Envio via OpenClaw CLI
Função auxiliar que chama `openclaw message send` via subprocess. Retorna `True` se enviou com sucesso, `False` se falhou. Não lança exceção — erros são tratados pelo chamador.

### 4. Construção dos blocos por e-mail
```python
block_lines = [
    "━━━━━━━━━━",
    f"📧 UID {uid} · {date}",
    f"De: {sender}",
    f"Assunto: {subject}",
]
if preview:
    block_lines.append(f"Preview: {preview}")
```
Para cada e-mail, monta um bloco de texto com separador visual, data, remetente, assunto e preview de até 200 caracteres do corpo.

### 5. Divisão em chunks (controle de tamanho)
```python
if current_ids and current_len + block_len > MAX_MSG_LEN:
    chunks.append((current_ids[:], "\n".join(current_lines)))
    current_ids = []
    current_lines = []
    current_len = 0
```
Verifica antes de adicionar cada bloco se ele estouraria o limite. Se sim, fecha o chunk atual e começa um novo. O `current_ids[:]` é uma cópia da lista para não ter referência compartilhada.

### 6. Envio e marcação
```python
all_ok = True
for chunk_ids, chunk_text in chunks:
    if not send_message(chunk_text):
        all_ok = False
        break

if all_ok:
    con.execute(
        f"UPDATE emails SET is_summarized=1 WHERE id IN (...)",
        ids,
    )
    con.commit()
```
Envia todos os chunks em sequência. Se qualquer um falhar, `all_ok` vai para `False` e o loop quebra. Só faz o UPDATE se tudo deu certo.

---

## Como usar

### Pré-requisito
- O `monitor_email.py` precisa ter rodado e criado o `emails.db` no mesmo diretório.
- A coluna `is_summarized` precisa existir na tabela. Se o banco foi criado por uma versão antiga do monitor, adicione:
  ```sql
  ALTER TABLE emails ADD COLUMN is_summarized INTEGER DEFAULT 0;
  ```

### Executar manualmente
```bash
cd email-monitor   # mesmo diretório onde está o emails.db
python3 ../email-digest/digest.py
```

Ou, se colocados no mesmo diretório:
```bash
python3 digest.py
```

### Executar via cron (a cada hora)
```cron
0 * * * * /usr/bin/python3 /path/to/email-monitor/digest.py >> /path/to/email-monitor/digest.log 2>&1
```

### Verificar status no banco
```bash
sqlite3 emails.db "SELECT uid, subject, is_summarized FROM emails ORDER BY id DESC LIMIT 20;"
```

---

## Diferença entre monitor e digest

| Característica     | `monitor_email.py`         | `digest.py`                    |
|--------------------|----------------------------|--------------------------------|
| Execução           | Loop contínuo (daemon)     | One-shot (chamado sob demanda) |
| Notificação        | Um e-mail por vez          | Todos pendentes agrupados      |
| Timing             | Ao receber o e-mail        | Quando chamado                 |
| Campo de controle  | `is_notified`              | `is_summarized`                |
| Caso de uso        | Alerta imediato            | Revisão periódica              |
