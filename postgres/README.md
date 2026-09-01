# codelab-postgres

L'image officielle `postgres:18`, plus un entrypoint versionne. La base de donnees partagee par tous les
services CodeLab : `codelab-dev`, `codelab-dagster`, `codelab-dagster-daemon` s'y connectent, et le panneau
`codelab-app-manager` cohabite avec elle dans le meme `credentials.env`.

Pas de port publie : la base n'est joignable que depuis le reseau `codelab`.

## Pourquoi cette image existe

Le script d'initialisation vivait directement dans le `docker-compose.yml`, en `entrypoint:` inline. Deux
problemes se sont succede :

1. Docker Compose interpole les `$VAR` d'un script inline **avant** que le shell ne les voie. `$ENV_FILE`,
   `$name`, `$content` arrivaient vides, le script echouait des `touch ""`, et `set -e` le faisait sortir
   avant de lancer Postgres. Corrige en doublant les `$`.
2. Sauf que ZimaOS **reecrit le compose a l'import** et annule cet echappement. Les `$$` redevenaient `$`,
   Compose les vidait a nouveau, et Postgres repartait en boucle de redemarrage
   (`mkdir: cannot create directory ''`).

Un fichier copie dans une image ne traverse aucune de ces deux reecritures. D'ou la regle du projet :
**aucune logique shell dans le `docker-compose.yml`** — elle vit dans un entrypoint versionne.

## Ce que fait l'entrypoint

Avant de rendre la main a `docker-entrypoint.sh` de l'image officielle :

1. **Resout le mot de passe**, dans cet ordre de priorite :
   - la valeur deja presente dans `credentials.env` (c'est celle avec laquelle la base a ete initialisee) ;
   - sinon l'ancien fichier `config/postgres_password` d'une installation anterieure, qui est ensuite
     supprime (migration) ;
   - sinon une valeur generee (32 octets aleatoires en hexadecimal).

   Une valeur existante n'est **jamais** regeneree par-dessus : la base refuserait la connexion.

2. **Ecrit ses blocs dans `credentials.env`** (`codelab-header`, `codelab-postgres`, `codelab-dev`),
   delimites par des marqueurs `# ===== <nom> =====`. Chaque service ne touche qu'a son propre bloc, les
   autres restent intacts quel que soit l'ordre de demarrage.

3. **Exporte `POSTGRES_PASSWORD`** et retire `POSTGRES_PASSWORD_FILE` — l'image officielle refuse les deux
   en meme temps.

Le fichier est relu a chaque demarrage, et l'operation est idempotente : redemarrer le conteneur ne duplique
aucun bloc et ne change aucune valeur.

## Variables d'environnement

| Variable | Role |
|---|---|
| `CODELAB_CONFIG_DIR` | Dossier de `credentials.env` (defaut `/var/lib/codelab/config`) |
| `POSTGRES_DB`, `POSTGRES_USER` | Lues par l'image officielle, fixees dans le compose |

Pas de `POSTGRES_PASSWORD` ni `POSTGRES_PASSWORD_FILE` dans le compose : l'entrypoint s'en charge.

## Volumes attendus

| Chemin conteneur | Contenu |
|---|---|
| `/var/lib/postgresql` | Donnees de la base |
| `/var/lib/codelab/config` | En lecture-ecriture : c'est ce service qui cree `credentials.env` |

## Monter de version

Changer le tag `FROM` dans `postgres/Dockerfile`, puis pousser un tag Git — les quatre images sortent
ensemble. **Attention** : un saut de version majeure demande une migration du repertoire de donnees
(`pg_upgrade` ou dump/restore). Ce n'est pas un simple changement de tag, et un demarrage sur un repertoire
d'une version anterieure echoue avec un message explicite dans les logs.

## Tester localement

```bash
docker build -f postgres/Dockerfile -t codelab-postgres-test .
docker run --rm -e POSTGRES_DB=codelab -e POSTGRES_USER=codelab \
  -v /tmp/codelab-config:/var/lib/codelab/config codelab-postgres-test
cat /tmp/codelab-config/credentials.env
```
