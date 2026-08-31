# codelab-app-manager

Panneau de controle et reverse-proxy pour les applications que tu developpes dans `/workspace`, servi sur un
unique port (`9001`), protege par mot de passe. Autonome : aucune dependance a Supervisor — le cycle de vie des
applications est gere directement par ce service.

## Ce que fait le service

- Sert un dashboard (`http://<IP-ZimaOS>:9001/`) organise autour d'un **bandeau fixe pleine largeur en haut** :
  a gauche, un bouton dedie pour reduire/etendre la barre laterale (transition douce — cubic-bezier, texte en
  fondu plutot qu'un `display:none` brutal), puis le logo CodeLab (fige, toujours visible que la barre soit
  repliee ou non — cliquer dessus ramene a la Vue d'ensemble depuis n'importe quelle page) ; a droite, le
  **bouton profil**. En dessous, la barre laterale reductible (etat retenu entre les sessions) avec 2 sections,
  et le contenu principal :
  - **Vue d'ensemble** — page d'accueil par defaut : compteurs (total / en ligne / arretees), barre de sante
    (repartition en ligne / arretee / erreur), ressources cumulees (CPU/memoire), et deux graphiques en barres
    (CPU et memoire par application en ligne). Pas d'actions rapides — uniquement des statistiques.
  - **Projets** — grille de tuiles compactes façon icônes iOS : l'icône occupe presque toute la tuile, le nom
    en dessous en petit texte centré, sans bordure ni fond autour (juste l'icône elle-même, deja arrondie).
    Cliquer sur une tuile ouvre le site dans un nouvel onglet. Un menu **"..."** discret (visible en permanence
    mais peu contrasté) donne acces a Modifier (masque tant que l'app tourne), Redemarrer (si en ligne),
    Activer/Desactiver, Lancer le build (si une commande de build est definie), Git pull (si le dossier est un
    depot Git), Metriques, Voir les logs et Supprimer. Une pastille sur l'icone indique le statut : verte (en
    ligne), grise (arretee), rouge (erreur), rouge clignotante (boucle de crash — redemarrage automatique
    interrompu apres 5 echecs). Le bouton "Ajouter un projet" vit dans la barre d'outils. **Bascule grille/liste** dans la barre d'outils (preference retenue) : la vue liste garde le meme
    menu "..." mais affiche icone + nom sur une ligne, plus dense. Pas de bandeau de stats ici (deja dans Vue
    d'ensemble), pas de message "aucun projet" quand c'est vide — juste la tuile, pas de tri (toujours par
    ordre alphabetique, recherche disponible en haut).
  - **Compte, 1er niveau** — clic sur l'icone de profil (generique, pas le logo CodeLab) en haut a droite :
    mini-menu deroulant avec seulement **Parametres** et **Deconnexion**, pour un acces rapide sans changer de
    page.
  - **Compte, 2e niveau** — clic sur **Parametres** dans le mini-menu : page dediee complete (preference
    d'apparence Auto / Clair / Sombre, deconnexion). Pas de commande de copie des identifiants dans
    l'interface — `credentials.env` est deja directement lisible depuis le disque du ZimaOS.
- Pour chaque application activee, lance sa commande de demarrage comme sous-processus, sur un port interne
  attribue automatiquement (plage `9101`–`9140`).
- Fait office de **reverse-proxy interne** : `http://<IP-ZimaOS>:9001/<nom-app>/` route vers le port interne de
  l'application correspondante — un seul port a exposer sur ZimaOS, quel que soit le nombre d'applications
  gerees.
- Protege l'ensemble du panneau et de son API par une **authentification par mot de passe**, generee
  automatiquement au premier demarrage (meme principe que le mot de passe Postgres de `codelab-postgres`).

## Fiabilite, observabilite, deploiement

- **Redemarrage automatique en cas de crash** — un thread de fond (`monitor_tick`, toutes les 10s) redemarre
  automatiquement toute application marquee active dont le process est mort de maniere inattendue (crash, pas
  un arret volontaire via le menu). Plafonne a 5 tentatives par tranche de 10 minutes : au-dela, l'application
  est laissee arretee et la pastille de statut clignote en rouge ("boucle de crash") jusqu'a intervention
  manuelle. Un arret volontaire (Desactiver) efface cet historique.
- **Rotation des journaux** — un fichier de log qui depasse 2 Mo est renomme en `.log.1` (ecrasant l'ancien
  s'il existe) au demarrage suivant de l'application concernee. Evite une croissance illimitee sur le disque.
- **Redemarrer** — action distincte d'Activer/Desactiver dans le menu "...", visible seulement si l'application
  tourne : l'arrete puis la relance immediatement (utile apres avoir modifie du code sans que l'app le recharge
  seule).
- **Historique de metriques** — CPU et memoire de chaque application en ligne sont enregistres a chaque
  actualisation du dashboard (~5s), conserves en memoire sur les ~30 derniers points (~2,5 min). Consultable via
  "Metriques" dans le menu "..." : deux mini-graphiques SVG (CPU, memoire), generes cote client sans
  dependance supplementaire.
- **Recherche dans les logs** — un champ de filtre au-dessus du journal en direct ; ne montre que les lignes
  correspondantes, cote client, sans requete supplementaire au serveur.
- **Build a la demande** — commande optionnelle (ex. `npm install`, `pip install -r requirements.txt`),
  configurable par application dans le formulaire d'ajout/edition, executee separement du lancement via
  "Lancer le build" dans le menu "..." (delai max 10 min). La sortie est ecrite dans le meme journal que
  l'application, consultable normalement.
- **Git pull** — visible dans le menu "..." uniquement si le dossier du projet contient un `.git`. Execute
  `git pull --ff-only` (delai max 2 min), le resultat est affiche directement.
- **Limite memoire optionnelle** — champ "Limite memoire en Mo" dans le formulaire d'ajout/edition ; applique
  une limite dure via `RLIMIT_AS` (herite par le process et ses enfants) au demarrage. Le process est arrete
  par le noyau s'il tente de la depasser — protege contre une fuite memoire qui saturerait le ZimaOS entier.
  Pas de limite CPU equivalente : `RLIMIT_CPU` tue un process une fois un total de secondes CPU cumule atteint,
  ce qui n'a pas de sens pour un serveur cense tourner indefiniment.

> **Ce qui n'a volontairement pas ete ajoute** : un terminal web par application (redondant avec l'acces SSH
> deja fourni par `codelab-dev` ; un vrai terminal interactif necessiterait un PTY + des websockets, une
> surface de securite supplementaire pour un gain marginal) ; des domaines personnalises (necessiterait de
> controler du DNS externe, impossible a automatiser depuis l'interieur d'un conteneur — le routage `/nom/`
> actuel fonctionne sans dependance externe) ; un auto-deploiement par webhook GitHub (necessiterait un
> endpoint public joignable + verification de signature — le bouton "Git pull" manuel couvre le besoin reel
> pour un usage personnel sur reseau local).

> **Structuration visuelle** : chaque page regroupe son contenu dans des "zones" (fond legerement different du
> fond de page, titre de section en majuscules) plutot que de laisser les cartes flotter librement — Vue
> d'ensemble a une zone "Resume" (les 3 cartes de stats) et une zone "Activite" (les 2 graphiques), Projets a
> une zone "Projets" (la grille), Parametres a une zone par carte.

## Fichiers

| Fichier | Role |
|---|---|
| `Dockerfile` | Construction de l'image (Flask, psutil, requests, Node.js) |
| `app.py` | Le service complet : auth, API, proxy, dashboard |

## Authentification

Au tout premier demarrage, un mot de passe admin est genere aleatoirement et ecrit dans
`credentials.env` (permissions `600`), sous `APP_MANAGER_ADMIN_PASSWORD` — jamais defini par toi, jamais dans le
compose. C'est le seul endroit ou il est stocke, lisible directement
depuis le disque du ZimaOS sans `docker exec` :

```bash
cat /DATA/AppData/codelab/config/credentials.env
```

`upsert_shared_block()` n'ecrit que son propre bloc, commentaires inclus (delimite par
`# ===== codelab-app-manager =====` / `# ===== /codelab-app-manager =====`) — les blocs deposes par
`codelab-postgres` (et l'en-tete general du fichier) restent intacts, peu importe l'ordre de demarrage des deux
services. Voir [Fichier `credentials.env`](#fichier-credentialsenv) plus bas pour le detail du
mecanisme.

Les sessions sont signees avec une cle secrete elle aussi generee et persistee dans le meme fichier
(`APP_MANAGER_SESSION_SECRET`), donc la connexion survit a un redemarrage
du conteneur. Une bascule anti-bruteforce limite les tentatives de connexion echouees a 5 par tranche de 5
minutes, par adresse IP.

> **Le proxy `/<nom-app>/...` n'est volontairement pas protege par cette authentification** — seuls le dashboard
> et son API le sont. Une application que tu deploies reste directement joignable (utile pour tester un
> webhook, par exemple), independamment du mot de passe du panneau.

## Fichier `credentials.env`

`upsert_shared_block()` (appelee depuis `bootstrap_secrets()`) ecrit un **bloc entier** — commentaires de
documentation inclus — dans `/var/lib/codelab/config/credentials.env`
(`/DATA/AppData/codelab/config/credentials.env` cote hote), le meme fichier et le meme volume que
`codelab-postgres` utilise pour ses propres identifiants. Le bloc est delimite par des marqueurs
(`# ===== codelab-app-manager =====` ... `# ===== /codelab-app-manager =====`) et entierement remplace a
chaque demarrage : toutes les lignes entre les deux marqueurs sont supprimees puis reecrites d'un coup,
commentaires compris — pas juste les paires `CLE=valeur`, sinon la documentation s'accumulerait en double a
chaque redemarrage. Les blocs des autres services (l'en-tete general du fichier et `codelab-postgres`, tous
deux ecrits par `codelab-postgres` dans `docker-compose.yml`) restent intacts, quel que soit l'ordre de
demarrage. Si le volume partage n'est pas monte (tests locaux hors compose, par exemple), l'ecriture echoue
silencieusement sans bloquer le demarrage du service — c'est une commodite, pas une dependance critique.

## Variables d'environnement

| Variable | Role |
|---|---|
| `APP_MANAGER_DIR` | Ou vit `app.py` dans l'image (`/opt/codelab/app-manager`) — lecture seule |
| `APP_MANAGER_STATE` | Ou vivent `apps.json`, les logs, le mot de passe admin et la cle de session — le seul dossier que le service ecrit |
| `APP_MANAGER_ROOT` | Racine du navigateur de dossiers et des chemins d'applications (`/workspace`) |
| `APP_MANAGER_SHARED_CONFIG` | Dossier de `credentials.env`, le fichier unique de secrets (`/var/lib/codelab/config`) |
| `MANAGER_PORT` | Port d'ecoute du panneau lui-meme (`9001`) |

## Volumes attendus

| Point de montage | Contenu |
|---|---|
| `/var/lib/codelab/app-manager` | `apps.json` et `logs/` — l'etat des applications. Aucun secret : ils sont tous dans `credentials.env` |
| `/workspace` | Racine dans laquelle chercher/lancer les applications |

## API

| Route | Methode | Auth | Role |
|---|---|---|---|
| `/health` | GET | non | Sonde du `HEALTHCHECK` Docker |
| `/login` | GET / POST | non | Page de connexion / verification du mot de passe |
| `/logout` | POST | oui | Termine la session |
| `/api/apps` | GET | oui | Liste des applications, avec statut, metriques en direct, `crash_looping`, `is_git`, `has_build` |
| `/api/browse?path=...` | GET | oui | Navigateur de dossiers, borne a `APP_MANAGER_ROOT` |
| `/api/detect?path=...` | GET | oui | Suggere une commande de lancement a partir du contenu du dossier |
| `/api/add` | POST | oui | Enregistre une application existante (nom, chemin, commande, build, limite memoire) |
| `/api/app/<nom>` | PUT | oui | Modifie le chemin/la commande/le build/la limite memoire d'une application **arretee** |
| `/api/toggle/<nom>` | POST | oui | Demarre ou arrete une application |
| `/api/restart/<nom>` | POST | oui | Arrete puis relance immediatement une application |
| `/api/build/<nom>` | POST | oui | Execute la commande de build (si definie), sortie dans le journal |
| `/api/git-pull/<nom>` | POST | oui | `git pull --ff-only` dans le dossier du projet (si c'est un depot Git) |
| `/api/metrics/<nom>` | GET | oui | Historique CPU/memoire en memoire (~30 derniers points) |
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

Le panneau ne cree jamais de projet : il n'ecrit rien dans `/workspace`. Un projet nait d'un `mkdir`, d'un
`git clone` ou d'un `code .` depuis une session SSH sur `codelab-dev` ; le panneau se contente de le declarer,
de le lancer et de le superviser. `POST /api/add` refuse d'ailleurs un chemin qui n'existe pas deja.

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
complet (ajout d'un dossier existant, demarrage, proxy, metriques, logs en direct, edition, arret).
