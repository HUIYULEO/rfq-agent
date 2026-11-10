# RFQ agent — deterministic pricing engine + stdlib HTTP server.
#
#   docker build -t rfq-agent .
#   docker run -p 8000:8000 -e GENESIS_ACCESS_TOKEN=... rfq-agent
#
# Only the two document libraries are installed: parsing (eml/xlsx/pdf), pricing,
# SQLite storage and the HTTP server are standard library, so the image stays small
# and there is no framework or build step to break.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RFQ_HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

RUN pip install --no-cache-dir "openpyxl>=3.1,<4" "python-docx>=1.1,<2"

COPY . .

# Fail the build, not the demo, when the six official emails were left out.
RUN mkdir -p runtime && test -d "data/询价信息素材"

EXPOSE 8000

# Shell-free probe: 200 from /healthz means the process is up and can see its data.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "rfq_agent.web"]
