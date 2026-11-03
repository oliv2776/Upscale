# Seul le client Docker est nécessaire (binaire statique) : pas besoin du daemon complet.
FROM docker:29-cli AS docker-cli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# mesa-va-drivers : encodage matériel VAAPI sur GPU AMD ; tzdata : plage horaire à l'heure locale.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates tini tzdata mesa-va-drivers \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY VERSION app.py ./
COPY upscale ./upscale
COPY static ./static
RUN mkdir -p /app/state
EXPOSE 27000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:27000/api/ping', timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
