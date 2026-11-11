# syntax=docker/dockerfile:1
#
# imageBio: the web interface built by Node, served by the Python package.
# Build from the root of the repository:
#
#     docker build -f deploy/Dockerfile -t imagebio:0.1.0 .
#
# NOT BUILT in the session that wrote it (no Docker daemon there): what it does was checked
# step by step outside Docker (see docs/exploitation.md).

FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# vite.config.ts writes the build into ../backend/src/imagebio/static
RUN npm run build

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY backend/pyproject.toml backend/README.md ./
COPY backend/src ./src
COPY --from=frontend /src/backend/src/imagebio/static ./src/imagebio/static
RUN pip install . && cd / && rm -rf /build

# Not root. The account that really runs the server is chosen at deployment (compose: "user:"),
# so that it can read the data: the directories below are for it to mount over.
RUN useradd --system --uid 10001 --user-group --no-create-home imagebio \
    && install -d -o imagebio -g imagebio -m 0750 /var/lib/imagebio /backups
USER imagebio
WORKDIR /var/lib/imagebio
EXPOSE 8000
# /healthz is open and says nothing but "ok" and the version
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
ENTRYPOINT ["imagebio"]
CMD ["serve", "--config", "/etc/imagebio/imagebio.toml"]
