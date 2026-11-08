# Changelog

Toutes les évolutions notables sont consignées ici. Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/)
et le projet utilise le [versionnage sémantique](https://semver.org/lang/fr/).

## [2.15.1] — 2026-11-08

### Corrigé

- Les **aperçus sont visibles dans la file d'attente** : badge « Aperçu », priorité, progression, logs,
  arrêt et suppression, sans boutons de déplacement réservés aux upscales.
- Les aperçus en cours apparaissent aussi sur le tableau de bord ; les compteurs « File d'attente » et
  « En cours » les incluent.

## [2.15.0] — 2026-11-08

### Ajouté

- **Remplacer un original déjà traité** par sa sortie du dossier de destination :
  - action **Remplacer l'original** sur chaque tâche terminée dans la file ;
  - action groupée **Remplacer les originaux** pour toutes les tâches terminées.
  L'original part dans la corbeille (`trash_days`, restaurable) et la sortie prend sa place dans le
  dossier source en gardant son nom. Si `trash_days=0`, l'original est supprimé.
- Les tâches déjà remplacées ne proposent plus l'action ; l'opération est sans effet sur une sortie absente.

## [2.14.0] — 2026-11-08

### Modifié

- **Analyse beaucoup plus rapide** : les ffprobe manquants sont lancés en parallèle
  (`UPSCALE_SCAN_WORKERS`, 4 par défaut) au lieu d'un par un.
- Les fichiers déjà connus, déjà produits, ou dont la sortie existe ne sont plus sondés du tout ;
  le cache de dimensions est préchargé en une requête.
- La progression affiche la phase « Lecture des métadonnées » avec son avancement, au lieu de rester à 100 %.

## [2.13.0] — 2026-11-08

### Ajouté

- **Miniatures extraites de la vidéo source** (`GET /api/jobs/<id>/thumbnail`) : affichées dans la tâche
  en cours, « À suivre » et « Derniers résultats », avec repli visuel si la vidéo n'est plus accessible.
- **Thème visuel chaleureux** : barre latérale claire, fond crème, cartes arrondies, icônes de statistiques,
  boutons et champs plus doux, bandeau mode réseau local allégé.

### Modifié

- Les miniatures sont mises en cache dans le volume d'état et nettoyées par la maintenance quand la tâche
  n'existe plus.

## [2.12.0] — 2026-11-08

### Ajouté

- **Glisser-déposer** dans « À suivre » du tableau de bord pour réordonner la file
  (`POST /api/jobs/<id>/move` accepte désormais un index).
- **Analyse d'un dossier parent** : tous les sous-dossiers sont parcourus, les dossiers source
  imbriqués sont fusionnés (le plus large suffit) et la destination peut être un sous-dossier de la
  source (elle est exclue du scan).
- Message d'analyse précisant quand les sous-dossiers sont exclus (`Inclure tous les sous-dossiers`).

### Corrigé

- Analyse robuste des grandes arborescences : dossiers illisibles et liens symboliques ignorés sans
  faire échouer tout le scan ; parcours `os.walk` avec élagage des dossiers internes et de la destination.
- Le sélecteur de dossier source retire les sous-dossiers déjà couverts par le parent choisi.

## [2.11.1] — 2026-11-08

### Corrigé

- `update.sh` force la recréation du conteneur (`--force-recreate`) et vérifie que la version démarrée
  correspond bien à `VERSION` ; sinon il s'arrête avec la commande de secours.
- L'HTML et les assets statiques sont servis en `Cache-Control: no-cache` : après une mise à jour, le
  navigateur ne peut plus conserver l'ancienne interface.

## [2.11.0] — 2026-11-08

### Ajouté

- **Nouvelle interface « console »** : navigation par vues (tableau de bord, bibliothèque, file, aperçu,
  modèles, réglages, automatisation, système), rail de statut persistant, carte « Tâche en cours » avec
  progression, segment, ETA et actions, prochaines tâches et derniers résultats, barre d'action fixe.
- **Suppression des aperçus** depuis l'historique de l'onglet Aperçu : retire l'entrée et ses fichiers
  (bouton désactivé pendant un aperçu en cours).

### Modifié

- Les réglages sont séparés de la bibliothèque ; la file n'est plus en bas d'une page unique.
- Mise en page responsive et barre latérale compacte sur mobile.

## [2.10.0] — 2026-11-08

### Ajouté

- **Vérification des mises à jour dans l'interface** (Système) : interroge GitHub Releases, affiche la
  version disponible, le lien vers les notes et la commande `update.sh` à copier. Cache de 30 minutes,
  désactivable avec `UPSCALE_UPDATE_CHECK=0`.

### Modifié

- Documentation : variable `UPSCALE_UPDATE_CHECK`, procédure de mise à jour.

## [2.9.0] — 2026-11-08

### Ajouté

- **Mode réseau local** : sans mot de passe, l'interface est en lecture seule par défaut ; un bandeau
  (et **Système → Accès**) permet d'autoriser les actions en un clic, mémorisé en base et réversible.
  `UPSCALE_ALLOW_ANONYMOUS=1` force le mode ; `UPSCALE_PASSWORD` reste prioritaire.
- Proposition d'interface 3.0 (`docs/UI-PROPOSAL.md`) avec maquette interactive et captures
  (`docs/ui-mockup.html`, `docs/ui-dashboard.png`, `docs/ui-queue.png`, `docs/ui-preview.png`).
- Release : le corps de la GitHub Release reprend la section courante du CHANGELOG.

### Modifié

- Le message de lecture seule renvoie vers le mode réseau local au lieu de demander un redémarrage.
- Documentation : mode sans mot de passe, captures et lien vers la proposition d'interface.

## [2.8.0] — 2026-11-08

Version de sécurité : durcissement de l'interface, des téléchargements et de la distribution.

### Ajouté

- Licence **AGPL-3.0**, fichiers `LICENSE` et `THIRD_PARTY_NOTICES.md`, copiés dans les images.
- `CHANGELOG.md`, `SECURITY.md` et workflow de publication d'une GitHub Release sur tag `vX.Y.Z`.
- En-têtes de sécurité HTTP (CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`).
- Limitation des tentatives d'authentification Basic (429 après 15 échecs en 5 minutes).
- `update.sh` : mise à jour sans perdre le segment en cours (pause, sauvegarde de la base, reprise).
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
- Fichiers partiels (`tmp_*`, `ai_*`) nettoyés à la reprise d'une tâche interrompue.

## [2.7.0] — 2026-11-03

- Interface des modèles IA, versions figées du convertisseur, modèles personnalisés via upscayl-bin.
