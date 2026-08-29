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

COPY dagster/definitions.py /opt/dagster/definitions.py

EXPOSE 3000

CMD ["dagster-webserver", "-h", "0.0.0.0", "-p", "3000", "-m", "definitions"]
