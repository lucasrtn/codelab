FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /opt/codelab/app-manager

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

COPY app-manager/app.py /opt/codelab/app-manager/app.py

EXPOSE 9001

CMD ["python", "/opt/codelab/app-manager/app.py"]
