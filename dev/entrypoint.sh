#!/bin/bash
# Entrypoint de codelab-dev. Quatre roles :
#
#   1. Cles hote SSH persistantes. Elles sont regenerees par apt au moment du
#      build : sans les persister, elles changent a chaque recreation du
#      conteneur (reinstall, mise a jour d'image), ce qui fait echouer VS Code
#      Remote-SSH (MitmPortForwardingDisabled). Generees une seule fois dans
#      CODELAB_SSH_DIR/host_keys, recopiees vers /etc/ssh a chaque demarrage.
#
#   2. authorized_keys. sshd lit directement le fichier du volume (directive
#      AuthorizedKeysFile posee ci-dessous), il n'y a donc plus qu'un seul
#      endroit ou vivent les cles SSH : CODELAB_SSH_DIR. La cle de
#      SSH_PUBLIC_KEY est AJOUTEE si absente, jamais substituee au fichier :
#      une cle ajoutee a la main ou par un demarrage precedent n'est jamais
#      perdue, et plusieurs appareils peuvent cohabiter.
#
#   3. Identifiants Postgres dans les shells SSH. Un sshd ne fait pas heriter
#      ses sessions de l'environnement du process qui l'a lance (comportement
#      OpenSSH normal), donc on les ecrit dans /etc/profile.d (shells de
#      login) et on source ce fichier depuis ~/.bashrc (shells interactifs
#      non-login, dont Remote-SSH de VS Code).
#
#   4. Ne jamais bloquer le demarrage. Ce service n'a volontairement pas de
#      "depends_on: service_healthy" (ZimaOS laisse le conteneur en "Created"
#      si Postgres tarde a l'installation), donc credentials.env peut ne pas
#      encore exister au premier boot : on l'attend brievement, puis on
#      demarre quand meme. SSH reste utilisable sans la base.
set -e

SSH_DIR="${CODELAB_SSH_DIR:-/var/lib/codelab/ssh}"
HOST_KEYS_DIR="$SSH_DIR/host_keys"
AUTHORIZED_KEYS="$SSH_DIR/authorized_keys"
ENV_FILE="${CODELAB_ENV_FILE:-/var/lib/codelab/config/credentials.env}"
PROFILE=/etc/profile.d/codelab-pg.sh
BASHRC=/home/vscode/.bashrc

# ------------------------------ cles SSH ------------------------------

mkdir -p "$HOST_KEYS_DIR"
chmod 700 "$SSH_DIR" "$HOST_KEYS_DIR"

for t in rsa ecdsa ed25519; do
    if [ ! -f "$HOST_KEYS_DIR/ssh_host_${t}_key" ]; then
        ssh-keygen -q -t "$t" -f "$HOST_KEYS_DIR/ssh_host_${t}_key" -N ''
        echo "[codelab-dev] cle hote $t generee dans $HOST_KEYS_DIR."
    fi
done
cp -f "$HOST_KEYS_DIR"/ssh_host_*_key "$HOST_KEYS_DIR"/ssh_host_*_key.pub /etc/ssh/
chmod 600 /etc/ssh/ssh_host_*_key

touch "$AUTHORIZED_KEYS"
chmod 600 "$AUTHORIZED_KEYS"
if [ -n "${SSH_PUBLIC_KEY:-}" ] && ! grep -qxF "$SSH_PUBLIC_KEY" "$AUTHORIZED_KEYS"; then
    printf '%s\n' "$SSH_PUBLIC_KEY" >> "$AUTHORIZED_KEYS"
    echo "[codelab-dev] cle publique ajoutee a $AUTHORIZED_KEYS."
fi

# Pose la directive au demarrage plutot qu'au build : le chemin vient de
# CODELAB_SSH_DIR, sshd et l'entrypoint ne peuvent donc pas diverger.
sed -i '/^AuthorizedKeysFile /d' /etc/ssh/sshd_config
echo "AuthorizedKeysFile $AUTHORIZED_KEYS" >> /etc/ssh/sshd_config

# -------------------------- identifiants Postgres --------------------------

# Tout ce bloc est une commodite : il pre-remplit PGHOST/PGPASSWORD/... dans
# les shells SSH. Il est appele plus bas de maniere non fatale -- une erreur
# ici ne doit jamais empecher sshd de demarrer, sinon une base indisponible
# couperait aussi l'acces SSH, c'est-a-dire le moyen d'aller la reparer.
configure_pg_profile() {

# Attente bornee : 30 s suffisent largement a codelab-postgres pour ecrire son
# bloc, et si le fichier n'arrive jamais on demarre quand meme sans PGPASSWORD.
for _ in $(seq 1 30); do
    if [ -r "$ENV_FILE" ] && grep -q '^POSTGRES_PASSWORD=' "$ENV_FILE"; then
        break
    fi
    sleep 1
done

PG_PASSWORD=""
if [ -r "$ENV_FILE" ]; then
    # tail : la derniere occurrence fait autorite (bloc reecrit en fin de fichier).
    PG_PASSWORD="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$ENV_FILE" | tail -n 1)"
fi
if [ -z "$PG_PASSWORD" ]; then
    echo "[codelab-dev] POSTGRES_PASSWORD introuvable dans $ENV_FILE :" \
         "les shells SSH demarreront sans PGPASSWORD."
fi

{
    echo '# Genere par codelab-dev au demarrage -- ne pas editer.'
    if [ -n "${PGHOST:-}" ]; then echo "export PGHOST=$(printf '%q' "$PGHOST")"; fi
    if [ -n "${PGPORT:-}" ]; then echo "export PGPORT=$(printf '%q' "$PGPORT")"; fi
    if [ -n "${PGDATABASE:-}" ]; then echo "export PGDATABASE=$(printf '%q' "$PGDATABASE")"; fi
    if [ -n "${PGUSER:-}" ]; then echo "export PGUSER=$(printf '%q' "$PGUSER")"; fi
    if [ -n "$PG_PASSWORD" ]; then echo "export PGPASSWORD=$(printf '%q' "$PG_PASSWORD")"; fi
} > "$PROFILE"
chmod 644 "$PROFILE"

# Une seule ligne de source, ajoutee une fois. L'ancienne version concatenait
# le contenu du profil dans .bashrc a chaque demarrage : le fichier grossissait
# d'un jeu d'exports a chaque "docker restart".
touch "$BASHRC"
if ! grep -qxF ". $PROFILE" "$BASHRC"; then
    printf '\n. %s\n' "$PROFILE" >> "$BASHRC"
fi
chown vscode:vscode "$BASHRC"

}

configure_pg_profile || echo "[codelab-dev] identifiants Postgres non pre-remplis" \
    "dans les shells SSH -- le service demarre quand meme."

# ------------------------------- demarrage -------------------------------

mkdir -p /run/sshd
/usr/sbin/sshd
exec "$@"
