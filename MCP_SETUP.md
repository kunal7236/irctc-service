# Deploy

## What ships

```
irctc/            the service layer (+ data/ station and train lookups)
mcp_server.py     the MCP server, 9 tools over Streamable HTTP
probe.py          call one tool from the CLI — local or deployed
tests/            57 unit tests (no network) + e2e_mcp.py (live)
requirements.txt
.env.example
```

`venv/` and `.env` are local only. Never copy either to the Pi.

## Copy to the Pi

```bash
cd ~/Downloads/PNRStatusBot
tar czf /tmp/irctc.tgz --exclude venv --exclude .env --exclude __pycache__ \
  --exclude '.DS_Store' irctc-service
scp /tmp/irctc.tgz kunal@rasp.local:~/
ssh kunal@rasp.local 'tar xzf ~/irctc.tgz -C ~ && rm ~/irctc.tgz'
```

## Build it there

```bash
ssh kunal@rasp.local
cd ~/irctc-service
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

If `cryptography` won't build (older Pi OS, no prebuilt wheel):

```bash
sudo apt install -y build-essential libssl-dev libffi-dev python3-dev cargo
```

## Configure

```bash
./venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
cp .env.example .env
nano .env
```

```ini
IRCTC_MCP_TOKEN=<the token you just generated>
IRCTC_MCP_HOST=127.0.0.1
IRCTC_MCP_PORT=8100
IRCTC_MCP_ALLOWED_HOSTS=irctcmcp.kunalka.me,127.0.0.1,localhost
```

Generate a fresh token here — don't reuse the laptop's. One token per place
means either can be revoked without touching the other.

**`IRCTC_MCP_ALLOWED_HOSTS` is the line that breaks deployments.** The server
refuses requests whose `Host` header it doesn't recognise, and through the
tunnel that header is `irctcmcp.kunalka.me`, not localhost. Omit it and every
request fails before reaching a tool, with an error that doesn't point at the
cause.

## Test on the Pi

```bash
cd ~/irctc-service
./venv/bin/python -m unittest discover -s tests -q          # 57, no network
set -a && . ./.env && set +a
./venv/bin/python mcp_server.py &
./venv/bin/python tests/e2e_mcp.py                          # live, needs the server up
IRCTC_TEST_PNR=<pnr> ./venv/bin/python tests/e2e_mcp.py     # + PNR and refund
```

## Run it as a service

`/etc/systemd/system/irctc-mcp.service`:

```ini
[Unit]
Description=IRCTC MCP server
After=network-online.target

[Service]
Type=simple
User=kunal
WorkingDirectory=/home/kunal/irctc-service
EnvironmentFile=/home/kunal/irctc-service/.env
ExecStart=/home/kunal/irctc-service/venv/bin/python mcp_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now irctc-mcp
journalctl -u irctc-mcp -f
```

## Tunnel

The bot already runs on 8000 behind `bot.kunalka.me`; this is a second
hostname on the same tunnel. In `~/.cloudflared/config.yml`:

```yaml
ingress:
  - hostname: bot.kunalka.me
    service: http://localhost:8000
  - hostname: irctcmcp.kunalka.me
    service: http://localhost:8100
  - service: http_status:404
```

```bash
cloudflared tunnel route dns <tunnel-name> irctcmcp.kunalka.me
sudo systemctl restart cloudflared
```

## Verify from outside

```bash
cd ~/Downloads/PNRStatusBot/irctc-service
IRCTC_MCP_URL=https://irctcmcp.kunalka.me/mcp IRCTC_MCP_TOKEN=<pi-token> \
  ./venv/bin/python probe.py find_station Delhi
```

Not `curl`. The server is stateful — a bare `tools/list` POST is rejected for
want of an `initialize` handshake and a session id, which reads as a broken
deployment when it isn't.

`GET /` returning 404 is also correct. MCP lives at `/mcp` and only speaks
POST; there is no page to open.

## Two ways to authenticate

The server accepts the same token either way, simultaneously.

**Header** — `Authorization: Bearer <token>` against `https://irctcmcp.kunalka.me/mcp`.
Use this wherever you can. Works with Gemini CLI, Claude Code, the MCP
Inspector.

**Token in the URL** — `https://irctcmcp.kunalka.me/mcp/<token>`, no header at
all. For clients whose UI offers OAuth or nothing: Gemini's custom connected
app dialog and Claude's connector UI without the request-headers beta.

The path form is weaker: URLs land in Cloudflare's logs and the AI provider's
logs far more readily than headers do. Fine for a personal read-only service.
Rotate the token and both forms change together.

## Connect agents

**Claude** — Settings → Connectors → Add custom connector. If the dialog shows
a **Request headers** section (beta, not on every account), use the plain
`/mcp` URL with Authentication set to None and an `authorization` header of
`Bearer <token>` — include the word Bearer, Claude sends the value verbatim.
If there is no such section, use the path form instead.

**Gemini app (web/mobile)** — Settings → Connected apps → custom app. It has
no header field, only OAuth client ID/secret, so paste the **path form**:
`https://irctcmcp.kunalka.me/mcp/<token>` and leave the OAuth fields empty.

**Gemini CLI** — `settings.json`:

```json
{
  "mcpServers": {
    "irctc": {
      "httpUrl": "https://irctcmcp.kunalka.me/mcp",
      "headers": { "Authorization": "Bearer <pi-token>" }
    }
  }
}
```

**ChatGPT** — Settings → Connectors → Create, developer mode. Its connector
flow expects OAuth 2.1 with dynamic client registration, so a static bearer
token may be refused. That's the reason if it fails, not a fault in the
server. Claude and Gemini accept the token fine.

## Security

A static shared secret in a header is fine for personal use over HTTPS, but:

- Anyone holding it gets your full quota against erail, confirmtkt and
  easemytrip, and can look up any PNR.
- No per-client identity, expiry, or revocation short of rotating and updating
  every agent.
- Rotate: edit `.env`, `sudo systemctl restart irctc-mcp`.
- Don't paste it into a chat with an agent you don't control — it lands in that
  provider's logs.

The upgrade path is OAuth via FastMCP's auth providers, which also unblocks
ChatGPT.
