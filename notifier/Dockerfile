FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install httpx google-cloud-storage

COPY notifier/main.py ./main.py

CMD ["python", "main.py"]
