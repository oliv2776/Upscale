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
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

ENV_FILE="${PROJECT_DIR}/.env"
TIMEOUT="${UPSCALE_UPDATE_TIMEOUT:-900}"
ASSUME_YES=0
BACKUP=1
PAUSED_BY_US=0
RESUMED=0

for arg in "$@"; do
  case "${arg}" in
    -y|--oui) ASSUME_YES=1 ;;
    --sans-sauvegarde) BACKUP=0 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

echo "==> Construction de la nouvelle image (le traitement en cours continue)"
"${COMPOSE[@]}" build

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
  fi
else
  echo "==> Conteneur non démarré : pas de pause ni de sauvegarde possible"
fi

echo "==> Bascule vers la nouvelle image"
"${COMPOSE[@]}" up -d --no-build

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

if [[ "${PAUSED_BY_US}" == "1" ]]; then
  echo "==> Reprise de la file au segment suivant"
  resume_queue
  RESUMED=1
fi

version="$("${COMPOSE[@]}" exec -T upscale python -c 'from upscale import config; print(config.VERSION)')"
echo "==> Upscale ${version} est à jour."
if [[ "${PAUSED_BY_US}" == "1" ]]; then
  echo "    Le traitement interrompu a repris ; les segments déjà encodés sont conservés."
elif [[ "$(is_paused)" == "1" ]]; then
  echo "    La file reste en pause (pause demandée avant la mise à jour)."
fi
if [[ -z "$(env_value UPSCALE_PASSWORD '')" && "$(env_value UPSCALE_ALLOW_ANONYMOUS 0)" != "1" ]]; then
  echo
  echo "Note 2.8 : sans mot de passe, l'interface est en lecture seule (le traitement continue)."
  echo "Définis UPSCALE_PASSWORD dans .env (ou UPSCALE_ALLOW_ANONYMOUS=1) pour reprendre la main."
fi
