# Seul le client Docker est nécessaire (binaire statique) : pas besoin du daemon complet.
FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# mesa-va-drivers : encodage matériel VAAPI sur GPU AMD ; tzdata : plage horaire à l'heure locale.
# mesa-vulkan-drivers / libvulkan1 / libgomp1 : moteur des modèles personnalisés (Vulkan).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates tini tzdata mesa-va-drivers mesa-vulkan-drivers libvulkan1 libgomp1 curl unzip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
# Moteur des modèles personnalisés (Vulkan, compatible AMD).
# upscayl-bin : fork maintenu de realesrgan-ncnn-vulkan, avec un ncnn récent (couches Swish, GELU... des
# architectures modernes comme SPAN).
ARG NCNN_VERSION=20251207-174704
ARG NCNN_URL=https://github.com/upscayl/upscayl-ncnn/releases/download/${NCNN_VERSION}/upscayl-bin-${NCNN_VERSION}-linux.zip
RUN curl -fsSL -o /tmp/ncnn.zip "${NCNN_URL}" \
    && mkdir -p /opt/upscayl && unzip -q /tmp/ncnn.zip -d /tmp/ncnn && rm /tmp/ncnn.zip \
    && bin="$(find /tmp/ncnn -type f -name upscayl-bin | head -n 1)" && test -n "${bin}" \
    && mv "${bin}" /opt/upscayl/upscayl-bin && chmod +x /opt/upscayl/upscayl-bin && rm -rf /tmp/ncnn

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
