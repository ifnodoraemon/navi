# syntax=docker/dockerfile:1
FROM python:3.13-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Install with --prefix to isolate the installation
RUN pip install --no-cache-dir --prefix=/install .

# Production stage
FROM python:3.13-slim

# Create a non-root user
RUN useradd -m -s /bin/bash navi
USER navi
WORKDIR /home/navi

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Expose standard port for the API/connector webhooks
EXPOSE 8000

# Basic healthcheck via the CLI
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["navi", "doctor"] || exit 1

# Default entrypoint
ENTRYPOINT ["navi"]
