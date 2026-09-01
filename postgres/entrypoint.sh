#!/bin/sh
# Entrypoint de codelab-postgres.
#
# Genere (ou reprend) le mot de passe Postgres au premier demarrage, l'ecrit
# dans credentials.env -- LE fichier de secrets de CodeLab, aucun autre --
# puis rend la main a l'entrypoint officiel de l'image.
#
# Pourquoi un fichier dans une image plutot qu'un script inline dans le
# docker-compose : ZimaOS reecrit le compose a l'import et desechappe les
# "$$" en "$". Docker Compose interpole alors ces "$VAR" avant que le shell
# ne les voie, et le script recoit des chaines vides ("mkdir: cannot create
# directory ''"). Un script versionne dans l'image ne traverse jamais cette
# reecriture -- c'est la seule facon fiable d'avoir du shell ici.
set -e

CONFIG_DIR="${CODELAB_CONFIG_DIR:-/var/lib/codelab/config}"
ENV_FILE="$CONFIG_DIR/credentials.env"
LEGACY_PW_FILE="$CONFIG_DIR/postgres_password"

mkdir -p "$CONFIG_DIR"
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Lit une cle dans credentials.env (chaine vide si absente).
get_value() {
  # Derniere occurrence : upsert_block reecrit toujours son bloc en fin de
  # fichier, donc une valeur laissee plus haut (edition a la main, ancien
  # format sans marqueurs) est forcement la perimee.
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

# Remplacement par BLOC entier (commentaires inclus), delimite par des
# marqueurs "# ===== NOM =====" / "# ===== /NOM =====" -- pas juste par
# prefixe de cle, sinon les commentaires documentant chaque bloc
# s'accumuleraient en double a chaque redemarrage. Chaque service ne touche
# qu'a son propre bloc, les autres restent intacts quel que soit l'ordre de
# demarrage.
upsert_block() {
  name="$1"; content="$2"
  awk -v s="# ===== $name =====" -v e="# ===== /$name =====" \
    '$0==s{skip=1} !skip{print} $0==e{skip=0}' "$ENV_FILE" > "$ENV_FILE.tmp"
  {
    cat "$ENV_FILE.tmp"
    echo "# ===== $name ====="
    printf '%s\n' "$content" | sed 's/^[[:space:]]*//'
    echo "# ===== /$name ====="
  } > "$ENV_FILE.new"
  mv "$ENV_FILE.new" "$ENV_FILE"
  rm -f "$ENV_FILE.tmp"
}

# Ordre de priorite : la valeur deja presente dans credentials.env fait
# autorite (c'est celle avec laquelle la base a ete initialisee), sinon on
# reprend l'ancien fichier postgres_password d'une install anterieure, sinon
# on genere. Ne JAMAIS regenerer par-dessus une valeur existante : la base
# refuserait la connexion.
PG_PASSWORD="$(get_value POSTGRES_PASSWORD)"
MIGRATED=0
if [ -z "$PG_PASSWORD" ] && [ -s "$LEGACY_PW_FILE" ]; then
  PG_PASSWORD="$(cat "$LEGACY_PW_FILE")"
  MIGRATED=1
fi
if [ -z "$PG_PASSWORD" ]; then
  PG_PASSWORD="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "[codelab-postgres] mot de passe genere."
fi

upsert_block "codelab-header" "# credentials.env -- TOUS les identifiants CodeLab, generes et
# geres automatiquement par les services au demarrage. C'est le seul
# fichier de secrets : rien n'est stocke ailleurs. Ne pas editer a la
# main, chaque bloc est entierement reecrit au redemarrage du service
# concerne. Permissions 600 -- lisible uniquement par root sur le
# disque du ZimaOS."

upsert_block "codelab-postgres" "# Base de donnees partagee par tous les services CodeLab.
# Utilisee par : codelab-dev, codelab-dagster, codelab-dagster-daemon,
# qui lisent POSTGRES_PASSWORD ici meme (plus de fichier dedie).
POSTGRES_HOST=codelab-postgres
POSTGRES_PORT=5432
POSTGRES_DB=codelab
POSTGRES_USER=codelab
POSTGRES_PASSWORD=$PG_PASSWORD"

upsert_block "codelab-dev" "# Pas de mot de passe : l'acces SSH (port 2222) se fait uniquement par
# cle publique. Les cles autorisees et les cles hote sont regroupees
# dans /DATA/AppData/codelab/config/ssh/ (authorized_keys et
# host_keys/) -- des fichiers de cles, pas des valeurs a lister ici."

chmod 600 "$ENV_FILE"

# Migration terminee et ecrite : l'ancien fichier n'a plus de raison
# d'exister. Supprime seulement apres coup, jamais avant.
if [ "$MIGRATED" = "1" ]; then
  rm -f "$LEGACY_PW_FILE"
  echo "[codelab-postgres] postgres_password migre vers credentials.env puis supprime."
fi

# L'image officielle refuse POSTGRES_PASSWORD et POSTGRES_PASSWORD_FILE
# simultanement : on ne passe que la variable, lue du fichier unique.
unset POSTGRES_PASSWORD_FILE
POSTGRES_PASSWORD="$PG_PASSWORD"
export POSTGRES_PASSWORD

exec docker-entrypoint.sh "$@"
