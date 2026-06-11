"""Minimal OpenAI-compatible Speech-to-Text server wrapping faster-whisper.

faster-whisper (CTranslate2 runtime, MIT) is a library, not a server. This thin
FastAPI wrapper exposes the one endpoint J.A.R.V.I.S. needs,
`POST /v1/audio/transcriptions`, in the OpenAI shape so the existing STT
"endpoint:<id>" provider talks to it unchanged (see services/stt/stt_service.py
-> _transcribe_api, which POSTs multipart {file, model, language} to
`{base_url}/audio/transcriptions` and reads back `{"text": ...}`).

We own all of this glue; the only external pieces are the faster-whisper library
and the public CTranslate2 Whisper weights it pulls from HuggingFace.

The model is loaded once at startup (warm) so the first turn isn't slow, and
inference is serialized behind a lock — the model is shared, GPU memory is
finite, and conversation-mode turns arrive one at a time anyway.

Env:
  DEVICE           cuda | cpu | auto (default auto -> cuda if available)
  WHISPER_MODEL    model size or HF id (default "large-v3-turbo")
  COMPUTE_TYPE     ct2 compute type (default auto: float16 on cuda, int8 on cpu)
  BEAM_SIZE        decoding beam (default 5; 1 = greedy, lower latency)
  VAD_FILTER       "1"/"0" — Silero VAD to drop non-speech (default 1)
  DEFAULT_LANGUAGE force a language code, else auto-detect (default "")
  MAX_AUDIO_BYTES  reject larger uploads (default 26214400 = 25 MiB)
"""
import io
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whisper-server")

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")
BEAM_SIZE = int(os.getenv("BEAM_SIZE", "5"))
VAD_FILTER = os.getenv("VAD_FILTER", "1") not in ("0", "false", "False", "")
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "").strip()
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(25 * 1024 * 1024)))

_STATE = {"model": None, "ready": False, "device": None, "compute_type": None}
# CTranslate2 transcribe is not guaranteed safe under concurrent calls on one
# model instance; serialize. Turns arrive one at a time in conversation mode.
_MODEL_LOCK = threading.Lock()


def _resolve_device() -> str:
    dev = os.getenv("DEVICE", "auto").lower()
    if dev != "auto":
        return dev
    # faster-whisper runs on CTranslate2, not torch. torch is only used here to
    # probe for a usable CUDA device; if it's missing/broken we fall back to CPU
    # instead of failing to boot.
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _resolve_compute_type(device: str) -> str:
    ct = os.getenv("COMPUTE_TYPE", "").strip()
    if ct:
        return ct
    return "float16" if device == "cuda" else "int8"


def _load_model():
    from faster_whisper import WhisperModel
    device = _resolve_device()
    compute_type = _resolve_compute_type(device)
    logger.info("Loading faster-whisper '%s' on device=%s compute_type=%s ...",
                WHISPER_MODEL, device, compute_type)
    try:
        model = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute_type)
    except Exception as e:
        # A GPU compute_type the card/driver rejects, OOM, etc. Fall back to CPU
        # int8 so STT still works rather than crash-looping the container.
        logger.error("GPU load failed (%s); falling back to CPU int8.", e)
        device, compute_type = "cpu", "int8"
        model = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute_type)
    _STATE.update(model=model, device=device, compute_type=compute_type)
    logger.info("faster-whisper ready (device=%s, compute_type=%s).", device, compute_type)
    return model


def _transcribe(audio_bytes: bytes, language: str) -> str:
    lang = (language or DEFAULT_LANGUAGE or "").strip() or None
    with _MODEL_LOCK:
        model = _STATE["model"]
        if model is None:
            raise HTTPException(status_code=503, detail="model not loaded")
        # faster-whisper decodes the container (webm/opus, wav, mp3, ...) via PyAV,
        # so we can hand it the raw bytes directly — no temp file needed.
        segments, info = model.transcribe(
            io.BytesIO(audio_bytes),
            language=lang,
            beam_size=BEAM_SIZE,
            vad_filter=VAD_FILTER,
            condition_on_previous_text=False,  # short independent turns; avoids loops
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
    logger.info("STT: %d chars, lang=%s (p=%.2f)", len(text),
                getattr(info, "language", "?"), getattr(info, "language_probability", 0.0))
    return text


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the model at boot so the first conversation turn isn't slow.
    _load_model()
    _STATE["ready"] = True
    yield


app = FastAPI(title="Whisper STT", lifespan=lifespan)


@app.get("/health")
async def health():
    if not _STATE["ready"]:
        return JSONResponse({"status": "loading"}, status_code=503)
    return {"status": "healthy", "model": WHISPER_MODEL,
            "device": _STATE["device"], "compute_type": _STATE["compute_type"]}


# OpenAI-compatible transcription. FastAPI runs this sync handler in a worker
# thread, so the blocking CTranslate2 inference never stalls the event loop.
@app.post("/v1/audio/transcriptions")
def transcriptions(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),     # accepted, ignored (server picks the model)
    language: Optional[str] = Form(None),
    response_format: Optional[str] = Form(None),  # accepted; we always return JSON
    prompt: Optional[str] = Form(None),    # accepted, ignored
    temperature: Optional[str] = Form(None),  # accepted, ignored
):
    audio = file.file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio upload")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")
    text = _transcribe(audio, language or "")
    return {"text": text}


# J.A.R.V.I.S. builds the URL as `{base_url}/audio/transcriptions`; when base_url
# ends in /v1 this resolves to /v1/audio/transcriptions above. Tolerate the
# un-versioned path too in case base_url is configured without /v1.
@app.post("/audio/transcriptions")
def transcriptions_unversioned(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
    response_format: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    temperature: Optional[str] = Form(None),
):
    return transcriptions(file, model, language, response_format, prompt, temperature)
