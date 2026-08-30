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
leurs logs.

**Dagster** : `http://<IP-ZimaOS>:3000/` — charge `/workspace/definitions.py` comme code Dagster.

**Mot de passe Postgres brut** (pour un client SQL externe comme DBeaver) :
```bash
docker exec codelab-postgres cat /var/lib/codelab/config/postgres_password
```

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

## Organisation du depot

```text
codelab/
├── docker-compose.yml
├── .github/workflows/build-images.yml
├── dev/           # SSH + VS Code Remote-SSH — voir dev/README.md
├── dagster/       # orchestration de jobs — voir dagster/README.md
└── app-manager/   # deploiement d'applications — voir app-manager/README.md
```

Ce README couvre l'installation et l'usage global. Le fonctionnement interne de chaque service (scripts de
demarrage, variables d'environnement, pieges connus) est documente dans son propre `README.md`.
