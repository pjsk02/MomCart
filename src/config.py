from pathlib import Path
from typing import Literal

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
    def resolve_chroma_path(cls, v: str | Path) -> Path:
        return Path(v)


settings = Settings()

if __name__ == "__main__":
    print(f"GEMMA_MODEL={settings.GEMMA_MODEL}")
    print(f"OLLAMA_HOST={settings.OLLAMA_HOST}")
    print(f"CHROMA_PATH={settings.CHROMA_PATH}")
