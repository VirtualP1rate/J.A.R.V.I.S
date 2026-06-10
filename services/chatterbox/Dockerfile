# Self-owned OpenAI-compatible Chatterbox TTS server.
# Base image already ships torch 2.6.0 built for CUDA 12.4 (works on the RTX
# 3090 / Ampere), matching chatterbox-tts==0.1.7's torch==2.6.0 pin so pip does
# not pull a mismatched or CPU-only wheel.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

# ffmpeg + libsndfile for audio decode/encode (reference clips, soundfile WAV out).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install torchaudio matching the base image's torch from the cu124 index first,
# so chatterbox-tts's torchaudio==2.6.0 pin is satisfied without disturbing torch.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu124 torchaudio==2.6.0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Weights download to the HF cache on first run (mount a volume to persist).
ENV HF_HOME=/root/.cache/huggingface \
    CHATTERBOX_MODEL=standard \
    VOICES_DIR=/voices \
    DEFAULT_VOICE=jarvis \
    DEVICE=auto \
    EXAGGERATION=0.5 \
    CFG_WEIGHT=0.5

EXPOSE 8004

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8004"]
