FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# One process, several threads: the in-process dialer thread is shared.
# (Extra workers are still safe — calls are claimed with SKIP LOCKED.)
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT:-8000} wsgi:app"]
