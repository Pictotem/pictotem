# Pictotem — édition Windows

Application pour borne pictotem : caméra USB/webcam, interface plein écran locale, galerie distante, QR code, impression via l'imprimante Windows par défaut, stockage des emails exportable. Se lance via **`run.bat`**, sans installation préalable de Python : un interpréteur Python portable (distribution "embeddable" officielle) est téléchargé et configuré automatiquement au premier lancement, dans `python-embed\` à côté de l'application. L'interface s'affiche dans une **fenêtre native** (pywebview / WebView2) — aucune application navigateur (Edge, Chrome...) n'est requise, seul le runtime Microsoft Edge WebView2, préinstallé sur Windows 10/11 à jour.

## Prérequis matériel
- PC Windows 10/11
- Caméra embarquée ou USB
- Écran tactile, ou pilotage clavier/souris
- Espace disque disponible : application < 1 Mo (hors `python-embed\`, `ffmpeg\` et les données générées par l'usage — photos, vidéos)

## Arborescence
- `run.bat` : point d'entrée, à double-cliquer
- `run.ps1` : orchestration réelle du lancement (appelé par `run.bat`), journalise tout dans `logs\launcher.log`
- `setup_python.ps1` : prépare Python portable (appelé automatiquement par `run.ps1` si besoin)
- `setup_ffmpeg.ps1` : prépare ffmpeg portable (appelé automatiquement par `run.ps1` si besoin — non bloquant si ça échoue, seule la capture vidéo en dépend)
- `python-embed\` : Python + dépendances, généré au premier lancement (rien à committer/livrer manuellement)
- `ffmpeg\` : ffmpeg.exe (+ ffprobe.exe si présent), généré au premier lancement
- `app/` : code source Flask (templates, static, logique)
- `config/config.toml` : configuration principale
- `config/message.txt` : message post-capture
- `data/` : base de données, photos, vidéos, emails, exports (généré au premier lancement)
- `pack/` (optionnel) : pack de cadres/accueil/démarrage chargé automatiquement à chaque lancement — voir « Pack de démarrage » ci-dessous
- `logs/` : logs (généré au premier lancement)
  - `logs\launcher.log` : déroulé de chaque lancement (préparation Python, démarrage, erreurs de démarrage) — à consulter en premier en cas de souci, y compris si l'application plante avant même d'avoir pu écrire son propre log
  - `logs\app.log` : log applicatif Flask (routes, caméra, impression...), une fois l'application effectivement démarrée

## Utilisation (poste final)
1. Copier tout le dossier `Dev\` (ou son contenu déployé) sur le PC cible — pas besoin d'installer Python ni ffmpeg au préalable.
2. Double-cliquer sur `run.bat`.
   - Au tout premier lancement : téléchargement et configuration de Python portable + installation des dépendances (Flask, OpenCV...) dans `python-embed\`, puis téléchargement de ffmpeg portable dans `ffmpeg\` (nécessaire uniquement pour la capture vidéo — si ce téléchargement échoue, l'application démarre quand même, seule la vidéo sera indisponible). **Connexion internet requise une seule fois.**
   - Les lancements suivants démarrent directement, entièrement hors-ligne.
   - Une fenêtre native s'ouvre automatiquement en plein écran (sans barre d'adresse ni menus, aucun navigateur externe) — voir `ui.kiosk_mode` ci-dessous pour désactiver.
3. Adapter `config/config.toml` si besoin : `server.base_url`, `camera.device` (index webcam, 0 par défaut), `print.printer_name` (vide = imprimante par défaut Windows), `ui.kiosk_mode` (false = fenêtre normale redimensionnable, pratique en développement). Un redémarrage de `run.bat` suffit.

## Mot de passe du back office
L'accès au back office (`/admin`) est protégé par un mot de passe défini dans `config/config.toml` :
```toml
[admin]
password = "changeme"
```
La valeur par défaut **doit être changée avant tout usage réel** — n'importe qui la connaissant pourrait accéder à l'administration. Éditez `password` dans la section `[admin]` et relancez `run.bat`.

Le même principe s'applique aux deux autres accès protégés par mot de passe, dans la section `[auth]` : `main_password` (interface principale, si activée en accès distant) et `gallery_password` (galerie). Les trois sont indépendants et à changer séparément.

Alternative sans toucher au fichier : variables d'environnement `PICTOTEM_ADMIN_PASSWORD`, `PICTOTEM_MAIN_PASSWORD` ou `PICTOTEM_GALLERY_PASSWORD`, prioritaires sur `config.toml`.

## Upload invités (partage depuis smartphone)
Permet aux invités d'envoyer leurs propres photos (prises sur leur téléphone) pour qu'elles rejoignent le diaporama `/bestof` — sans jamais apparaître dans la galerie ni dans la table des captures officielles de la borne. Désactivé par défaut, à activer depuis `/admin/guest-uploads`.

Fonctionnement :
- Un lien de partage `/share/<token>` (avec QR code imprimable) donne accès à une page d'envoi simple depuis un téléphone — glisser-déposer ou sélection de fichiers, plusieurs photos à la fois.
- Le `token` est un secret régénérable en un clic depuis le back office : c'est la seule barrière d'accès (pas de mot de passe, pensé pour un scan QR rapide en évènement). Régénérer le lien invalide immédiatement l'ancien.
- Quand l'upload est activé, un bouton « Ajoutez vos photos » apparaît automatiquement dans la galerie (`/gallery`) — sans autre impact sur la galerie ni le best-of existants.
- **Modération** : activable/désactivable. Activée par défaut, les photos envoyées restent « en attente » jusqu'à validation dans `/admin/guest-uploads` avant de rejoindre `/bestof`.
- **Quota** : nombre maximum de photos par invité (identifié par cookie, indépendant de la modération), configurable.
- **Sécurité/vie privée** : chaque photo est revalidée côté serveur (rejet des fichiers qui ne sont pas de vraies images, quelle que soit leur extension), limitée en taille, et systématiquement ré-encodée — ce qui supprime au passage toutes les métadonnées EXIF (dont la géolocalisation GPS parfois présente dans les photos de smartphone) avant stockage et diffusion publique. Un anti-abus limite aussi le nombre d'envois par adresse IP par minute.
- Formats acceptés : JPG, PNG, WEBP (les iPhone récents envoient du HEIC par défaut, mais Safari le convertit automatiquement en JPEG lors d'un envoi via un formulaire web classique).

## Recommencer, Photo strip
- **Recommencer** : sur l'écran de relecture (photo, vidéo ou photo strip), un bouton « Recommencer » supprime la capture qui vient d'être prise et relance immédiatement une nouvelle prise dans le même mode — évite d'accumuler des essais ratés dans la galerie.
- **Photo strip** : mode de capture supplémentaire (bouton dédié sur l'accueil, activable/désactivable via `capture.photo_strip.enabled` dans `config.toml`) qui prend plusieurs photos à la suite (`shots`, 3 par défaut) et les assemble en une seule bande verticale (`background_color`, `gap_px`). Le résultat est une capture « photo » comme une autre : imprimable, votable, visible dans la galerie et le best-of, sans aucune configuration supplémentaire.

## Galerie officielle + uploads invités
Quand l'upload invités est activé **et** que « Inclure dans la galerie officielle » est coché dans `/admin/guest-uploads`, la galerie (`/gallery`) affiche aussi les photos invités approuvées, avec un filtre supplémentaire **Toutes / Officielles / Invités**. Les photos invités y sont signalées par un badge « Invité » (coin de la vignette, ne recouvre pas l'image) et ne sont pas votables. Ce comportement est entièrement désactivé par défaut : la galerie reste inchangée tant que ces deux réglages ne sont pas explicitement activés. Le même badge apparaît sur le diaporama `/bestof` pour les photos invités qui y sont diffusées.

## Tableau de bord admin
La page `/admin` affiche désormais un tableau de bord : compteurs (photos, vidéos, emails, uploads invités en attente/publiés), usage disque détaillé par dossier (`data/photos`, `data/photos_raw`, `data/videos`, etc.) avec l'espace libre restant sur le disque, et le statut de ffmpeg/imprimante/caméra. Deux actions de nettoyage y sont disponibles : vider les fichiers bruts (sauvegardes pré-cadre, sans risque) et purger les captures officielles plus anciennes qu'un nombre de jours donné (irréversible).

## Pack de démarrage (cadres, accueil)
Pour préparer le thème d'un événement sans repasser par l'admin à chaque fois : déposez un dossier `pack\` à la racine (à côté de `run.bat`), contenant un `pack.json` et les images qu'il référence. À **chaque lancement**, l'application le recharge automatiquement (avant l'ouverture du navigateur) — modifiable/remplaçable à tout moment, il suffit de relancer `run.bat`.

Format `pack.json` :
```json
{
  "name": "Thème anniversaire",
  "welcome": "Accueil.png",
  "default": "cadre-1",
  "frames": [
    { "filename": "Cadre_1.png", "id": "cadre-1", "label": "Cadre 1", "sort_order": 10 },
    { "filename": "Cadre_2.png", "id": "cadre-2", "label": "Cadre 2", "sort_order": 20 }
  ]
}
```
- `welcome` (optionnel) : cadre affiché en overlay sur l'écran d'accueil — PNG avec transparence.
- `default` (optionnel) : identifiant du cadre sélectionné par défaut.
- `frames` : liste des cadres — chaque `filename` doit être un PNG avec transparence, présent dans le même dossier que `pack.json` (ou un sous-dossier).

C'est le même format que l'import ZIP de l'admin (`/admin/frames` → « Importer un pack ») — un ZIP de pack peut d'ailleurs simplement être décompressé tel quel dans `pack\`.

## Développement
```powershell
cd app
..\python-embed\python.exe app.py
```
(ou avec votre propre installation Python locale, si vous en avez une, pour un cycle de développement plus rapide qu'avec l'interpréteur portable).

Flask sert toujours une vraie interface HTTP normale en arrière-plan (la fenêtre native n'est qu'un client parmi d'autres) : vous pouvez donc aussi ouvrir `http://127.0.0.1/` dans un navigateur classique en parallèle si vous préférez ses outils de développement.

## URLs
- Borne locale : `http://127.0.0.1/` (ou `http://127.0.0.1:8080/` si le port 80 est indisponible)
- Galerie distante : `http://<IP_DU_PC>/gallery`
- Export emails CSV : `/admin/exports/emails.csv`
- Export emails JSON : `/admin/exports/emails.json`

## Remarques
- L'application n'envoie pas d'emails ; elle ne fait que les collecter.
- L'impression n'est disponible que pour les photos, via `mspaint /pt` (imprimante par défaut ou nommée dans `config.toml`).
- Si le port 80 est déjà occupé (IIS, Skype...), l'application bascule automatiquement sur le port 8080.
- Au démarrage, l'interface s'affiche dans une fenêtre native (pywebview, WebView2 sous Windows) en plein écran (`ui.kiosk_mode = true` par défaut) — aucun navigateur externe requis. `kiosk_mode = false` donne une fenêtre normale redimensionnable, avec DevTools accessibles par clic droit (pratique en développement).
- Prérequis système : le runtime **Microsoft Edge WebView2**, préinstallé sur Windows 11 et Windows 10 à jour. S'il manque, `run.bat` affiche une erreur explicite au démarrage avec le lien de téléchargement (petit composant Microsoft, pas un navigateur à installer).
- Pour quitter le mode kiosque : Alt+F4.
- `ffmpeg` est téléchargé et installé automatiquement (portable, build "essentials" gyan.dev, ~90 Mo, une seule fois) dans `ffmpeg\` par `setup_ffmpeg.ps1`, nécessaire uniquement pour la capture vidéo (transcodage, overlay) — la photo fonctionne sans. Si le téléchargement échoue (pas de connexion, etc.), l'application démarre quand même ; voir `logs\launcher.log`.
- Pour forcer une réinstallation propre de l'environnement Python, supprimez simplement le dossier `python-embed\` et relancez `run.bat` (idem pour `ffmpeg\` afin de forcer un nouveau téléchargement de ffmpeg).
- En cas d'échec au démarrage (Python, dépendances, ou plantage immédiat de l'application), `run.bat` garde la fenêtre ouverte et affiche l'erreur ; le détail complet est aussi conservé dans `logs\launcher.log`.
