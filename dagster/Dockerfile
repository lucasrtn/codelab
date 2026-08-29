FROM python:3.13-slim

ARG DAGSTER_VERSION=1.13.7

ENV PYTHONUNBUFFERED=1 DAGSTER_HOME=/opt/dagster/home

WORKDIR /opt/dagster

RUN pip install --no-cache-dir "dagster==${DAGSTER_VERSION}" "dagster-webserver==${DAGSTER_VERSION}"

RUN mkdir -p /opt/dagster/home /workspace

EXPOSE 3000

CMD ["dagster-webserver", "-h", "0.0.0.0", "-p", "3000", "-m", "definitions"]
