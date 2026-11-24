FROM rclone/rclone:1.76.0 AS rclone

FROM python:3.13.18-slim-trixie
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[ai]"

ENV PYTHONUNBUFFERED=1 \
    HOME=/data \
    SCANFILE_DATA_DIR=/data \
    RCLONE_CONFIG=/config/rclone.conf
VOLUME ["/data"]
ENTRYPOINT ["scanfile-watch"]
