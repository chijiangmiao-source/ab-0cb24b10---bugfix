FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    CRASH_MODE=soft

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app
COPY web /app/web
COPY scripts /app/scripts
COPY tests /app/tests

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# Default service is the web/API host; the compose "verify" one-shot service
# overrides the command to run scripts/verify.py and exits with its code.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
