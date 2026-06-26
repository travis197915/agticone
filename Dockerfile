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
COPY uhc-execution-engine /app/uhc-execution-engine
COPY uhc-llm /app/uhc-llm

RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
