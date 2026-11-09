FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY leadhunter ./leadhunter
RUN pip install --no-cache-dir .
VOLUME ["/app/data"]
CMD ["python", "-m", "leadhunter"]
