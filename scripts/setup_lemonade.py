#!/usr/bin/env python3
"""Register the local lemonade-sdk server with J.A.R.V.I.S and route the
chat LLM, Speech-to-Text (Whisper) and Text-to-Speech (Kokoro) through it.

Lemonade exposes an OpenAI-compatible API, so everything works through the
existing endpoint machinery — no application code changes. Image generation is
NOT handled here (it runs on a separate server).

Base URL is read from the LEMONADE_BASE_URL env var so the same script works
both natively (http://localhost:11434/api/v1) and inside Docker, where the host
is reached via http://host.docker.internal:11434/api/v1.

Idempotent: re-running updates the existing endpoint (keyed on base_url).
"""

import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import SessionLocal, ModelEndpoint, init_db
from src.settings import load_settings, save_settings

BASE_URL = os.environ.get("LEMONADE_BASE_URL", "http://localhost:11434/api/v1")

# ── Model ids (verified live against the running lemonade server) ──
CHAT_MODEL = "Qwen3.6-35B-A3B-GGUF"   # labels: vision, tool-calling
STT_MODEL = "Whisper-Base"            # label: transcription
TTS_MODEL = "kokoro-v1"               # label: tts
TTS_VOICE = "af_heart"                # default Kokoro voice

# The chat endpoint also fronts the audio models (STT/TTS just POST to
# {base}/audio/...). Non-chat model ids — including lemonade's image model
# Z-Image-Turbo — are hidden from the chat picker so they aren't offered as
# chat models.
ALL_MODELS = [CHAT_MODEL, STT_MODEL, TTS_MODEL, "Z-Image-Turbo"]
HIDDEN = [STT_MODEL, TTS_MODEL, "Z-Image-Turbo"]


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        ep = (
            db.query(ModelEndpoint)
            .filter(ModelEndpoint.base_url == BASE_URL, ModelEndpoint.model_type == "llm")
            .first()
        )
        if ep is None:
            ep = ModelEndpoint(id=str(uuid.uuid4())[:8], base_url=BASE_URL)
            db.add(ep)
        ep.name = "Lemonade (local)"
        ep.base_url = BASE_URL
        ep.is_enabled = True
        ep.model_type = "llm"
        ep.endpoint_kind = "local"
        ep.api_key = None
        ep.cached_models = json.dumps(ALL_MODELS)
        ep.hidden_models = json.dumps(HIDDEN)
        ep.owner = None
        db.commit()
        llm_id = ep.id
        print(f"llm endpoint 'Lemonade (local)' (id={llm_id}) -> {BASE_URL}")
    finally:
        db.close()

    settings = load_settings()
    settings.update({
        "default_endpoint_id": llm_id,
        "default_model": CHAT_MODEL,
        "stt_enabled": True,
        "stt_provider": f"endpoint:{llm_id}",
        "stt_model": STT_MODEL,
        "tts_enabled": True,
        "tts_provider": f"endpoint:{llm_id}",
        "tts_model": TTS_MODEL,
        "tts_voice": TTS_VOICE,
    })
    save_settings(settings)

    print(f"Default chat -> {CHAT_MODEL} (endpoint {llm_id})")
    print(f"STT          -> {STT_MODEL} (endpoint:{llm_id})")
    print(f"TTS          -> {TTS_MODEL}, voice={TTS_VOICE} (endpoint:{llm_id})")
    print("Done.")


if __name__ == "__main__":
    main()
