# Convert Anything

Pick a converter (Image, Audio, Video, Document, Spreadsheet, Slides or PDF), drop files into its own upload area, choose a format, download.
Built on the same stack as the PDF suite: Flask + gunicorn, a single vanilla HTML page, Docker on Fly.io.

## What it converts (v1)

| Type | In | Out | Engine |
|---|---|---|---|
| Images | PNG, JPG, WebP, AVIF, GIF, BMP, TIFF, ICO, HEIC/HEIF | PNG, JPG, WebP, AVIF, GIF, BMP, TIFF, ICO, PDF | Pillow, pillow-heif |
| Audio | MP3, WAV, OGG, FLAC, M4A, AAC, Opus, WMA, AIFF, AMR | MP3, WAV, OGG, FLAC, M4A, AAC, Opus | ffmpeg |
| Video | MP4, MOV, MKV, WebM, AVI, M4V, FLV, WMV, MPEG, 3GP, TS | MP4, WebM, MOV, MKV, AVI, GIF, plus MP3/WAV/M4A (audio only) | ffmpeg |
| Text documents | DOCX, DOC, ODT, RTF, TXT, Markdown, HTML | PDF, DOCX, ODT, RTF, TXT, Markdown, HTML, EPUB | LibreOffice, pandoc |
| Spreadsheets | XLSX, XLS, ODS, CSV | PDF, XLSX, ODS, CSV, HTML | LibreOffice |
| Presentations | PPTX, PPT, ODP | PDF, PPTX, ODP, PNG/JPG (one per slide) | LibreOffice, PyMuPDF |
| PDF | PDF | DOCX, TXT, PNG/JPG (one per page) | pdf2docx, PyMuPDF |

Extras: batch up to 20 files (results come back as a zip), combine several images into one PDF,
image quality and resize, video resolution cap, audio bitrate. Animated GIF → WebP/GIF keeps the animation.
Transparent images converted to JPG/PDF get a white background.

## Run locally

Install the system tools first: `ffmpeg`, `libreoffice`, `pandoc`.

```bash
# Ubuntu / Debian
sudo apt install ffmpeg libreoffice pandoc
# macOS (Homebrew)
brew install ffmpeg pandoc && brew install --cask libreoffice
```

Then install the Python packages and start the app (Python 3.10+):

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py            # http://localhost:5000
```

Or with Docker:

```bash
docker build -t convert-anything .
docker run -p 8080:8080 convert-anything   # http://localhost:8080
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Builds small fixtures (image, audio, video, docx, xlsx, pptx, pdf, md, html, csv, txt) and runs
every input through every target it offers, checking the output's file signature.

## API

- `GET /api/formats` – supported input extensions, their targets, and which tools are installed.
- `POST /api/convert` – multipart form: `files` (one or more), `target` (e.g. `mp3`), optional
  `quality`, `max_size`, `max_height`, `audio_bitrate`, `combine_pdf`. Returns the converted file,
  or a zip when there is more than one output.

## Deploy (Fly.io)

Same as the PDF suite: `fly launch --no-deploy` to create the app, then replace the placeholder
`app = 'convert-anything'` in `fly.toml` with your app's name,
add `FLY_API_TOKEN` as a GitHub secret, and pushes to `main` deploy through `.github/workflows/fly.yml`.
