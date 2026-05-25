FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUDIO_BACKEND=pulseaudio \
    CHROME_BINARY_PATH=/usr/bin/google-chrome \
    MEETINGS_OUTPUT_DIR=/app/meetings \
    SEEN_MEETINGS_PATH=/app/state/seen_meetings.json \
    BOT_SESSION_PATH=/app/state/bot_session.pkl \
    XDG_RUNTIME_DIR=/tmp/runtime-bot

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        dbus \
        ffmpeg \
        fonts-liberation \
        gnupg \
        libasound2 \
        libatk-bridge2.0-0 \
        libgbm1 \
        libgtk-3-0 \
        libnss3 \
        libportaudio2 \
        libxss1 \
        procps \
        pulseaudio \
        pulseaudio-utils \
        xvfb \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --shell /bin/bash bot \
    && mkdir -p /app/meetings /app/state /app/secrets \
    && chown -R bot:bot /app /tmp

COPY --chown=bot:bot . .
RUN chmod +x /app/docker-entrypoint.sh

USER bot

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "main.py"]
