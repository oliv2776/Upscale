# Sécurité

## Signaler une vulnérabilité

N'ouvre pas d'issue publique pour une faille exploitable. Utilise l'onglet **Security → Report a
vulnerability** du dépôt GitHub, ou écris à l'auteur du dépôt. Indique la version, la configuration
(mode socket ou local, authentification activée ou non) et les étapes de reproduction.

## Modèle de menace

Upscale est conçu pour un **réseau local de confiance** (NAS, maison). Il ne doit jamais être exposé
directement à Internet : utilise un VPN ou un reverse proxy HTTPS avec authentification.

Points structurels à connaître :

- **Socket Docker** : en mode standard, Upscale monte `/var/run/docker.sock`. Un processus qui contrôle ce
  socket peut obtenir les droits administrateur de l'hôte. C'est nécessaire pour lancer Video2X dans un
  conteneur frère. Le mode `docker-compose.local.yml` supprime ce socket (Video2X est alors embarqué).
  Un proxy de socket limite la surface mais ne constitue pas une vraie frontière de sécurité.
- **Conversion de modèles** : les fichiers `.pth` sont lus avec le dépickleur restreint de spandrel, mais
  les fichiers ONNX et ncnn traversent des parseurs natifs (pnnx, ncnn). La conversion tourne dans le
  conteneur Upscale, avec un environnement nettoyé de ses secrets et, quand le conteneur est root et que
  `setpriv` est disponible, sous l'utilisateur `nobody` (sans accès au socket Docker ni à la base).
  `UPSCALE_CONVERTER_DROP_PRIVILEGES=0` désactive ce repli. N'installe que des modèles de sources connues,
  et préfère le mode sans socket si cette surface te concerne.
- **Téléchargements** : seules les adresses `http(s)` publiques sont acceptées par défaut ; les adresses
  privées exigent `UPSCALE_ALLOW_PRIVATE_MODEL_URLS=1`. Chaque redirection est revérifiée.

## Bonnes pratiques

- Définis toujours `UPSCALE_PASSWORD` (ou `UPSCALE_ALLOW_ANONYMOUS=1` en connaissance de cause).
- Derrière un reverse proxy, conserve l'en-tête `Host` et liste le nom public dans
  `UPSCALE_TRUSTED_HOSTS` ; `X-Forwarded-Host` n'est pas utilisé pour la protection CSRF.
- Garde le port 27000 sur le LAN/VPN, jamais sur Internet.
- Sauvegarde le volume `uscale-state` (file, réglages, historique) avant une mise à jour majeure.
