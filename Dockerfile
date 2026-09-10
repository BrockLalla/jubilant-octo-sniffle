# Cloud deployment image (Render). Not used by the local macOS app build --
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

# Shell form (not exec-array form) so $PORT actually gets substituted --
# Render assigns this dynamically at runtime, so it can't be baked in as
# a fixed value the way render.yaml's other env vars are. Falls back to
# 8080 for a plain `docker run` with nothing set.
#
# Single worker process (SQLite on the mounted disk is single-writer --
# see render.yaml/README for why this app deliberately never scales past
# one instance), multiple threads for real concurrency within that
# process.
CMD gunicorn -w 1 --threads 4 -b 0.0.0.0:${PORT:-8080} "app:create_app()"
