# Cloud deployment image (Fly.io). Not used by the local macOS app build --
# see pantry.spec/mac_launcher.py for that, still the primary path for
# anyone running a fully local copy.
FROM python:3.12-slim

WORKDIR /app

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY app.py db.py emailer.py netinfo.py ./
COPY routes/ routes/
COPY templates/ templates/
COPY static/ static/

# Single worker process (SQLite on the mounted volume is single-writer --
# see fly.toml/README for why this app deliberately never scales past one
# machine), multiple threads for real concurrency within that process.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8080", "app:create_app()"]
