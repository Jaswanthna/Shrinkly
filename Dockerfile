FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHRINKLY_DATA_DIR=/data \
    SHRINKLY_REDIS_URL=redis://redis:6379/0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        libjpeg62-turbo \
        libopenjp2-7 \
        libfreetype6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 shrinkly \
    && useradd --system --uid 10001 --gid shrinkly --home-dir /app shrinkly

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY frontend ./frontend
RUN mkdir -p /data && chown shrinkly:shrinkly /data

USER shrinkly
EXPOSE 8000
ENTRYPOINT ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0"]
CMD ["--port", "8000"]
