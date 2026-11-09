FROM python:3.12-slim
WORKDIR /app
# ffmpeg — сборка коротких видео, DejaVu — кириллица на слайдах
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY leadhunter ./leadhunter
RUN pip install --no-cache-dir . \
    && patchright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
VOLUME ["/app/data"]
CMD ["python", "-m", "leadhunter"]
