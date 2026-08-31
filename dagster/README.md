# codelab-dagster

Image partagee par les deux services Dagster du compose : `codelab-dagster` (interface web, `dagster-webserver`)
et `codelab-dagster-daemon` (`dagster-daemon run`, execute les schedules et sensors). Meme image, deux commandes
de demarrage differentes — voir `command:` de chaque service dans `docker-compose.yml`.

## Ce que fait l'image

- Installe `dagster`, `dagster-webserver` (memes versions, `DAGSTER_VERSION`), plus `dagster-postgres` et
  `dagster-docker` (versions **non figees** — voir [Versionnement des paquets Dagster](#versionnement-des-paquets-dagster)).
- Embarque une configuration et un exemple de code par defaut (`dagster.yaml.default`,
  `definitions.default.py`), deployes automatiquement au tout premier demarrage si absents — jamais utilises
  directement en fonctionnement normal (voir [entrypoint.sh](#entrypointsh)).

## Fichiers

| Fichier | Role |
|---|---|
| `Dockerfile` | Construction de l'image |
| `entrypoint.sh` | Amorcage au demarrage (voir ci-dessous) |
| `dagster.yaml` | Modele de configuration du stockage Dagster — pointe vers Postgres |
| `definitions.py` | Exemple de code Dagster, copie dans `/workspace` s'il y est absent |

## entrypoint.sh

Execute avant toute commande (`dagster-webserver` ou `dagster-daemon run`), dans cet ordre :

1. **Resout le mot de passe Postgres.** Lit `POSTGRES_PASSWORD` dans `CODELAB_ENV_FILE`
   (`credentials.env`, le fichier unique de secrets ecrit par `codelab-postgres`) et l'exporte en
   `DAGSTER_PG_PASSWORD` — `dagster.yaml` ne sait lire un secret que depuis une variable d'environnement.
   Attend le fichier jusqu'a 30 s, au cas ou le conteneur soit lance seul, hors de la stack.
2. **Deploie `dagster.yaml` dans `DAGSTER_HOME`** s'il n'y est pas deja (premier demarrage uniquement — ne
   jamais ecraser une configuration existante).
3. **Deploie un `definitions.py` d'exemple dans `/workspace`** s'il est absent, pour eviter un crash au boot sur
   une installation neuve avant que tu aies ajoute ton propre code.
4. Passe la main a la commande reelle (`exec "$@"`).

## dagster.yaml : stockage dans Postgres

Sans ce fichier, Dagster utilise du SQLite local (comportement par defaut), ce qui isole ses metadonnees
(runs, event logs, schedules) du reste de l'application et les rend vulnerables a la perte de volume. Ce
`dagster.yaml` configure les trois stockages (`run_storage`, `event_log_storage`, `schedule_storage`) sur la
meme base Postgres partagee que le reste de CodeLab, avec les identifiants lus depuis l'environnement :

```yaml
postgres_db:
  username: {env: DAGSTER_PG_USER}
  password: {env: DAGSTER_PG_PASSWORD}
  hostname: {env: DAGSTER_PG_HOST}
  db_name:  {env: DAGSTER_PG_DB}
  port:     {env: DAGSTER_PG_PORT}
```

## Chargement de `/workspace/definitions.py`

La commande de `codelab-dagster` est `dagster-webserver -h 0.0.0.0 -p 3000 -m definitions` (voir
`Dockerfile`) ; celle de `codelab-dagster-daemon` est `dagster-daemon run -m definitions` (voir
`docker-compose.yml`). Dans les deux cas, `-m definitions` demande a Python de resoudre un **module** nomme
`definitions` — la variable d'environnement `PYTHONPATH=/workspace` (fixee dans le compose) garantit que c'est
bien `/workspace/definitions.py` qui est charge, et non une eventuelle copie embarquee dans l'image.

> **`dagster-daemon run` sans `-m definitions` echoue au demarrage** avec
> `Error: No arguments given and no [tool.dagster] block in pyproject.toml found.` — c'est le meme mecanisme de
> resolution de code que le webserver, l'argument est simplement facile a oublier puisqu'il n'a pas de valeur
> par defaut cote CLI.

## Versionnement des paquets Dagster

Piege classique de l'ecosysteme Dagster : `dagster` et `dagster-webserver` suivent la meme version
(`DAGSTER_VERSION`, ex. `1.13.7`), mais les paquets d'integration (`dagster-postgres`, `dagster-docker`, et plus
generalement tous les `dagster-*` sauf le coeur et le webserver) suivent **leur propre schema de version**,
historiquement decale — `dagster-postgres==1.13.7` n'existe simplement pas sur PyPI.

C'est pourquoi le `Dockerfile` les installe **sans version figee** :

```dockerfile
RUN pip install --no-cache-dir \
    "dagster==${DAGSTER_VERSION}" \
    "dagster-webserver==${DAGSTER_VERSION}" \
    "dagster-postgres" \
    "dagster-docker"
```

`pip` resout alors automatiquement la version de `dagster-postgres`/`dagster-docker` compatible avec la version
exacte de `dagster` installee, via leurs propres contraintes de dependance declarees. Ne jamais figer ces deux
paquets sur `${DAGSTER_VERSION}`.

## Developper / tester localement

```bash
docker build -f dagster/Dockerfile -t codelab-dagster-test .   # contexte = racine du depot

# Webserver seul, sans Postgres (dagster.yaml par defaut ne sera pas utilisable sans base --
# utile uniquement pour verifier que l'image demarre et sert du HTTP)
docker run --rm -p 3000:3000 -v "$PWD/workspace-test:/workspace" codelab-dagster-test
```

Pour un test complet avec stockage Postgres, plus simple de passer par `docker compose up codelab-postgres
codelab-dagster` depuis la racine du depot, avec les variables `DAGSTER_PG_*` deja definies dans
`docker-compose.yml`.
