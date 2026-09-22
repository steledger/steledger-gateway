FROM python:3.12-slim
# Ownership annotation for the MCP registry (must equal server.json `name`).
# Must equal "name" in mcp_server/server.json: the registry refuses to publish a
# manifest whose package carries a different server name, as proof the image is
# ours. Change one and you must change the other.
LABEL io.modelcontextprotocol.server.name="com.steledger/gateway"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
CMD ["python", "server.py"]
