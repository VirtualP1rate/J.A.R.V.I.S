"""Minimal OpenAI-compatible TTS server wrapping the official Chatterbox library.

resemble-ai/chatterbox (PyPI `chatterbox-tts`, MIT) is a library, not a server.
This thin FastAPI wrapper exposes the one endpoint J.A.R.V.I.S. needs,
`POST /v1/audio/speech`, and clones the voice from a reference clip on every
request. We own all of this glue; the only external pieces are Resemble's
official library and its public HuggingFace weights.

J.A.R.V.I.S. sends an OpenAI-style body `{model, input, voice, response_format,
speed}` and detects WAV vs MP3 by magic bytes, so we return WAV directly (no
ffmpeg/mp3 encoding needed).

Env:
  DEVICE          cuda | cpu | auto (default auto -> cuda if available)
  VOICES_DIR      directory of reference clips (default /voices)
  DEFAULT_VOICE   voice id (clip stem) used when none/unknown requested (default "jarvis")
  EXAGGERATION    default expressiveness 0.0-1.0+ (default 0.5)
  CFG_WEIGHT      default pace/speaker-adherence 0.0-1.0 (default 0.5)
  MAX_CHARS       hard cap on input length (default 5000)
  CHUNK_CHARS     split longer text into ~this-size sentence chunks (default 600)
"""
import io
import logging
import os
import re
import struct
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chatterbox-server")

MODEL_VARIANT = os.getenv("CHATTERBOX_MODEL", "standard").lower()  # "standard" | "turbo"
VOICES_DIR = Path(os.getenv("VOICES_DIR", "/voices"))
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "jarvis")
DEFAULT_EXAGGERATION = float(os.getenv("EXAGGERATION", "0.5"))
DEFAULT_CFG_WEIGHT = float(os.getenv("CFG_WEIGHT", "0.5"))
MAX_CHARS = int(os.getenv("MAX_CHARS", "5000"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "600"))
_AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg", ".m4a")

# cond_key identifies the voice conditionals currently baked into the model
# (variant, reference-clip path, exaggeration). While it matches a request we
# reuse model.conds instead of re-cloning the voice on every call.
_STATE: Dict[str, object] = {"model": None, "ready": False, "variant": None, "cond_key": None}
# Serializes synthesis and model reloads: the model is not safe for concurrent
# use, and a variant hot-swap must run exclusively (free old -> load new).
_MODEL_LOCK = threading.Lock()


def _resolve_device() -> str:
    dev = os.getenv("DEVICE", "auto").lower()
    if dev == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    return dev


def _load_model(variant: str):
    """Load a Chatterbox model for the given variant ('standard' | 'turbo')."""
    if variant == "turbo":
        from chatterbox.tts_turbo import ChatterboxTurboTTS as _Model
    else:
        from chatterbox.tts import ChatterboxTTS as _Model
    device = _resolve_device()
    logger.info("Loading Chatterbox (%s) on device=%s ...", variant, device)
    model = _Model.from_pretrained(device=device)
    _STATE["model"] = model
    _STATE["variant"] = variant
    _STATE["cond_key"] = None  # fresh model has no baked-in voice conditionals
    logger.info("Chatterbox ready (variant=%s, sr=%s).", variant, getattr(model, "sr", "?"))
    return model


def _ensure_variant(variant: str):
    """Hot-swap the loaded model to `variant` if it differs. Caller holds _MODEL_LOCK.
    Frees the old model before loading the new one to avoid GPU OOM."""
    if not variant or variant == _STATE.get("variant"):
        return
    logger.info("Switching Chatterbox variant %s -> %s", _STATE.get("variant"), variant)
    _STATE["model"] = None
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass
    _load_model(variant)


