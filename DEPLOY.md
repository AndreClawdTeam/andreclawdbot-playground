# DEPLOY.md — andreclawdbot-playground

## Como está sendo servido

Site estático puro (HTML/CSS/JS) servido via **Python `http.server`**, gerenciado pelo **systemd**.

- **Serviço:** `playground.service`
- **Porta:** `3002`
- **URL de acesso:** `http://138.197.19.184:3002/`
- **Diretório servido:** `/home/clawdbot/.openclaw/workspace/coding/andreclawdbot-playground`

## Como atualizar

```bash
cd /home/clawdbot/.openclaw/workspace/coding/andreclawdbot-playground
git pull origin master
# Não precisa reiniciar o serviço — Python serve os arquivos do disco diretamente
```

## Como parar / reiniciar o serviço

```bash
# Status
sudo systemctl status playground.service

# Reiniciar
sudo systemctl restart playground.service

# Parar
sudo systemctl stop playground.service

# Iniciar
sudo systemctl start playground.service

# Ver logs
sudo journalctl -u playground.service -f
```

## Serviço systemd

Arquivo: `/etc/systemd/system/playground.service`

O serviço está configurado com `WantedBy=multi-user.target`, ou seja, sobe automaticamente no boot.

## ⚠️ O que não fazer

- Não usar as portas 22, 1025, 1143, 5432, 8765, 8766, 9222, 18789, 18792 — todas em uso por outros serviços.
- Não instalar nginx sem verificar conflitos com outros projetos.
