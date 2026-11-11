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
# sha256 of that zip as downloaded on 2026-11-11. The build fails if the
# release asset ever changes; update both lines together when upgrading.
ARG RADIANCE_SHA256=04ee53cafbb64b943a53616b3d0ee379dd7ef80379c83aa7a145e547d9809c28
RUN set -eux; \
    curl -fsSL -o /tmp/radiance.zip "$RADIANCE_URL"; \
    echo "$RADIANCE_SHA256  /tmp/radiance.zip" | sha256sum -c -; \
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
COPY pyproject.toml README.md requirements-web.lock ./
COPY src ./src
# Exact versions of every dependency (regenerate with:
#   uv pip compile pyproject.toml --extra web --python-version 3.12 \
#     --python-platform x86_64-unknown-linux-gnu -o requirements-web.lock
# ), so a rebuild installs what was tested, not whatever was released since.
RUN pip install --no-cache-dir -c requirements-web.lock ".[web]"

RUN useradd --create-home app
USER app

EXPOSE 8080

# --proxy-headers: lets the app see the original scheme. The rate limiter
# does NOT use the X-Forwarded-For address this also exposes (a caller can
# type anything there); it reads Fly-Client-IP / CF-Connecting-IP instead,
# see src/pbr2rad/web/netsec.py.
CMD ["uvicorn", "pbr2rad.web.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
