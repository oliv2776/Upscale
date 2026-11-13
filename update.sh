#!/usr/bin/env bash
# Mise à jour d'Upscale sans perdre le travail en cours.
#
#   1. met la file en pause et attend la fin du segment en cours (aucune image perdue) ;
#   2. sauvegarde la base (file, réglages, historique) dans backups/ ;
#   3. construit la nouvelle image pendant que le traitement continue, puis bascule ;
#   4. remet la file en route (même sans mot de passe : la reprise passe par la base).
#
# Usage : git pull && bash update.sh [-y] [--sans-sauvegarde]
#         (docker compose doit être accessible à l'utilisateur courant ; pas besoin de sudo avec le groupe docker)
#
# Mises à jour automatiques (correctifs de sécurité compris) :
#   sudo bash update.sh --installer-auto      # vérification chaque nuit (04:00), par un minuteur systemd
#   sudo bash update.sh --desinstaller-auto
#   bash update.sh --auto                     # ce que lance le minuteur : ne fait rien si l'image n'a pas changé
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

ENV_FILE="${PROJECT_DIR}/.env"
TIMEOUT="${UPSCALE_UPDATE_TIMEOUT:-900}"
ASSUME_YES=0
AUTO=0
ACTION=update
KEEP_BACKUPS="${UPSCALE_KEEP_BACKUPS:-10}"
BACKUP=1
PAUSED_BY_US=0
RESUMED=0

for arg in "$@"; do
  case "${arg}" in
    -y|--oui) ASSUME_YES=1 ;;
    --sans-sauvegarde) BACKUP=0 ;;
    --auto) AUTO=1; ASSUME_YES=1; TIMEOUT="${UPSCALE_UPDATE_TIMEOUT:-21600}" ;;
    --installer-auto) ACTION=install-auto ;;
    --desinstaller-auto) ACTION=remove-auto ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Option inconnue : ${arg}"; exit 1 ;;
  esac
done

env_value() {
  local key="$1" default="$2" value=""
  if [[ -f "${ENV_FILE}" ]]; then
    value="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- | tr -d '"' || true)"
    if [[ -z "${value}" && "${key}" == UPSCALE_* ]]; then
      value="$(grep -E "^USCALE_${key#UPSCALE_}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- | tr -d '"' || true)"
    fi
  fi
  echo "${value:-$default}"
}

if [[ -f "${ENV_FILE}" ]] && grep -qE '^USCALE_' "${ENV_FILE}"; then
  echo "Note : .env contient d'anciens noms USCALE_* ; renomme-les en UPSCALE_* (retirés en 3.0)." >&2
fi

UNIT_DIR="${UPSCALE_SYSTEMD_DIR:-/etc/systemd/system}"

set_env_value() {
  local key="$1" value="$2"
  touch "${ENV_FILE}"
  if grep -qE "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    echo "${key}=${value}" >> "${ENV_FILE}"
  fi
}

if [[ "${ACTION}" == "remove-auto" ]]; then
  [[ "${EUID}" -eq 0 ]] || { echo "Lance : sudo bash update.sh --desinstaller-auto"; exit 1; }
  systemctl disable --now upscale-update.timer 2>/dev/null || true
  rm -f "${UNIT_DIR}/upscale-update.timer" "${UNIT_DIR}/upscale-update.service"
  systemctl daemon-reload 2>/dev/null || true
  echo "Mises à jour automatiques désactivées. (UPSCALE_PULL_POLICY reste inchangé dans .env.)"
  exit 0
fi

if [[ "${ACTION}" == "install-auto" ]]; then
  [[ "${EUID}" -eq 0 ]] || { echo "Lance : sudo bash update.sh --installer-auto"; exit 1; }
  when="${UPSCALE_AUTO_UPDATE_TIME:-04:00}"
  [[ "${when}" =~ ^[0-2][0-9]:[0-5][0-9]$ ]] || { echo "UPSCALE_AUTO_UPDATE_TIME doit être au format HH:MM"; exit 1; }
  owner="$(stat -c '%U' "${PROJECT_DIR}")"
  pre=""
  if [[ -d "${PROJECT_DIR}/.git" && "${owner}" != "root" && "${owner}" != "UNKNOWN" ]] && command -v runuser >/dev/null 2>&1; then
    # Scripts et fichiers compose : récupérés avant le lancement (jamais pendant que update.sh s'exécute).
    pre="ExecStartPre=-$(command -v runuser) -u ${owner} -- $(command -v git) -C ${PROJECT_DIR} pull --ff-only --quiet"
  fi
  {
    echo "[Unit]"
    echo "Description=Mise à jour automatique d'Upscale (image publiée, correctifs de sécurité)"
    echo "After=docker.service network-online.target"
    echo "Wants=network-online.target"
    echo
    echo "[Service]"
    echo "Type=oneshot"
    echo "WorkingDirectory=${PROJECT_DIR}"
    [[ -z "${pre}" ]] || echo "${pre}"
    echo "ExecStart=/bin/bash ${PROJECT_DIR}/update.sh --auto"
  } > "${UNIT_DIR}/upscale-update.service"
  {
    echo "[Unit]"
    echo "Description=Vérification nocturne des mises à jour d'Upscale"
    echo
    echo "[Timer]"
    echo "OnCalendar=*-*-* ${when}:00"
    echo "RandomizedDelaySec=20min"
    echo "Persistent=true"
    echo
    echo "[Install]"
    echo "WantedBy=timers.target"
  } > "${UNIT_DIR}/upscale-update.timer"
  # Le NAS utilise désormais l'image publiée (reconstruite dès qu'un correctif de sécurité existe).
  if [[ "$(docker inspect -f '{{.Config.Image}}' upscale 2>/dev/null || true)" != *upscale-local* ]]; then
    set_env_value UPSCALE_PULL_POLICY always
    if [[ "${owner}" != "root" && "${owner}" != "UNKNOWN" ]]; then chown "${owner}": "${ENV_FILE}" 2>/dev/null || true; fi
    chmod 600 "${ENV_FILE}"
  fi
  if [[ -z "${UPSCALE_SYSTEMD_DIR:-}" ]]; then
    systemctl daemon-reload
    systemctl enable --now upscale-update.timer
  fi
  echo "Mises à jour automatiques activées : vérification chaque nuit vers ${when}."
  echo "  Le conteneur n'est redémarré que si une nouvelle image a été publiée, à la fin du segment en cours."
  echo "  État       : systemctl list-timers upscale-update.timer"
  echo "  Journal    : journalctl -u upscale-update.service -n 50"
  echo "  Maintenant : sudo systemctl start upscale-update.service"
  echo "  Désactiver : sudo bash update.sh --desinstaller-auto"
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker est introuvable."
  exit 1
fi

# Même variante que le conteneur en cours (mode standard ou mode local sans socket).
COMPOSE=(docker compose)
if [[ -f "${PROJECT_DIR}/docker-compose.override.yml" ]]; then
  if [[ "$(docker inspect -f '{{.Config.Image}}' upscale 2>/dev/null || true)" == *upscale-local* ]]; then
    COMPOSE=(docker compose -f docker-compose.local.yml -f docker-compose.override.yml)
  fi
elif [[ "$(docker inspect -f '{{.Config.Image}}' upscale 2>/dev/null || true)" == *upscale-local* ]]; then
  COMPOSE=(docker compose -f docker-compose.local.yml)
fi

container_running() {
  "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx upscale
}

# Exécute un petit programme Python dans le conteneur (accès direct à la base, sans authentification).
db_python() {
  "${COMPOSE[@]}" exec -T upscale python -
}

count_running() {
  db_python <<'PY'
from upscale import db
with db.connect() as conn:
    print(conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0])
PY
}

