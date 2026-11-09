FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY leadhunter ./leadhunter
RUN pip install --no-cache-dir . \
    && patchright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
VOLUME ["/app/data"]
CMD ["python", "-m", "leadhunter"]
