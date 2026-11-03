#!/usr/bin/env bash
set -Eeuo pipefail

# Upscale one-shot installer for Ubuntu/Debian NAS hosts.
if [[ "${EUID}" -ne 0 ]]; then
  echo "Lance ce script avec sudo : sudo bash install.sh"
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"

env_value() {
  # Lit une valeur dans .env sans l'exécuter (accepte aussi l'ancien préfixe USCALE_).
  local key="$1" default="$2" value=""
  if [[ -f "${ENV_FILE}" ]]; then
    value="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- | tr -d '"' || true)"
    if [[ -z "${value}" && "${key}" == UPSCALE_* ]]; then
      value="$(grep -E "^USCALE_${key#UPSCALE_}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- | tr -d '"' || true)"
    fi
  fi
  echo "${value:-$default}"
}

DATA_DIR="${UPSCALE_HOST_DATA:-${USCALE_HOST_DATA:-$(env_value UPSCALE_HOST_DATA /data)}}"
if [[ "${DATA_DIR}" != /* ]]; then
  echo "UPSCALE_HOST_DATA doit être un chemin absolu (reçu : ${DATA_DIR})"
  exit 1
fi

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
VAAPI_DEVICE="${RENDER_DEV:-/dev/dri/renderD128}"

# Fichiers produits attribués à l'utilisateur de la session qui a lancé sudo (plutôt qu'à root).
# Lancé directement en root : propriétaire du dossier vidéo, sinon le premier utilisateur (UID 1000).
OWNER_UID="${SUDO_UID:-}"
OWNER_GID="${SUDO_GID:-}"
if [[ -z "${OWNER_UID}" || "${OWNER_UID}" == "0" ]]; then
  OWNER_UID="$(stat -c '%u' "${DATA_DIR}/videos")"
  OWNER_GID="$(stat -c '%g' "${DATA_DIR}/videos")"
  if [[ "${OWNER_UID}" == "0" ]] && getent passwd 1000 > /dev/null; then
    OWNER_UID=1000
    OWNER_GID="$(getent passwd 1000 | cut -d: -f4)"
  fi
  if [[ "${OWNER_UID}" == "0" ]]; then OWNER_UID=""; OWNER_GID=""; fi
fi
if [[ -n "${OWNER_UID}" ]]; then
  echo "Fichiers produits attribués à : $(getent passwd "${OWNER_UID}" | cut -d: -f1 || echo "${OWNER_UID}") (${OWNER_UID}:${OWNER_GID})"
fi
HOST_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo Europe/Paris)"
if [[ -n "${OWNER_UID}" ]]; then
  for dir in "${DATA_DIR}/videos" "${DATA_DIR}/videos_upscaled"; do
    if [[ "$(stat -c '%u' "${dir}")" == "0" ]]; then chown "${OWNER_UID}:${OWNER_GID}" "${dir}"; fi
  done
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  cat > "${ENV_FILE}" <<EOF
UPSCALE_HOST_DATA=${DATA_DIR}
UPSCALE_DATA_ROOT=/data
UPSCALE_PORT=27000
VIDEO_GID=${VIDEO_GID}
RENDER_GID=${RENDER_GID}
VIDEO2X_IMAGE=ghcr.io/k4yt3x/video2x:6.4.0
VIDEO2X_GPU=
VAAPI_DEVICE=${VAAPI_DEVICE}
DEFAULT_SOURCE=/data/videos
DEFAULT_DESTINATION=/data/videos_upscaled
UPSCALE_USER=admin
UPSCALE_PASSWORD=
UPSCALE_MIN_FREE_GB=5
PUID=${OWNER_UID}
PGID=${OWNER_GID}
TZ=${HOST_TZ}
EOF
  chmod 600 "${ENV_FILE}"
else
  echo "Un fichier .env existe déjà : il est conservé (seules les clés manquantes sont ajoutées)."
  add_missing() {
    local key="$1" value="$2"
    if ! grep -qE "^${key}=" "${ENV_FILE}"; then
      echo "${key}=${value}" >> "${ENV_FILE}"
      echo "  + ${key}=${value}"
    fi
  }
  add_missing PUID "${OWNER_UID}"
  add_missing PGID "${OWNER_GID}"
  add_missing TZ "${HOST_TZ}"
  add_missing VAAPI_DEVICE "${VAAPI_DEVICE}"
fi
# .env doit rester lisible par l'utilisateur qui lance « docker compose » sans sudo :
# il appartient au propriétaire du dossier Upscale (celui qui l'a cloné), en lecture réservée.
PROJECT_OWNER="$(stat -c '%u:%g' "${PROJECT_DIR}")"
if [[ "${PROJECT_OWNER}" != 0:* ]]; then
  chown "${PROJECT_OWNER}" "${ENV_FILE}"
fi
chmod 600 "${ENV_FILE}"

PORT="$(env_value UPSCALE_PORT 27000)"
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

# Disques supplémentaires : sudo UPSCALE_DISQUES="/mnt/disque2 /mnt/disque3" bash install.sh
if [[ -n "${UPSCALE_DISQUES:-}" ]]; then
  echo "==> Disques supplémentaires"
  for disk in ${UPSCALE_DISQUES}; do
    bash "${PROJECT_DIR}/bibliotheques.sh" ajouter "${disk}" --sans-redemarrage || true
  done
fi

echo "==> Construction et démarrage d'Upscale"
cd "${PROJECT_DIR}"
docker compose up -d --build

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Upscale est démarré."
echo "Interface : http://${IP_ADDR:-IP_DU_NAS}:${PORT}"
echo "Sources   : ${DATA_DIR}/videos"
echo "Sorties   : ${DATA_DIR}/videos_upscaled"
echo
echo
bash "${PROJECT_DIR}/bibliotheques.sh" liste
echo "Autre disque : sudo bash ${PROJECT_DIR}/bibliotheques.sh ajouter /chemin/du/disque"
echo
echo "Logs : cd ${PROJECT_DIR} && docker compose logs -f upscale"
echo "IMPORTANT : le port ${PORT} doit rester sur le LAN/VPN. Ne l'expose pas sur Internet."
if [[ -z "$(env_value UPSCALE_PASSWORD '')" ]]; then
  echo "Conseil : définis UPSCALE_PASSWORD dans ${ENV_FILE} puis relance « docker compose up -d » pour protéger l'interface."
fi
