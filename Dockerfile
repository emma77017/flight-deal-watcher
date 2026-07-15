FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py flightsearch.py alerts.py store.py scheduler.py promos.py config.cloud.toml ./
# default config = the sanitized cloud one; secrets arrive via env (.env file).
# To customize routes on the Pi, bind-mount your own config.toml over this.
RUN cp config.cloud.toml config.toml

CMD ["python", "scheduler.py"]
