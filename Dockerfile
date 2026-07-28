# ---------------------------------------------------------------------------
# Stage 1: fetch official Radiance binaries (oconv/rpict power /preview)
# ---------------------------------------------------------------------------
# linux/amd64 throughout: official Radiance Linux builds are x86_64-only,
# and Fly.io machines are amd64. (Local builds on Apple Silicon run via Rosetta.)
FROM --platform=linux/amd64 debian:bookworm-slim AS radiance

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Official LBNL release; the zip wraps a Linux tarball whose layout has
# shifted between releases, so locate the install tree via rpict.
ARG RADIANCE_URL=https://github.com/LBNL-ETA/Radiance/releases/download/rad6R0P2/Radiance_c1700d56_Linux.zip
RUN set -eux; \
    curl -fsSL -o /tmp/radiance.zip "$RADIANCE_URL"; \
    unzip -q /tmp/radiance.zip -d /tmp/rad; \
    find /tmp/rad -name '*.tar.gz' -exec tar -xzf {} -C /tmp/rad \; ; \
    raddir="$(dirname "$(dirname "$(find /tmp/rad -type f -name rpict | head -n 1)")")"; \
    mv "$raddir" /opt/radiance; \
    RAYPATH=/opt/radiance/lib /opt/radiance/bin/rpict -version

# ---------------------------------------------------------------------------
# Stage 2: application
# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 python:3.12-slim

COPY --from=radiance /opt/radiance/bin /opt/radiance/bin
COPY --from=radiance /opt/radiance/lib /opt/radiance/lib
ENV PATH="/opt/radiance/bin:${PATH}" \
    RAYPATH=".:/opt/radiance/lib" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[web]"

RUN useradd --create-home app
USER app

EXPOSE 8080

# --proxy-headers/--forwarded-allow-ips: trust X-Forwarded-For from the
# platform proxy so the per-IP rate limiter sees real client IPs, not the
# proxy's. Safe because only the Fly proxy can reach this port.
CMD ["uvicorn", "pbr2rad.web.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
