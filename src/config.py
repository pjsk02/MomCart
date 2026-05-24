from __future__ import annotations

from pathlib import Path
from typing import Literal, Union

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    MOM_ID: int = 0
    SHOPKEEPER_ID: int = 0

    # Notion
    NOTION_API_TOKEN: str = ""
    NOTION_DATABASE_ID: str = ""

    @field_validator("NOTION_DATABASE_ID", mode="before")
    @classmethod
    def extract_database_uuid(cls, v: str) -> str:
        """Accept full Notion URLs or bare UUIDs — always store the bare UUID."""
        import re
        if not v:
            return v
        # match 32-hex-char UUID with optional dashes, anywhere in the string
        m = re.search(r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})", v, re.I)
        if m:
            raw = m.group(1).replace("-", "")
            return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
        return v

    # Ollama / Gemma
    OLLAMA_HOST: str = "http://localhost:11434"
    GEMMA_MODEL: str = "gemma4:e4b"

    # Whisper
    WHISPER_MODEL: str = "large-v3"
    WHISPER_DEVICE: Literal["cuda", "cpu"] = "cpu"
    WHISPER_COMPUTE_TYPE: Literal["float16", "int8", "float32"] = "int8"

    # Chroma
    CHROMA_PATH: Path = Path("./data/chroma")

    @field_validator("CHROMA_PATH", mode="before")
    @classmethod
    def resolve_chroma_path(cls, v: Union[str, Path]) -> Path:
        return Path(v)


settings = Settings()

if __name__ == "__main__":
    print(f"GEMMA_MODEL={settings.GEMMA_MODEL}")
    print(f"OLLAMA_HOST={settings.OLLAMA_HOST}")
    print(f"CHROMA_PATH={settings.CHROMA_PATH}")
