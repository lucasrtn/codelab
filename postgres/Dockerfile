# Image Postgres de CodeLab : l'officielle, plus un entrypoint versionne.
#
# Elle n'existe que pour sortir le shell du docker-compose.yml. ZimaOS
# reecrit le compose a l'import et desechappe les "$$" en "$" ; Docker
# Compose interpole alors ces variables avant le shell, et le script recoit
# des chaines vides. Un fichier copie dans l'image ne traverse jamais cette
# reecriture.
#
# Montee de version Postgres : changer le tag ci-dessous, puis pousser un
# tag Git -- les quatre images sortent ensemble, comme toujours. Attention,
# un saut de version majeure demande une migration du repertoire de donnees
# (pg_upgrade ou dump/restore) : ce n'est pas un simple changement de tag.
FROM postgres:18

COPY postgres/entrypoint.sh /usr/local/bin/codelab-entrypoint.sh
RUN chmod +x /usr/local/bin/codelab-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/codelab-entrypoint.sh"]
CMD ["postgres"]
