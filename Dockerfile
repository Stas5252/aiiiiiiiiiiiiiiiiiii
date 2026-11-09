FROM python:3.12-slim
WORKDIR /app
# ffmpeg — видео и мультики, DejaVu — кириллица на слайдах, cloudflared — публичный HTTPS для Mini App без домена
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core curl ca-certificates \
    && curl -fsSL -o /usr/local/bin/cloudflared \
       https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture) \
    && chmod +x /usr/local/bin/cloudflared \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY leadhunter ./leadhunter
RUN pip install --no-cache-dir . \
    && patchright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
VOLUME ["/app/data"]
CMD ["python", "-m", "leadhunter"]