def _discover_voices() -> Dict[str, Path]:
    """Map voice id (filename stem) -> clip path for every audio file in VOICES_DIR."""
    voices: Dict[str, Path] = {}
    if VOICES_DIR.is_dir():
        for p in sorted(VOICES_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in _AUDIO_EXTS:
                voices[p.stem] = p
    return voices


def _resolve_voice(requested: Optional[str]) -> Optional[Path]:
    """Pick a reference clip: exact match, else the default, else any available."""
    voices = _discover_voices()
    if not voices:
        return None
    if requested and requested in voices:
        return voices[requested]
    if DEFAULT_VOICE in voices:
        return voices[DEFAULT_VOICE]
    return next(iter(voices.values()))


def _split_text(text: str) -> list[str]:
    """Sentence-aware chunking so long inputs stay coherent and bounded."""
    text = text.strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for s in sentences:
        if current and len(current) + len(s) + 1 > CHUNK_CHARS:
            chunks.append(current.strip())
            current = s
        else:
            current = f"{current} {s}".strip()
    if current.strip():
        chunks.append(current.strip())
    return chunks


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Turbo is faster with stronger pacing; both share the same generate() kwargs
    # we use (audio_prompt_path / exaggeration / cfg_weight). The boot variant
    # comes from env; requests can hot-swap it at runtime (see _ensure_variant).
    _load_model(MODEL_VARIANT)
    _STATE["ready"] = True
    logger.info("Voices: %s", list(_discover_voices()))
    yield


app = FastAPI(title="Chatterbox TTS", lifespan=lifespan)


class SpeechRequest(BaseModel):
    input: str
    voice: Optional[str] = None
    model: Optional[str] = None
    response_format: Optional[str] = None  # accepted, ignored (we return WAV)
    speed: Optional[float] = None          # accepted, ignored (Chatterbox has no speed knob)
    exaggeration: Optional[float] = None
    cfg_weight: Optional[float] = None
    model_variant: Optional[str] = None    # "standard" | "turbo"; hot-swaps if changed
    stream: bool = False                   # chunked WAV: PCM streams out as each text chunk is generated


@app.get("/health")
async def health():
    if not _STATE["ready"]:
        return JSONResponse({"status": "loading"}, status_code=503)
    return {"status": "healthy"}


@app.get("/v1/audio/voices")
async def list_voices():
    return {"voices": [{"id": vid, "name": vid} for vid in _discover_voices()]}


@app.get("/v1/model")
async def current_model():
    return {"variant": _STATE.get("variant")}


def _prepare(req: SpeechRequest):
    """Validate the request and resolve voice/knobs. Raises 400 on bad input
    BEFORE any response bytes go out (matters for the streaming path, where an
    error after the first yield can only abort the connection)."""
    text = (req.input or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input is empty")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]

    ref = _resolve_voice(req.voice)
    exaggeration = req.exaggeration if req.exaggeration is not None else DEFAULT_EXAGGERATION
    cfg_weight = req.cfg_weight if req.cfg_weight is not None else DEFAULT_CFG_WEIGHT
    if ref is None:
        logger.warning("No reference clip found in %s; using Chatterbox default voice.", VOICES_DIR)
    return text, ref, exaggeration, {"exaggeration": exaggeration, "cfg_weight": cfg_weight}


def _ensure_conditionals(model, ref, exaggeration: float):
    """Voice conditioning is expensive, so bake it into the model once per
    (variant, clip, exaggeration) and reuse it. generate() falls back to
    the cached model.conds whenever audio_prompt_path is omitted — which is
    exactly what generate(audio_prompt_path=ref) would recompute every call.
    Caller holds _MODEL_LOCK."""
    if ref is not None:
        key = (_STATE.get("variant"), str(ref), round(exaggeration, 3))
        if _STATE.get("cond_key") != key:
            model.prepare_conditionals(str(ref), exaggeration=exaggeration)
            _STATE["cond_key"] = key


def _generate_chunk(req: SpeechRequest, chunk: str, ref, exaggeration: float,
                    gen_kwargs: dict) -> tuple[np.ndarray, int]:
    """Synthesize one text chunk under the model lock. Acquiring per chunk (not
    per request) keeps the lock from being held while streamed bytes drain to a
    slow client, and lets concurrent requests interleave at chunk granularity."""
    with _MODEL_LOCK:
        _ensure_variant(req.model_variant)
        model = _STATE["model"]
        if model is None:
            raise HTTPException(status_code=503, detail="model not loaded")
        _ensure_conditionals(model, ref, exaggeration)
        with torch.no_grad():
            wav = model.generate(chunk, **gen_kwargs)
        return wav.squeeze(0).detach().cpu().numpy().astype(np.float32), int(model.sr)


def _synthesize(req: SpeechRequest) -> bytes:
    text, ref, exaggeration, gen_kwargs = _prepare(req)
    chunks = _split_text(text)
    if not chunks:
        raise HTTPException(status_code=400, detail="nothing to synthesize")

    pieces, sr = [], 0
    for chunk in chunks:
        pcm, sr = _generate_chunk(req, chunk, ref, exaggeration, gen_kwargs)
        pieces.append(pcm)

    audio = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _wav_stream_header(sr: int) -> bytes:
    """44-byte PCM16-mono WAV header with max-size RIFF/data fields — the
    streaming-WAV convention; players read frames until EOF."""
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def _split_text_streaming(text: str) -> list[str]:
    """Sentence-packed chunks with growing budgets (~100 -> 220 -> 480 ->
    CHUNK_CHARS). A small first chunk gets the opening audio out fast; each
    later chunk at most ~doubles, so with GPU synthesis running >2x realtime
    a clip's playback outlasts the next chunk's generation and the stream
    stays (near-)gapless. A single sentence longer than its budget becomes
    its own chunk — never split mid-sentence."""
    text = text.strip()
    if not text:
        return []
    budgets = (100, 220, 480)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for s in sentences:
        budget = budgets[len(chunks)] if len(chunks) < len(budgets) else CHUNK_CHARS
        if current and len(current) + len(s) + 1 > budget:
            chunks.append(current)
            current = s
        else:
            current = f"{current} {s}".strip()
    if current:
        chunks.append(current)
    return chunks


def _synthesize_stream(req: SpeechRequest, ref, exaggeration: float,
                       gen_kwargs: dict, chunks: list[str]):
    """Yield a WAV header, then raw PCM16 as each text chunk finishes — the
    client starts playing after the first chunk instead of the whole text.
    Sync generator: Starlette iterates it in a worker thread."""
    header_sent = False
    for chunk in chunks:
        pcm, sr = _generate_chunk(req, chunk, ref, exaggeration, gen_kwargs)
        if not header_sent:
            yield _wav_stream_header(sr)
            header_sent = True
        yield (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


# Sync handlers: FastAPI runs them in a worker thread, so the blocking GPU
# synthesis (seconds per call under _MODEL_LOCK) never stalls the event loop —
# /health and other requests stay responsive while a clip is being generated.
@app.post("/v1/audio/speech")
def speech(req: SpeechRequest):
    if req.stream:
        text, ref, exaggeration, gen_kwargs = _prepare(req)
        chunks = _split_text_streaming(text)
        if not chunks:
            raise HTTPException(status_code=400, detail="nothing to synthesize")
        return StreamingResponse(
            _synthesize_stream(req, ref, exaggeration, gen_kwargs, chunks),
            media_type="audio/wav",
            headers={"Content-Disposition": "inline; filename=speech.wav"})
    audio = _synthesize(req)
    return Response(content=audio, media_type="audio/wav",
                    headers={"Content-Disposition": "inline; filename=speech.wav"})


# J.A.R.V.I.S. builds the URL as `{base_url}/audio/speech`; when base_url already
# ends in /v1 this resolves to /v1/audio/speech above. Tolerate the un-versioned
# path too in case base_url is set without /v1.
@app.post("/audio/speech")
def speech_unversioned(req: SpeechRequest):
    return speech(req)
