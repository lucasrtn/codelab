FROM python:3.13-slim

ARG DAGSTER_VERSION=1.13.7

WORKDIR /opt/dagster

ENV DAGSTER_HOME=/opt/dagster/home
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir \
    "dagster==${DAGSTER_VERSION}" \
    "dagster-webserver==${DAGSTER_VERSION}" \
    "dagster-postgres==${DAGSTER_VERSION}" \
    "dagster-docker==${DAGSTER_VERSION}"

RUN mkdir -p /opt/dagster/home

# Copies "par defaut", jamais utilisees directement : entrypoint.sh ne les
# deploie que si /workspace (volume utilisateur) ou DAGSTER_HOME (volume
# d'etat) sont vides, au tout premier demarrage.
COPY dagster/definitions.py /opt/dagster/definitions.default.py
COPY dagster/dagster.yaml /opt/dagster/dagster.yaml.default
COPY dagster/entrypoint.sh /opt/dagster/entrypoint.sh
RUN chmod +x /opt/dagster/entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/opt/dagster/entrypoint.sh"]
CMD ["dagster-webserver", "-h", "0.0.0.0", "-p", "3000", "-m", "definitions"]
