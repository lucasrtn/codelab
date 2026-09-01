#!/bin/sh
# Entrypoint commun a codelab-dagster (webserver) et codelab-dagster-daemon.
# Deux roles :
#   1. Lire le mot de passe Postgres dans credentials.env -- le fichier unique
#      de secrets CodeLab -- et l'exposer en DAGSTER_PG_PASSWORD, car
#      dagster.yaml ne sait lire un secret que depuis une env var.
#   2. Amorcer /opt/dagster/home et /workspace au tout premier demarrage,
#      sans jamais ecraser ce que l'utilisateur a deja modifie.
set -e

ENV_FILE="${CODELAB_ENV_FILE:-/var/lib/codelab/config/credentials.env}"

# Ces deux services ont "depends_on: codelab-postgres: service_healthy", donc
# credentials.env est deja ecrit quand on arrive ici. L'attente couvre le cas
# ou quelqu'un lance le conteneur seul, sans la stack.
i=0
while [ "$i" -lt 30 ]; do
  if [ -r "$ENV_FILE" ] && grep -q '^POSTGRES_PASSWORD=' "$ENV_FILE"; then
    break
  fi
  i=$((i + 1))
  sleep 1
done

if [ -r "$ENV_FILE" ]; then
  # tail : la derniere occurrence fait autorite (bloc reecrit en fin de fichier).
  DAGSTER_PG_PASSWORD="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$ENV_FILE" | tail -n 1)"
  export DAGSTER_PG_PASSWORD
fi
if [ -z "${DAGSTER_PG_PASSWORD}" ]; then
  echo "[codelab] POSTGRES_PASSWORD introuvable dans $ENV_FILE -- la connexion" \
       "a la base va echouer." >&2
fi

mkdir -p "${DAGSTER_HOME}"
if [ ! -f "${DAGSTER_HOME}/dagster.yaml" ]; then
  cp /opt/dagster/dagster.yaml.default "${DAGSTER_HOME}/dagster.yaml"
  echo "[codelab] dagster.yaml initialise dans ${DAGSTER_HOME} (stockage Postgres)."
fi

if [ ! -f /workspace/definitions.py ]; then
  cp /opt/dagster/definitions.default.py /workspace/definitions.py
  echo "[codelab] /workspace/definitions.py absent : exemple copie depuis l'image."
fi

exec "$@"