is_paused() {
  db_python <<'PY'
from upscale import db
print("1" if db.is_paused() else "0")
PY
}

pause_queue() {
  db_python <<'PY'
from upscale import db
db.set_paused(True)
PY
}

resume_queue() {
  # Uniquement le drapeau en base : le worker du conteneur le lit toutes les 10 s.
  # Ne jamais appeler worker.ensure_worker() ici : cela démarrerait un worker dans le processus
  # « docker compose exec », qui disparaîtrait en emportant la tâche réservée.
  db_python <<'PY'
from upscale import db
db.set_paused(False)
PY
}

backup_database() {
  local backup="$1"
  mkdir -p "${PROJECT_DIR}/backups"
  chmod 700 "${PROJECT_DIR}/backups"
  db_python <<'PY' >/dev/null
import sqlite3
from upscale import config
source = sqlite3.connect(str(config.db_path()))
target = sqlite3.connect("/tmp/upscale-update-backup.sqlite3")
source.backup(target)
source.close()
target.close()
PY
  "${COMPOSE[@]}" cp upscale:/tmp/upscale-update-backup.sqlite3 "${backup}" >/dev/null
  "${COMPOSE[@]}" exec -T upscale rm -f /tmp/upscale-update-backup.sqlite3
}

cleanup() {
  trap - EXIT
  local code
  code=$?
  if [[ "${PAUSED_BY_US}" == "1" && "${RESUMED}" == "0" ]] && container_running; then
    echo "==> Reprise de la file après interruption de la mise à jour"
    resume_queue || true
  fi
  exit "${code}"
}
trap cleanup EXIT

if [[ "${ASSUME_YES}" != "1" ]]; then
  echo "Upscale va être mis à jour. Le segment en cours sera terminé avant l'arrêt."
  read -r -p "Continuer ? [o/N] " answer
  [[ "${answer}" =~ ^[oOyY]$ ]] || { echo "Annulé."; exit 0; }
fi

