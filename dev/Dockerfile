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
    && mkdir -p /home/vscode/.ssh \
    && chown -R vscode:vscode /home/vscode

COPY dev/requirements.txt /opt/codelab/dev/requirements.txt

RUN pip install --no-cache-dir -r /opt/codelab/dev/requirements.txt

COPY dev/runner.py /opt/codelab/dev/runner.py

RUN mkdir -p /run/sshd \
    && sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config

EXPOSE 22

# Un sshd ne fait PAS heriter ses sessions de l'environnement du process qui
# l'a lance (comportement OpenSSH normal, meme dans ce script). Pour que
# PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD soient disponibles dans un
# shell ouvert via SSH (dont Remote-SSH de VS Code, qui source ~/.bashrc),
# on les ecrit explicitement a deux endroits : /etc/profile.d (shells de
# login) et ~/.bashrc (shells interactifs non-login).
#
# Les cles hote SSH (/etc/ssh/ssh_host_*_key) sont regenerees par apt au
# moment du build : sans les persister, elles changent a chaque
# recreation du conteneur (reinstall, mise a jour d'image...), ce qui
# fait echouer VS Code Remote-SSH (MitmPortForwardingDisabled) a chaque
# fois. On les genere une seule fois dans le volume persistant
# HOST_KEYS_DIR, puis on les recopie vers /etc/ssh a chaque demarrage :
# meme empreinte tant que HOST_KEYS_DIR n'est pas efface.
CMD ["/bin/bash", "-lc", "\
    HOST_KEYS_DIR=/etc/ssh/host_keys; \
    mkdir -p \"$HOST_KEYS_DIR\"; \
    for t in rsa ecdsa ed25519; do \
        if [ ! -f \"$HOST_KEYS_DIR/ssh_host_${t}_key\" ]; then \
            ssh-keygen -q -t \"$t\" -f \"$HOST_KEYS_DIR/ssh_host_${t}_key\" -N ''; \
        fi; \
    done && \
    cp -f \"$HOST_KEYS_DIR\"/ssh_host_*_key \"$HOST_KEYS_DIR\"/ssh_host_*_key.pub /etc/ssh/ && \
    chmod 600 /etc/ssh/ssh_host_*_key && \
    mkdir -p /home/vscode/.ssh && \
    chmod 700 /home/vscode/.ssh && \
    if [ -n \"${SSH_PUBLIC_KEY}\" ]; then \
        printf '%s\\n' \"${SSH_PUBLIC_KEY}\" > /home/vscode/.ssh/authorized_keys; \
    fi && \
    chown -R vscode:vscode /home/vscode/.ssh && \
    chmod 600 /home/vscode/.ssh/authorized_keys 2>/dev/null || true && \
    { \
        [ -n \"${PGHOST}\" ] && echo \"export PGHOST=$(printf '%q' \"${PGHOST}\")\"; \
        [ -n \"${PGPORT}\" ] && echo \"export PGPORT=$(printf '%q' \"${PGPORT}\")\"; \
        [ -n \"${PGDATABASE}\" ] && echo \"export PGDATABASE=$(printf '%q' \"${PGDATABASE}\")\"; \
        [ -n \"${PGUSER}\" ] && echo \"export PGUSER=$(printf '%q' \"${PGUSER}\")\"; \
        if [ -n \"${PGPASSWORD_FILE}\" ] && [ -f \"${PGPASSWORD_FILE}\" ]; then \
            echo \"export PGPASSWORD=$(printf '%q' \"$(cat \"${PGPASSWORD_FILE}\")\")\"; \
        fi; \
        true; \
    } > /etc/profile.d/codelab-pg.sh && \
    cat /etc/profile.d/codelab-pg.sh >> /home/vscode/.bashrc && \
    chown vscode:vscode /home/vscode/.bashrc && \
    /usr/sbin/sshd && \
    exec python /opt/codelab/dev/runner.py \
"]
