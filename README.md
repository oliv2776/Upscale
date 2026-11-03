# Uscale

Uscale est une application auto-hébergée qui analyse une bibliothèque vidéo et lance des traitements d'upscaling IA avec Video2X / Real-ESRGAN. Elle fournit une interface web en français sur le port **27000**, une file de tâches persistante, un comparateur avant/après et des automatisations (dossier surveillé, plage horaire, notifications, Jellyfin/Plex).

Le projet cible notamment les NAS sous Ubuntu équipés d'un GPU AMD compatible Vulkan, comme la Radeon RX 5500 XT.

> La qualité dépend surtout du modèle et de la source. Utilise **Aperçu** pour comparer les modèles sur un extrait avant de lancer toute ta bibliothèque.

## Fonctionnalités

**Qualité**
- Real-ESRGAN via Video2X (Plus, Plus Anime, AnimeVideo v3), avec le plus petit facteur suffisant (×2, ×3 ou ×4 selon le modèle).
- Modèle **Automatique** : un modèle de vision Ollama regarde trois images de la vidéo et choisit entre animation et prise de vue réelle.
- Désentrelacement automatique (bwdif), débruitage optionnel avant l'IA (hqdn3d ou nlmeans), doublement des images par seconde (RIFE, expérimental).
- Format d'affichage respecté (DVD anamorphiques), sortie en pixels carrés.
- Encodage final H.264, HEVC 10 bits, AV1 10 bits, ou matériel VAAPI (H.264/HEVC) sur GPU AMD.
- Audio, sous-titres (mov_text converti en SRT), chapitres et polices jointes conservés ; vérification automatique de la durée et des pistes du fichier produit.

**Fiabilité**
- Traitement par **segments** (3 min par défaut) : une coupure, un redémarrage ou une pause ne fait perdre que le segment en cours ; « Relancer » reprend là où la tâche s'était arrêtée.
- Espace disque borné : l'intermédiaire sans perte ne dépasse jamais un segment ; vérification avant et pendant le traitement.
- **Réglages figés par tâche** au moment de la mise en file : changer un réglage n'affecte pas les vidéos déjà en attente.
- Les originaux ne sont jamais modifiés, une sortie existante n'est jamais écrasée, l'arborescence relative est conservée.

**Organisation**
- Profils par dossier (ex. Animation → AnimeVideo v3, Films → Plus 1080p HEVC).
- Aperçu multi-modèles avec comparateur à curseur.
- File d'attente : réordonner, relancer, supprimer, filtrer, paginer ; progression et temps restant.
- Dossier surveillé, plage horaire (ex. 22 h – 7 h), notifications ntfy / Gotify / webhook, actualisation Jellyfin / Plex, suffixe de nom de fichier.
- Statistiques GPU en direct (charge, température, VRAM, puissance).
- Fichiers produits attribués à ton utilisateur (PUID/PGID) plutôt qu'à root.
- Authentification par mot de passe optionnelle, protection CSRF.

## Prérequis

- NAS ou serveur Ubuntu 22.04/24.04 (ou Linux compatible Docker).
- Docker Engine et Docker Compose v2.
- GPU AMD exposé via /dev/dri et pilote Mesa Vulkan opérationnel (paquets hôte mesa-vulkan-drivers et vulkan-tools).
- Accès au dépôt GitHub. S'il est privé, configure une clé SSH autorisée sur GitHub avant le clonage.

### À propos de la RX 5500 XT

La présence de la carte dans lspci ne suffit pas : vulkaninfo --summary doit afficher le GPU AMD, et non uniquement llvmpipe (rendu logiciel CPU). La RX 5500 XT sait aussi encoder en H.264 et HEVC matériel (VAAPI).

## Installation rapide (recommandée)

~~~bash
git clone git@github.com:oliv2776/Upscale.git
cd Upscale && sudo bash install.sh
~~~

