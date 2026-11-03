# Uscale

Uscale est une application auto-hébergée pour analyser une bibliothèque vidéo et lancer des traitements d'upscaling IA avec Video2X / Real-ESRGAN. Elle fournit une interface web en français sur le port **27000**, une file de tâches persistante et un explorateur de dossiers.

Le projet cible notamment les NAS sous Ubuntu équipés d'un GPU AMD compatible Vulkan, comme la Radeon RX 5500 XT.

> La qualité et la vitesse dépendent du pilote Mesa/Vulkan, du conteneur Video2X, du modèle et du fichier source. Fais un essai sur une courte vidéo avant d'ajouter toute ta bibliothèque.

## Fonctionnalités

- Interface web responsive, accessible depuis un navigateur du réseau local.
- Analyse des dossiers et sous-dossiers à la recherche de vidéos.
- Filtre selon la hauteur source (jusqu'à 240p, 360p, 480p ou 720p).
- Upscaling IA avec Real-ESRGAN via Video2X (modèles généralistes et animation).
- Résolution de sortie configurable : 480p, 720p ou 1080p.
- Sortie MKV H.264, conservation des pistes audio et sous-titres lorsque leurs codecs le permettent.
- File persistante SQLite : pause, reprise, annulation et journaux par tâche.
- Un seul traitement à la fois pour limiter la charge GPU.
- Les originaux ne sont jamais écrasés et l'arborescence relative est conservée.

## Prérequis

- NAS ou serveur Ubuntu 22.04/24.04 (ou Linux compatible Docker).
- Docker Engine et Docker Compose v2 fonctionnels.
- GPU AMD exposé à Linux via /dev/dri et pilote Mesa Vulkan opérationnel.
- Paquets hôte mesa-vulkan-drivers et vulkan-tools.
- Accès au dépôt GitHub. S'il est privé, configure une clé SSH autorisée sur GitHub avant le clonage.
- Espace disque disponible pour les vidéos temporaires et les fichiers de sortie.

### À propos de la RX 5500 XT

La RX 5500 XT peut exécuter le traitement Vulkan si le pilote Mesa de l'hôte expose correctement le périphérique. La présence de la carte dans lspci ne suffit pas : vulkaninfo --summary doit afficher le GPU AMD, et non uniquement llvmpipe (rendu logiciel CPU).

## Installation rapide (recommandée)

Sur un NAS Ubuntu avec accès SSH et une clé GitHub autorisée pour ce dépôt privé, l'installation se résume à deux commandes :

~~~bash
git clone git@github.com:oliv2776/Upscale.git
cd Upscale && sudo bash install.sh
~~~

Le script install.sh :
- installe les dépendances système manquantes (Docker/Compose, Git et pilotes/outils Vulkan) ;
- active Docker au démarrage ;
- crée /data/videos et /data/videos_upscaled ;
- détecte les GID des périphériques AMD /dev/dri ;
- génère .env automatiquement sans écraser un fichier existant ;
- construit et démarre Uscale sur le port **27000**.

Une fois terminé, ouvre http://IP_DU_NAS:27000 depuis ton réseau local. Le reste se configure dans l'interface : dossiers, résolution, modèle Real-ESRGAN, file d'attente et paramètres Ollama. Pour trouver l'adresse IP du NAS : hostname -I.

Le script part du principe que les vidéos se trouvent sous /data. Pour un autre point de montage, lance-le avec sudo USCALE_HOST_DATA=/chemin/du/stockage bash install.sh. L'explorateur affichera ce stockage dans le conteneur sous /data.

Si le dépôt est privé, la clé SSH doit être ajoutée à ton compte GitHub avant le clonage. Le script ne modifie pas les paramètres de ton instance Ollama existante.

## Installation manuelle et dépannage


Toutes les commandes ci-dessous sont à exécuter dans un terminal SSH du NAS. Les commandes avec sudo nécessitent les droits administrateur.

### 1. Vérifier Docker et installer les outils système

~~~bash
docker --version
docker compose version
sudo systemctl enable --now docker
docker info
~~~

Si Docker n'est pas installé, installe Docker Engine et le plugin Compose v2 en suivant la documentation officielle adaptée à ta version d'Ubuntu :
https://docs.docker.com/engine/install/ubuntu/

Installe les outils Vulkan et Git :

~~~bash
sudo apt update
sudo apt install -y git mesa-vulkan-drivers vulkan-tools
~~~

FFmpeg est installé dans le conteneur Uscale ; il n'est pas nécessaire de l'installer séparément sur l'hôte.

### 2. Vérifier le GPU AMD

~~~bash
ls -l /dev/dri
vulkaninfo --summary
~~~

Tu dois normalement retrouver un périphérique de rendu comme /dev/dri/renderD128 et une carte AMD dans le résumé Vulkan.

Si vulkaninfo n'affiche que llvmpipe, arrête-toi ici : le traitement utiliserait le CPU au lieu du GPU. Vérifie les pilotes Mesa, redémarre le NAS après une mise à jour du pilote si nécessaire, puis relance vulkaninfo --summary.

Affiche les GID associés aux périphériques DRM :

~~~bash
stat -c '%n : GID=%g' /dev/dri/card* /dev/dri/renderD*
~~~

Conserve les GID affichés pour la configuration Docker à l'étape 5.

### 3. Préparer les dossiers vidéo

La configuration par défaut monte le dossier hôte /data dans le conteneur au même chemin :

~~~bash
sudo mkdir -p /data/videos
sudo mkdir -p /data/videos_upscaled
~~~

Place quelques vidéos de test dans /data/videos. Vérifie que Docker peut lire les sources et écrire dans le dossier de destination.

**Important :** le dossier de destination doit être distinct du dossier source. Ne place pas videos_upscaled à l'intérieur de videos.

Si tes médias sont ailleurs (par exemple /mnt/media), consulte la section **Utiliser un autre emplacement de stockage** avant de démarrer.

### 4. Récupérer le projet

Avec une clé SSH GitHub autorisée :

~~~bash
git clone git@github.com:oliv2776/Upscale.git
cd Upscale
~~~

Si le dépôt est privé, le clonage HTTPS demandera une authentification GitHub. N'inscris jamais un token personnel directement dans une commande ou dans le fichier .env.

### 5. Configurer les variables d'environnement

Crée le fichier local de configuration :

~~~bash
cp .env.example .env
nano .env
~~~

Exemple pour un NAS dont les médias sont stockés dans /data :

~~~dotenv
USCALE_HOST_DATA=/data
USCALE_DATA_ROOT=/data
USCALE_PORT=27000

# Remplacer par les GID réellement trouvés à l'étape 2.
VIDEO_GID=44
RENDER_GID=109

VIDEO2X_IMAGE=ghcr.io/k4yt3x/video2x:6.4.0
DEFAULT_SOURCE=/data/videos
DEFAULT_DESTINATION=/data/videos_upscaled
~~~

Les valeurs 44 et 109 sont des valeurs d'exemple, pas des valeurs garanties pour ton NAS. Remplace-les par les GID réels des périphériques /dev/dri/card* et /dev/dri/renderD*. En pratique, le GID de card* va dans VIDEO_GID et celui de renderD* dans RENDER_GID.

Pour récupérer les GID du premier périphérique de chaque type :

~~~bash
stat -c '%g' /dev/dri/card0
stat -c '%g' /dev/dri/renderD128
~~~

Adapte les noms si tes périphériques ont d'autres numéros. Ne copie pas aveuglément les valeurs d'exemple.

### 6. Construire et démarrer Uscale

Depuis le dossier du dépôt :

~~~bash
docker compose up -d --build
~~~

Vérifie que le conteneur fonctionne :

~~~bash
docker compose ps
docker compose logs -f --tail=200 uscale
~~~

Au premier traitement, Docker peut télécharger l'image Video2X, qui peut représenter plusieurs gigaoctets. Prévois du temps et de l'espace disque.

### 7. Ouvrir l'interface web

Depuis un ordinateur ou un téléphone connecté au même réseau local, ouvre :

~~~text
http://ADRESSE_IP_DU_NAS:27000
~~~

Pour trouver l'adresse IP du NAS :

~~~bash
hostname -I
~~~

L'interface affiche l'état de connexion au serveur et les dossiers configurés. Le port publié est modifiable avec USCALE_PORT dans .env ; le port interne du conteneur reste 27000.

## Première utilisation

1. Dans **Dossier source**, sélectionne /data/videos ou le répertoire contenant tes vidéos.
2. Dans **Dossier de destination**, sélectionne /data/videos_upscaled.
3. Choisis la hauteur maximale des vidéos sources. Pour traiter les vidéos 240p, 360p et 480p, sélectionne **480p**.
4. Choisis la résolution de sortie, par exemple **720p**.
5. Choisis Real-ESRGAN Plus pour les vidéos réelles ; AnimeVideo v3 pour les dessins animés ; Plus Anime pour une restauration plus détaillée des images animées. General v3 est une variante plus légère.
6. Clique sur **Analyser les vidéos** pour compter les fichiers éligibles.
7. Clique sur **Lancer le traitement** pour ajouter les fichiers à la file.
8. Consulte la file et ouvre **Logs** sur une tâche pour voir son journal.

Les sorties reprennent l'arborescence relative du dossier source et utilisent l'extension .mkv. Les vidéos déjà présentes à destination sont ignorées. La base SQLite conserve l'historique des tâches et la signature des fichiers (chemin, taille et date de modification) pour éviter de remettre en file un fichier inchangé déjà traité.

## Utiliser un autre emplacement de stockage

Si les médias sont stockés dans /mnt/media, édite .env ainsi :

~~~dotenv
USCALE_HOST_DATA=/mnt/media
USCALE_DATA_ROOT=/data
DEFAULT_SOURCE=/data/videos
DEFAULT_DESTINATION=/data/videos_upscaled
~~~

Crée les répertoires sur l'hôte :

~~~bash
sudo mkdir -p /mnt/media/videos
sudo mkdir -p /mnt/media/videos_upscaled
~~~

Dans l'interface, les chemins sont ceux vus **depuis le conteneur**, donc /data/videos et /data/videos_upscaled, et non /mnt/media/.... Redémarre après modification :

~~~bash
docker compose up -d
~~~

Le montage /data est la racine autorisée par l'explorateur web. Les chemins extérieurs à cette racine sont refusés.

## Paramètres de traitement

| Paramètre | Rôle |
|---|---|
| Hauteur source maximale | Exclut les vidéos plus grandes que la valeur choisie |
| Hauteur de sortie | Hauteur finale demandée : 480, 720 ou 1080 pixels |
| Modèle IA | Plus (général), General v3 (léger), AnimeVideo v3 (animation vidéo) ou Plus Anime (animation détaillée) |
| CRF | Qualité H.264 : 16 est un réglage de haute qualité ; plus bas augmente la qualité et la taille du fichier |
| Preset | slow améliore l'efficacité de compression, sans changer le modèle IA ; il prend plus de temps |
| Sous-dossiers | Active ou désactive le parcours récursif |

Real-ESRGAN effectue la reconstruction IA en facteur ×4 ; FFmpeg ajuste ensuite la hauteur à la résolution finale demandée en conservant le ratio d'image avec le filtre Lanczos. Video2X écrit d'abord un intermédiaire FFV1 sans perte ; FFmpeg réalise ensuite l'unique encodage avec perte en H.264 (CRF 16 et preset slow par défaut). Cela évite de compresser deux fois les détails reconstruits par l'IA. L'intermédiaire sans perte peut nécessiter beaucoup d'espace disque pendant le traitement ; il est supprimé automatiquement à la fin. Les pistes audio et sous-titres sont copiées depuis l'original lorsque leurs codecs sont compatibles avec le conteneur de sortie.

Le traitement est séquentiel : une seule vidéo est envoyée à Video2X à la fois. Cela limite la consommation de VRAM et évite de saturer la RX 5500 XT. L'upscaling IA peut être lent, surtout sur les vidéos longues. Le modèle Plus est le point de départ recommandé pour les vidéos réelles ; les modèles spécialisés doivent être comparés sur un extrait court, car une netteté plus forte ne signifie pas toujours un rendu plus fidèle.

## Gestion du service

Voir l'état des conteneurs :

~~~bash
docker compose ps
~~~

Suivre les journaux :

~~~bash
docker compose logs -f --tail=200 uscale
~~~

Redémarrer Uscale :

~~~bash
docker compose restart uscale
~~~

Mettre à jour le projet :

~~~bash
git pull
docker compose up -d --build
~~~

Arrêter Uscale :

~~~bash
docker compose down
~~~

Cette commande arrête et supprime le conteneur, mais conserve le volume applicatif et les vidéos.

### Persistance et sauvegarde

La file d'attente, les paramètres et les journaux sont stockés dans le volume Docker uscale-state, monté dans /app/state. Ce volume survit à docker compose down. Sauvegarde ce volume si tu souhaites conserver l'historique des traitements et les paramètres.

Pour supprimer définitivement cet historique sans supprimer les vidéos :

~~~bash
docker compose down
docker volume rm uscale_uscale-state
~~~

## Dépannage

### L'interface ne répond pas sur le port 27000

~~~bash
docker compose ps
docker compose logs --tail=200 uscale
sudo ss -ltnp | grep 27000
~~~

Vérifie que le port n'est pas déjà utilisé et que le pare-feu du NAS autorise les connexions depuis le réseau local.

### Docker n'est pas accessible depuis Uscale

Le conteneur Uscale utilise /var/run/docker.sock pour lancer les traitements Video2X. Vérifie :

~~~bash
ls -l /var/run/docker.sock
docker info
docker compose logs --tail=200 uscale
~~~

### Le GPU n'est pas détecté

Sur l'hôte :

~~~bash
ls -l /dev/dri
vulkaninfo --summary
~~~

Vérifie ensuite VIDEO_GID et RENDER_GID dans .env, puis reconstruis et redémarre :

~~~bash
docker compose up -d --build
~~~

Si Vulkan n'est pas disponible dans le conteneur de traitement, le problème se situe généralement au niveau du pilote hôte, du périphérique /dev/dri ou des permissions de groupe.

### Une tâche échoue

Dans la file de traitement, clique sur **Logs** pour la tâche concernée. Tu peux aussi consulter :

~~~bash
docker compose logs -f uscale
~~~

Teste d'abord un fichier court de quelques secondes avant de soumettre une grande collection.

## Intégration Ollama (facultative)

Uscale peut détecter une instance Ollama existante et afficher sa version ainsi que les modèles locaux. L'adresse et le modèle se configurent depuis le panneau **Connexion Ollama** de l'interface web. Uscale interroge l'API locale officielle : GET /api/version et GET /api/tags. Le modèle sélectionné est mémorisé dans les paramètres.

Par défaut, Uscale essaie http://host.docker.internal:11434 puis la passerelle Docker habituelle http://172.17.0.1:11434. Le fichier Compose crée l'alias host.docker.internal vers l'hôte Linux. Si Ollama tourne dans un autre conteneur ou sur une autre machine, indique dans l'interface son URL accessible depuis Uscale, par exemple http://192.168.1.20:11434.

### Ollama installé directement sur Ubuntu

Par défaut, Ollama écoute souvent sur 127.0.0.1:11434, adresse qui n'est pas joignable depuis un conteneur Docker. Pour autoriser les connexions depuis le réseau Docker, configure le service Ollama sur l'hôte :

~~~bash
sudo systemctl edit ollama
~~~

Ajoute ces lignes dans l'éditeur :

~~~ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
~~~

Puis applique le changement :

~~~bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
curl http://127.0.0.1:11434/api/tags
~~~

Retourne dans Uscale, renseigne http://host.docker.internal:11434 et clique sur **Tester la connexion**, puis **Enregistrer**. Si l'instance Ollama est sur une autre machine, utilise son IP et son port à la place.

**Sécurité :** n'expose pas le port 11434 sur Internet. Si Ollama écoute sur toutes les interfaces, limite son accès avec le pare-feu au réseau Docker et/ou au LAN de confiance. L'API locale Ollama ne nécessite normalement pas de clé d'API ; toute personne qui peut y accéder peut solliciter les modèles installés.

### À quoi sert Ollama dans Uscale ?

Ollama est un assistant optionnel pour des fonctions d'analyse, de classification ou de génération de métadonnées. Il ne remplace pas Video2X / Real-ESRGAN : le calcul d'upscaling des images reste effectué par le moteur vidéo dédié. Uscale n'installe, ne télécharge et ne modifie aucun modèle Ollama automatiquement.

## Sécurité — à lire avant toute exposition réseau

**Le socket Docker est extrêmement privilégié.** Un processus qui peut contrôler le socket Docker de l'hôte peut généralement obtenir des privilèges équivalents à ceux de l'administrateur du NAS.

L'interface Uscale ne possède pas encore de système d'authentification intégré. Par conséquent :

- Ne publie pas le port 27000 sur Internet.
- N'ouvre pas ce port sur ta box ou ton routeur.
- Réserve l'accès au réseau local de confiance.
- Pour un accès distant, passe par un VPN (par exemple WireGuard ou Tailscale) ou un reverse proxy avec authentification robuste.
- Ne donne pas accès à l'interface à des utilisateurs non fiables.

L'explorateur limite les chemins à la racine configurée (/data) et refuse les liens symboliques qui permettraient de sortir de cette racine. Cette restriction ne remplace pas les précautions réseau ci-dessus.

## Documentation et projets utilisés

- [Video2X — documentation](https://docs.video2x.org/)
- [Video2X — exécution en conteneur](https://docs.video2x.org/running/container.html)
- [Video2X — ligne de commande](https://docs.video2x.org/running/command-line.html)
- [Video2X — dépôt GitHub](https://github.com/k4yt3x/video2x)
