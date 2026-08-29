FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 VIRTUAL_ENV=/opt/codelab/venv PATH="/opt/codelab/venv/bin:$PATH"
WORKDIR /opt/codelab

RUN apt-get update && apt-get install -y --no-install-recommends openssh-server git ca-certificates && rm -rf /var/lib/apt/lists/* && python -m venv "${VIRTUAL_ENV}" && mkdir -p /run/sshd /workspace /home/vscode/.ssh

COPY dev/requirements.txt /opt/codelab/requirements.txt

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r /opt/codelab/requirements.txt

COPY dev/runner.py /opt/codelab/runner.py

RUN useradd --uid 1000 --create-home --shell /bin/bash vscode && chown -R vscode:vscode /home/vscode /workspace && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config

COPY --chmod=755 <<'EOF' /usr/local/bin/docker-entrypoint.sh
#!/bin/sh
set -eu
mkdir -p /home/vscode/.ssh
chown vscode:vscode /home/vscode/.ssh
chmod 700 /home/vscode/.ssh
if [ -n "${SSH_PUBLIC_KEY:-}" ]; then
  printf '%s\n' "${SSH_PUBLIC_KEY}" > /home/vscode/.ssh/authorized_keys
  chown vscode:vscode /home/vscode/.ssh/authorized_keys
  chmod 600 /home/vscode/.ssh/authorized_keys
fi
ssh-keygen -A
/usr/sbin/sshd
exec "$@"
EOF

EXPOSE 22

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

CMD ["/opt/codelab/venv/bin/python", "/opt/codelab/runner.py"]