Le script install.sh :
- installe les dépendances système manquantes (Docker/Compose, Git, pilotes/outils Vulkan) et active Docker au démarrage ;
- crée /data/videos et /data/videos_upscaled, attribués à l'utilisateur qui a lancé sudo ;
- détecte le GPU piloté par amdgpu (même si un iGPU occupe card0), ses GID et son périphérique VAAPI ;
- génère .env (sans écraser un fichier existant) avec ton fuseau horaire et ton PUID/PGID ;
- télécharge l'image Video2X à l'avance (plusieurs Go) ;
- construit et démarre Uscale sur le port **27000** (ou celui défini dans .env).

Ouvre ensuite http://IP_DU_NAS:27000 (hostname -I donne l'adresse). Pour un autre point de montage : sudo USCALE_HOST_DATA=/chemin/du/stockage bash install.sh — l'interface affiche toujours ce stockage sous /data.

**Mise à jour :**

~~~bash
git pull
docker compose up -d --build
~~~

L'historique, les réglages et les tâches en attente sont conservés (migration automatique de la base).

## Installation manuelle

### 1. Docker et outils système

~~~bash
docker --version && docker compose version
sudo systemctl enable --now docker
sudo apt update && sudo apt install -y git mesa-vulkan-drivers vulkan-tools
~~~

Si Docker n'est pas installé : https://docs.docker.com/engine/install/ubuntu/. FFmpeg est fourni dans le conteneur.

### 2. GPU AMD

~~~bash
ls -l /dev/dri
vulkaninfo --summary
stat -c '%n : GID=%g' /dev/dri/card* /dev/dri/renderD*
~~~

Si vulkaninfo n'affiche que llvmpipe, arrête-toi : le traitement utiliserait le CPU. Le GID de card* va dans VIDEO_GID, celui de renderD* dans RENDER_GID.

### 3. Dossiers vidéo

~~~bash
sudo mkdir -p /data/videos /data/videos_upscaled
sudo chown "$USER": /data/videos /data/videos_upscaled
~~~

Le dossier de destination doit être distinct du dossier source (ni l'un dans l'autre).

### 4. Configuration

~~~bash
git clone git@github.com:oliv2776/Upscale.git && cd Upscale
cp .env.example .env && nano .env
~~~

Les variables principales sont décrites dans .env.example et dans la section **Variables d'environnement** plus bas. Ne copie pas aveuglément les GID d'exemple (44 et 109).

### 5. Démarrage

~~~bash
docker compose up -d --build
docker compose logs -f --tail=200 uscale
~~~

Au premier traitement, Docker peut télécharger l'image Video2X (plusieurs Go).

## Utilisation

1. **Configuration** : choisis les dossiers source et destination, la hauteur maximale des sources à traiter (480p pour traiter 240p, 360p et 480p), la résolution de sortie, le modèle, les filtres et l'encodeur. Clique sur **Enregistrer**.
2. **Aperçu** : choisis une vidéo, un instant de départ et quelques modèles, puis **Générer l'aperçu**. Les aperçus passent devant la file (même en pause). Compare ensuite deux variantes avec le curseur ; l'original agrandi sans IA sert de référence.
3. **Profils par dossier** (facultatif) : par exemple /data/videos/Animation → AnimeVideo v3. Un fichier utilise le profil du dossier le plus précis qui le contient ; les champs « Global » reprennent les réglages généraux.
4. **Analyser les vidéos** compte les fichiers éligibles ; **Lancer le traitement** les met en file.
5. **File de traitement** : suis la progression par segment, réordonne (⤒ ↑ ↓), relance, supprime ou consulte les journaux.

Les sorties reprennent l'arborescence du dossier source en .mkv, avec le suffixe éventuel (Film (2001).avi → Film (2001) - 720p IA.mkv). Si deux sources du même dossier ne diffèrent que par l'extension (film.avi et film.mp4), les sorties deviennent film.avi.mkv et film.mp4.mkv. Les vidéos déjà présentes à destination ou déjà traitées (même chemin, taille et date) sont ignorées.

