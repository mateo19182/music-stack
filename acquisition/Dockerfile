FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS node
FROM python:3.11-slim-bookworm@sha256:2333bd330d12de02514770b3585cad313644316047cdee24a7acfdece6de6efb
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BEETSDIR=/state/beets/config XDG_CONFIG_HOME=/state/xdg XDG_CACHE_HOME=/state/cache
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app ./app
USER 1000:1000
EXPOSE 4534
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4534", "--no-access-log"]
