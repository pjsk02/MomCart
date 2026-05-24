"""CLI: test a voice file end-to-end (STT → parse → print)."""
import asyncio
import sys
from pathlib import Path


async def run(audio_path: Path) -> None:
    from src.stt import transcribe

    print(f"Transcribing {audio_path}...")
    transcript = await transcribe(audio_path)
    print(f"Transcript: {transcript!r}\n")

    from src.agent import format_item_list, parse_grocery_text

    print("Parsing grocery items...")
    items = await parse_grocery_text(transcript)

    if not items:
        print("No items parsed.")
        return

    print(f"Parsed {len(items)} items:\n{format_item_list(items)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.test_voice <path/to/audio.ogg>")
        sys.exit(1)

    asyncio.run(run(Path(sys.argv[1])))
