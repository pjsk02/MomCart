# MomCart 🛒

> A Telegram bot that lets mom build a grocery list by voice, photo, or text — and ships a live packing checklist to the neighborhood shopkeeper with one-tap status buttons.

Built for the [DEV.to Gemma 4 Challenge](https://dev.to/challenges/gemma).

---

## Why it exists

Mom currently writes the monthly pantry list by hand, photographs it, and sends the photo to the shopkeeper on WhatsApp. She forgets items, sends "addendums," and the back-and-forth takes 30+ minutes. MomCart removes the typing friction, remembers past orders, tracks out-of-stock items, and gives the shopkeeper a structured, tappable checklist.

---

## How it works

```
Mom speaks / types / sends photo
          ↓
  faster-whisper  ←─ voice note transcribed locally
          ↓
  Gemma 4 E4B (Ollama)  ←─ parses Hinglish / Telugu / English grocery text
          ↓
  Chroma pantry DB  ←─ fuzzy-matches items to canonical catalog names
          ↓
  Notion (via MCP)  ←─ items written as Status='cart' rows
          ↓
  Mom builds cart over multiple days, then says "bhej do"
          ↓
  Status flipped to 'pending'; shopkeeper notified
          ↓
  Shopkeeper taps ✅ / ⚠️ / ❌ per item  →  Notion updated live
          ↓
  ❌ Out triggers substitution prompt to mom + wishlist entry
          ↓
  Mom checks /status, /last, /wishlist anytime
```

---

## Features

### 🗣️ Multi-modal input
Mom can send a **voice note** (Hinglish / Telugu / English), a **photo** (OCR — bonus), or plain **text**. faster-whisper transcribes audio locally with automatic language detection; Gemma 4 E4B parses the result into structured items with quantity and unit.

### 🛒 Persistent accumulation cart
Items are written to Notion immediately as `Status='cart'` — no confirmation step. Mom can add items across multiple sessions and days. The active cart is tracked in `data/active_cart.json` with a stable `CartID`.

- `/cart` — view the current cart as a formatted table
- `/undo` — remove only the last batch of items added
- `/remove <item>` — remove a single item (fuzzy-canonicalized via Chroma)
- `/clear` — empty the entire cart
- `/send` or "bhej do" — flip all cart rows to `pending` and notify shopkeeper

### 📦 Shopkeeper packing checklist
After `/send`, the shopkeeper receives a single Telegram message listing every item with three inline buttons per row:

| Button | Meaning | Notion status |
|--------|---------|---------------|
| ✅ | Packed | `packed` |
| ⚠️ | Partial stock | `partial` |
| ❌ | Out of stock | `out` |

Tapping a button collapses that row to a single label and updates Notion live. Mom watches progress with `/status`.

### 🔁 Substitution dialog *(Feature 5)*
When the shopkeeper taps ❌ Out on any item, the bot automatically:

1. Looks up the item's category in the Chroma pantry catalog
2. Finds the top 2 most similar items **in the same category** via vector search
3. Sends mom a new message with two substitute options and a Skip button

```
❌ toor dal nahi mila / Out of stock.
Substitute lena hai?

1️⃣ moong dal
2️⃣ chana dal
❌ Skip
```

- Mom taps a choice → substitute is added to her active cart instantly
- Mom taps Skip → no substitute, item stays in wishlist
- If shopkeeper later corrects a mistaken ❌ tap → wishlist row is auto-deleted

### 💭 Wishlist cart *(Feature 6)*
Every item marked ❌ Out is automatically added to a persistent **wishlist** in Notion (`Status='wishlist'`). If the same item goes out twice, its quantity accumulates — no duplicate rows.

- `/wishlist` — view all unmet items as a formatted table
- `/wishlist remove <item>` or `/wishlist_remove <item>` — remove one entry
- `/wishlist clear` — empty the whole wishlist
- "add all from wishlist" / "wishlist se sab add karo" → bulk-add to active cart

**Proactive nudge:** when mom starts a fresh cart after a `/send`, the bot checks for any open wishlist items and sends a one-time prompt like:

> "Wishlist mein 2 items hain pichli baar ke (toor dal, mustard oil). Add karu?"

Replying "haan" or "yes" adds them all to the new cart automatically.

### 🧠 "Same as last time" recall
Mom can say "last time jaisa" or "same order" and the bot fetches her last order from the Chroma `past_orders` collection, re-parses any additions, and builds the combined list — without going to Notion.

### 📜 Order history
- `/last` — shows the most recently sent order with live statuses (packed / partial / out / pending), excluding wishlist and cart-draft rows
- `/status` — shows a count summary for the current active order

---

## Tech stack

| Layer | Tool | Why |
|-------|------|-----|
| Bot framework | `python-telegram-bot` 22.5 (async) | Native async, inline keyboards, file download |
| Speech-to-text | `faster-whisper` large-v3 | Local, fast, Hinglish-capable, VAD filter |
| LLM | Gemma 4 E4B via Ollama | Gemma 4 Challenge requirement; runs on-device |
| LLM client | `langchain-ollama` `ChatOllama` | Structured JSON output mode, zero-temp parsing |
| Agent orchestration | `LangGraph` / `langchain-core` | ReAct agent pattern, tool chaining |
| Vector DB | `chromadb` (local persistent) | Pantry catalog + order history embeddings |
| Embeddings | `all-MiniLM-L6-v2` (Chroma default) | Fast, good enough for grocery canonicalization |
| Data store | Notion database | Human-readable, shareable, already used by family |
| Notion integration | `@notionhq/notion-mcp-server` (stdio MCP) | Official Notion MCP; no custom API wrapper needed |
| MCP client | `langchain-mcp-adapters` `MultiServerMCPClient` | Bridges MCP tools to LangChain tool interface |
| Settings | `pydantic-settings` `BaseSettings` | Typed env loading, `.env` file support |
| Logging | `loguru` | Structured, colored, zero-config |
| Data wrangling | `pandas` | BigBasket CSV filtering at seed time |
| Runtime | Python 3.11, asyncio-first | Required by `langchain-mcp-adapters` |

---

## Notion database setup

Create a database called **Grocery Orders** with these exact columns:

| Column | Type | Options |
|--------|------|---------|
| Item | Title | — |
| Qty | Number | — |
| Unit | Select | `kg`, `g`, `L`, `ml`, `pcs`, `packet` |
| Status | Select | `pending`, `packed`, `partial`, `out`, `cart`, `wishlist` |
| OrderID | Rich text | — |
| CreatedAt | Created time | — |

---

## Setup

### Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) running locally with `gemma4:e4b` pulled (`ollama pull gemma4:e4b`)
- Node.js 18+ (for the Notion MCP server — installed automatically via `npx`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Notion internal integration token + the database above

### Install

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .
```

### Configure

```bash
cp .env.example .env
# fill in all values — see .env.example for descriptions
```

### Seed the pantry catalog

Download `BigBasket Products.csv` from [Kaggle](https://www.kaggle.com/datasets/surajjha101/bigbasket-entire-product-list-28k-datapoints) and save it as `data/bigbasket.csv`, then:

```bash
python -m scripts.seed_pantry
```

This loads ~200 filtered grocery SKUs + 41 hand-curated fresh produce items into Chroma.

### Run

```bash
python -m src.bot
```

---

## Commands

### Mom's commands

| Command | What it does |
|---------|-------------|
| `/start` | Show help |
| `/cart` | View current cart |
| `/send` | Send cart to shopkeeper |
| `/clear` | Empty the cart |
| `/remove <item>` | Remove one item from cart |
| `/undo` | Remove the last batch added |
| `/last` | Show last sent order with statuses |
| `/status` | Live packing progress for current order |
| `/wishlist` | View out-of-stock items from past orders |
| `/wishlist remove <item>` | Remove one item from wishlist |
| `/wishlist clear` | Empty the wishlist |

**Natural language shortcuts** (no slash needed):
- "bhej do" / "send to shop" / "shop ko bhej do" → `/send`
- "add all from wishlist" / "wishlist se sab add karo" → add wishlist to cart
- "last time jaisa" / "same order" → recall previous order
- "haan" / "yes" → confirm wishlist nudge prompt

### Shopkeeper's interface

Shopkeeper receives one Telegram message per order with inline ✅ / ⚠️ / ❌ buttons. No commands needed — button taps update Notion directly.

---

## Project structure

```
momcart/
├── CLAUDE.md              — project brief + architecture decisions
├── README.md
├── .env.example
├── pyproject.toml
├── src/
│   ├── config.py          — env loading, typed settings (pydantic-settings)
│   ├── bot.py             — all Telegram handlers, cart/wishlist/sub logic
│   ├── stt.py             — faster-whisper singleton + VAD transcription
│   ├── agent.py           — Gemma 4 JSON parsing, canonicalization, recall
│   ├── notion_tools.py    — Notion MCP client: cart, wishlist, order ops
│   ├── memory.py          — Chroma collections: pantry_items, past_orders
│   ├── pantry_seed.py     — BigBasket CSV → Chroma loader + fresh produce
│   └── prompts.py         — all system prompts in one place
├── scripts/
│   ├── seed_pantry.py     — CLI to populate Chroma from CSV
│   └── test_voice.py      — CLI to test a voice file end-to-end
└── data/
    ├── bigbasket.csv      — pantry catalog (gitignored)
    ├── chroma/            — persistent vector store (gitignored)
    ├── active_cart.json   — current cart state (auto-managed)
    └── active_wishlist.json — muted wishlist items (future)
```

---

## Demo walkthrough

1. Mom: *"do kilo toor dal, ek litre mustard oil"* (voice note)
2. Bot: "Cart mein add ho gaya: — 2 kg toor dal — 1 L mustard oil"
3. Mom adds more items over the next day
4. Mom: "bhej do" → order dispatched to shopkeeper
5. Shopkeeper taps ❌ on toor dal → substitution prompt sent to mom
6. Mom taps "moong dal" → moong added to cart, toor dal in wishlist
7. Shopkeeper taps ❌ on mustard oil, mom taps Skip → mustard oil in wishlist
8. Next day mom adds potatoes → bot nudges: "Wishlist mein 2 items hain (toor dal, mustard oil). Add karu?"
9. Mom: "haan" → both moved from wishlist to cart automatically
10. Mom: `/status` → live count of packed / partial / out / pending

---

## Environment variables

See [`.env.example`](.env.example) for the full list. Key variables:

```
TELEGRAM_BOT_TOKEN=   # from @BotFather
MOM_ID=               # Telegram user ID of mom
SHOPKEEPER_ID=        # Telegram user ID of shopkeeper
NOTION_API_TOKEN=     # Notion internal integration secret
NOTION_DATABASE_ID=   # Notion DB URL or bare UUID
OLLAMA_HOST=http://localhost:11434
GEMMA_MODEL=gemma4:e4b
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cpu    # or cuda
```
