# email-monitor

Monitor de caixa de entrada via IMAP que roda continuamente no VPS, armazena e-mails em SQLite e envia notificações pelo Telegram.

---

## Objetivo

Monitorar a caixa de entrada do ProtonMail em tempo real (a cada 30 segundos), salvar cada e-mail novo numa base de dados SQLite local e notificar o André via Telegram com um preview do remetente e assunto.

O ProtonMail não expõe IMAP diretamente — ele usa criptografia end-to-end. Para contornar isso, é usada a bridge **Hydroxide**: um servidor local que faz proxy IMAP e traduz o ProtonMail para um IMAP padrão.

---

## Stack

- **Python 3** — linguagem principal
- **imaplib** — biblioteca padrão do Python para comunicação IMAP
- **sqlite3** — biblioteca padrão para persistência local
- **email / email.policy** — parse de mensagens RFC 822
- **subprocess** — para disparar notificações via CLI do OpenClaw
- **Hydroxide** — bridge ProtonMail → IMAP (processo externo, roda separado)

---

## Como foi feito

### Problema
O ProtonMail cifra os e-mails no servidor. Não dá para usar IMAP padrão diretamente — as credenciais do ProtonMail não funcionam via `imaplib`.

### Solução: Hydroxide
O [Hydroxide](https://github.com/emersion/hydroxide) é um daemon Go que:
1. Autentica com a API do ProtonMail usando as credenciais reais
2. Expõe um servidor IMAP local em `127.0.0.1:1143`
3. Descriptografa os e-mails transparentemente na hora do fetch

O script conecta nesse servidor local como se fosse um IMAP qualquer.

### Persistência: SQLite
Ao invés de processar em memória e arriscar perder e-mails em caso de restart, todo e-mail novo é salvo numa tabela SQLite (`emails.db`). A coluna `uid` tem constraint `UNIQUE`, então rodar o script várias vezes não duplica nada.

### Notificação: OpenClaw CLI
O OpenClaw tem uma CLI (`openclaw message send`) que permite enviar mensagens pelo Telegram. O script usa `subprocess.run` para chamar essa CLI diretamente, sem precisar de credenciais de bot ou token no código.

### Loop de polling
O script roda um `while True` com `time.sleep(30)`. Simples, leve, sem dependências externas de agendamento.

---

## Passo a passo do código

### 1. Configuração (`HOST`, `PORT`, `USERNAME`, etc.)
```python
HOST = "127.0.0.1"
PORT = 1143
USERNAME = "andreclawdbot"
PASSWORD = "..."
POLL_SECONDS = 30
```
Aponta para o servidor IMAP local do Hydroxide. A senha é gerada pelo Hydroxide (não é a senha real do ProtonMail).

### 2. `init_db(con)` — Criação da tabela
```python
CREATE TABLE IF NOT EXISTS emails (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         INTEGER UNIQUE,
    ...
)
```
Cria a tabela `emails` se não existir. O `uid` é o UID IMAP — identificador único do e-mail no servidor. `UNIQUE` garante idempotência.

### 3. `strip_html(html)` — Limpeza de HTML
Remove tags `<style>`, `<script>`, todas as outras tags HTML e entidades como `&nbsp;`. Resulta em texto limpo para salvar no banco.

### 4. `extract_body(msg)` — Extração do corpo
Percorre as partes MIME do e-mail. Prefere `text/plain`; se não houver, usa `text/html` e passa pelo `strip_html`. Trunca em 4000 caracteres para não explodir o banco.

### 5. `parse_address(raw)` — Parse do remetente
Faz regex para extrair nome e e-mail de strings no formato `"Nome" <email@exemplo.com>` ou simplesmente `email@exemplo.com`.

### 6. `notify(...)` — Envio de notificação
Chama `openclaw message send` via subprocess com o canal e target configurados. Loga sucesso ou falha.

### 7. `fetch_new(con)` — Núcleo do polling
```python
known_uids = {row[0] for row in con.execute("SELECT uid FROM emails")}
```
1. Carrega todos os UIDs já salvos no banco
2. Conecta no IMAP local e busca todos os UIDs do servidor
3. Calcula a diferença: `new_uids = [u for u in all_uids if u not in known_uids]`
4. Para cada UID novo: faz `FETCH RFC822` (mensagem completa), faz parse, salva no banco e envia notificação

### 8. `main()` — Loop principal
Abre a conexão SQLite uma vez (não abre/fecha a cada iteração para eficiência) e roda o loop de polling indefinidamente.

---

## Como usar

### Pré-requisitos

1. **Hydroxide rodando** como daemon:
   ```bash
   hydroxide imap &
   ```
   (primeira vez: `hydroxide auth andreclawdbot` para autenticar)

2. **Python 3.8+** instalado

3. Nenhuma dependência externa além da stdlib Python

### Executar

```bash
cd email-monitor
python3 monitor_email.py
```

Para rodar como serviço em background (systemd):

```ini
[Unit]
Description=Email Monitor (IMAP → SQLite)
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/email-monitor/monitor_email.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Verificar e-mails salvos

```bash
sqlite3 emails.db "SELECT uid, from_email, subject, received_at FROM emails ORDER BY id DESC LIMIT 10;"
```

### Log

```bash
tail -f inbox.log
```

---

## Estrutura de arquivos gerados

```
email-monitor/
├── monitor_email.py   # Este script
├── emails.db          # Base de dados SQLite (gerada automaticamente)
└── inbox.log          # Log de operações (gerado automaticamente)
```
