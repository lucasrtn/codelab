#!/bin/sh
# Entrypoint commun a codelab-dagster (webserver) et codelab-dagster-daemon.
# Deux roles :
#   1. Lire le mot de passe Postgres partage (fichier depose par codelab-init)
#      et l'exposer comme variable d'environnement DAGSTER_PG_PASSWORD, car
#      dagster.yaml ne sait lire un secret que depuis une env var, pas un
#      fichier.
#   2. Amorcer /opt/dagster/home et /workspace au tout premier demarrage,
#      sans jamais ecraser ce que l'utilisateur a deja modifie.
set -e

if [ -n "${DAGSTER_PG_PASSWORD_FILE}" ] && [ -f "${DAGSTER_PG_PASSWORD_FILE}" ]; then
  DAGSTER_PG_PASSWORD="$(cat "${DAGSTER_PG_PASSWORD_FILE}")"
  export DAGSTER_PG_PASSWORD
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
