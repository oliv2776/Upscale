#!/usr/bin/env bash
# Disques supplémentaires visibles dans Upscale.
#
#   sudo bash bibliotheques.sh liste
#   sudo bash bibliotheques.sh ajouter /mnt/disque2/films [Nom]
#   sudo bash bibliotheques.sh retirer Nom
#
# Chaque dossier ajouté apparaît dans l'interface sous /disques/<Nom>. La liste est écrite dans
# docker-compose.override.yml (chargé automatiquement par Docker Compose, ignoré par git), puis
# Upscale est redémarré. Option --sans-redemarrage pour ne pas redémarrer.
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERRIDE="${PROJECT_DIR}/docker-compose.override.yml"
RESTART=1
ARGS=()
for arg in "$@"; do
  if [[ "${arg}" == "--sans-redemarrage" ]]; then RESTART=0; else ARGS+=("${arg}"); fi
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

usage() {
  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

# Entrées : lignes « chemin_hôte|Nom », lues dans le fichier override.
read_entries() {
  [[ -f "${OVERRIDE}" ]] || return 0
  sed -n 's#^      - "\(.*\):/disques/\([^"]*\)"$#\1|\2#p' "${OVERRIDE}"
}

write_entries() {
  local entries=("$@") tmp
  if [[ ${#entries[@]} -eq 0 ]]; then
    rm -f "${OVERRIDE}"
    return
  fi
  tmp="$(mktemp "${OVERRIDE}.XXXX")"
  {
    echo "# Généré par bibliotheques.sh : disques supplémentaires visibles dans Upscale sous /disques/<Nom>."
    echo "# Modifier avec : sudo bash bibliotheques.sh ajouter|retirer|liste"
    echo "services:"
    echo "  upscale:"
    echo "    volumes:"
    for entry in "${entries[@]}"; do
      echo "      - \"${entry%%|*}:/disques/${entry##*|}\""
    done
  } > "${tmp}"
  mv "${tmp}" "${OVERRIDE}"
  # Lisible par « docker compose » lancé sans sudo, et rendu au propriétaire du dossier Upscale.
  chmod 644 "${OVERRIDE}"
  local owner
  owner="$(stat -c '%u:%g' "${PROJECT_DIR}")"
  if [[ "${owner}" != 0:* ]]; then chown "${owner}" "${OVERRIDE}" 2> /dev/null || true; fi
}

restart() {
  [[ "${RESTART}" == 1 ]] || return 0
  cd "${PROJECT_DIR}"
  local image
  image="$(docker inspect -f '{{.Config.Image}}' upscale 2>/dev/null || true)"
  echo "==> Redémarrage d'Upscale"
  if [[ "${image}" == *upscale-local* ]]; then
    local files=(-f docker-compose.local.yml)
    [[ -f "${OVERRIDE}" ]] && files+=(-f "${OVERRIDE}")
    docker compose "${files[@]}" up -d
  else
    docker compose up -d
  fi
}

host_data() {
  local value=""
  if [[ -f "${PROJECT_DIR}/.env" ]]; then
    value="$(grep -E '^U(P)?SCALE_HOST_DATA=' "${PROJECT_DIR}/.env" | tail -n 1 | cut -d= -f2- | tr -d '"' || true)"
  fi
  echo "${value:-/data}"
}

command="${1:-liste}"
case "${command}" in
  liste|list)
    echo "Disque principal : $(host_data)  →  /data"
    mapfile -t entries < <(read_entries)
    if [[ ${#entries[@]} -eq 0 ]]; then
      echo "Aucun disque supplémentaire. Ajoute-en un avec : sudo bash bibliotheques.sh ajouter /chemin/du/disque"
    fi
    for entry in "${entries[@]}"; do
      echo "${entry##*|} : ${entry%%|*}  →  /disques/${entry##*|}"
    done
    ;;
  ajouter|add)
    [[ $# -ge 2 ]] || usage
    path="${2%/}"
    [[ "${path}" == /* ]] || { echo "Le chemin doit être absolu : ${path}"; exit 1; }
    [[ -d "${path}" ]] || { echo "Dossier introuvable sur le NAS : ${path}"; exit 1; }
    name="${3:-$(basename "${path}")}"
    # Lettres accentuées acceptées ; espaces et caractères gênants pour Docker remplacés par « - ».
    name="${name// /-}"
    name="${name//[\/:\"\\\$\`]/-}"
    [[ -n "${name}" && "${name}" != "data" && "${name}" != .* ]] || { echo "Nom invalide : précise-le en 3e argument."; exit 1; }
    main="$(host_data)"
    if [[ "${path}" == "${main%/}" || "${path}" == "${main%/}"/* ]]; then
      echo "${path} est déjà visible dans Upscale sous /data${path#"${main%/}"} : rien à faire."
      exit 0
    fi
    mapfile -t entries < <(read_entries)
    for entry in "${entries[@]}"; do
      if [[ "${entry%%|*}" == "${path}" ]]; then echo "Déjà ajouté sous /disques/${entry##*|}."; exit 0; fi
      if [[ "${entry##*|}" == "${name}" ]]; then echo "Le nom ${name} est déjà utilisé : précise un autre nom en 3e argument."; exit 1; fi
    done
    entries+=("${path}|${name}")
    write_entries "${entries[@]}"
    echo "Ajouté : ${path}  →  /disques/${name}"
    restart
    ;;
  retirer|remove)
    [[ $# -ge 2 ]] || usage
    mapfile -t entries < <(read_entries)
    kept=()
    found=0
    for entry in "${entries[@]}"; do
      if [[ "${entry##*|}" == "$2" || "${entry%%|*}" == "${2%/}" ]]; then found=1; else kept+=("${entry}"); fi
    done
    [[ "${found}" == 1 ]] || { echo "Aucun disque nommé $2 (voir : sudo bash bibliotheques.sh liste)."; exit 1; }
    write_entries "${kept[@]+"${kept[@]}"}"
    echo "Retiré : $2 (les fichiers du disque ne sont pas touchés)."
    restart
    ;;
  *)
    usage
    ;;
esac
