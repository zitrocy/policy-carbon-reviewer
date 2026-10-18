FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY samples ./samples

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /srv/app
USER appuser

# Cloud Run injects PORT (default 8080). GEMINI_API_KEY and GEMINI_MODEL are
# runtime env vars; never bake keys into the image.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
