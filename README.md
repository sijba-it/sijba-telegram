# sijba-telegram

Chat with sijba through your own Telegram bot. Connector for the sijba
provisioner (order it in the config tool, tab Connectors), or run it anywhere
that reaches Core.

- **Inbound:** long-polls the Telegram Bot API (no public URL needed) and
  injects text from paired chats via `POST /api/message` with source `telegram`.
- **Outbound:** registers with Core for replies with source `telegram` and
  sends them back to the originating chat (`source_meta.telegram_chat_id`).
- **Pairing:** only chats that sent `/pair <code>` are served; `/unpair` leaves.
  Unpaired chats get no answer except a hint on commands. The list is kept in
  Core's store (`/api/store/<HANDLER_ID>/paired_chats`). Five wrong codes per
  chat lock it for ten minutes.

Text only for now (no files, voice, or groups-specific handling). Messages pass
through Telegram's servers.

## Setup

1. In Telegram, talk to @BotFather → `/newbot` → copy the token.
2. Order "Telegram" in the config tool with the token and a pairing code.
3. Send `/pair <code>` to your bot.

## Env

| Env | Default | |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | from @BotFather |
| `TELEGRAM_PAIRING_CODE` | *(required)* | code for `/pair` |
| `TELEGRAM_TARGET` | *(empty)* | handler or channel id to send messages to; empty = normal pipeline |
| `SIJBA_URL` / `SIJBA_TOKEN` | | Core and its token |
| `HANDLER_ID` | `telegram` | registered id, also the store namespace |
| `LISTEN_PORT` / `ADVERTISE_URL` | `9102` / auto | where Core calls `/invoke` |

The bot's webhook is removed on start (`getUpdates` needs that). Run one
instance per bot token.

`connector.json` is the order form the provisioner bakes into its catalog.

## Tests

```bash
docker build -f Dockerfile.test .
```
