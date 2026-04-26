# Microsoft's official Playwright image: Python 3.11 + Chromium + system deps
# preinstalled. Pin the tag to match the playwright version in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

# The base image already has chromium; no `playwright install` needed.

EXPOSE 8080

CMD ["sh", "-c", "uvicorn airhost_mcp.server:app --host 0.0.0.0 --port ${PORT}"]
