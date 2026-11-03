#!/usr/bin/env bash
set -Eeuo pipefail

# Uscale one-shot installer for Ubuntu/Debian NAS hosts.
if [[ "${EUID}" -ne 0 ]]; then
  echo "Lance ce script avec sudo : sudo bash install.sh"
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${USCALE_HOST_DATA:-/data}"

echo "==> Préparation des paquets système"
export DEBIAN_FRONTEND=noninteractive
apt-get update
PACKAGES=(git mesa-vulkan-drivers vulkan-tools ca-certificates)
if ! command -v docker >/dev/null 2>&1; then
  PACKAGES+=(docker.io)
fi
if ! docker compose version >/dev/null 2>&1; then
  PACKAGES+=(docker-compose-v2)
fi
apt-get install -y "${PACKAGES[@]}"
systemctl enable --now docker

echo "==> Préparation du stockage"
mkdir -p "${DATA_DIR}/videos" "${DATA_DIR}/videos_upscaled"

echo "==> Détection des permissions GPU"
VIDEO_GID=""
RENDER_GID=""
if [[ -e /dev/dri/card0 ]]; then VIDEO_GID="$(stat -c '%g' /dev/dri/card0)"; fi
if [[ -e /dev/dri/renderD128 ]]; then RENDER_GID="$(stat -c '%g' /dev/dri/renderD128)"; fi
VIDEO_GID="${VIDEO_GID:-$(getent group video | cut -d: -f3 || true)}"
RENDER_GID="${RENDER_GID:-$(getent group render | cut -d: -f3 || true)}"
VIDEO_GID="${VIDEO_GID:-44}"
RENDER_GID="${RENDER_GID:-109}"

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
  cat > "${PROJECT_DIR}/.env" <<EOF
USCALE_HOST_DATA=${DATA_DIR}
USCALE_DATA_ROOT=/data
USCALE_PORT=27000
VIDEO_GID=${VIDEO_GID}
RENDER_GID=${RENDER_GID}
VIDEO2X_IMAGE=ghcr.io/k4yt3x/video2x:6.4.0
DEFAULT_SOURCE=/data/videos
DEFAULT_DESTINATION=/data/videos_upscaled
EOF
  chmod 600 "${PROJECT_DIR}/.env"
else
  echo "Un fichier .env existe déjà : il est conservé."
fi

echo "==> Vérification Vulkan (informationnelle)"
if command -v vulkaninfo >/dev/null 2>&1; then
  vulkaninfo --summary 2>&1 | head -n 35 || true
else
  echo "vulkaninfo indisponible; vérifie le pilote GPU manuellement."
fi

echo "==> Construction et démarrage d'Uscale"
cd "${PROJECT_DIR}"
docker compose up -d --build

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Uscale est démarré."
echo "Interface : http://${IP_ADDR:-IP_DU_NAS}:27000"
echo "Sources   : ${DATA_DIR}/videos"
echo "Sorties   : ${DATA_DIR}/videos_upscaled"
echo
echo "Logs : cd ${PROJECT_DIR} && docker compose logs -f uscale"
echo "IMPORTANT : le port 27000 doit rester sur le LAN/VPN. Ne l'expose pas sur Internet."
