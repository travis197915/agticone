# Re-use a single Python version across all FROM statements.
ARG PYTHON_VERSION=3.13

# Internal UHG/Optum edge node (no public Docker Hub access).
# EPL workflows can override this via --build-arg JF_EDGE_NODE=...
ARG JF_EDGE_NODE=edgeinternal1uhg.optum.com

# Well-known non-root UID/GID shipped in the Optum golden (Chainguard) image.
ARG CHAINGUARD_USER_AND_GROUP=65532

# Declare both base images up front for readability and re-use of the aliases.
# Both stages use the -dev golden image because the runtime must keep a shell so
# docker-entrypoint.sh can launch Django + Celery together.
#  - final   : runtime image (keeps a shell, runs the entrypoint script)
#  - builder : same -dev image, used to build the virtual environment
FROM ${JF_EDGE_NODE}/glb-docker-uhg-loc/uhg-goldenimages/python:${PYTHON_VERSION}-latest-dev AS final
FROM ${JF_EDGE_NODE}/glb-docker-uhg-loc/uhg-goldenimages/python:${PYTHON_VERSION}-latest-dev AS builder

USER root
WORKDIR /app

# Copy requirements first for better layer caching.
COPY requirements.txt ./

# Enterprise Registry (JFrog Artifactory) authentication via BuildKit secrets.
# The --mount=type=secret values exist ONLY during this RUN and never land in a
# layer, so the credentials are not baked into the image.
#   docker build --platform linux/amd64 --load \
#       --secret id=jf-user,env=DOCKER_ER_USER \
#       --secret id=jf-token,env=DOCKER_ER_TOKEN \
#       -t agentic-backend:dev .
# psycopg2-binary ships its own libpq, so no apt/build packages are required.
RUN --mount=type=secret,id=jf-user,env=DOCKER_ER_USER \
    --mount=type=secret,id=jf-token,env=DOCKER_ER_TOKEN <<ER_AUTHENTICATION_BLOCK
set -eu
export PIP_INDEX_URL="https://${DOCKER_ER_USER}:${DOCKER_ER_TOKEN}@edgeinternal1uhg.optum.com/artifactory/api/pypi/epl-pypi-vir/simple"
python -m venv /opt/venv
/opt/venv/bin/pip install --no-cache-dir --upgrade pip
# Strip the "-e ./uhc-*" editable local packages: their source is not present
# yet at this layer. They are installed from source in the dedicated step below
# (after their directories are COPYed) so that this slow install layer caches.
grep -vE '^[[:space:]]*-e[[:space:]]' requirements.txt > /tmp/requirements.core.txt
/opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.core.txt
rm -rf /root/.cache/pip
find /opt/venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find /opt/venv -type f -name "*.pyc" -delete
ER_AUTHENTICATION_BLOCK

# Install the local src-layout packages into the venv. Copying the source alone
# is not enough — they must be pip-installed to be importable as
# uhc_sop_ingestion / uhc_api_agent / uhc_execution_engine. Done in a separate
# step so the (slow) requirements install layer above stays cached. --no-deps
# because every dependency is already pinned in requirements.txt.
COPY uhc-sop-ingestion ./uhc-sop-ingestion
COPY uhc-api-agent ./uhc-api-agent
COPY uhc-execution-engine ./uhc-execution-engine
COPY uhc-llm ./uhc-llm
RUN --mount=type=secret,id=jf-user,env=DOCKER_ER_USER \
    --mount=type=secret,id=jf-token,env=DOCKER_ER_TOKEN <<LOCAL_PKG_BLOCK
set -eu
export PIP_INDEX_URL="https://${DOCKER_ER_USER}:${DOCKER_ER_TOKEN}@edgeinternal1uhg.optum.com/artifactory/api/pypi/epl-pypi-vir/simple"
/opt/venv/bin/pip install --no-cache-dir --no-deps ./uhc-sop-ingestion ./uhc-api-agent ./uhc-execution-engine ./uhc-llm
rm -rf /root/.cache/pip
LOCAL_PKG_BLOCK

# Copy the full application source (Django project + local uhc-* packages).
COPY . .


# ---------------------------------------------------------------------------
# RabbitMQ stage — pulled only so the final image can copy the broker binaries.
# Declared here (after the builder's RUN/COPY steps) so it does NOT become the
# active build stage for the commands above.
# ---------------------------------------------------------------------------
FROM ${JF_EDGE_NODE}/glb-docker-uhg-loc/uhg-goldenimages/rabbitmq:4.3-latest-dev AS rabbitmq


# ---------------------------------------------------------------------------
# Final runtime stage (keeps a shell so the entrypoint script can run)
# ---------------------------------------------------------------------------
FROM final

ARG CHAINGUARD_USER_AND_GROUP

# UTF-8 locale, no .pyc files (immutable image), unbuffered logs.
# PATH also exposes the Erlang runtime + RabbitMQ launcher scripts so the
# entrypoint can start the embedded broker.
ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:/usr/lib/erlang/bin:/usr/sbin:$PATH" \
    PYTHONPATH="/app" \
    APP_PORT=8000

WORKDIR /app

# Bring over the virtual environment and the application source, owned by the
# non-root golden-image user.
COPY --from=builder --chown=${CHAINGUARD_USER_AND_GROUP}:${CHAINGUARD_USER_AND_GROUP} /opt/venv /opt/venv
COPY --from=builder --chown=${CHAINGUARD_USER_AND_GROUP}:${CHAINGUARD_USER_AND_GROUP} /app /app

# ── Embed the RabbitMQ broker so one container runs RabbitMQ + Django + Celery.
# Erlang is copied first (rabbitmq-server depends on it), then the RabbitMQ
# release and its launcher symlinks in /usr/sbin. These paths follow the
# Wolfi/Chainguard apk layout used by the Optum golden image. If the build
# fails to find rabbitmq-server/erl, verify the real paths with:
#   docker run --rm <rabbitmq-golden> sh -lc \
#     'command -v rabbitmq-server erl epmd; ls -d /usr/lib/erlang /usr/lib/rabbitmq'
COPY --from=rabbitmq /usr/lib/erlang   /usr/lib/erlang
COPY --from=rabbitmq /usr/lib/rabbitmq /usr/lib/rabbitmq
COPY --from=rabbitmq /usr/sbin/rabbitmq-server  /usr/sbin/rabbitmq-server
COPY --from=rabbitmq /usr/sbin/rabbitmqctl      /usr/sbin/rabbitmqctl
COPY --from=rabbitmq /usr/sbin/rabbitmq-plugins /usr/sbin/rabbitmq-plugins

# Ensure the entrypoint script is executable.
RUN chmod +x /app/docker-entrypoint.sh

# 8000 = Django HTTP (published). 5672 = RabbitMQ AMQP (in-container/loopback
# only — Celery reaches it via localhost, so it need not be published).
EXPOSE 8000

# Run as the non-root golden-image user.
USER ${CHAINGUARD_USER_AND_GROUP}

# Start Django + Celery together via the project entrypoint script. Invoked
# through `sh` so it works regardless of the script's executable bit. The script
# honours APP_PORT, CELERY_CONCURRENCY and CELERY_QUEUES env vars.
ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
