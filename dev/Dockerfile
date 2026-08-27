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

WORKDIR /workspace

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