if [[ "${AUTO}" == "1" ]]; then
  running_image="$(docker inspect -f '{{.Image}}' upscale 2>/dev/null || true)"
  image_name="$("${COMPOSE[@]}" config --images 2>/dev/null | head -n 1)"
  if [[ "${image_name}" == *upscale-local* ]]; then
    # Variante locale : pas d'image publiée. Reconstruction avec une base à jour ; la couche des paquets
    # système est refaite une fois par semaine (APT_REFRESH change chaque semaine).
    echo "==> Reconstruction de l'image locale (base à jour)"
    "${COMPOSE[@]}" build --pull --build-arg "APT_REFRESH=$(date +%G-%V)" >/dev/null
  else
    export UPSCALE_PULL_POLICY=always
    echo "==> Recherche d'une nouvelle image publiée"
    "${COMPOSE[@]}" pull --quiet upscale
  fi
  new_image="$(docker image inspect -f '{{.Id}}' "${image_name}" 2>/dev/null || true)"
  if [[ -n "${new_image}" && "${new_image}" == "${running_image}" ]] && container_running; then
    echo "==> Déjà à jour : rien à faire."
    exit 0
  fi
  echo "==> Nouvelle image disponible"
else
  echo "==> Construction de la nouvelle image (le traitement en cours continue)"
  "${COMPOSE[@]}" build
fi

if container_running; then
  running="$(count_running)"
  if [[ "${running}" != "0" ]]; then
    if [[ "$(is_paused)" == "1" ]]; then
      echo "==> File déjà en pause : attente de la fin du segment en cours"
    else
      echo "==> ${running} traitement(s) en cours : pause à la fin du segment"
      pause_queue
      PAUSED_BY_US=1
    fi
    deadline=$((SECONDS + TIMEOUT))
    while [[ "$(count_running)" != "0" ]]; do
      if (( SECONDS >= deadline )); then
        echo "Délai dépassé (${TIMEOUT}s) : le segment en cours dure trop longtemps."
        echo "Relance avec UPSCALE_UPDATE_TIMEOUT=3600 bash update.sh, ou plus tard."
        exit 1
      fi
      sleep 5
    done
    echo "==> Segment terminé, travail mis en pause"
  fi

  if [[ "${BACKUP}" == "1" ]]; then
    stamp="$(date +%Y%m%d-%H%M%S)"
    backup="${PROJECT_DIR}/backups/upscale-db-${stamp}.sqlite3"
    echo "==> Sauvegarde de la base : ${backup}"
    backup_database "${backup}"
    echo "    (contient les jetons de notification : dossier en 700)"
    if [[ "${AUTO}" == "1" && "${KEEP_BACKUPS}" =~ ^[0-9]+$ ]]; then
      # Mises à jour automatiques : seules les dernières sauvegardes sont conservées.
      find "${PROJECT_DIR}/backups" -maxdepth 1 -name 'upscale-db-*.sqlite3' -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | tail -n "+$((KEEP_BACKUPS + 1))" | cut -d' ' -f2- \
        | while read -r old; do rm -f -- "${old}"; done
    fi
  fi
else
  echo "==> Conteneur non démarré : pas de pause ni de sauvegarde possible"
fi

echo "==> Bascule vers la nouvelle image"
"${COMPOSE[@]}" up -d --no-build --force-recreate

echo "==> Attente du serveur"
ready=0
for ((attempt = 0; attempt < 60; attempt++)); do
  if "${COMPOSE[@]}" exec -T upscale python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:27000/api/ping', timeout=2)" \
      >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "${ready}" != "1" ]]; then
  echo "Le serveur ne répond pas encore : vérifie « docker compose logs --tail=100 upscale »."
  exit 1
fi

version="$("${COMPOSE[@]}" exec -T upscale python -c 'from upscale import config; print(config.VERSION)')"
expected="$(cat "${PROJECT_DIR}/VERSION")"
if [[ "${AUTO}" == "1" ]]; then
  # L'image publiée peut être en avance ou en retard de quelques minutes sur le dépôt local.
  expected="${version}"
  docker image prune -f >/dev/null 2>&1 || true
fi
if [[ "${version}" != "${expected}" ]]; then
  echo "ERREUR : le conteneur a démarré en version ${version}, alors que le dépôt est en ${expected}."
  echo "Le conteneur n'a pas été recréé. Force avec :"
  echo "  docker compose up -d --build --force-recreate"
  echo "La file reste/est remise en route sur l'ancienne version en attendant."
  exit 1
fi

if [[ "${PAUSED_BY_US}" == "1" ]]; then
  echo "==> Reprise de la file au segment suivant"
  resume_queue
  RESUMED=1
fi

echo "==> Upscale ${version} est à jour."
if [[ "${PAUSED_BY_US}" == "1" ]]; then
  echo "    Le traitement interrompu a repris ; les segments déjà encodés sont conservés."
elif [[ "$(is_paused)" == "1" ]]; then
  echo "    La file reste en pause (pause demandée avant la mise à jour)."
fi
if [[ -z "$(env_value UPSCALE_PASSWORD '')" && "$(env_value UPSCALE_ALLOW_ANONYMOUS 0)" != "1" ]]; then
  echo
  echo "Note : sans mot de passe, l'interface est en lecture seule par défaut (le traitement continue)."
  echo "Active le mode réseau local depuis le bandeau ou Système → Accès, ou définis UPSCALE_PASSWORD."
fi
