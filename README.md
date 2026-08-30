# CodeLab

Environnement de developpement personnel, auto-heberge sur [ZimaOS](https://www.zimaspace.com/zimaos) (compatible CasaOS).
Une seule installation fournit une base de donnees partagee, un acces SSH/VS Code, un orchestrateur de jobs et un
gestionnaire d'applications — installable en un import de `docker-compose.yml` depuis l'interface ZimaOS, sans
commande a executer sur le serveur.

## Services

| Service | Role | Port |
|---|---|---|
| **Postgres** | Base de donnees partagee par tous les autres services | interne uniquement |
| **Dev** | Acces SSH + VS Code Remote-SSH, avec un utilisateur dedie | `2222` |
| **Dagster** | Orchestration et planification de jobs (interface web + daemon) | `3000` |
| **App-manager** | Deploiement et supervision des applications que tu developpes | `9001` |

Zero configuration manuelle apres l'installation : mot de passe de base de donnees genere automatiquement, cles
SSH generees et persistees, et connexion a Postgres deja prete dans l'environnement du conteneur `dev`.

## Installation

1. Dans ZimaOS : **App Store → Install via docker-compose**, coller le contenu de `docker-compose.yml`.
2. ZimaOS demande une valeur pour **`SSH_PUBLIC_KEY`** : ta cle publique SSH (contenu de
   `~/.ssh/id_ed25519.pub` ou equivalent).
3. Le meme ecran liste les volumes avec leurs chemins par defaut
   (`/DATA/AppData/codelab/workspace` et `/DATA/AppData/codelab/postgres`) — modifiables directement la si tu
   veux un autre emplacement.
4. Installer. Mot de passe Postgres, etat interne des services et cles hote SSH sont generes automatiquement au
   premier demarrage.

## Utilisation

**SSH / VS Code**
```bash
ssh vscode@<IP-ZimaOS> -p 2222
```
Ton code vit dans `/workspace`. La connexion Postgres ne demande aucune configuration :
```bash
python3 -c "import psycopg; print(psycopg.connect().execute('SELECT version();').fetchone()[0])"
```

**App-manager** : `http://<IP-ZimaOS>:9001/` — demarrer/arreter tes apps deployees depuis `/workspace`, consulter
leurs logs. Protege par mot de passe, genere automatiquement au premier demarrage (voir ci-dessous pour le
recuperer).

**Dagster** : `http://<IP-ZimaOS>:3000/` — charge `/workspace/definitions.py` comme code Dagster.

**Tous les identifiants au meme endroit** — mot de passe Postgres, mot de passe admin app-manager, cle de
session — sont regroupes dans un fichier lisible directement depuis le disque du ZimaOS, sans `docker exec` :
```bash
cat /DATA/AppData/codelab/config/credentials.env
```
Chaque service y ecrit son propre bloc au demarrage (delimite par des commentaires `# ===== <service> =====`,
documentant a quoi sert chaque valeur) ; rien a taper a la main, rien a chercher dans quel conteneur exec. Le
bloc `codelab-dev` explique pourquoi ce service n'a pas de mot de passe (SSH par cle publique uniquement).

## Persistance des donnees

Tout vit sous `/DATA/AppData/codelab/` sur le disque du ZimaOS (aucun volume Docker nomme) : ca survit a un
redemarrage, une recreation de conteneur et une reinstallation. Seule exception a connaitre : si l'ecran de
desinstallation ZimaOS propose de supprimer les donnees de l'application, il faut decocher cette case pour les
conserver.

## Publication des images (CI)

`.github/workflows/build-images.yml` build et pousse automatiquement les images vers `ghcr.io/lucasrtn/...` a
chaque push sur `main`. **Etape unique a faire a la main** apres le premier run : les packages GitHub sont crees
prives par defaut, donc ZimaOS ne peut pas les tirer tant qu'ils ne sont pas passes en **Public**
(`github.com/lucasrtn?tab=packages` → package → Package settings → Danger Zone → Change visibility).

## Versionner CodeLab (figer une release)

`latest` bouge a chaque push sur `main` — pratique en developpement, risque en production si un changement
casse quelque chose (comme observe pendant ce projet). CodeLab se versionne **comme un tout** : dev, dagster et
app-manager sortent toujours ensemble, sous un seul numero — pas de version separee par service.

```bash
git tag v1.0.0
git push origin v1.0.0
```

Ce tag declenche le workflow, qui reconstruit et publie **les 3 images en meme temps**, toutes avec ce numero :
`ghcr.io/lucasrtn/codelab-dev:1.0.0`, `codelab-dagster:1.0.0`, `codelab-app-manager:1.0.0`. `latest` n'est pas
touche.

**Sur GitHub (sans ligne de commande)** : Releases → Create a new release → tag `v1.0.0` (target `main`) →
Publish release. Gabarit a reprendre :

- **Titre** : `CodeLab v1.0.0`
- **Description** :
  ```
  Version complete de CodeLab : dev, dagster et app-manager.
  Images : ghcr.io/lucasrtn/codelab-{dev,dagster,app-manager}:1.0.0

  Changements :
  - ...
  ```

**Revenir a cette version precise** : dans `docker-compose.yml`, remplacer le tag `:latest` par `:1.0.0` sur les
**3 services** (`codelab-dev`, `codelab-dagster`, `codelab-app-manager`), puis reimporter le compose sur ZimaOS.

**Lister les versions disponibles** :
```bash
git tag -l "v*"
```
ou visuellement sur `github.com/lucasrtn/codelab/releases`, ou sur `github.com/lucasrtn?tab=packages` pour
chaque image individuellement.

## Icone de l'application

Le depot etant prive, `raw.githubusercontent.com/.../icon.png` n'est pas accessible sans authentification —
ZimaOS n'en a pas. L'icone est donc encodee directement dans `docker-compose.yml`
(`x-casaos.icon: "data:image/png;base64,..."`), aucune requete externe n'est necessaire pour l'afficher. Le
fichier source reste `icon.svg`/`icon.png` a la racine du depot, a re-encoder si tu la changes :

```bash
python3 -c "import base64; print('data:image/png;base64,' + base64.b64encode(open('icon.png','rb').read()).decode())"
```

## Organisation du depot

```text
codelab/
├── docker-compose.yml
├── icon.svg / icon.png
├── .github/workflows/build-images.yml
├── dev/           # SSH + VS Code Remote-SSH — voir dev/README.md
├── dagster/       # orchestration de jobs — voir dagster/README.md
└── app-manager/   # deploiement d'applications — voir app-manager/README.md
```

Ce README couvre l'installation et l'usage global. Le fonctionnement interne de chaque service (scripts de
demarrage, variables d'environnement, pieges connus) est documente dans son propre `README.md`.
