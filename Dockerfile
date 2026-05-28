FROM python:3.13-slim

WORKDIR /app

# Build/runtime deps for common Python packages in this repo (psycopg, etc.).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       curl \
       libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY uhc-sop-ingestion /app/uhc-sop-ingestion
COPY uhc-api-agent /app/uhc-api-agent

RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir -e /app/uhc-sop-ingestion \
    && pip install --no-cache-dir -e /app/uhc-api-agent

COPY . /app

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
