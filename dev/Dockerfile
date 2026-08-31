FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/codelab/dev

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-server \
        git \
        curl \
        ca-certificates \
        sudo \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash vscode \
    && chown -R vscode:vscode /home/vscode

COPY dev/requirements.txt /opt/codelab/dev/requirements.txt

RUN pip install --no-cache-dir -r /opt/codelab/dev/requirements.txt

COPY dev/runner.py /opt/codelab/dev/runner.py
COPY dev/entrypoint.sh /opt/codelab/dev/entrypoint.sh
RUN chmod +x /opt/codelab/dev/entrypoint.sh

RUN mkdir -p /run/sshd \
    && sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config

EXPOSE 22

# Toute la logique de demarrage (cles SSH, identifiants Postgres) vit dans
# dev/entrypoint.sh : un script versionne et testable, plutot qu'une ligne de
# shell echappee dans un CMD.
ENTRYPOINT ["/opt/codelab/dev/entrypoint.sh"]
CMD ["python", "/opt/codelab/dev/runner.py"]
