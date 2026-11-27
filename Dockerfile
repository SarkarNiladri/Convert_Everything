FROM python:3.11-slim

# System converters: ffmpeg (audio/video), LibreOffice (office docs), pandoc (Markdown/HTML/EPUB), fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    pandoc \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-core \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 1000 appuser \
    && mkdir -p converter_uploads \
    && chown -R appuser:appuser /app
USER appuser

ENV PORT=8080
EXPOSE 8080

# Video conversion can take minutes, so the request timeout is long.
CMD ["sh", "-c", "gunicorn -w 1 --threads 4 -b 0.0.0.0:${PORT} --timeout 900 app:app"]
