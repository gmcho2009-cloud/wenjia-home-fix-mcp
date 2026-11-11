# Minimal image for directory build checks (Glama / Smithery style).
# The server is pure standard library: no pip install, no build step.
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# server.py resolves its data file from either ./mcp_data.json or ./data/mcp_data.json.
COPY server.py ./
COPY data/mcp_data.json ./data/mcp_data.json

# 0.0.0.0 so a container health check can reach it; port matches the hosted service.
ENV HOST=0.0.0.0 \
    PORT=8765 \
    WENJIA_CALL_LOG=/tmp/calls.jsonl

EXPOSE 8765

# /health answers immediately: {"ok":true,"version":"...","categories":40,"symptoms":224}
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2).status==200 else 1)"

CMD ["python3", "server.py"]
