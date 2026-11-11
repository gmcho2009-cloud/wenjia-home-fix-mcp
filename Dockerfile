# Minimal image for directory build checks (Glama / Smithery style).
# The server is pure standard library: no pip install, no build step.
FROM python:3.13-slim

WORKDIR /app

COPY server.py mcp_data.json ./

# 0.0.0.0 so a container health check can reach it; port matches the hosted service.
ENV HOST=0.0.0.0 \
    PORT=8765 \
    WENJIA_CALL_LOG=/tmp/calls.jsonl

EXPOSE 8765

# /health answers immediately: {"ok":true,"version":"...","categories":40,"symptoms":224}
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2).status==200 else 1)"

CMD ["python", "server.py"]
