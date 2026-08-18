FROM python:3.11-slim

# Unbuffered stdout means container logs (our JSON log lines) show up as
# they're written instead of being held back until the process exits.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies are installed before the app code is copied in, so
# rebuilding after an app-only change reuses Docker's cached layer
# instead of reinstalling every dependency from scratch.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The API and the worker are the same image with different commands (see
# docker-compose.yml) — no reason for either process to run as root.
RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
