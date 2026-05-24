# MomCart – Project Brief

## What we're building
A Telegram bot that lets my mom build a grocery order by voice, photo, or text and ships a packing list to our neighborhood shopkeeper who checks items off via inline buttons. Built for the DEV.to Gemma 4 Challenge (submission due May 24, 2026).

## Why it exists
Mom currently writes the monthly pantry list by hand, photographs it, and sends the photo to the shopkeeper on WhatsApp. She forgets items, sends "addendums," and the back-and-forth takes 30+ minutes. MomCart removes the typing/writing friction, remembers past orders, and gives the shopkeeper a structured packable list.

## The two users
- **Mom** – Hindi/Telugu/English speaker, sends voice notes and photos, hates typing. Identified by her Telegram user ID (env: `MOM_ID`).
- **Shopkeeper** – Receives the order as a single Telegram message with inline buttons (✅ Packed / ⚠️ Partial / ❌ Out) next to each item. Identified by `SHOPKEEPER_ID`. Same bot, routed by chat ID.

## Locked architecture decisions (do not relitigate)
- **LLM:** Gemma 4 E4B via Ollama (`gemma4:e4b`). Call from Python via `langchain_ollama.ChatOllama`.
- **STT:** `faster-whisper` `large-v3` model. Gemma 4's native audio path is not used (Ollama support immature as of May 2026).
- **Agent orchestration:** LangGraph (`create_react_agent` is fine for MVP).
- **Vector DB:** Chroma (local, persistent) – two collections: `pantry_items` (SKU catalog) and `past_orders` (order history embeddings).
- **Data store:** Notion database "Grocery Orders" – pre-created in the Notion UI, accessed via the official local Notion MCP server (`@notionhq/notion-mcp-server` over stdio) wired through `langchain-mcp-adapters`.
- **Bot framework:** `python-telegram-bot` v22.7 (async).
- **Pantry seed:** BigBasket Kaggle CSV (27,555 rows) filtered to ~200 common Indian pantry items. Gemma generates Hindi/Telugu names from English at seed time.

## Non-goals (do NOT build these unless explicitly asked)
- Web UI / mobile app / PWA. Telegram is the only frontend.
- Payment, delivery, inventory management.
- User authentication beyond Telegram chat IDs.
- Multi-tenant support. Single household.
- Training/fine-tuning any models. Everything off-the-shelf.
- Production deployment / cloud hosting / Docker. Runs on dev laptop.
- Photo OCR is *bonus*, not core. Do it only if voice + text path is shipped.

## Coding conventions
- Python 3.11+. Async-first (`asyncio`, async handlers).
- Type hints on every function signature.
- One module per concern: `bot.py`, `stt.py`, `agent.py`, `notion_tools.py`, `memory.py`, `pantry_seed.py`, `config.py`.
- All secrets/IDs in `.env`, loaded via `python-dotenv`. Never hardcode.
- Use `loguru` for logging – info level for happy path, debug for LLM inputs/outputs.
- Prefer composition over inheritance. No classes when a function will do.
- Every external call (Ollama, Whisper, Notion, Telegram) wrapped in try/except with logged context.
- Pydantic models for the structured grocery item shape:
  ```python
  class GroceryItem(BaseModel):
      name_en: str            # canonical English name from pantry catalog
      name_native: str | None # Hindi/Telugu as user said it (optional)
      qty: float
      unit: Literal["kg", "g", "L", "ml", "pcs", "packet"]
  ```

## Repo structure (target)
```
momcart/
├── CLAUDE.md
├── README.md
├── .env.example
├── pyproject.toml
├── src/
│   ├── config.py          # env loading, IDs, paths
│   ├── bot.py             # python-telegram-bot entrypoint + handlers
│   ├── stt.py             # faster-whisper wrapper
│   ├── agent.py           # LangGraph agent + Gemma 4 ChatOllama
│   ├── notion_tools.py    # MCP client → LangChain tools
│   ├── memory.py          # Chroma collections (pantry + past_orders)
│   ├── pantry_seed.py     # one-shot BigBasket → Chroma loader
│   └── prompts.py         # all system prompts in one place
├── data/
│   ├── bigbasket.csv      # downloaded once
│   └── chroma/            # persistent vector store
└── scripts/
    ├── seed_pantry.py     # CLI to populate Chroma from CSV
    └── test_voice.py      # CLI to test a voice file end-to-end
```

## Environment variables (put in `.env.example`)
```
TELEGRAM_BOT_TOKEN=
MOM_ID=
SHOPKEEPER_ID=
NOTION_API_TOKEN=
NOTION_DATABASE_ID=
OLLAMA_HOST=http://localhost:11434
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cuda             # or cpu
WHISPER_COMPUTE_TYPE=float16    # or int8
CHROMA_PATH=./data/chroma
GEMMA_MODEL=gemma4:e4b
```

## Demo requirements (this is what gets recorded for the submission)
1. Mom sends a Hinglish voice note: *"do kilo aata, ek paav haldi, biscuit ka packet"*
2. Bot replies with parsed list for confirmation.
3. Mom: *"haan, aur do kg gud add karo"* → agent appends jaggery.
4. Mom: *"send to shop"* → order written to Notion, message dispatched to shopkeeper.
5. Shopkeeper taps ✅ on atta, ⚠️ on haldi → Notion updates live, mom can `/status` to see.
6. Bonus: mom says *"last time jaisa"* → agent retrieves past order from Chroma.

## Style for code generation
- Write the smallest thing that works. No premature abstraction.
- When in doubt about a library API, **stop and tell me** rather than guess – I'll provide docs.
- Include a `__main__` block in scripts so I can `python -m src.bot` directly.
- After every meaningful module, suggest one terminal command I can run to smoke-test it before moving on.
