# codelab-app-manager

Panneau de gestion et reverse-proxy pour les applications que tu developpes dans `/workspace`, servi sur un
unique port (`9001`). Autonome : aucune dependance externe hormis Flask, aucun processus de supervision tiers
(pas de Supervisor) — le cycle de vie des applications est gere directement par ce service.

## Ce que fait le service

- Sert un panneau web (`http://<IP-ZimaOS>:9001/`) listant les applications enregistrees, avec un interrupteur
  demarrer/arreter, l'acces aux logs, et un formulaire d'ajout.
- Pour chaque application activee, lance sa commande de demarrage comme sous-processus, sur un port interne
  attribue automatiquement (plage `9101`–`9140`).
- Fait office de **reverse-proxy interne** : `http://<IP-ZimaOS>:9001/<nom-app>/` route vers le port interne de
  l'application correspondante — un seul port a exposer sur ZimaOS, quel que soit le nombre d'applications
  gerees.

## Fichiers

| Fichier | Role |
|---|---|
| `Dockerfile` | Construction de l'image (Flask + requests) |
| `app.py` | Le service complet : API, proxy, interface web |

## Variables d'environnement

| Variable | Role |
|---|---|
| `APP_MANAGER_DIR` | Ou vit `app.py` dans l'image (`/opt/codelab/app-manager`) — lecture seule |
| `APP_MANAGER_STATE` | Ou vivent `apps.json` et les logs (`/var/lib/codelab/app-manager`) — le seul dossier que le service ecrit |
| `APP_MANAGER_ROOT` | Racine du navigateur de dossiers et des chemins d'applications (`/workspace`) |
| `MANAGER_PORT` | Port d'ecoute du panneau lui-meme (`9001`) |

Code et etat sont deliberement separes : ce que tu ne dois jamais editer a la main (`app.py`, l'image) est
distinct de ce que le service produit (`apps.json`, logs) — pas de risque de melanger configuration et donnees
generees en modifiant `APP_MANAGER_DIR` sans toucher `APP_MANAGER_STATE`, ou l'inverse.

## Volumes attendus

| Point de montage | Contenu |
|---|---|
| `/var/lib/codelab/app-manager` | `apps.json` (registre des applications) + `logs/` — seul etat persistant du service |
| `/workspace` | Racine dans laquelle chercher/lancer les applications |

## API

| Route | Methode | Role |
|---|---|---|
| `/health` | GET | Sonde du `HEALTHCHECK` Docker |
| `/api/apps` | GET | Liste des applications enregistrees et leur etat |
| `/api/browse?path=...` | GET | Navigateur de dossiers, borne a `APP_MANAGER_ROOT` |
| `/api/add` | POST | Enregistre une application existante (nom, chemin, commande) |
| `/api/create` | POST | Cree un nouveau projet minimal (`index.html`) et le demarre immediatement |
| `/api/toggle/<nom>` | POST | Demarre ou arrete une application |
| `/api/app/<nom>` | DELETE | Retire une application du registre (le dossier du projet n'est jamais touche) |
| `/api/logs/<nom>` | GET | 120 dernieres lignes du journal de l'application |
| `/<nom>/...` | * | Proxy transparent vers l'application, si elle est demarree |

## Fonctionnement du proxy

`_proxy()` relaie chaque requete (methode, en-tetes hors `HOP` — `connection`, `keep-alive`, etc. —, corps pour
`POST`/`PUT`/`PATCH`) vers `http://127.0.0.1:<port-interne>/<chemin>`, et renvoie la reponse telle quelle. Deux
cas particuliers geres explicitement :

- **Application non demarree** : reponse `503` avec une page d'explication plutot qu'une erreur de connexion
  brute.
- **Application en train de demarrer** : `urllib.request.urlopen` echoue avant que le process interne n'ecoute
  encore sur son port → reponse `502` invitant a reessayer, plutot qu'une erreur cryptique.

Les chemins contenant espaces ou accents sont re-encodes (`urllib.parse.quote(sub, safe="/")`) avant transmission
— necessaires car Flask les livre deja decodes, et une ligne de requete HTTP brute n'accepte ni espace ni
caractere non-ASCII tel quel.

## Cycle de vie d'une application

1. **Attribution du port** : `next_port()` prend le premier port libre dans `9101`–`9140`.
2. **Demarrage** (`start()`) : `subprocess.Popen(["bash", "-lc", <commande>], cwd=<chemin>, env={PORT: ..., ...})`,
   sortie standard et erreur redirigees vers `logs/<nom>.log`. Le process tourne dans son propre groupe
   (`start_new_session=True`), pour permettre un arret propre du groupe entier (pas juste du process racine).
3. **Arret** (`stop()`) : `SIGTERM` au groupe, jusqu'a 3 secondes pour un arret propre, puis `SIGKILL` si
   necessaire.
4. **Reprise au redemarrage du conteneur** (`resume()`) : toute application marquee `enabled: true` dans
   `apps.json` est relancee automatiquement au demarrage du service — l'etat "actif" survit donc a un
   redemarrage du conteneur `codelab-app-manager` lui-meme.

## Developper / tester localement

```bash
docker build -f app-manager/Dockerfile -t codelab-app-manager-test .   # contexte = racine du depot
docker run --rm -p 9001:9001 \
  -v "$PWD/workspace-test:/workspace" \
  -v codelab-app-manager-test-state:/var/lib/codelab/app-manager \
  codelab-app-manager-test
```

Puis ouvrir `http://localhost:9001/` — le bouton **Ajouter** permet de tester le cycle complet (enregistrement,
demarrage, proxy, logs, arret) sur un projet minimal (`python3 -m http.server $PORT` par exemple).
