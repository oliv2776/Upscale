# Seul le client Docker est nécessaire (binaire statique) : pas besoin du daemon complet.
FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# mesa-va-drivers : encodage matériel VAAPI sur GPU AMD ; tzdata : plage horaire à l'heure locale.
# mesa-vulkan-drivers / libvulkan1 / libgomp1 : moteur des modèles personnalisés (Vulkan).
# « apt-get upgrade » : une reconstruction applique les correctifs de sécurité Debian même si l'image de
# base n'a pas encore été republiée. APT_REFRESH : changer sa valeur force à refaire cette couche.
# intel-media-va-driver : encodage matériel sur les cartes Intel (Arc, iGPU) ; mesa couvre AMD.
ARG APT_REFRESH=0
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates curl tini tzdata libvulkan1 mesa-vulkan-drivers mesa-va-drivers intel-media-va-driver \
      libgpg-error0 libgomp1 unzip util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
# Moteur des modèles personnalisés (Vulkan, compatible AMD).
# upscayl-bin : fork maintenu de realesrgan-ncnn-vulkan, avec un ncnn récent (couches Swish, GELU... des
# architectures modernes comme SPAN).
ARG NCNN_VERSION=20251207-174704
ARG NCNN_URL=https://github.com/upscayl/upscayl-ncnn/releases/download/${NCNN_VERSION}/upscayl-bin-${NCNN_VERSION}-linux.zip
ARG NCNN_SHA256=a9fab3c770b62f2b7a35d8d6d61eb4e8b3aef79128b665c919d080b85a2292f2
RUN curl -fsSL -o /tmp/ncnn.zip "${NCNN_URL}" \
    && echo "${NCNN_SHA256}  /tmp/ncnn.zip" | sha256sum -c - \
    && mkdir -p /opt/upscayl && unzip -q /tmp/ncnn.zip -d /tmp/ncnn && rm /tmp/ncnn.zip \
    && bin="$(find /tmp/ncnn -type f -name upscayl-bin | head -n 1)" && test -n "${bin}" \
    && mv "${bin}" /opt/upscayl/upscayl-bin && chmod +x /opt/upscayl/upscayl-bin && rm -rf /tmp/ncnn

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY VERSION app.py ./
COPY upscale ./upscale
COPY static ./static
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/upscale/
RUN mkdir -p /app/state
EXPOSE 27000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:27000/api/ping', timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
