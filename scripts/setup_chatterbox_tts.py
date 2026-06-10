#!/usr/bin/env python3
"""Register the bundled Chatterbox TTS container as a J.A.R.V.I.S TTS endpoint.

The chatterbox service (services/chatterbox) exposes an OpenAI-compatible
/v1/audio/speech endpoint on port 8004 and clones the reference clip in
data/chatterbox_voices. J.A.R.V.I.S already supports OpenAI-style TTS via the
"endpoint:<id>" provider, so this just:

  1. Upserts a ModelEndpoint pointing at http://chatterbox:8004/v1.
     - model_type="tts" keeps it OUT of the LLM/image model pickers (those
       filter to model_type=="llm"/"image"), while Settings -> Speech lists it
       because its pinned model name contains "tts".
  2. Points the TTS settings at it (provider, voice, pacing model name).
  3. Disables the old Kokoro endpoint (kokoro-tts) if present.

Idempotent: re-running updates the existing row/settings in place.

Run inside the jarvis container:
    docker compose exec jarvis python scripts/setup_chatterbox_tts.py
Override defaults via flags, e.g. --voice jarvis --base-url http://chatterbox:8004/v1
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENDPOINT_ID = "chatterbox-tts"
OLD_KOKORO_ID = "kokoro-tts"


def main() -> int:
    ap = argparse.ArgumentParser(description="Wire the Chatterbox TTS container into J.A.R.V.I.S.")
    ap.add_argument("--base-url", default="http://chatterbox:8004/v1",
                    help="Chatterbox OpenAI-compatible base URL (default: in-network compose host).")
    ap.add_argument("--voice", default="jarvis",
                    help="Voice id = reference clip stem in data/chatterbox_voices (default: jarvis).")
    ap.add_argument("--model", default="chatterbox", help="Model name sent in synthesis requests.")
    ap.add_argument("--name", default="Chatterbox TTS", help="Display name for the endpoint.")
    ap.add_argument("--speed", default="1", help="Speech speed multiplier (Chatterbox ignores it).")
    ap.add_argument("--no-activate", action="store_true",
                    help="Register the endpoint but do not switch the TTS provider to it.")
    ap.add_argument("--keep-kokoro", action="store_true",
                    help="Do not disable the old kokoro-tts endpoint.")
    args = ap.parse_args()

    from src.database import SessionLocal, ModelEndpoint
    from src.settings import load_settings, save_settings

    # Pinned model name must contain "tts" so Settings -> Speech lists it.
    models_json = json.dumps(["tts-1"])

    db = SessionLocal()
    try:
        ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == ENDPOINT_ID).first()
        action = "updated" if ep else "created"
        if not ep:
            ep = ModelEndpoint(id=ENDPOINT_ID)
            db.add(ep)
        ep.name = args.name
        ep.base_url = args.base_url
        ep.api_key = None
        ep.is_enabled = True
        ep.model_type = "tts"
        ep.endpoint_kind = "local"
        ep.model_refresh_mode = "manual"
        ep.cached_models = models_json
        ep.pinned_models = models_json

        # Retire the old Kokoro endpoint so it stops showing in the dropdown.
        if not args.keep_kokoro:
            old = db.query(ModelEndpoint).filter(ModelEndpoint.id == OLD_KOKORO_ID).first()
            if old:
                old.is_enabled = False
                print(f"[chatterbox] disabled old endpoint: {OLD_KOKORO_ID}")
        db.commit()
    finally:
        db.close()
    print(f"[chatterbox] endpoint {action}: id={ENDPOINT_ID} base_url={args.base_url}")

    s = load_settings()
    s["tts_enabled"] = True
    s["tts_model"] = args.model
    s["tts_voice"] = args.voice
    s["tts_speed"] = str(args.speed)
    if not args.no_activate:
        s["tts_provider"] = f"endpoint:{ENDPOINT_ID}"
    save_settings(s)
    print(f"[chatterbox] TTS settings: provider={s['tts_provider']} voice={s['tts_voice']} "
          f"model={s['tts_model']} enabled={s['tts_enabled']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
