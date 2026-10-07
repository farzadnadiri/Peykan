FROM ubuntu:22.04

LABEL maintainer=""
LABEL description="Dockerfile for MCP Demo with vCAN and ECUs simulation"
LABEL version="1.0"
USER root
# Install required packages
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . /home/pi/peykan

# Set working directory
WORKDIR /home/pi/peykan

# Create and activate virtual environment
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt
RUN pip3 install --no-cache-dir -e .

# Ensure src layout is importable if running scripts directly
ENV PYTHONPATH="/home/pi/peykan/src:${PYTHONPATH}"

# Expose MCP and other relevant ports
EXPOSE 6278 80 443 5000 8080

# Create non-root user and run app as non-root (no privileged needed for virtual backend)
RUN useradd -ms /bin/bash appuser && chown -R appuser:appuser /home/pi
USER appuser

# Entrypoint: run MCP and simulation concurrently using virtual backend
ENV PEYKAN_CAN_INTERFACE=virtual
ENV PEYKAN_CAN_CHANNEL=bus0
# The server listens on loopback by default; inside a container it must
# accept connections from the host's port mapping.
ENV PEYKAN_MCP_HOST=0.0.0.0
CMD ["bash", "-c", "peykan server --port 6278 & peykan simulate && wait"]

# Healthcheck: verify server port is accepting connections
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD bash -lc 'python3 - <<PY\nimport socket,sys\ns=socket.socket()\n\ntry:\n s.connect(("127.0.0.1", 6278))\n s.close()\n sys.exit(0)\nexcept Exception:\n sys.exit(1)\nPY'