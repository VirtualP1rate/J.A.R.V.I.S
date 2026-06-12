# routes/tts_routes.py
"""
TTS API routes — multi-provider (local Kokoro, API endpoint, browser).
"""

import asyncio
import itertools

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
import logging
import os

logger = logging.getLogger(__name__)

class TTSRequest(BaseModel):
    text: str
    format: str = "audio"  # "audio", "base64", or "stream" (progressive bytes)

def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    @router.get("/stats")
    async def get_tts_stats():
        """Get TTS service statistics"""
        try:
            return tts_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get TTS stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/synthesize")
    async def synthesize_speech(request: TTSRequest):
        """Synthesize speech from text"""
        try:
            if not tts_service.available:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "TTS service not available"}
                )
            
            # Synthesis blocks on remote HTTP or local GPU inference for
            # seconds per call (one call per sentence in conversation mode) —
            # keep it off the event loop so concurrent SSE streams keep flowing.
            if request.format == "stream":
                # Progressive audio: bytes go out as the TTS engine produces
                # them (Chatterbox yields PCM per text chunk), so playback can
                # start before the full text is synthesized. The generator is
                # sync — Starlette iterates it in a worker thread, keeping the
                # event loop free. Pull the first piece eagerly so an outright
                # failure is still a clean 500 rather than an empty 200.
                gen = tts_service.synthesize_stream(request.text)
                first = await asyncio.to_thread(next, gen, None)
                if first is None:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                if first[:4] == b"RIFF":
                    mime = "audio/wav"
                elif first[:3] == b'ID3' or (len(first) >= 2 and first[0] == 0xff and (first[1] & 0xe0) == 0xe0):
                    mime = "audio/mpeg"
                else:
                    mime = "application/octet-stream"
                return StreamingResponse(itertools.chain([first], gen), media_type=mime)

            if request.format == "base64":
                audio_b64 = await asyncio.to_thread(tts_service.synthesize_to_base64, request.text)
                if not audio_b64:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                return {"audio": audio_b64}
            
            else:  # audio format
                audio_data = await asyncio.to_thread(tts_service.synthesize, request.text)
                if not audio_data:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                
                # Detect format from magic bytes (MP3: ID3 tag or sync word ff e0+)
                is_mp3 = audio_data[:3] == b'ID3' or (len(audio_data) >= 2 and audio_data[0] == 0xff and (audio_data[1] & 0xe0) == 0xe0)
                mime = "audio/mpeg" if is_mp3 else "audio/wav"
                return Response(
                    content=audio_data,
                    media_type=mime,
                    headers={
                        "Content-Disposition": "inline; filename=speech.mp3" if "mpeg" in mime else "inline; filename=speech.wav"
                    }
                )
        
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Synthesis error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": f"Synthesis failed: {str(e)}"}
            )

    @router.post("/clear-cache")
    async def clear_tts_cache():
        """Clear TTS cache"""
        try:
            tts_service.clear_cache()
            return {"success": True, "message": "Cache cleared"}
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Voice library (Chatterbox cloned voices) ──

    @router.get("/voices")
    async def list_tts_voices():
        """List cloned voices available on the active TTS endpoint."""
        try:
            return {"voices": tts_service.list_voices()}
        except Exception as e:
            logger.error(f"Failed to list voices: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/voices")
    async def upload_tts_voice(request: Request, name: str = Form(...), voice_file: UploadFile = File(...)):
        """Admin only: add a reference clip for voice cloning."""
        from core.middleware import require_admin
        from src.upload_limits import read_upload_limited, get_chat_upload_max_bytes
        require_admin(request)
        ext = os.path.splitext(voice_file.filename or "")[1]
        data = await read_upload_limited(voice_file, get_chat_upload_max_bytes(), label="Voice clip")
        try:
            stem = tts_service.save_voice(name, data, ext)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"success": True, "voice": stem}

    @router.delete("/voices/{name}")
    async def delete_tts_voice(request: Request, name: str):
        """Admin only: remove a cloned voice reference clip."""
        from core.middleware import require_admin
        require_admin(request)
        removed = tts_service.delete_voice(name)
        if not removed:
            raise HTTPException(status_code=404, detail="voice not found")
        return {"success": True}

    return router
