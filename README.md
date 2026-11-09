<p align="center">
  <img src="logo.svg" width="96" alt="GitHub Hub">
</p>

<h1 align="center">GitHub Hub</h1>

<p align="center">
  Découvre les projets GitHub du moment, puis installe-les, lance-les et mets-les à jour en un clic.<br>
  Une appli de bureau Linux, sans compte ni abonnement.
</p>

---

## Ce qu'il fait

- **Découvrir**
  - 🚀 **Gros buzz** : les dépôts qui explosent, classés par étoiles gagnées par jour.
  - 🔥 **Tendances** par catégorie : Jeux vidéo, IA, Création, Sécurité, Outils, Développement.
  - 🔎 **Recherche sur tout GitHub**, avec tri, langage et filtres en un clic (`stars:>1000`, `topic:emulator`…).
- **Installer en un clic** : le hub télécharge le dépôt (clone partiel) puis le compile avec la bonne recette (Cargo, npm, CMake, Make…).
- **Lancer**, avec le mode de ton choix :
  - 🖥️ **Fenêtre** : l'interface graphique de l'appli ;
  - ⌨️ **Terminal** : dans Konsole ;
  - 🧩 **Dans le hub** : un formulaire pour les outils en ligne de commande, avec sélecteur de fichiers ;
  - 🌐 **Version web**, quand le projet en propose une.
- **Mettre à jour** un projet ou **tout mettre à jour**. Tes modifications locales sont mises de côté et réappliquées, et le projet n'est recompilé que si son code a changé.
- **Panneau Activité** : la progression et le journal de chaque téléchargement ou compilation.
- **❤ Favoris**, gardés même quand un projet n'est plus en tendance.
- **👤 Mes dépôts** : tes propres projets GitHub, privés compris, lus avec l'outil `gh` déjà connecté (filtres publics, privés et forks).
- Fiche de détails pour chaque projet : README complet, licence, forks, activité.

## Prérequis

| Outil | À quoi il sert |
|---|---|
| Python 3.11+ et **PySide6** (avec QtWebEngine) | La fenêtre de l'appli |
| `git` | Télécharger et mettre à jour les dépôts |
| `cargo`, `npm`, `cmake` + `ninja`, `make` | Facultatifs : seulement pour compiler les projets qui en ont besoin |
| `konsole` ou `xterm`, `kdialog` ou `zenity` | Facultatifs : le mode Terminal et le choix de fichiers |
| `gh` (GitHub CLI), connecté avec `gh auth login` | Facultatif : la rubrique « Mes dépôts » |

Sur Arch / CachyOS :

```bash
sudo pacman -S pyside6 qt6-webengine git
```

## Lancer

```bash
git clone https://github.com/BiasedMonkey/github-hub
cd github-hub
./lancer.sh
```

Pour l'ajouter au menu des applications (KDE, GNOME…) :

```bash
./installer-raccourci.sh
```

## Où vont les fichiers

Tout est rangé dans `~/GitHubHub` (modifiable avec la variable `HUB_DIR`) :

- `repos/` : les projets téléchargés et compilés ;
- `favorites.json` : tes favoris ;
- `cache.json` : les infos GitHub mises en cache pendant 6 h.

## Limite de GitHub

Sans compte, l'API de recherche de GitHub accepte 10 recherches par minute. Le hub met les résultats en cache et réessaie tout seul. Pour lever la limite, lance-le avec un jeton personnel :

```bash
GITHUB_TOKEN=ton_jeton ./lancer.sh
```

## Sécurité

- **« Ma sélection »** (`catalog.json`) ne contient que des dépôts officiels vérifiés, avec leur recette de compilation.
- Installer un projet **hors de cette sélection** demande une confirmation : le hub compile et exécute du code téléchargé. Méfie-toi des « cheats », « boosters FPS » et « cracks ».
- Le serveur interne n'écoute que sur `127.0.0.1`, et refuse les requêtes venant d'autres sites.

## Structure

| Fichier | Rôle |
|---|---|
| `app.py` | La fenêtre de bureau (PySide6 + QtWebEngine) |
| `server.py` | Toute la logique : GitHub, git, compilation, lancement, API interne |
| `index.html` | L'interface |
| `catalog.json` | La sélection de projets et leurs recettes |
| `logo.svg` | Le logo |
| `lancer.sh`, `installer-raccourci.sh` | Le lancement et le raccourci du menu |

On peut aussi lancer `python3 server.py` seul, pour déboguer dans un navigateur sur http://127.0.0.1:8765.