## Paramètres de traitement

| Paramètre | Rôle |
|---|---|
| Hauteur source maximale | Exclut les vidéos plus grandes que la valeur choisie |
| Hauteur de sortie | 480p à 2160p, format d'affichage conservé |
| Modèle IA | Plus (réel), Plus Anime (animation détaillée), AnimeVideo v3 (animation, ×2/×3/×4, rapide), Automatique (Ollama). L'ancien choix « General v3 », absent de Video2X 6.4, est remplacé automatiquement par Plus |
| Désentrelacement | Automatique (selon ffprobe), toujours ou jamais |
| Débruitage | Aucun, léger / moyen (hqdn3d), fort (nlmeans, pour VHS, plus lent) |
| Images par seconde | Inchangées ou doublées avec RIFE (expérimental, avant l'upscaling) |
| Encodeur | H.264, HEVC 10 bits (~40 % plus léger), AV1 10 bits (le plus léger, lent), H.264/HEVC VAAPI (matériel, rapide) |
| CRF | 16 par défaut pour H.264 ; converti pour HEVC (+2), AV1 (+10) et VAAPI (QP +4) |
| Preset | Vitesse de l'encodeur logiciel (converti pour SVT-AV1) |

### Comment se déroule une tâche

1. Préparation : filtres éventuels puis découpe de la vidéo en segments sans perte (FFV1) à la résolution source. Avec RIFE, l'interpolation est faite sur la vidéo entière à la résolution source avant la découpe.
2. Pour chaque segment : Video2X agrandit le segment (intermédiaire sans perte), puis FFmpeg l'encode à la taille finale. L'intermédiaire est aussitôt supprimé.
3. Assemblage : les segments encodés sont concaténés sans réencodage, avec l'audio, les sous-titres, les chapitres et les polices de l'original.
4. Vérification : durée, nombre de pistes audio et de sous-titres (et images/s avec RIFE). Une anomalie est signalée par ⚠ dans la file, sans supprimer le fichier.

Les fichiers de travail sont dans un dossier caché .uscale-ID à côté de la sortie. Ils sont conservés si la tâche échoue ou est interrompue (pour reprendre) et supprimés à la fin, à l'annulation ou à la suppression de la tâche. Un changement de réglages qui affecte le résultat repart de zéro.

**Pause** : la vidéo en cours s'arrête à la fin de son segment et reprendra à partir du suivant. **Arrêter** interrompt immédiatement et supprime les fichiers de travail.

**Espace disque** : Uscale estime l'espace nécessaire (segments à la résolution source, un segment agrandi, la sortie) et refuse de démarrer s'il manque de la place ; pendant le traitement, la tâche s'arrête si l'espace libre descend sous USCALE_MIN_FREE_GB.

## Automatisation

- **Dossier surveillé** : le dossier source est analysé à intervalle régulier et les nouvelles vidéos sont mises en file. Les fichiers modifiés depuis moins de 2 minutes sont ignorés (copie en cours).
- **Plage horaire** : les upscalings ne démarrent qu'à l'intérieur de la plage (qui peut passer minuit, ex. 22:00–07:00, heure du fuseau TZ). Hors plage, la vidéo en cours s'arrête à la fin de son segment.
- **Notifications** : ntfy (URL du sujet, jeton optionnel), Gotify (URL du serveur + jeton d'application) ou webhook JSON (POST avec event, title, message, job_id, source, destination). Bouton **Tester la notification**.
- **Serveur multimédia** : après chaque vidéo terminée, Jellyfin (clé API, POST /Library/Refresh) ou Plex (X-Plex-Token, actualisation de chaque bibliothèque) réanalyse ses bibliothèques. Pointe la bibliothèque du serveur sur le dossier de destination ; le suffixe aide à distinguer les versions.

Les jetons ne sont jamais renvoyés au navigateur : laisse le champ vide pour conserver le jeton enregistré.

## Ollama (facultatif)

Uscale détecte une instance Ollama (version et modèles via GET /api/version et /api/tags). Avec un modèle de vision (llava, qwen2.5vl, gemma3…) choisi dans le panneau **Connexion Ollama**, le modèle IA **Automatique** extrait trois images de chaque vidéo et demande s'il s'agit d'animation (→ AnimeVideo v3) ou de prise de vue réelle (→ Plus). En cas d'échec, Plus est utilisé ; le choix est noté dans le journal.

Par défaut, Uscale essaie http://host.docker.internal:11434 puis http://172.17.0.1:11434. Si Ollama est installé directement sur Ubuntu, il écoute souvent sur 127.0.0.1 seulement :

~~~bash
sudo systemctl edit ollama   # ajouter : [Service] puis Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl daemon-reload && sudo systemctl restart ollama
~~~

N'expose pas le port 11434 sur Internet.

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| USCALE_HOST_DATA | /data | Dossier des vidéos sur l'hôte (chemin absolu) |
| USCALE_DATA_ROOT | /data | Même dossier vu dans le conteneur et l'interface |
| USCALE_PORT | 27000 | Port publié |
| VIDEO_GID / RENDER_GID | 44 / 109 | GID de /dev/dri/card* et /dev/dri/renderD* |
| VIDEO2X_IMAGE | ghcr.io/k4yt3x/video2x:6.4.0 | Image Video2X (mode standard) |
| VIDEO2X_GPU | (vide) | Index du GPU Vulkan (voir **Tester le GPU**) |
| VAAPI_DEVICE | /dev/dri/renderD128 | Périphérique pour l'encodage matériel |
| USCALE_USER / USCALE_PASSWORD | admin / (vide) | Authentification de l'interface |
| USCALE_TRUSTED_HOSTS | (vide) | Noms publics autorisés derrière un reverse proxy |
| USCALE_MIN_FREE_GB | 5 | Espace libre minimal conservé |
| USCALE_SEGMENT_SECONDS | 180 | Durée des segments |
| USCALE_LOG_RETENTION_DAYS | 90 | Conservation des journaux des tâches terminées |
| PUID / PGID | (vide) | Propriétaire des fichiers produits |
| TZ | Europe/Paris | Fuseau horaire (plage horaire) |
| USCALE_PULL_POLICY / USCALE_VERSION | build / latest | Construire l'image ou utiliser celle de GHCR |

## Image publiée sur GHCR

Chaque push sur main publie ghcr.io/oliv2776/upscale:latest (et un tag par version vX.Y.Z). Pour l'utiliser au lieu de construire l'image sur le NAS, mets dans .env :

~~~dotenv
USCALE_PULL_POLICY=always
USCALE_VERSION=latest
~~~

puis docker compose pull && docker compose up -d. Si le dépôt est privé, le paquet l'est aussi : connecte d'abord le NAS avec un jeton GitHub ayant le droit read:packages (docker login ghcr.io -u oliv2776). Pour revenir à une version précise : USCALE_VERSION=2.0.0.

## Mode sans socket Docker (expérimental)

Par défaut, Uscale lance Video2X dans un conteneur frère via /var/run/docker.sock, ce qui revient à lui donner les droits administrateur du NAS. La variante docker-compose.local.yml installe l'AppImage officielle de Video2X **dans** l'image Uscale et l'exécute directement avec /dev/dri : plus de socket Docker.

~~~bash
docker compose down
docker compose -f docker-compose.local.yml up -d --build
~~~

Mêmes .env, données, réglages et file d'attente. Pour revenir : docker compose -f docker-compose.local.yml down && docker compose up -d. La CI construit cette image et y exécute un vrai traitement Video2X (sur Vulkan logiciel), mais elle n'a pas encore été validée sur une Radeon : teste-la sur une courte vidéo.

## Gestion du service

~~~bash
docker compose ps
docker compose logs -f --tail=200 uscale
docker compose restart uscale
docker compose down          # conserve le volume uscale-state et les vidéos
~~~

La file, les réglages, les journaux et les aperçus sont dans le volume Docker uscale-state (/app/state). Les journaux des tâches terminées sont supprimés après USCALE_LOG_RETENTION_DAYS jours, et seuls les 10 derniers aperçus sont gardés. Pour tout effacer sans toucher aux vidéos :

~~~bash
docker compose down
docker volume ls | grep uscale-state   # généralement upscale_uscale-state
docker volume rm upscale_uscale-state
~~~

## Dépannage

- **L'interface ne répond pas** : docker compose ps, docker compose logs --tail=200 uscale, sudo ss -ltnp | grep 27000.
- **Le conteneur ne démarre pas (/dev/dri)** : Uscale a besoin de /dev/dri (encodage VAAPI, statistiques GPU). Vérifie ls -l /dev/dri sur l'hôte.
- **Le GPU n'est pas détecté** : **Tester le GPU** lance Video2X --list-devices avec les mêmes permissions que les traitements. S'il affiche plusieurs GPU (iGPU + Radeon), indique l'index de la Radeon dans VIDEO2X_GPU. Vérifie VIDEO_GID et RENDER_GID, puis docker compose up -d --build.
- **Une tâche échoue** : bouton **Logs** dans la file. Corrige la cause puis **Relancer** : les segments déjà encodés sont conservés.
- **Encodage VAAPI en erreur** : vérifie VAAPI_DEVICE (le renderD* de la Radeon) ; sinon utilise un encodeur logiciel.
- **Redémarrage pendant un traitement** : le conteneur Video2X orphelin est supprimé au démarrage et la tâche reprend au segment en cours.

## Sécurité — à lire avant toute exposition réseau

**Le socket Docker est extrêmement privilégié** : un processus qui le contrôle peut obtenir les droits administrateur du NAS (voir le mode sans socket ci-dessus).

Définis USCALE_PASSWORD (authentification HTTP Basic). Sans mot de passe, l'interface est ouverte à tout le réseau local. Les actions de l'API exigent un corps JSON et refusent les requêtes venant d'un autre site (protection CSRF) ; derrière un reverse proxy, transmets X-Forwarded-Host ou liste le nom public dans USCALE_TRUSTED_HOSTS. L'authentification Basic n'est pas chiffrée : hors du LAN, utilise un VPN (WireGuard, Tailscale) ou un reverse proxy HTTPS. Ne publie jamais le port 27000 sur Internet.

L'explorateur limite les chemins à la racine configurée (/data) et ignore les liens symboliques qui en sortiraient.

## Développement

~~~bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
~~~

Le code est dans le paquet uscale/ : config, db, settings (réglages et profils), media (ffprobe et commandes FFmpeg), runner (processus, Video2X), scanner, pipeline (segments et aperçus), worker (file, plage horaire, surveillance, maintenance), notify, gpu, ollama, web (API Flask).

Les tests utilisent FFmpeg, un faux Video2X (Docker et local, Real-ESRGAN et RIFE) et un faux serveur HTTP : ils couvrent le pipeline complet, la reprise après pause, les aperçus, les notifications, Jellyfin/Plex, Ollama et la sécurité, sans GPU. La CI construit aussi les deux images Docker et exécute un vrai traitement Video2X sur Vulkan logiciel (lavapipe).

## Documentation et projets utilisés

- [Video2X — documentation](https://docs.video2x.org/)
- [Video2X — dépôt GitHub](https://github.com/k4yt3x/video2x)
- [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN)
- [ntfy](https://docs.ntfy.sh/publish/), [Gotify](https://gotify.net/docs/pushmsg)
- Police [Manrope](https://github.com/sharanda/manrope) (SIL Open Font License)
