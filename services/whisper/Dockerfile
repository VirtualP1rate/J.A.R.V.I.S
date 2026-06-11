# Self-owned OpenAI-compatible faster-whisper STT server.
# Reuses the same CUDA 12.4 + cuDNN 9 base as the Chatterbox TTS service so the
# image layers are shared and GPU support is known-good on the RTX 3090. PyAV
# (a faster-whisper dep) needs the ffmpeg system libs for audio decode.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Weights download to the HF cache on first run (mount a volume to persist).
ENV HF_HOME=/root/.cache/huggingface \
    WHISPER_MODEL=large-v3-turbo \
    DEVICE=auto \
    BEAM_SIZE=5 \
    VAD_FILTER=1 \
    DEFAULT_LANGUAGE=en

EXPOSE 8005

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8005"]
