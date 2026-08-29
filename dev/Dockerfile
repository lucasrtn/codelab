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
# PGPASSWORD soit disponible dans un shell ouvert via SSH (dont Remote-SSH
# de VS Code, qui source ~/.bashrc), on l'ecrit explicitement a deux
# endroits : /etc/profile.d (shells de login) et ~/.bashrc (shells
# interactifs non-login).
CMD ["/bin/bash", "-lc", "\
    mkdir -p /home/vscode/.ssh && \
    chmod 700 /home/vscode/.ssh && \
    if [ -n \"${SSH_PUBLIC_KEY}\" ]; then \
        printf '%s\\n' \"${SSH_PUBLIC_KEY}\" > /home/vscode/.ssh/authorized_keys; \
    fi && \
    chown -R vscode:vscode /home/vscode/.ssh && \
    chmod 600 /home/vscode/.ssh/authorized_keys 2>/dev/null || true && \
    if [ -n \"${PGPASSWORD_FILE}\" ] && [ -f \"${PGPASSWORD_FILE}\" ]; then \
        PGPASSWORD=\"$(cat \"${PGPASSWORD_FILE}\")\"; \
        { echo \"export PGPASSWORD=$(printf '%q' \"$PGPASSWORD\")\"; } > /etc/profile.d/codelab-pg.sh; \
        { echo \"export PGPASSWORD=$(printf '%q' \"$PGPASSWORD\")\"; } >> /home/vscode/.bashrc; \
        chown vscode:vscode /home/vscode/.bashrc; \
    fi && \
    /usr/sbin/sshd && \
    exec python /opt/codelab/dev/runner.py \
"]
