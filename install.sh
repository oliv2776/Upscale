#!/usr/bin/env bash
set -Eeuo pipefail

# Uscale one-shot installer for Ubuntu/Debian NAS hosts.
if [[ "${EUID}" -ne 0 ]]; then
  echo "Lance ce script avec sudo : sudo bash install.sh"
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${USCALE_HOST_DATA:-/data}"
ENV_FILE="${PROJECT_DIR}/.env"

if [[ "${DATA_DIR}" != /* ]]; then
  echo "USCALE_HOST_DATA doit être un chemin absolu (reçu : ${DATA_DIR})"
  exit 1
fi

env_value() {
  # Lit une valeur dans .env sans l'exécuter.
  local key="$1" default="$2" value=""
  if [[ -f "${ENV_FILE}" ]]; then
    value="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- | tr -d '"' || true)"
  fi
  echo "${value:-$default}"
}

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
CARD_DEV=""
RENDER_DEV=""
# Cherche la carte pilotée par amdgpu (évite de prendre un iGPU Intel en card0).
for card in /sys/class/drm/card[0-9]*; do
  [[ -e "${card}" && "$(basename "${card}")" != *-* ]] || continue
  driver="$(basename "$(readlink -f "${card}/device/driver" 2>/dev/null || true)")"
  if [[ "${driver}" == "amdgpu" ]]; then
    CARD_DEV="/dev/dri/$(basename "${card}")"
    device="$(readlink -f "${card}/device")"
    for render in /sys/class/drm/renderD[0-9]*; do
      [[ -e "${render}" ]] || continue
      if [[ "$(readlink -f "${render}/device")" == "${device}" ]]; then
        RENDER_DEV="/dev/dri/$(basename "${render}")"
      fi
    done
    break
  fi
done
if [[ -z "${CARD_DEV}" ]]; then
  echo "Aucun GPU amdgpu détecté : utilisation de card0 / renderD128 par défaut."
  CARD_DEV=/dev/dri/card0
  RENDER_DEV=/dev/dri/renderD128
else
  echo "GPU AMD : ${CARD_DEV} ${RENDER_DEV}"
fi
VIDEO_GID=""
RENDER_GID=""
if [[ -e "${CARD_DEV}" ]]; then VIDEO_GID="$(stat -c '%g' "${CARD_DEV}")"; fi
if [[ -n "${RENDER_DEV}" && -e "${RENDER_DEV}" ]]; then RENDER_GID="$(stat -c '%g' "${RENDER_DEV}")"; fi
VIDEO_GID="${VIDEO_GID:-$(getent group video | cut -d: -f3 || true)}"
RENDER_GID="${RENDER_GID:-$(getent group render | cut -d: -f3 || true)}"
VIDEO_GID="${VIDEO_GID:-44}"
RENDER_GID="${RENDER_GID:-109}"

if [[ ! -f "${ENV_FILE}" ]]; then
  cat > "${ENV_FILE}" <<EOF
USCALE_HOST_DATA=${DATA_DIR}
USCALE_DATA_ROOT=/data
USCALE_PORT=27000
VIDEO_GID=${VIDEO_GID}
RENDER_GID=${RENDER_GID}
VIDEO2X_IMAGE=ghcr.io/k4yt3x/video2x:6.4.0
VIDEO2X_GPU=
DEFAULT_SOURCE=/data/videos
DEFAULT_DESTINATION=/data/videos_upscaled
USCALE_USER=admin
USCALE_PASSWORD=
USCALE_MIN_FREE_GB=5
EOF
  chmod 600 "${ENV_FILE}"
else
  echo "Un fichier .env existe déjà : il est conservé."
fi

PORT="$(env_value USCALE_PORT 27000)"
IMAGE="$(env_value VIDEO2X_IMAGE ghcr.io/k4yt3x/video2x:6.4.0)"

echo "==> Vérification Vulkan (informationnelle)"
if command -v vulkaninfo >/dev/null 2>&1; then
  vulkaninfo --summary 2>&1 | head -n 35 || true
else
  echo "vulkaninfo indisponible; vérifie le pilote GPU manuellement."
fi

echo "==> Téléchargement de l'image Video2X (${IMAGE}, plusieurs Go)"
if ! docker pull "${IMAGE}"; then
  echo "Téléchargement impossible pour l'instant : il sera relancé au premier traitement."
fi

echo "==> Construction et démarrage d'Uscale"
cd "${PROJECT_DIR}"
docker compose up -d --build

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Uscale est démarré."
echo "Interface : http://${IP_ADDR:-IP_DU_NAS}:${PORT}"
echo "Sources   : ${DATA_DIR}/videos"
echo "Sorties   : ${DATA_DIR}/videos_upscaled"
echo
echo "Logs : cd ${PROJECT_DIR} && docker compose logs -f uscale"
echo "IMPORTANT : le port ${PORT} doit rester sur le LAN/VPN. Ne l'expose pas sur Internet."
if [[ -z "$(env_value USCALE_PASSWORD '')" ]]; then
  echo "Conseil : définis USCALE_PASSWORD dans ${ENV_FILE} puis relance « docker compose up -d » pour protéger l'interface."
fi
