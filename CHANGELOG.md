# Changelog

Toutes les évolutions notables sont consignées ici. Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/)
et le projet utilise le [versionnage sémantique](https://semver.org/lang/fr/).

## [2.8.0] — 2026-11-08

Version de sécurité : durcissement de l'interface, des téléchargements et de la distribution.

### Ajouté

- Licence **AGPL-3.0**, fichiers `LICENSE` et `THIRD_PARTY_NOTICES.md`, copiés dans les images.
- `CHANGELOG.md`, `SECURITY.md` et workflow de publication d'une GitHub Release sur tag `vX.Y.Z`.
- En-têtes de sécurité HTTP (CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`).
- Limitation des tentatives d'authentification Basic (429 après 15 échecs en 5 minutes).
- Bandeau d'avertissement dans l'interface quand aucun mot de passe n'est configuré.
- `UPSCALE_ALLOW_PRIVATE_MODEL_URLS` pour autoriser explicitement un miroir de modèles sur le LAN.
- Contre-pression de la découpe : `SIGSTOP`/`SIGCONT` pour borner les segments en attente.
- Ré-estimation de l'espace disque d'après le débit réellement mesuré des segments encodés.

### Modifié

- **Sans `UPSCALE_PASSWORD`, l'interface passe en lecture seule** : les actions (POST) sont refusées avec
  un message explicite. `UPSCALE_ALLOW_ANONYMOUS=1` rétablit l'ancien comportement.
- `install.sh` génère désormais un mot de passe aléatoire et l'affiche à la fin de l'installation.
- La protection CSRF n'accepte plus `Host`/`X-Forwarded-Host` fournis par le client : l'origine doit être
  le même site que l'hôte de la requête, et cet hôte doit être une IP, `localhost` ou figurer dans
  `UPSCALE_TRUSTED_HOSTS`. Un DNS rebinding ne suffit plus.
- Les valeurs booléennes sont validées strictement (`"false"` n'active plus `replace_original`).
- Les téléchargements de modèles refusent les adresses privées, loopback, link-local et réservées, et
  revérifient chaque redirection.
- Les sous-processus de conversion n'héritent plus des secrets du conteneur, s'exécutent sous « nobody »
  quand le conteneur est root (`setpriv`), et sont tués avec leurs enfants en cas de dépassement de délai.
- L'annulation d'une tâche terminée renvoie 409 au lieu d'un faux succès.
- `Restaurer l'original` déplace l'original avant de supprimer la version upscalée (opération réversible).
- Estimation d'espace initiale réduite en mode découpe/IA simultanées, désormais cohérente avec la
  contre-pression.
- Conteneurs : `read_only`, `cap_drop`, `tmpfs`, `stop_grace_period`, rotation des journaux Docker.
- CI : permissions en lecture seule et annulation des exécutions obsolètes.

### Corrigé

- Course possible entre la suppression d'une tâche et sa réservation par le worker.
- Fuite de secrets vers les sous-processus de conversion de modèles.
- Processus enfants du convertisseur laissés en vie après un délai dépassé.

## [2.7.0] — 2026-11-03

- Interface des modèles IA, versions figées du convertisseur, modèles personnalisés via upscayl-bin.
