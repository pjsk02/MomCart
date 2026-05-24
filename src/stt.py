from pathlib import Path

from loguru import logger

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        from src.config import settings

        logger.info(
            f"Loading Whisper model={settings.WHISPER_MODEL} "
            f"device={settings.WHISPER_DEVICE} compute={settings.WHISPER_COMPUTE_TYPE}"
        )
        try:
            _model = WhisperModel(
                settings.WHISPER_MODEL,
                device=settings.WHISPER_DEVICE,
                compute_type=settings.WHISPER_COMPUTE_TYPE,
            )
            logger.info("Whisper model loaded")
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            raise
    return _model


async def transcribe(audio_path: Path) -> str:
    try:
        model = _get_model()
        logger.debug(f"Transcribing {audio_path}")
        segments, info = model.transcribe(
            str(audio_path),
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        logger.info(f"Transcript ({info.language}): {text!r}")
        return text
    except Exception as e:
        logger.error(f"Transcription failed for {audio_path}: {e}")
        raise
