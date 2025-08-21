FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ---- system packages for audio/voice ----
# - ffmpeg: stream/convert audio
# - libopus0: opus encoder/decoder used by discord.py voice
RUN apt-get update && apt-get install -y --no-install-recommends \
ffmpeg \
libopus0 \
&& rm -rf /var/lib/apt/lists/*

# ---- python deps ----
COPY requirements.txt ./
RUN pip install --no-cache-dir -U pip setuptools wheel \
&& pip install --no-cache-dir -r requirements.txt

# ---- app code ----
COPY . .

CMD ["python", "-u", "main.py"]