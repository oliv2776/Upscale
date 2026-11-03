# Uscale

Uscale est une interface web locale pour organiser l'upscaling IA d'une bibliothèque vidéo avec Video2X et un GPU Vulkan (notamment AMD sous Ubuntu).

Fonctionnalités :
- Interface web sur le port **27000**
- Explorateur de dossiers limité à /data
- Scan récursif des vidéos et filtre par hauteur source
- File d'attente persistante SQLite, pause/reprise/annulation
- Real-ESRGAN Plus ou Real-ESRGAN Anime
- Sortie MKV, hauteur configurable, audio et sous-titres repris depuis l'original
- Journaux par tâche
- Aucun fichier source n'est modifié

## Installation sur Ubuntu / NAS

### 1. Préparer les dossiers

Sur l'hôte Ubuntu :

~~~bash
sudo mkdir -p /data/videos /data/videos_upscaled
~~~

Le conteneur monte le dossier hôte /data dans /data. L'explorateur web ne peut naviguer que sous cette racine. Si tes médias sont ailleurs, adapte USCALE_HOST_DATA et USCALE_DATA_ROOT dans le fichier .env et le montage correspondant dans docker-compose.yml. Le chemin visible dans l'interface doit être identique au chemin monté dans le conteneur.

### 2. Vérifier le pilote AMD/Vulkan

~~~bash
sudo apt update
sudo apt install -y mesa-vulkan-drivers vulkan-tools ffmpeg
vulkaninfo --summary
ls -l /dev/dri
getent group video
getent group render
~~~

La RX 5500 XT doit apparaître dans les périphériques Vulkan. Ne poursuis pas si seul llvmpipe (rendu logiciel CPU) est listé.

### 3. Configurer les GID

~~~bash
cp .env.example .env
VIDEO_GID=$(getent group video | cut -d: -f3)
RENDER_GID=$(getent group render | cut -d: -f3)
sed -i "s/^VIDEO_GID=.*/VIDEO_GID=$VIDEO_GID/" .env
sed -i "s/^RENDER_GID=.*/RENDER_GID=$RENDER_GID/" .env
~~~

Si le groupe render n'existe pas, vérifie les groupes disponibles avec ls -ln /dev/dri et utilise le GID propriétaire de renderD*. Même vérification pour video.

### 4. Démarrer Uscale

Depuis le dossier du projet :

~~~bash
docker compose up -d --build
docker compose logs -f uscale
~~~

Ouvre ensuite http://ADRESSE_IP_DU_NAS:27000.

Le premier contrôle GPU peut télécharger l'image Video2X (plusieurs Go selon les couches déjà présentes).

## Utilisation

1. Choisis le dossier source et le dossier de destination dans l'explorateur.
2. Choisis la hauteur source maximale (240, 360, 480 ou 720).
3. Choisis la hauteur de sortie (480, 720 ou 1080).
4. Sélectionne Real-ESRGAN Plus pour les vidéos générales ou le modèle Anime pour l'animation.
5. Clique sur **Analyser les vidéos** pour compter les fichiers éligibles.
6. Clique sur **Lancer le traitement** pour les placer dans la file.
7. Suis les tâches dans la file et ouvre les logs en cas de problème.

Les sorties conservent l'arborescence relative du dossier source. Une vidéo déjà présente à la destination est ignorée. La base SQLite évite de remettre dans la file un fichier inchangé déjà traité.

## Paramètres et fonctionnement

- Video2X effectue la reconstruction IA avec Real-ESRGAN en facteur ×4.
- FFmpeg ajuste ensuite la hauteur à la cible demandée (720p par défaut), conserve le ratio d'image et encode en H.264 dans un conteneur MKV.
- L'audio et les sous-titres sont copiés depuis le fichier original lorsque les codecs le permettent.
- Le CRF règle la qualité H.264 (valeur basse = meilleure qualité et fichier plus gros).
- Un seul job est traité à la fois pour limiter la pression sur la RX 5500 XT.
- Le worker reprend les tâches en attente après un redémarrage du conteneur.
- Les fichiers temporaires sont supprimés après réussite ou échec.

## Sécurité

**Important :** le conteneur Uscale utilise le socket Docker de l'hôte pour lancer les conteneurs Video2X. L'accès au socket Docker équivaut pratiquement à un accès administrateur sur l'hôte. L'interface n'intègre pas d'authentification : ne l'expose pas directement sur Internet. Utilise-la uniquement sur un réseau local de confiance ou place-la derrière un reverse proxy avec authentification. Ne publie pas le port 27000 sur Internet.

L'explorateur est limité à la racine configurée (/data) et refuse les liens symboliques pour empêcher de sortir de cette racine.

## Dépannage

### Docker indisponible dans l'interface

~~~bash
docker compose logs --tail=200 uscale
ls -l /var/run/docker.sock
~~~

### GPU non détecté

~~~bash
vulkaninfo --summary
ls -l /dev/dri
docker run --rm --device /dev/dri ghcr.io/k4yt3x/video2x:6.4.0 --list-gpus
~~~

Vérifie les pilotes Mesa/Vulkan sur l'hôte et les GID configurés dans .env.

### Une tâche échoue

Ouvre **Logs** sur la ligne concernée, ou consulte :

~~~bash
docker compose logs -f uscale
~~~

### Réinitialiser l'historique

Arrête Uscale puis supprime le volume Docker uscale_uscale-state (cela supprime la file et les journaux, pas les médias) :

~~~bash
docker compose down
docker volume rm uscale_uscale-state
~~~

## Références

- [Video2X — documentation CLI](https://docs.video2x.org/running/command-line.html)
- [Video2X — conteneur Docker](https://docs.video2x.org/running/container.html)
- [Video2X — GitHub](https://github.com/k4yt3x/video2x)
