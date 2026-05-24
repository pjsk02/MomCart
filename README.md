# MomCart 🛒

A Telegram bot that lets mom build a grocery order by voice, photo, or text — and ships a packing list to the neighborhood shopkeeper with one-tap status buttons.

Built for the [DEV.to Gemma 4 Challenge](https://dev.to/challenges/gemma).

---

## How it works

```
Mom (voice/photo/text)
        ↓
  faster-whisper STT
        ↓
  Gemma 4 (E4B via Ollama) — parse & canonicalize items
        ↓
  Chroma vector DB — fuzzy match against pantry catalog
        ↓
  Mom confirms list
        ↓
  Notion MCP — write order rows
        ↓
  Shopkeeper gets Telegram message with ✅ / ⚠️ / ❌ buttons
        ↓
  Buttons update Notion live; mom checks /status
```

## Stack

| Layer | Tool |
|---|---|
| Bot | python-telegram-bot 22.7 |
| STT | faster-whisper large-v3 |
| LLM | Gemma 4 E4B via Ollama |
| Agent | LangGraph |
| Vector DB | Chroma (local) |
| Data store | Notion (via MCP) |

## Setup

### Prerequisites
- Python 3.11+
- [Ollama](https://ollama.com) running locally with `gemma4:e4b` pulled
- Node.js (for the Notion MCP server)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Notion internal integration token + database

### Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -e .
```

### Configure

```bash
cp .env.example .env
# fill in all values in .env
```

### Seed pantry

Download `bigbasket.csv` from [Kaggle](https://www.kaggle.com/datasets/surajjha101/bigbasket-entire-product-list-28k-datapoints) and place it at `data/bigbasket.csv`, then:

```bash
python -m scripts.seed_pantry
```

### Run

```bash
python -m src.bot
```

## Demo flow

1. Mom: *"do kilo aata, ek paav haldi, biscuit ka packet"* (voice note)
2. Bot: parsed list + confirmation prompt
3. Mom: *"haan, aur do kg gud add karo"*
4. Mom: *"send to shop"*
5. Shopkeeper sees inline-button message, taps status per item
6. Mom: `/status` → live packing progress

## Project structure

```
src/
  config.py        — env + settings
  bot.py           — Telegram handlers
  stt.py           — faster-whisper wrapper
  agent.py         — LangGraph + Gemma 4
  notion_tools.py  — Notion MCP client
  memory.py        — Chroma collections
  pantry_seed.py   — BigBasket CSV loader
  prompts.py       — all system prompts
scripts/
  seed_pantry.py   — CLI: populate Chroma
  test_voice.py    — CLI: end-to-end voice test
data/
  chroma/          — persistent vector store (gitignored)
  bigbasket.csv    — pantry catalog (gitignored)
```
