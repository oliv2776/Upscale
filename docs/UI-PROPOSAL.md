# Proposition d'interface — Upscale 3.0

> Objectif : passer d'une **page de présentation** à une **console d'exploitation** lisible en un coup d'œil,
> utilisable au quotidien sur un NAS, en desktop comme sur mobile.
>
> **Statut (2.13)** : thème chaleureux et miniatures vidéo ajoutés ; l'itération 1 est implémentée (vues, rail de statut, tableau de bord, carte tâche
> active, barre d'action fixe) ainsi que la suppression des aperçus. Les itérations 2 à 4 restent à faire.
>
> Maquette interactive : [`ui-mockup.html`](ui-mockup.html) (ouvrir dans un navigateur).
> Captures : [`ui-dashboard.png`](ui-dashboard.png), [`ui-queue.png`](ui-queue.png), [`ui-preview.png`](ui-preview.png).

## 1. Diagnostic de l'interface actuelle

L'interface actuelle est soignée (thème clair, accent rouge, police Manrope, cartes) mais elle est organisée
comme une **page marketing** puis comme un **formulaire géant**, pas comme un outil de suivi.

| Constat | Effet |
|---|---|
| 8 sections empilées sur une seule page (jusqu'à ~4 000 px de haut) | Il faut faire défiler pour voir la file, située en section 08 |
| Le *hero* « Donnez une nouvelle dimension à vos vidéos » occupe ~260 px en haut | L'espace le plus précieux ne montre ni la tâche en cours ni l'état du GPU |
| La file est en bas, après Réglages, Modèles, Aperçu et Automatisation | L'écran le plus consulté est le moins accessible |
| Les 4 compteurs (en attente / en cours / terminées / erreurs) n'affichent pas la tâche active | Impossible de répondre à « où en est mon film ? » sans descendre |
| Les réglages (sources, qualité, encodage, profils) sont mêlés aux actions | Beaucoup de scroll avant « Lancer le traitement » |
| ~25 champs visibles simultanément, aides en petits textes | Charge cognitive élevée, erreurs de configuration |
| `--muted: #858b9b` sur blanc ≈ **3,4:1** | Sous le seuil WCAG AA (4,5:1) pour le texte courant |
| Boutons icônes avec `title` seulement, modales sans piège de focus | Accessibilité clavier/lecteur d'écran insuffisante |
| Tableau à 5 colonnes, pas de recherche ni de filtres par dossier/modèle | Difficile à utiliser sur mobile et sur une grosse bibliothèque |
| Actions destructrices (remplacer l'original, supprimer) confirmées par `confirm()` natif | Pas d'annulation, pas de contexte |
| La barre latérale ne montre que la version | Aucun statut persistant quand on est en bas de page |

## 2. Principes directeurs

1. **Une seule question en haut de l'écran : « que fait la machine ? »** La tâche active, sa progression, son
   ETA et les actions pause/arrêt doivent être visibles sans défiler.
2. **Séparer opérer et configurer.** Le quotidien (file, bibliothèque, aperçu) ne doit pas côtoyer les 25
   réglages. La configuration devient un espace dédié, avec un bouton « Enregistrer » collant.
3. **Une action primaire par écran.** Tableau de bord → « Ajouter des vidéos » ; Bibliothèque → « Mettre en
   file » ; Aperçu → « Générer ».
4. **Progressive disclosure.** Les options avancées (RIFE, VAAPI, CRF, rendu personnalisé) sont repliées
   derrière « Options avancées » avec des valeurs par défaut sûres.
5. **Tout état a un retour visuel.** Chargement (skeleton), vide (message + action), erreur (bandeau + logs),
   succès (toast), action longue (progression).
6. **Le LAN n'est pas une erreur.** Le mode sans mot de passe est un choix explicite, présenté comme tel, et
   réversible en un clic (voir §6).

## 3. Architecture de l'information

```
Tableau de bord   → état global, tâche active, à suivre, derniers résultats
Bibliothèque      → dossiers source, analyse, vidéos éligibles, mise en file
File              → tâches en attente/en cours/terminées, réordonnancement, logs
Aperçu            → comparateur avant/après plein écran
Modèles           → catalogue, installation, vérification, licences
Réglages          → sortie, qualité, encodage, profils, automatisation, intégrations
Système           → santé, GPU, disque, sauvegardes, mise à jour, accès réseau
```

La navigation latérale reste fixe ; un **rail de statut** en haut reste visible au scroll :

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  File 12   ·   En cours 1   ·   GPU 62 % / 58 °C   ·   1,8 To libres   ·  ⏸ │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 4. Écran par écran

### 4.1 Tableau de bord (écran d'accueil)

```
┌ Sidebar ┬ Topbar : Tableau de bord            [Rechercher]  [+ Ajouter des vidéos] ┐
│         ├ Rail : file · en cours · GPU · disque · pause                            │
│         ├──────────────────────────────────────┬───────────────────────────────────┤
│         │ Tâche en cours (grande carte)        │ À suivre (3)                      │
│         │ Dune (2021) · 480p → 1080p · Plus ×2 │ ○ Film A · 720p · en attente      │
│         │ ████████████████░░░░  63 %           │ ○ Film B · 1080p · en attente     │
│         │ segment 18/42 · reste 1 h 12 · 1,4×  │ ○ Série C · 720p · en attente     │
│         │ [Pause] [Voir les logs] [Détails]    │                                   │
│         ├──────────────────────────────────────┼───────────────────────────────────┤
│         │ Derniers résultats                   │ Système                           │
│         │ ✓ Film X · 1080p · il y a 2 h        │ GPU 62 % · 58 °C · 4,2/8 Go       │
│         │ ⚠ Film Y · avertissement             │ Disque 1,8 To libres              │
│         │ ✓ Film Z · 720p · il y a 5 h         │ Débit 1,4× temps réel             │
└─────────┴──────────────────────────────────────┴───────────────────────────────────┘
```

- La carte « Tâche en cours » contient le nom, la résolution source → sortie, le modèle, la progression, le
  segment, l'ETA, la vitesse et les actions.
- « À suivre » permet de réordonner par glisser-déposer.
- « Derniers résultats » propose « Comparer » (ouvre l'aperçu) et « Ouvrir le dossier ».
- En l'absence de tâche : carte vide avec « Analyser ma bibliothèque » et « Choisir un aperçu ».

### 4.2 Bibliothèque

- Deux cartes : **Sources** (liste, ajout, espace libre par disque) et **Destination**.
- Bouton « Analyser » → résultats en ligne : nombre de vidéos, éligibles, ignorées (déjà traitées, en cours
  de copie), avec filtres (dossier, résolution, codec, durée).
- Tableau des vidéos éligibles avec cases à cocher, colonne « sortie prévue », « modèle appliqué (profil) »,
  et bouton « Mettre en file la sélection ».
- Le mode « remplacer les originaux » est une zone distincte, avec un avertissement visuel et un choix de
  corbeille ; il n'est jamais mêlé aux réglages de qualité.

### 4.3 File de traitement

- Barre d'outils : recherche, filtres avec **compteurs**, « Rafraîchir automatiquement » (pause du polling
  quand l'onglet est caché), « Tout mettre en pause ».
- Lignes compactes : poignée de réordonnancement, vignette/nom, résolution → sortie, puce modèle, statut,
  barre de progression + ETA, menu d'actions (logs, relancer, restaurer, supprimer).
- Sélection multiple → actions groupées (pause, relancer, supprimer).
- Les erreurs affichent la première ligne utile et un bouton « Logs » ; le journal s'ouvre dans un panneau
  latéral (pas une modale plein écran) pour garder la file visible.

### 4.4 Aperçu

- Formulaire en 3 champs alignés : vidéo, début, durée ; puis sélection des modèles sous forme de puces.
- Comparateur **plein écran** (au moins 60 % de la hauteur), avec poignée, lecture synchronisée, raccourcis
  clavier (espace = lecture, ←/→ = image par image), et libellés lisibles.
- Variantes en onglets : « Votre configuration », « Plus ×2 », « Peau normale »… ; un clic change la variante
  de droite.
- Historique des aperçus en bas, avec vignette et date.

### 4.5 Modèles

- Catalogue en cartes : nom, facteur, architecture, taille, licence, badge « vérifié (PSNR) », bouton
  « Utiliser ».
- Installation : un champ lien + un bouton, avec progression étape par étape (téléchargement, conversion,
  vérification) et journal repliable.
- Import local dans un panneau secondaire, pas un `<details>` perdu en bas de page.

### 4.6 Réglages

Sous-onglets : **Sortie** · **Qualité** · **Encodage** · **Profils** · **Automatisation** · **Intégrations**.

- Barre « Enregistrer » collante en bas, avec indicateur « modifications non enregistrées ».
- Chaque champ a une aide courte (1 ligne) et une infobulle pour le détail.
- Les options avancées sont repliées ; le CRF affiche la valeur et son équivalent par encodeur.
- Les profils par dossier sont un tableau éditable avec validation en ligne (chemin, modèle, sortie).
- Les intégrations (ntfy, Gotify, webhook, Jellyfin, Plex, Ollama) ont un bouton « Tester » et un état
  connecté/déconnecté, comme aujourd'hui, mais regroupées.

### 4.7 Système

- Santé : Docker, GPU Vulkan, Video2X, moteur ncnn, disque, base.
- Accès réseau : mode sans mot de passe (activer / repasser en lecture seule), adresse d'écoute, hôtes de
  confiance.
- Maintenance : sauvegarde de la base, export des réglages, journal, mise à jour.
- Le bouton « Tester le GPU » reste, avec un état de progression.

## 5. Design system

| Élément | Proposition |
|---|---|
| Couleurs | Fond `#f5f6f8`, surfaces blanches, encre `#14161f`, muted `#5f6675` (AA), accent rouge `#c81e2b`, succès `#0f766e`, alerte `#b45309` |
| Typographie | Manrope ; échelle 12 / 13 / 14 / 16 / 20 / 28 / 36 ; titres 600-700, corps 400-500 |
| Espacement | Base 4 px : 4, 8, 12, 16, 24, 32, 48 |
| Rayons | 10 px (champs), 14 px (cartes), 999 px (puces) |
| Ombres | Une seule élévation : `0 1px 2px rgba(16,24,40,.06), 0 8px 24px rgba(16,24,40,.06)` |
| Composants | Bouton primaire/secondaire/texte, puce de statut, barre de progression, carte, ligne de file, onglets, panneau latéral, toast, skeleton, modale de confirmation |
| Statuts | En attente (gris), En cours (rouge), Terminé (vert), Erreur (ambre), Annulé (gris barré) — avec texte, pas seulement la couleur |
| Mouvement | 150-200 ms, `prefers-reduced-motion` respecté |

## 6. Mode réseau local (sans mot de passe)

Le besoin est légitime sur un NAS domestique. Le compromis proposé :

- par défaut, sans mot de passe, l'interface reste en **lecture seule** (sécurité contre une exposition
  accidentelle) ;
- un bandeau explicite propose **« Autoriser sur mon réseau »** ;
- le choix est mémorisé en base (survit aux redémarrages), réversible depuis **Système** ;
- `UPSCALE_ALLOW_ANONYMOUS=1` force l'accès, `UPSCALE_PASSWORD` reprend la main dès qu'il est défini.

C'est déjà implémenté dans cette version (voir le bandeau et le bloc « Accès » dans Système).

## 7. Accessibilité et mobile

- Contraste AA (4,5:1) pour le texte, 3:1 pour les grands titres et les icônes.
- `aria-label` sur tous les boutons icônes, `role="dialog"` + focus trap + `Échap` pour les modales,
  `aria-live="polite"` pour les toasts et la progression.
- Navigation clavier complète ; raccourcis : `/` recherche, `p` pause/reprise, `n` nouvel ajout, `?` aide.
- Mobile : navigation en barre inférieure (5 entrées), cartes au lieu du tableau, comparateur plein écran,
  actions principales en bas d'écran.
- Tableaux : première colonne figée, défilement horizontal maîtrisé, ou bascule en cartes sous 720 px.

## 8. Plan d'implémentation

| Itération | Contenu | Risque |
|---|---|---|
| 1 (1-2 j) | Tableau de bord + rail de statut + carte tâche active + bandeau LAN | Faible : réutilise `/api/jobs` |
| 2 (2-3 j) | Bibliothèque + file + panneau de logs + filtres/compteurs | Moyen : nouveaux endpoints de recherche |
| 3 (2-3 j) | Réglages en sous-onglets + profils + aperçu plein écran + a11y | Moyen : refonte du formulaire |
| 4 (1 j) | Mobile, raccourcis, tests visuels, captures | Faible |

Les endpoints existants suffisent pour l'itération 1. Les itérations suivantes peuvent ajouter
`GET /api/library` (fichiers éligibles paginés) et `GET /api/jobs?since=` (SSE) pour éviter le polling.

## 9. Indicateurs de succès

- Temps pour lancer un traitement après ouverture : **< 30 s** (aujourd'hui : plusieurs écrans de scroll).
- Temps pour répondre à « où en est la tâche ? » : **0 scroll**.
- Taux d'erreurs de configuration : suivi des 400 sur `/api/config`.
- Utilisation mobile : largeur < 720 px sans tableau illisible.
- Accessibilité : 0 violation axe critique sur les 3 écrans principaux.

## 10. Ce que la maquette ne couvre pas

La maquette est statique (données d'exemple) : elle valide la hiérarchie, les espacements et les couleurs,
pas les interactions réelles. Le branchement sur l'API existante se fait dans l'itération 1.
