FROM python:3.13-slim

WORKDIR /opt/dagster

RUN pip install --no-cache-dir dagster dagster-webserver

COPY dagster_code /opt/dagster/code

EXPOSE 3000

CMD ["dagster-webserver", "-h", "0.0.0.0", "-p", "3000", "-w", "/opt/dagster/code/workspace.yaml"]