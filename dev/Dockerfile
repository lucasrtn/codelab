FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y \
        openssh-server \
        git \
        curl \
        ca-certificates \
        sudo \
        python3 \
        python3-pip \
        python3-venv \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Utilisateur de développement
RUN useradd -m -s /bin/bash vscode \
    && usermod -aG sudo vscode \
    && echo "vscode ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/vscode \
    && chmod 0440 /etc/sudoers.d/vscode

# Préparation SSH
RUN mkdir -p /run/sshd \
    && mkdir -p /home/vscode/.ssh \
    && chown -R vscode:vscode /home/vscode/.ssh \
    && chmod 700 /home/vscode/.ssh

# Pas d'authentification SSH par mot de passe
RUN sed -i \
      's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' \
      /etc/ssh/sshd_config \
    && sed -i \
      's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' \
      /etc/ssh/sshd_config

# Clé SSH publique injectée au démarrage
RUN printf '#!/bin/sh\n\
set -e\n\
mkdir -p /home/vscode/.ssh\n\
chown vscode:vscode /home/vscode/.ssh\n\
chmod 700 /home/vscode/.ssh\n\
if [ -n "$SSH_PUBLIC_KEY" ] && [ ! -f /home/vscode/.ssh/authorized_keys ]; then\n\
  printf "%s\\n" "$SSH_PUBLIC_KEY" > /home/vscode/.ssh/authorized_keys\n\
  chown vscode:vscode /home/vscode/.ssh/authorized_keys\n\
  chmod 600 /home/vscode/.ssh/authorized_keys\n\
fi\n\
exec "$@"\n' > /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

WORKDIR /workspace

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
