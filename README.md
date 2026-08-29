# Codelab

Architecture Docker Compose pour Codelab.

## Services

- `dev` : environnement de développement SSH sur `2222`.
- `postgres` : base PostgreSQL.
- `dagster` : interface Dagster sur `3000`.
- `dagster-daemon` : daemon Dagster.
- `app-manager` : gestionnaire d'applications sur `9001`.

## Organisation

```text
codelab/
├── docker-compose.yml
├── definitions.py
├── dev/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── runner.py
├── app-manager/
│   ├── Dockerfile
│   └── app.py
└── dagster/
    └── Dockerfile
```

Aucun `workspace.yaml` ou `dagster.yaml` n'est utilisé.

## Secrets

La clé SSH publique n'est jamais stockée dans un Dockerfile ni dans Git.

Sur la ZimaOS :
```bash
export SSH_PUBLIC_KEY="$(cat /DATA/AppData/codelab/config/ssh_public_key)"
docker compose up -d --build
```

Le mot de passe PostgreSQL est lu depuis :
`/DATA/AppData/codelab/config/postgres/password`

## Dagster

Le workspace hôte est monté dans `/workspace`. Le fichier `/workspace/definitions.py` est chargé comme module Python `definitions`.
