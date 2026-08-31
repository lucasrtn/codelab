# codelab-dev

Conteneur d'environnement de developpement : serveur SSH + utilisateur dedie, pret pour VS Code Remote-SSH,
avec un acces Postgres deja configure dans l'environnement de session.

## Ce que fait l'image

- Base `python:3.13-slim` + `openssh-server`, `git`, `curl`, `sudo`.
- Un utilisateur `vscode` (UID 1000), sans mot de passe, authentification **uniquement par cle publique**
  (`PasswordAuthentication no`, `PermitRootLogin no`).
- Un script de demarrage (`CMD` du `Dockerfile`) qui, a chaque lancement du conteneur :
  1. Genere les cles hote SSH si elles n'existent pas encore dans le volume persistant, sinon reutilise celles
     deja presentes (voir [Cles hote SSH](#cles-hote-ssh-et-empreinte-stable)).
  2. Ecrit `SSH_PUBLIC_KEY` dans `/home/vscode/.ssh/authorized_keys`.
  3. Exporte les variables de connexion Postgres (`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`) pour
     qu'elles soient disponibles dans toute session SSH interactive (voir
     [Variables Postgres dans une session SSH](#variables-postgres-dans-une-session-ssh)).
  4. Demarre `sshd` en arriere-plan.
  5. Passe la main a `runner.py`, qui ne fait que maintenir le conteneur en vie (`sleep infinity`) — tout le
     travail reel se fait via les sessions SSH, pas dans ce process.

## Fichiers

| Fichier | Role |
|---|---|
| `Dockerfile` | Construction de l'image et script de demarrage |
| `requirements.txt` | Dependances Python installees dans l'image (`dagster`, `dagster-webserver`, `psycopg[binary]`) |
| `runner.py` | Process qui maintient le conteneur actif apres le demarrage de `sshd` |

> `requirements.txt` inclut `dagster`/`dagster-webserver` pour permettre d'inspecter ou de tester du code Dagster
> directement depuis ce conteneur (ex. lancer un job en local avant de le pousser), en plus de l'instance Dagster
> dediee qui tourne dans les conteneurs `codelab-dagster*`.

## Variables d'environnement

| Variable | Origine | Usage |
|---|---|---|
| `SSH_PUBLIC_KEY` | Saisie a l'installation ZimaOS | **Ajoutee** a `authorized_keys` si absente — les cles deja presentes sont conservees |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` | Fixees dans `docker-compose.yml` | Connexion a `codelab-postgres` |
| `CODELAB_ENV_FILE` | Fixee dans `docker-compose.yml` | `credentials.env` : `POSTGRES_PASSWORD` y est lu pour construire `PGPASSWORD` |
| `CODELAB_SSH_DIR` | Fixee dans `docker-compose.yml` | Dossier unique des cles : `authorized_keys` + `host_keys/` |

## Volumes attendus

| Point de montage | Contenu |
|---|---|
| `/workspace` | Ton code — partage avec `codelab-dagster`, `codelab-dagster-daemon` et `codelab-app-manager` |
| `/var/lib/codelab/ssh` | Tout le SSH au meme endroit : `authorized_keys` et `host_keys/` (cote hote : `config/ssh/`) |
| `/var/lib/codelab/config` | Lecture seule — `credentials.env`, d'ou est lu le mot de passe Postgres |

## Cles hote SSH et empreinte stable

`apt-get install openssh-server` genere des cles hote **au moment du build de l'image**. Sans precaution, ces
cles changent a chaque reconstruction d'image (nouvelle version poussee, reinstallation...), ce qui declenche
l'avertissement `REMOTE HOST IDENTIFICATION HAS CHANGED` cote client — et, pire, VS Code Remote-SSH bloque
carrement la connexion (`MitmPortForwardingDisabled`) au lieu de simplement avertir.

Pour eviter ca : au premier demarrage, le script genere les cles hote (`rsa`, `ecdsa`, `ed25519`) dans
`/var/lib/codelab/ssh/host_keys` (mappe sur `/DATA/AppData/codelab/config/ssh/host_keys` cote hote) si elles n'y sont pas deja, puis
les copie vers `/etc/ssh/` avant de lancer `sshd`. Tant que ce dossier n'est pas efface, l'empreinte reste
identique a travers tous les redemarrages et reinstallations.

Si l'empreinte a change malgre tout (premiere migration vers cette version, ou dossier efface manuellement), sur
la machine cliente :

```bash
ssh-keygen -R "[<IP-ZimaOS>]:2222"
ssh vscode@<IP-ZimaOS> -p 2222   # accepter yes a la nouvelle empreinte
```

## Variables Postgres dans une session SSH

`sshd` n'herite **pas** de l'environnement du process qui l'a lance (comportement standard d'OpenSSH, y compris
dans ce script) : les variables `environment:` du `docker-compose.yml` ne sont donc pas automatiquement visibles
dans un shell ouvert via SSH. Le script de demarrage les ecrit explicitement dans deux endroits pour contourner
ca :

- `/etc/profile.d/codelab-pg.sh` — lu par les shells de connexion (login shells)
- `~/.bashrc` de `vscode` — lu par les shells interactifs non-login (le cas le plus courant pour un terminal VS
  Code ou une session `ssh` classique)

Verifier que ca fonctionne, une fois connecte :

```bash
env | grep ^PG
python3 -c "import psycopg; print(psycopg.connect().execute('SELECT version();').fetchone()[0])"
```

`psycopg.connect()` sans argument lit ces variables automatiquement.

## Developper / tester localement

```bash
docker build -f dev/Dockerfile -t codelab-dev-test .   # contexte = racine du depot
docker run --rm -it \
  -e SSH_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" \
  -p 2222:22 \
  codelab-dev-test
```

Sans variables `PG*` ni `credentials.env` accessible, la partie Postgres du script est ignoree : le script
attend le fichier 30 s, log un avertissement, puis demarre `sshd` quand meme. C'est deliberé — une base
indisponible ne doit jamais couper l'acces SSH, qui est justement le moyen d'aller la reparer.
