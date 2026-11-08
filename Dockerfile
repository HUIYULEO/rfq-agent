FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements-docker.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-docker.txt \
    && groupadd --gid 10001 rfq \
    && useradd --uid 10001 --gid rfq --no-create-home rfq \
    && mkdir -p /app/data /app/runtime \
    && chown rfq:rfq /app/runtime

# Keep the module at /app/rfq_agent: existing ROOT-relative paths then resolve
# to /app/data and /app/runtime, including when invoking the CLI.
COPY rfq_agent/ ./rfq_agent/
USER 10001:10001
EXPOSE 8765

# Liveness only: missing business data or model credentials do not kill the UI.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=3).close()"]

CMD ["python", "-m", "rfq_agent.web", "--host", "0.0.0.0", "--port", "8765"]
