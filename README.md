# CodeLab

Application ZimaOS (compatible CasaOS) : environnement de developpement
tout-en-un.

## Services

- `codelab-init` : conteneur d'amorcage, s'execute une fois puis s'arrete.
  Genere le mot de passe Postgres au tout premier demarrage.
- `postgres` : base PostgreSQL partagee par tous les services.
- `dev` : environnement SSH + VS Code Remote-SSH sur le port `2222`.
- `dagster` : interface Dagster sur le port `3000`, stockage dans Postgres.
- `dagster-daemon` : daemon Dagster (schedules / sensors).
- `app-manager` : gestionnaire d'applications deployees, sur le port `9001`.

## Installation (ZimaOS uniquement, aucune commande)

1. Dans ZimaOS : **App Store > Install via docker-compose**, coller le
   contenu de `docker-compose.yml`.
2. ZimaOS peut demander trois valeurs avant l'installation (sinon, des
   valeurs par defaut raisonnables s'appliquent) :
   - **SSH_PUBLIC_KEY** : ta cle publique SSH (ex. contenu de
     `~/.ssh/id_ed25519.pub`) — pas de defaut, a fournir.
   - **WORKSPACE_PATH** : l'emplacement de ton code (defaut :
     `/DATA/AppData/codelab/workspace`). Partage entre `dev`, `dagster`,
     `dagster-daemon` et `app-manager`.
   - **POSTGRES_DATA_PATH** : l'emplacement des donnees Postgres (defaut :
     `/DATA/AppData/codelab/postgres`).
3. Installer. Aucun acces SSH ni fichier a preparer a la main : ces trois
   valeurs sont les seules donnees jamais saisies par toi, tout le reste
   (mot de passe Postgres, etat interne Dagster/app-manager, cles hote
   SSH) est genere et gere automatiquement par Docker.

Ces deux chemins sont les seules donnees qui vivent reellement sur le
disque du serveur, sous ton controle explicite. Tout le reste (mot de
passe genere, etat Dagster, registre app-manager, cles hote SSH) est
stocke dans des volumes Docker nommes — rien a gerer manuellement, et rien
qui ne soit pas decrit dans ce `docker-compose.yml`.

## Se connecter

- **SSH / VS Code** : `ssh vscode@<IP-ZimaOS> -p 2222`, ou Remote-SSH avec
  le meme hote/port/utilisateur. `PGPASSWORD` est deja disponible dans le
  shell (variable exportee automatiquement au demarrage de la session).
- **App-manager** : `http://<IP-ZimaOS>:9001/`
- **Dagster** : `http://<IP-ZimaOS>:3000/`

Si tu as besoin du mot de passe Postgres brut (par ex. pour un client SQL
externe comme DBeaver) :
```bash
docker exec codelab-postgres cat /var/lib/codelab/config/postgres_password
```

## Organisation

```text
codelab/
├── docker-compose.yml
├── dev/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── runner.py
├── app-manager/
│   ├── Dockerfile
│   └── app.py
└── dagster/
    ├── Dockerfile
    ├── dagster.yaml       # stockage Postgres (run/event/schedule)
    ├── definitions.py     # exemple, copie dans /workspace au 1er demarrage
    └── entrypoint.sh
```

## Dagster

Le workspace hote est monte dans `/workspace`. `/workspace/definitions.py`
est charge comme module Python `definitions` (via `PYTHONPATH=/workspace`)
— c'est bien le fichier du workspace qui est utilise, pas celui embarque
dans l'image. S'il est absent au premier demarrage, un exemple minimal y
est copie automatiquement pour eviter un crash au boot.

Runs, event logs et schedules sont stockes dans Postgres (`dagster.yaml`,
deploye automatiquement dans `DAGSTER_HOME` au premier demarrage) : toutes
les metadonnees Dagster restent donc accessibles depuis les autres
services, comme le reste des donnees de l'application.

## Publication des images (CI)

`.github/workflows/build-images.yml` build et pousse automatiquement les
3 images (`codelab-dev`, `codelab-dagster`, `codelab-app-manager`) vers
`ghcr.io/lucasrtn/...` a chaque push sur `main` qui touche leur dossier,
en `amd64` + `arm64`. Declenchable aussi a la main via l'onglet Actions
("Run workflow").

**Etape a faire une seule fois, a la main, sur GitHub** (le workflow ne
peut pas le faire lui-meme) : par defaut, `ghcr.io` cree les packages en
**prive**. ZimaOS n'a pas d'identifiants Docker configures, donc un
`docker compose pull` echouera (401) tant que chaque package reste prive.

Apres le premier run du workflow :
1. `github.com/lucasrtn?tab=packages`
2. Ouvrir `codelab-dev`, `codelab-dagster`, `codelab-app-manager` un par un
3. **Package settings > Danger Zone > Change visibility > Public**

A refaire uniquement au tout premier push (une fois public, ca le reste).

## Notes

- Le reseau `codelab_codelab` est cree automatiquement par ZimaOS au
  premier `docker compose up` (il n'est plus declare `external`).
- `WORKSPACE_PATH` et `POSTGRES_DATA_PATH` ont un repli par defaut
  (`/DATA/AppData/codelab/workspace` et `/.../postgres`) : si ZimaOS ne
  te les demande pas explicitement a l'import, l'installation fonctionne
  quand meme avec ces valeurs. Tu peux les changer plus tard en editant
  les variables d'environnement de la stack depuis ZimaOS.
- Remplacer `icon` dans le bloc `x-casaos` du `docker-compose.yml` par
  l'URL definitive d'une icone hebergee dans le repo une fois disponible.
