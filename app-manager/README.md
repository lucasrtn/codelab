# codelab-app-manager

Panneau de controle et reverse-proxy pour les applications que tu developpes dans `/workspace`, servi sur un
unique port (`9001`), protege par mot de passe. Autonome : aucune dependance a Supervisor — le cycle de vie des
applications est gere directement par ce service.

## Ce que fait le service

- Sert un dashboard (`http://<IP-ZimaOS>:9001/`) organise en 3 onglets :
  - **Apps** — cartes des applications enregistrees (icone, statut, metriques CPU/memoire en direct, recherche
    et tri), bouton d'ajout (dossier existant ou modele de demarrage rapide).
  - **Logs** — selecteur d'application + flux de journal en direct (memes SSE que le volet rapide ouvrable
    depuis une carte, juste en plein format).
  - **Parametres** — commande prete a copier pour recuperer le mot de passe admin, et rappel du workspace
    configure.
- Pour chaque application activee, lance sa commande de demarrage comme sous-processus, sur un port interne
  attribue automatiquement (plage `9101`–`9140`).
- Fait office de **reverse-proxy interne** : `http://<IP-ZimaOS>:9001/<nom-app>/` route vers le port interne de
  l'application correspondante — un seul port a exposer sur ZimaOS, quel que soit le nombre d'applications
  gerees.
- Protege l'ensemble du panneau et de son API par une **authentification par mot de passe**, generee
  automatiquement au premier demarrage (meme principe que le mot de passe Postgres de `codelab-postgres`).
  L'onglet Parametres affiche la commande de recuperation directement, avec un bouton copier.

## Fichiers

| Fichier | Role |
|---|---|
| `Dockerfile` | Construction de l'image (Flask, psutil, requests, Node.js) |
| `app.py` | Le service complet : auth, API, proxy, dashboard |

## Authentification

Au tout premier demarrage, un mot de passe admin est genere aleatoirement et ecrit dans
`STATE_DIR/admin_password` (permissions `600`) — jamais defini par toi, jamais dans le compose. Il est aussi
recopie dans un `.env` partage avec `codelab-postgres`, lisible directement depuis le disque du ZimaOS sans
`docker exec` :

```bash
cat /DATA/AppData/codelab/config/.env
```

`upsert_shared_env()` n'ecrit que ses propres cles (`APP_MANAGER_*`) dans ce fichier — les lignes `POSTGRES_*`
deposees par `codelab-postgres` restent intactes, peu importe l'ordre de demarrage des deux services. Voir
[Fichier `.env` partage](#fichier-env-partage) plus bas pour le detail du mecanisme.

Les sessions sont signees avec une cle secrete elle aussi generee et persistee (`STATE_DIR/flask_secret_key`),
donc la connexion survit a un redemarrage du conteneur. Une bascule anti-bruteforce limite les tentatives de
connexion echouees a 5 par tranche de 5 minutes, par adresse IP.

> **Le proxy `/<nom-app>/...` n'est volontairement pas protege par cette authentification** — seuls le dashboard
> et son API le sont. Une application que tu deploies reste directement joignable (utile pour tester un
> webhook, par exemple), independamment du mot de passe du panneau.

## Fichier `.env` partage

`upsert_shared_env()` (dans `bootstrap_secrets()`) ecrit `APP_MANAGER_URL` et `APP_MANAGER_ADMIN_PASSWORD` dans
`/var/lib/codelab/config/.env` (`/DATA/AppData/codelab/config/.env` cote hote), le meme fichier et le meme
volume que `codelab-postgres` utilise pour ses propres identifiants (`POSTGRES_*`). Principe : chaque service ne
touche qu'aux lignes commencant par son propre prefixe, en relisant puis reecrivant le fichier entier a chaque
demarrage — aucun des deux services n'ecrase jamais les lignes de l'autre, quel que soit l'ordre de demarrage.
Si le volume partage n'est pas monte (tests locaux hors compose, par exemple), l'ecriture echoue silencieusement
sans bloquer le demarrage du service — c'est une commodite, pas une dependance critique.

## Variables d'environnement

| Variable | Role |
|---|---|
| `APP_MANAGER_DIR` | Ou vit `app.py` dans l'image (`/opt/codelab/app-manager`) — lecture seule |
| `APP_MANAGER_STATE` | Ou vivent `apps.json`, les logs, le mot de passe admin et la cle de session — le seul dossier que le service ecrit |
| `APP_MANAGER_ROOT` | Racine du navigateur de dossiers et des chemins d'applications (`/workspace`) |
| `APP_MANAGER_SHARED_CONFIG` | Dossier du `.env` partage avec `codelab-postgres` (`/var/lib/codelab/config`) |
| `MANAGER_PORT` | Port d'ecoute du panneau lui-meme (`9001`) |

## Volumes attendus

| Point de montage | Contenu |
|---|---|
| `/var/lib/codelab/app-manager` | `apps.json`, `admin_password`, `flask_secret_key`, `logs/` — tout l'etat persistant du service |
| `/workspace` | Racine dans laquelle chercher/lancer les applications |

## API

| Route | Methode | Auth | Role |
|---|---|---|---|
| `/health` | GET | non | Sonde du `HEALTHCHECK` Docker |
| `/login` | GET / POST | non | Page de connexion / verification du mot de passe |
| `/logout` | POST | oui | Termine la session |
| `/api/apps` | GET | oui | Liste des applications, avec `cpu_percent` et `memory_mb` en direct |
| `/api/browse?path=...` | GET | oui | Navigateur de dossiers, borne a `APP_MANAGER_ROOT` |
| `/api/detect?path=...` | GET | oui | Suggere une commande de lancement a partir du contenu du dossier |
| `/api/add` | POST | oui | Enregistre une application existante (nom, chemin, commande) |
| `/api/create` | POST | oui | Cree un nouveau projet a partir d'un modele (`static`/`flask`/`node`) et le demarre |
| `/api/app/<nom>` | PUT | oui | Modifie le chemin/la commande d'une application **arretee** |
| `/api/toggle/<nom>` | POST | oui | Demarre ou arrete une application |
| `/api/app/<nom>` | DELETE | oui | Retire une application du registre (le dossier n'est jamais touche) |
| `/api/logs/<nom>` | GET | oui | 120 dernieres lignes du journal |
| `/api/logs/<nom>/stream` | GET | oui | Flux de logs en direct (Server-Sent Events) |
| `/api/icon/<nom>` | GET | oui | Icone du projet si trouvee, sinon avatar SVG genere |
| `/<nom>/...` | * | **non** | Proxy transparent vers l'application, si elle est demarree |

## Auto-detection de la commande de lancement

`detect_command()` inspecte le dossier choisi et propose une commande sans jamais l'imposer (le champ reste
editable dans le formulaire) :

| Indice trouve | Commande suggeree |
|---|---|
| `package.json` avec `scripts.start` | `npm start` |
| `package.json` avec `main` | `node <main>` |
| `manage.py` | `python3 manage.py runserver 0.0.0.0:$PORT` |
| `app.py` / `main.py` | `python3 app.py` / `python3 main.py` |
| `Procfile` avec une ligne `web:` | le contenu de cette ligne |
| `index.html` seul | `python3 -m http.server $PORT` |

## Modeles de demarrage rapide

`/api/create` scaffolde un projet minimal fonctionnel, sans etape d'installation supplementaire (pas de
`pip install` ni `npm install` a l'ajout — tout tourne des la creation) :

| Modele | Fichier genere | Commande | Dependance image |
|---|---|---|---|
| `static` | `index.html` | `python3 -m http.server $PORT` | aucune |
| `flask` | `app.py` (Flask minimal) | `python3 app.py` | `flask` (deja installe pour le service lui-meme) |
| `node` | `index.js` (module `http` natif) | `node index.js` | `nodejs` (installe via le `Dockerfile`) |

Le modele `node` n'utilise que le module `http` natif de Node, volontairement, pour eviter toute resolution de
dependances `npm` a la creation.

## Metriques CPU / memoire

`proc_stats()` agrege le CPU et la memoire du process lance (`bash -lc <commande>`) et de **tous ses
descendants** via `psutil` — indispensable puisque `bash -lc` est presque toujours un parent transparent, le
vrai travail se faisant dans un process enfant (`python3`, `node`, etc.).

Point d'implementation a connaitre : `psutil.Process.cpu_percent()` ne renvoie une valeur exploitable qu'a
partir du **second** appel sur un meme objet `Process` (le premier sert d'amorce et renvoie toujours `0.0`). Un
cache de `Process` par PID (`_proc_cache`) est donc maintenu entre deux appels a `/api/apps`, pour que chaque
requete affiche le delta depuis la precedente plutot qu'un `0.0` permanent.

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
4. **Edition** (`PUT /api/app/<nom>`) : refusee tant que l'application tourne (`400`), pour eviter un
   changement de chemin/commande en plein vol.
5. **Reprise au redemarrage du conteneur** (`resume()`) : toute application marquee `enabled: true` dans
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

Au premier lancement, le mot de passe admin genere apparait dans les logs du conteneur
(`docker logs codelab-app-manager-test`). Se connecter sur `http://localhost:9001/`, puis tester le cycle
complet (creation depuis un modele, demarrage, proxy, metriques, logs en direct, edition, arret).
