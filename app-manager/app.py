"""
CodeLab app-manager -- panneau de controle + reverse proxy, sur un port unique.

  http://<IP>:9001/            panneau CodeLab (protege par mot de passe)
  http://<IP>:9001/login       page de connexion
  http://<IP>:9001/<projet>/   le projet, servi via proxy interne (pas protege)
  http://<IP>:9001/health      sonde du HEALTHCHECK Docker (pas protege)

Autonome : aucune dependance a Supervisor. Les processus sont geres ici.
Dependances externes : Flask, psutil.

Les chemins sont pilotes par variables d'environnement pour rester alignes
sur les points de montage declares dans docker-compose.yml :
  config/app-manager -> /opt/codelab/app-manager      (APP_MANAGER_DIR)
  data/app-manager   -> /var/lib/codelab/app-manager  (APP_MANAGER_STATE)
  workspace          -> /workspace                    (APP_MANAGER_ROOT)
  config (partage)   -> /var/lib/codelab/config       (APP_MANAGER_SHARED_CONFIG)
Le code est en lecture seule ; apps.json, les journaux, le mot de passe
admin genere et la cle de session Flask vivent tous dans STATE_DIR. Les
identifiants sont aussi recopies dans credentials.env, partage avec
codelab-postgres, pour consultation directe depuis le disque du ZimaOS.

Fonctionnalites de fiabilite/observabilite/deploiement ajoutees :
  - redemarrage automatique en cas de crash (plafonne, voir monitor_tick)
  - rotation des journaux (2 Mo, un seul fichier .1 conserve)
  - vrai bouton "Redemarrer" (stop puis start), distinct du toggle
  - historique de metriques en memoire (~2.5 min) + mini-graphiques SVG
  - recherche dans les logs en direct (filtre cote client)
  - "Lancer le build" (commande optionnelle, separee du lancement)
  - "Git pull" (visible seulement si le dossier contient .git)
  - limite memoire optionnelle par app (RLIMIT_AS via preexec_fn)
  - 4e modele de demarrage rapide : "API" (Python stdlib, JSON)
"""
import json
import os
import re
import resource
import secrets
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

import psutil
from flask import Flask, Response, jsonify, redirect, request, session, send_file
from werkzeug.security import check_password_hash

CONFIG_DIR = os.environ.get("APP_MANAGER_DIR", "/opt/codelab/app-manager")
STATE_DIR = os.environ.get("APP_MANAGER_STATE", "/var/lib/codelab/app-manager")
APPS_FILE = os.path.join(STATE_DIR, "apps.json")
LOG_DIR = os.path.join(STATE_DIR, "logs")
ROOT = os.environ.get("APP_MANAGER_ROOT", "/workspace")
PORT_MIN, PORT_MAX = 9101, 9140
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
       "proxy-authenticate", "proxy-authorization", "te", "trailers"}

ADMIN_PASSWORD_FILE = os.path.join(STATE_DIR, "admin_password")
SECRET_KEY_FILE = os.path.join(STATE_DIR, "flask_secret_key")
SHARED_CONFIG_DIR = os.environ.get("APP_MANAGER_SHARED_CONFIG", "/var/lib/codelab/config")
# Pas de nom en "." (fichier cache sous Unix) : le navigateur de fichiers
# ZimaOS n'offre pas d'option pour les afficher.
SHARED_ENV_FILE = os.path.join(SHARED_CONFIG_DIR, "credentials.env")

flask_app = Flask(__name__)
procs = {}          # nom -> subprocess.Popen
lock = threading.Lock()
_proc_cache = {}     # pid -> psutil.Process (prime pour cpu_percent delta)
_login_attempts = {}  # ip -> [timestamps des echecs recents]
_restart_history = {}  # nom -> [timestamps des redemarrages auto recents]
_metrics_history = {}  # nom -> deque[(timestamp, cpu_percent, memory_mb)]

MAX_LOG_BYTES = 2 * 1024 * 1024  # 2 Mo -- rotation simple, un seul fichier .1 conserve
METRICS_HISTORY_LEN = 30         # ~2.5 min a 5s/poll, ~30 points suffisent pour une tendance
RESTART_WINDOW = 600             # 10 min
RESTART_MAX_ATTEMPTS = 5         # au-dela, on arrete d'essayer (evite une boucle de crash infinie)


def rotate_log_if_needed(name):
    """Renomme le journal en .1 (ecrasant l'ancien .1 s'il existe) s'il
    depasse MAX_LOG_BYTES. Appele au demarrage d'une app, avant de
    rouvrir le fichier en ecriture -- pas de logique de purge en tache
    de fond, juste un controle a chaque (re)demarrage."""
    path = os.path.join(LOG_DIR, name + ".log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


# --------------------------- bootstrap secrets ---------------------------

def upsert_shared_block(name, comment_lines, pairs):
    """Ecrit/met a jour un BLOC entier (commentaires + cles) dans
    credentials.env, partage avec codelab-postgres
    (/DATA/AppData/codelab/config/credentials.env sur le disque du ZimaOS --
    pas de "." en tete de nom, le navigateur de fichiers ZimaOS ne propose
    pas d'afficher les fichiers caches). Le bloc est delimite par des
    marqueurs "# ===== <name> =====" / "# ===== /<name> =====" et remplace
    entierement a chaque appel -- pas juste les lignes CLE=valeur, sinon
    les commentaires documentant ce bloc s'accumuleraient en double a
    chaque redemarrage. Les blocs des autres services (ex. codelab-postgres)
    restent intacts quel que soit l'ordre de demarrage : meme logique que
    cote codelab-postgres dans docker-compose.yml."""
    try:
        os.makedirs(SHARED_CONFIG_DIR, exist_ok=True)
        start, end = f"# ===== {name} =====", f"# ===== /{name} ====="
        existing = []
        if os.path.exists(SHARED_ENV_FILE):
            with open(SHARED_ENV_FILE) as f:
                existing = f.read().splitlines()
        kept, skip = [], False
        for line in existing:
            if line == start:
                skip = True
                continue
            if line == end:
                skip = False
                continue
            if not skip:
                kept.append(line)
        block = [start] + list(comment_lines) + [f"{k}={v}" for k, v in pairs.items()] + [end]
        with open(SHARED_ENV_FILE, "w") as f:
            f.write("\n".join(kept + block) + "\n")
        os.chmod(SHARED_ENV_FILE, 0o600)
    except OSError as e:
        # Le volume partage n'est peut-etre pas monte (ex. test local sans
        # docker-compose) -- ne bloque jamais le demarrage du service pour
        # ca, c'est une commodite, pas une dependance critique.
        print(f"[app-manager] credentials.env partage non ecrit ({e}), ignore.", flush=True)


def bootstrap_secrets():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(ADMIN_PASSWORD_FILE):
        pw = secrets.token_urlsafe(18)
        with open(ADMIN_PASSWORD_FILE, "w") as f:
            f.write(pw)
        os.chmod(ADMIN_PASSWORD_FILE, 0o600)
        print(f"[app-manager] Mot de passe admin genere dans {ADMIN_PASSWORD_FILE}", flush=True)
    if not os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(SECRET_KEY_FILE, 0o600)
    with open(SECRET_KEY_FILE) as f:
        flask_app.secret_key = f.read().strip()

    upsert_shared_block(
        "codelab-app-manager",
        [
            "# Panneau web de gestion des applications deployees (http://<IP>:9001/).",
            "# APP_MANAGER_ADMIN_PASSWORD : mot de passe de connexion au panneau.",
            "# APP_MANAGER_SESSION_SECRET : cle de signature des sessions -- la",
            "#   changer deconnecte tout le monde ; ne jamais la partager.",
        ],
        {
            "APP_MANAGER_URL": "http://<IP-ZimaOS>:9001",
            "APP_MANAGER_ADMIN_PASSWORD": admin_password() or "",
            "APP_MANAGER_SESSION_SECRET": flask_app.secret_key,
        },
    )


def admin_password():
    try:
        with open(ADMIN_PASSWORD_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


# --------------------------- auth ---------------------------

RATE_LIMIT_WINDOW = 300  # 5 min
RATE_LIMIT_MAX = 5


def _client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


def rate_limited():
    ip = _client_ip()
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) >= RATE_LIMIT_MAX


def register_failed_attempt():
    ip = _client_ip()
    _login_attempts.setdefault(ip, []).append(time.time())


def is_authed():
    return session.get("authed") is True


def require_auth(view):
    def wrapped(*a, **kw):
        if not is_authed():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Non authentifie."}), 401
            return redirect("/login")
        return view(*a, **kw)
    wrapped.__name__ = view.__name__
    return wrapped


# --------------------------- persistance ---------------------------

def load():
    try:
        with open(APPS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save(apps):
    tmp = APPS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(apps, f, indent=2)
    os.replace(tmp, APPS_FILE)


def next_port(apps):
    used = {a["port"] for a in apps.values()}
    for p in range(PORT_MIN, PORT_MAX + 1):
        if p not in used:
            return p
    return None


# ------------------------- cycle de vie ----------------------------

def is_running(name):
    p = procs.get(name)
    return p is not None and p.poll() is None


def start(name):
    apps = load()
    a = apps.get(name)
    if not a or is_running(name):
        return
    
    # 1. S'assurer que le dossier workspace/app existe sur l'hôte/conteneur
    app_path = a["path"]
    os.makedirs(app_path, exist_ok=True)

    # 2. Si le dossier est complètement vide, créer un script minimal de secours pour éviter le crash
    if not os.listdir(app_path):
        fallback_file = os.path.join(app_path, "app.py")
        with open(fallback_file, "w") as f:
            f.write(
                'import os\n'
                'from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n\n'
                'class Handler(BaseHTTPRequestHandler):\n'
                '    def do_GET(self):\n'
                '        self.send_response(200)\n'
                '        self.send_header("Content-Type", "text/html; charset=utf-8")\n'
                '        self.end_headers()\n'
                '        self.wfile.write(f"<h1>CodeLab App : {os.path.basename(os.getcwd())}</h1><p>En attente du code de votre application...</p>".encode())\n\n'
                'if __name__ == "__main__":\n'
                '    port = int(os.environ.get("PORT", 8000))\n'
                '    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()\n'
            )

    os.makedirs(LOG_DIR, exist_ok=True)
    rotate_log_if_needed(name)
    out = open(os.path.join(LOG_DIR, name + ".log"), "ab", buffering=0)
    env = dict(os.environ, PORT=str(a["port"]), PYTHONUNBUFFERED="1")

    max_mem = a.get("max_memory_mb")
    preexec = None
    if max_mem:
        mem_bytes = int(max_mem) * 1024 * 1024

        def _limit_resources():
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

        preexec = _limit_resources

    with lock:
        procs[name] = subprocess.Popen(
            ["bash", "-lc", a["command"]],
            cwd=app_path, env=env, stdout=out, stderr=out,
            start_new_session=True, preexec_fn=preexec)
    apps[name]["enabled"] = True
    save(apps)


def stop(name):
    p = procs.get(name)
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            for _ in range(30):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            if p.poll() is None:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    if p:
        for pid in list(_proc_cache):
            try:
                if _proc_cache[pid].pid == p.pid or True:
                    pass
            except Exception:
                pass
    procs.pop(name, None)
    _restart_history.pop(name, None)  # arret volontaire : on oublie l'historique de crash
    apps = load()
    if name in apps:
        apps[name]["enabled"] = False
        save(apps)


def resume():
    for name, a in load().items():
        if a.get("enabled"):
            start(name)


# ------------------------- redemarrage automatique ----------------------------

def monitor_tick():
    """Un tour de surveillance : toute app marquee "enabled" dans
    apps.json mais dont le process est mort (crash, pas un arret
    volontaire -- stop() met "enabled" a False) est redemarree
    automatiquement, avec une limite d'essais pour eviter de s'epuiser
    sur une app qui plante en boucle des le demarrage."""
    apps = load()
    now = time.time()
    for name, a in apps.items():
        if not a.get("enabled") or is_running(name):
            continue
        hist = [t for t in _restart_history.get(name, []) if now - t < RESTART_WINDOW]
        if len(hist) < RESTART_MAX_ATTEMPTS:
            hist.append(now)
            _restart_history[name] = hist
            print(f"[app-manager] {name} arretee de maniere inattendue, "
                  f"redemarrage automatique ({len(hist)}/{RESTART_MAX_ATTEMPTS})", flush=True)
            start(name)
        else:
            _restart_history[name] = hist


def is_crash_looping(name):
    hist = [t for t in _restart_history.get(name, []) if time.time() - t < RESTART_WINDOW]
    return len(hist) >= RESTART_MAX_ATTEMPTS


def start_monitor_thread():
    def _loop():
        while True:
            time.sleep(10)
            try:
                monitor_tick()
            except Exception as e:
                print(f"[app-manager] erreur dans le moniteur de redemarrage : {e}", flush=True)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


# ------------------------- build / git pull ----------------------------

def run_build(name):
    apps = load()
    a = apps.get(name)
    if not a:
        return False, "Application inconnue."
    cmd = (a.get("build_command") or "").strip()
    if not cmd:
        return False, "Aucune commande de build definie pour cette application."
    os.makedirs(LOG_DIR, exist_ok=True)
    logf = os.path.join(LOG_DIR, name + ".log")
    with open(logf, "ab") as out:
        out.write(f"\n$ {cmd}\n".encode())
        try:
            r = subprocess.run(["bash", "-lc", cmd], cwd=a["path"],
                                stdout=out, stderr=out, timeout=600)
            ok = r.returncode == 0
            msg = None if ok else f"Le build a echoue (code {r.returncode}) -- voir le journal."
        except subprocess.TimeoutExpired:
            out.write(b"\n[build] delai depasse (10 min), arrete.\n")
            ok, msg = False, "Le build a depasse le delai de 10 minutes."
    return ok, msg


def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))


def git_pull(name):
    apps = load()
    a = apps.get(name)
    if not a:
        return False, "Application inconnue."
    if not is_git_repo(a["path"]):
        return False, "Ce dossier n'est pas un depot Git (pas de .git)."
    try:
        r = subprocess.run(["git", "pull", "--ff-only"], cwd=a["path"],
                            capture_output=True, text=True, timeout=120)
        output = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Delai depasse (2 min)."
    except FileNotFoundError:
        return False, "La commande git n'est pas installee dans cette image."


# ------------------------- historique de metriques ----------------------------

def record_metrics(name, cpu_percent, memory_mb):
    dq = _metrics_history.setdefault(name, deque(maxlen=METRICS_HISTORY_LEN))
    dq.append((time.time(), cpu_percent, memory_mb))


def get_metrics_history(name):
    return list(_metrics_history.get(name, []))


# ------------------------- metriques ----------------------------

def proc_stats(pid):
    """CPU% et memoire (Mo) cumules sur le process et tous ses enfants.
    cpu_percent() ne donne une valeur exploitable qu'a partir du 2e appel
    sur un meme objet psutil.Process (le 1er sert d'amorce) -- d'ou le
    cache _proc_cache, reutilise entre deux appels a /api/apps."""
    try:
        top = psutil.Process(pid)
    except psutil.Error:
        return {"cpu_percent": 0.0, "memory_mb": 0.0}

    try:
        pids = [top.pid] + [c.pid for c in top.children(recursive=True)]
    except psutil.Error:
        pids = [top.pid]

    cpu_total, mem_total = 0.0, 0
    for cpid in pids:
        pr = _proc_cache.get(cpid)
        if pr is None:
            try:
                pr = psutil.Process(cpid)
                pr.cpu_percent(interval=None)  # amorce
                _proc_cache[cpid] = pr
                continue  # premiere mesure ignoree, pas encore fiable
            except psutil.Error:
                continue
        try:
            cpu_total += pr.cpu_percent(interval=None)
            mem_total += pr.memory_info().rss
        except psutil.Error:
            _proc_cache.pop(cpid, None)

    return {"cpu_percent": round(cpu_total, 1), "memory_mb": round(mem_total / (1024 * 1024), 1)}


# ------------------------- auto-detection de commande ----------------------------

def detect_command(path):
    def exists(*parts):
        return os.path.exists(os.path.join(path, *parts))

    def read(*parts):
        try:
            with open(os.path.join(path, *parts), errors="ignore") as f:
                return f.read()
        except OSError:
            return ""

    if exists("package.json"):
        try:
            pkg = json.loads(read("package.json"))
        except Exception:
            pkg = {}
        if isinstance(pkg.get("scripts"), dict) and "start" in pkg["scripts"]:
            return "npm start"
        if pkg.get("main"):
            return f"node {pkg['main']}"
        return "npm start"

    if exists("manage.py"):
        return "python3 manage.py runserver 0.0.0.0:$PORT"

    if exists("app.py"):
        if "Flask(" in read("app.py"):
            return "python3 app.py"
        return "python3 app.py"

    if exists("main.py"):
        return "python3 main.py"

    if exists("Procfile"):
        for line in read("Procfile").splitlines():
            if line.strip().startswith("web:"):
                return line.split(":", 1)[1].strip()

    if exists("index.html") and not exists("package.json"):
        return "python3 -m http.server $PORT"

    return ""


# ------------------------- icones ----------------------------

ICON_CANDIDATES = ["icon.png", "icon.svg", "favicon.png", "favicon.ico", "logo.png", "logo.svg"]
PALETTE = ["#316dca", "#8957e5", "#bf3989", "#cf222e", "#bc4c00", "#9a6700", "#1a7f37", "#0969da"]


def find_icon(path):
    for name in ICON_CANDIDATES:
        p = os.path.join(path, name)
        if os.path.isfile(p):
            return p
    return None


def default_icon_svg(name):
    color = PALETTE[sum(map(ord, name or "?")) % len(PALETTE)]
    letter = (name[:1] or "?").upper()
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="16" fill="{color}"/>'
        f'<text x="32" y="43" font-family="system-ui,sans-serif" font-size="26" '
        f'font-weight="600" fill="#fff" text-anchor="middle">{letter}</text></svg>'
    )




def valid_name(raw):
    return re.sub(r"[^a-z0-9_-]", "-", (raw or "").strip().lower()).strip("-")


# ------------------------------ auth --------------------------------

@flask_app.post("/login")
def login_submit():
    if rate_limited():
        return jsonify({"error": "Trop de tentatives. Reessaie dans quelques minutes."}), 429
    d = request.get_json(force=True, silent=True) or request.form
    pw = (d.get("password") or "").strip()
    real = admin_password()
    if real is not None and pw and pw == real:
        session.permanent = True
        session["authed"] = True
        return jsonify({"ok": True})
    register_failed_attempt()
    return jsonify({"error": "Mot de passe incorrect."}), 401


@flask_app.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@flask_app.get("/health")
def health():
    return Response("ok\n", mimetype="text/plain")


# ------------------------------ API --------------------------------

@flask_app.get("/api/apps")
@require_auth
def api_apps():
    apps = load()
    out = []
    for name in sorted(apps):
        a = apps[name]
        run = is_running(name)
        stats = proc_stats(procs[name].pid) if run and name in procs else {"cpu_percent": 0.0, "memory_mb": 0.0}
        if run:
            record_metrics(name, stats["cpu_percent"], stats["memory_mb"])
        out.append({
            "name": name, "path": a["path"], "command": a["command"],
            "port": a["port"], "running": run,
            "failed": bool(a.get("enabled")) and not run,
            "crash_looping": is_crash_looping(name),
            "is_git": is_git_repo(a["path"]),
            "has_build": bool((a.get("build_command") or "").strip()),
            "build_command": a.get("build_command") or "",
            "max_memory_mb": a.get("max_memory_mb"),
            **stats,
        })
    return jsonify({"apps": out})


@flask_app.get("/api/browse")
@require_auth
def api_browse():
    path = os.path.abspath(request.args.get("path", ROOT))
    if not (path == ROOT or path.startswith(ROOT + "/")):
        path = ROOT
    try:
        entries = os.listdir(path)
    except (PermissionError, FileNotFoundError):
        entries = []
    return jsonify({
        "path": path,
        "label": path.replace(ROOT, "") or "/",
        "parent": os.path.dirname(path) if path != ROOT else None,
        "dirs": sorted(d for d in entries
                       if os.path.isdir(os.path.join(path, d)) and not d.startswith(".")),
        "hasIndex": os.path.exists(os.path.join(path, "index.html")),
    })


@flask_app.get("/api/detect")
@require_auth
def api_detect():
    path = os.path.abspath(request.args.get("path", ""))
    if not (path == ROOT or path.startswith(ROOT + "/")) or not os.path.isdir(path):
        return jsonify({"command": ""})
    return jsonify({"command": detect_command(path)})


STATIC_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>%s</title>
</head>
<body>
  <h1>Bienvenue dans CodeLab</h1>
  <p>Projet : %s</p>
</body>
</html>
"""

FLASK_TEMPLATE = '''import os
from flask import Flask

app = Flask(__name__)


@app.get("/")
def index():
    return "<h1>Bienvenue dans CodeLab</h1><p>Projet Flask : %s</p>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
'''

NODE_TEMPLATE = '''const http = require("http");
const port = process.env.PORT || 8000;

http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end("<h1>Bienvenue dans CodeLab</h1><p>Projet Node : %s</p>");
}).listen(port, () => console.log("Listening on " + port));
'''

API_TEMPLATE = '''import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"project": "%s", "status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # les acces sont deja captures par le journal de l'app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
'''

TEMPLATES = {
    "static": {"file": "index.html", "content": STATIC_TEMPLATE, "args": 2,
               "command": "python3 -m http.server $PORT"},
    "flask": {"file": "app.py", "content": FLASK_TEMPLATE, "args": 1,
              "command": "python3 app.py"},
    "node": {"file": "index.js", "content": NODE_TEMPLATE, "args": 1,
             "command": "node index.js"},
    "api": {"file": "app.py", "content": API_TEMPLATE, "args": 1,
            "command": "python3 app.py"},
}


@flask_app.post("/api/create")
@require_auth
def api_create():
    d = request.get_json(force=True)
    name = valid_name(d.get("name"))
    tpl_name = d.get("template") or "static"
    command = (d.get("command") or "").strip()
    build_command = (d.get("build_command") or "").strip()
    max_memory_mb = d.get("max_memory_mb") or None

    if not name:
        return jsonify({"error": "Le nom est obligatoire."}), 400
    if name in ("api", "static", "health", "login", "logout"):
        return jsonify({"error": "Ce nom est reserve."}), 400
    if tpl_name not in TEMPLATES:
        return jsonify({"error": "Modele inconnu."}), 400

    apps = load()
    if name in apps:
        return jsonify({"error": "Une application porte deja ce nom."}), 400

    path = os.path.join(ROOT, name)
    if os.path.exists(path):
        return jsonify({"error": "Le dossier existe deja : " + path}), 400

    port = next_port(apps)
    if not port:
        return jsonify({"error": "Plus de port interne disponible."}), 400

    tpl = TEMPLATES[tpl_name]
    os.makedirs(path)
    content = tpl["content"] % ((name, name) if tpl["args"] == 2 else name)
    with open(os.path.join(path, tpl["file"]), "w") as f:
        f.write(content)

    apps[name] = {
        "path": path,
        "command": command or tpl["command"],
        "port": port,
        "enabled": False,
        "build_command": build_command,
        "max_memory_mb": max_memory_mb,
    }
    save(apps)
    start(name)

    return jsonify({"ok": True, "name": name, "path": path, "port": port, "running": is_running(name)})


@flask_app.post("/api/add")
@require_auth
def api_add():
    d = request.get_json(force=True)
    name = valid_name(d.get("name"))
    path = (d.get("path") or "").strip()
    command = (d.get("command") or "").strip()
    build_command = (d.get("build_command") or "").strip()
    max_memory_mb = d.get("max_memory_mb") or None
    apps = load()

    if not name:
        return jsonify({"error": "Le nom est obligatoire."}), 400
    if name in ("api", "static", "health", "login", "logout"):
        return jsonify({"error": "Ce nom est reserve."}), 400
    if name in apps:
        return jsonify({"error": "Une application porte deja ce nom."}), 400
    if not os.path.isdir(path):
        return jsonify({"error": "Dossier introuvable : " + path}), 400
    if not command:
        return jsonify({"error": "La commande de lancement est obligatoire."}), 400
    port = next_port(apps)
    if not port:
        return jsonify({"error": "Plus de port interne disponible."}), 400

    apps[name] = {
        "path": path, "command": command, "port": port, "enabled": False,
        "build_command": build_command, "max_memory_mb": max_memory_mb,
    }
    save(apps)
    return jsonify({"ok": True, "name": name, "port": port})


@flask_app.put("/api/app/<n>")
@require_auth
def api_edit(n):
    apps = load()
    if n not in apps:
        return jsonify({"error": "Application inconnue."}), 404
    if is_running(n):
        return jsonify({"error": "Arrete l'application avant de la modifier."}), 400
    d = request.get_json(force=True)
    path = (d.get("path") or "").strip()
    command = (d.get("command") or "").strip()
    if not os.path.isdir(path):
        return jsonify({"error": "Dossier introuvable : " + path}), 400
    if not command:
        return jsonify({"error": "La commande de lancement est obligatoire."}), 400
    apps[n]["path"] = path
    apps[n]["command"] = command
    apps[n]["build_command"] = (d.get("build_command") or "").strip()
    apps[n]["max_memory_mb"] = d.get("max_memory_mb") or None
    save(apps)
    return jsonify({"ok": True})


@flask_app.post("/api/toggle/<n>")
@require_auth
def api_toggle(n):
    if n not in load():
        return jsonify({"error": "Application inconnue."}), 404
    stop(n) if is_running(n) else start(n)
    return jsonify({"ok": True})


@flask_app.post("/api/restart/<n>")
@require_auth
def api_restart(n):
    if n not in load():
        return jsonify({"error": "Application inconnue."}), 404
    stop(n)
    for _ in range(30):
        if not is_running(n):
            break
        time.sleep(0.1)
    start(n)
    return jsonify({"ok": True})


@flask_app.post("/api/build/<n>")
@require_auth
def api_build(n):
    if n not in load():
        return jsonify({"error": "Application inconnue."}), 404
    ok, msg = run_build(n)
    if not ok:
        return jsonify({"error": msg}), 400
    return jsonify({"ok": True})


@flask_app.post("/api/git-pull/<n>")
@require_auth
def api_git_pull(n):
    if n not in load():
        return jsonify({"error": "Application inconnue."}), 404
    ok, output = git_pull(n)
    if not ok:
        return jsonify({"error": output}), 400
    return jsonify({"ok": True, "output": output})


@flask_app.get("/api/metrics/<n>")
@require_auth
def api_metrics(n):
    if n not in load():
        return jsonify({"error": "Application inconnue."}), 404
    hist = get_metrics_history(n)
    return jsonify({
        "points": [{"t": t, "cpu": cpu, "mem": mem} for t, cpu, mem in hist],
    })


@flask_app.delete("/api/app/<n>")
@require_auth
def api_delete(n):
    stop(n)
    apps = load()
    apps.pop(n, None)
    save(apps)
    return jsonify({"ok": True})


@flask_app.get("/api/logs/<n>")
@require_auth
def api_logs(n):
    f = os.path.join(LOG_DIR, n + ".log")
    if not os.path.exists(f):
        return jsonify({"lines": ["Aucun journal pour le moment."]})
    with open(f, errors="replace") as fh:
        lines = [l.rstrip() for l in fh.readlines()[-120:]]
    return jsonify({"lines": lines or ["Journal vide."]})


@flask_app.get("/api/logs/<n>/stream")
@require_auth
def api_logs_stream(n):
    f = os.path.join(LOG_DIR, n + ".log")

    def gen():
        pos = max(0, os.path.getsize(f) - 4000) if os.path.exists(f) else 0
        yield "retry: 2000\n\n"
        while True:
            if os.path.exists(f):
                with open(f, errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                for line in chunk.splitlines():
                    yield f"data: {line}\n\n"
            time.sleep(0.5)

    return Response(stream_with_context(gen()), mimetype="text/event-stream")


@flask_app.get("/api/icon/<n>")
@require_auth
def api_icon(n):
    apps = load()
    a = apps.get(n)
    icon_path = find_icon(a["path"]) if a else None
    if icon_path:
        return send_file(icon_path)
    return Response(default_icon_svg(n), mimetype="image/svg+xml")


LOGIN_PAGE = '<!doctype html>\n<html lang="fr"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>CodeLab &middot; Connexion</title>\n<link rel="icon" type="image/png" href="data:image/png;base64,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">\n<style>\n:root{\n  --bg:#f6f7f9; --surface:#ffffff; --surface2:#f0f1f3; --bg-input:#f0f1f3;\n  --line:#e2e4e8; --line2:#d7dae0; --txt:#1c2129; --dim:#5b6472; --dim2:#8891a0;\n  --accent:#316dca; --accent-h:#2a5fb0; --accent-ring:rgba(49,109,202,.18);\n  --ok:#1a7f37; --ok-bg:rgba(26,127,55,.1); --ok-border:rgba(26,127,55,.3);\n  --err:#cf222e; --err-bg:rgba(207,34,46,.08); --err-txt:#a4030f; --err-border:rgba(207,34,46,.28);\n  --shadow:rgba(28,33,41,.08); --r:10px;\n}\n@media (prefers-color-scheme: dark){\n  :root:not([data-theme="light"]):not([data-theme="dark"]){\n    --bg:#0d1117; --surface:#161b22; --surface2:#1c2129; --bg-input:#0d1117;\n    --line:#262c36; --line2:#30363d; --txt:#e6edf3; --dim:#8b949e; --dim2:#6e7681;\n    --accent:#4c8eff; --accent-h:#3d7dda; --accent-ring:rgba(76,142,255,.22);\n    --ok:#3fb950; --ok-bg:rgba(63,185,80,.1); --ok-border:rgba(63,185,80,.35);\n    --err:#f85149; --err-bg:rgba(248,81,73,.1); --err-txt:#ff9b95; --err-border:rgba(248,81,73,.35);\n    --shadow:rgba(1,4,9,.6);\n  }\n}\n[data-theme="dark"]{\n  --bg:#0d1117; --surface:#161b22; --surface2:#1c2129; --bg-input:#0d1117;\n  --line:#262c36; --line2:#30363d; --txt:#e6edf3; --dim:#8b949e; --dim2:#6e7681;\n  --accent:#4c8eff; --accent-h:#3d7dda; --accent-ring:rgba(76,142,255,.22);\n  --ok:#3fb950; --ok-bg:rgba(63,185,80,.1); --ok-border:rgba(63,185,80,.35);\n  --err:#f85149; --err-bg:rgba(248,81,73,.1); --err-txt:#ff9b95; --err-border:rgba(248,81,73,.35);\n  --shadow:rgba(1,4,9,.6);\n}\n*{box-sizing:border-box;margin:0;padding:0}\nbody{background:var(--bg);color:var(--txt);\n  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;\n  -webkit-font-smoothing:antialiased; transition:background .15s,color .15s}\n\nbody{min-height:100vh;display:flex;align-items:center;justify-content:center}\n.card{width:100%;max-width:340px;padding:32px 28px;background:var(--surface);\n border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 32px var(--shadow)}\n.brand{display:flex;align-items:center;gap:10px;margin-bottom:22px}\n.brand .dot{width:10px;height:10px;border-radius:50%;background:var(--accent)}\n.brand span{font-size:15px;font-weight:600}\nlabel{display:block;font-size:12px;color:var(--dim);margin-bottom:6px;font-weight:500}\ninput{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--line2);\n background:var(--bg-input);color:var(--txt);font:inherit;font-size:14px}\ninput:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ring)}\nbutton{width:100%;margin-top:16px;padding:10px;border:none;border-radius:8px;\n background:var(--accent);color:#fff;font:inherit;font-size:14px;font-weight:600;cursor:pointer}\nbutton:hover{background:var(--accent-h)}\nbutton:disabled{opacity:.6;cursor:default}\n.err{margin-top:12px;padding:9px 12px;border-radius:8px;font-size:12.5px;\n background:var(--err-bg);color:var(--err-txt);border:1px solid var(--err-border);display:none}\n.hint{margin-top:18px;font-size:11.5px;color:var(--dim2);line-height:1.5}\ncode{font-family:ui-monospace,Menlo,monospace;background:var(--bg-input);padding:1px 5px;border-radius:4px}\n</style></head><body>\n<div class="card">\n  <div class="brand"><div class="dot"></div><span>CodeLab</span></div>\n  <label>Mot de passe</label>\n  <input type="password" id="pw" autofocus autocomplete="current-password">\n  <button id="go" onclick="submit()">Se connecter</button>\n  <div class="err" id="err"></div>\n  <div class="hint">Mot de passe genere automatiquement au premier demarrage. Pour le retrouver :\n  <br><code>docker exec codelab-app-manager cat /var/lib/codelab/app-manager/admin_password</code></div>\n</div>\n<script>\nconst pw=document.getElementById(\'pw\'), err=document.getElementById(\'err\'), go=document.getElementById(\'go\');\nasync function submit(){\n  err.style.display=\'none\'; go.disabled=true; go.textContent=\'Connexion...\';\n  try{\n    const r=await fetch(\'/login\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},\n      body:JSON.stringify({password:pw.value})});\n    const d=await r.json();\n    if(r.ok){ location.href=\'/\'; return; }\n    err.textContent=d.error||\'Erreur.\'; err.style.display=\'block\';\n  }catch(e){ err.textContent=\'Erreur reseau.\'; err.style.display=\'block\'; }\n  go.disabled=false; go.textContent=\'Se connecter\';\n}\npw.addEventListener(\'keydown\',e=>{if(e.key===\'Enter\')submit();});\n</script>\n</body></html>\n'

DASHBOARD_PAGE = '<!doctype html>\n<html lang="fr"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>CodeLab &middot; App Manager</title>\n<link rel="icon" type="image/png" href="data:image/png;base64,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">\n<style>\n:root{\n  --bg:#f6f7f9; --surface:#ffffff; --surface2:#f0f1f3; --bg-input:#f0f1f3;\n  --line:#e2e4e8; --line2:#d7dae0; --txt:#1c2129; --dim:#5b6472; --dim2:#8891a0;\n  --accent:#316dca; --accent-h:#2a5fb0; --accent-ring:rgba(49,109,202,.18);\n  --ok:#1a7f37; --ok-bg:rgba(26,127,55,.1); --ok-border:rgba(26,127,55,.3);\n  --err:#cf222e; --err-bg:rgba(207,34,46,.08); --err-txt:#a4030f; --err-border:rgba(207,34,46,.28);\n  --shadow:rgba(28,33,41,.08); --r:10px;\n}\n@media (prefers-color-scheme: dark){\n  :root:not([data-theme="light"]):not([data-theme="dark"]){\n    --bg:#0d1117; --surface:#161b22; --surface2:#1c2129; --bg-input:#0d1117;\n    --line:#262c36; --line2:#30363d; --txt:#e6edf3; --dim:#8b949e; --dim2:#6e7681;\n    --accent:#4c8eff; --accent-h:#3d7dda; --accent-ring:rgba(76,142,255,.22);\n    --ok:#3fb950; --ok-bg:rgba(63,185,80,.1); --ok-border:rgba(63,185,80,.35);\n    --err:#f85149; --err-bg:rgba(248,81,73,.1); --err-txt:#ff9b95; --err-border:rgba(248,81,73,.35);\n    --shadow:rgba(1,4,9,.6);\n  }\n}\n[data-theme="dark"]{\n  --bg:#0d1117; --surface:#161b22; --surface2:#1c2129; --bg-input:#0d1117;\n  --line:#262c36; --line2:#30363d; --txt:#e6edf3; --dim:#8b949e; --dim2:#6e7681;\n  --accent:#4c8eff; --accent-h:#3d7dda; --accent-ring:rgba(76,142,255,.22);\n  --ok:#3fb950; --ok-bg:rgba(63,185,80,.1); --ok-border:rgba(63,185,80,.35);\n  --err:#f85149; --err-bg:rgba(248,81,73,.1); --err-txt:#ff9b95; --err-border:rgba(248,81,73,.35);\n  --shadow:rgba(1,4,9,.6);\n}\n*{box-sizing:border-box;margin:0;padding:0}\nbody{background:var(--bg);color:var(--txt);\n  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;\n  -webkit-font-smoothing:antialiased; transition:background .15s,color .15s}\n\nhtml,body{height:100%}\n.topbar{height:56px;flex:none;display:flex;align-items:center;gap:6px;padding:0 16px;\n background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:50;\n box-shadow:0 1px 3px var(--shadow)}\n.side-toggle-btn{width:32px;height:32px;border-radius:8px;border:none;background:transparent;\n color:var(--dim);display:inline-flex;align-items:center;justify-content:center;cursor:pointer;flex:none}\n.side-toggle-btn:hover{background:var(--surface2);color:var(--txt)}\n.side-toggle-btn svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.8}\n.topbar-brand{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:8px;cursor:pointer;\n user-select:none;flex:none}\n.topbar-brand:hover{background:var(--surface2)}\n.topbar-brand img{width:26px;height:26px;border-radius:7px;flex:none}\n.topbar-brand b{font-size:14.5px;font-weight:600}\n.page-body{display:flex;flex:1;min-height:0}\n.sidebar{width:216px;flex:none;background:var(--surface);border-right:1px solid var(--line);\n display:flex;flex-direction:column;position:sticky;top:56px;height:calc(100vh - 56px);overflow:auto;\n transition:width .28s cubic-bezier(.4,0,.2,1);box-shadow:1px 0 3px var(--shadow)}\n.side-nav{padding:12px 10px;display:flex;flex-direction:column;gap:2px;flex:1}\n.side-item{display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:8px;color:var(--dim);\n font-size:13.5px;font-weight:500;cursor:pointer;user-select:none;\n transition:background .15s,color .15s,padding .28s cubic-bezier(.4,0,.2,1)}\n.side-item svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.8;flex:none}\n.side-item span{display:inline-block;overflow:hidden;white-space:nowrap;opacity:1;max-width:160px;\n transition:opacity .18s ease,max-width .28s cubic-bezier(.4,0,.2,1)}\n.side-item:hover{background:var(--surface2);color:var(--txt)}\n.side-item.active{background:var(--accent-ring);color:var(--accent)}\n.sidebar.collapsed{width:60px}\n.sidebar.collapsed .side-item span{opacity:0;max-width:0}\n.sidebar.collapsed .side-item{justify-content:center}\n.main-area{flex:1;min-width:0}\n.acct-wrap{position:relative;flex:none}\n.acct-btn{width:34px;height:34px;border-radius:50%;background:var(--accent);color:#fff;font-size:13px;\n font-weight:600;display:inline-flex;align-items:center;justify-content:center;cursor:pointer;\n border:2px solid transparent;padding:0}\n.acct-btn svg{width:18px;height:18px;stroke:#fff;fill:none;stroke-width:1.8}\n.acct-btn.active{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ring)}\n.acct-menu{position:absolute;top:calc(100% + 8px);right:0;width:190px;background:var(--surface);\n border:1px solid var(--line2);border-radius:12px;box-shadow:0 12px 36px var(--shadow);\n display:none;z-index:40;overflow:hidden;padding:6px}\n.acct-menu.show{display:block}\n.acct-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;color:var(--txt);\n font-size:13px;font-weight:500;cursor:pointer;user-select:none}\n.acct-item svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8;flex:none}\n.acct-item:hover{background:var(--surface2)}\n.spacer{flex:1}\n.search{position:relative;max-width:320px}\n.search input{width:100%;padding:7px 12px 7px 32px;border-radius:8px;border:1px solid var(--line2);\n background:var(--bg-input);color:var(--txt);font:inherit;font-size:13px}\n.search input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ring)}\n.search svg{position:absolute;left:9px;top:50%;transform:translateY(-50%);width:14px;height:14px;\n stroke:var(--dim2);fill:none;stroke-width:2}\n.icon-btn{width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;\n border-radius:8px;border:1px solid var(--line2);background:var(--surface2);color:var(--dim);\n cursor:pointer;flex:none}\n.icon-btn:hover{color:var(--txt);border-color:var(--dim2)}\n.icon-btn svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8}\n.wrap{max-width:1080px;margin:0 auto;padding:16px 24px 60px}\n.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap}\nh2{font-size:15px;font-weight:600}\nselect{padding:7px 10px;border-radius:8px;border:1px solid var(--line2);background:var(--surface);\n color:var(--txt);font:inherit;font-size:12.5px}\n.btn{display:inline-flex;align-items:center;gap:7px;border:1px solid transparent;cursor:pointer;\n font:inherit;font-size:13px;font-weight:500;border-radius:8px;padding:7px 14px;text-decoration:none}\n.btn-primary{background:var(--accent);color:#fff;box-shadow:0 1px 2px var(--shadow)}\n.btn-primary:hover{background:var(--accent-h);box-shadow:0 2px 6px var(--shadow)}\n.btn-default{background:var(--surface2);color:var(--txt);border-color:var(--line2)}\n.btn-default:hover{border-color:var(--dim2)}\n.btn-quiet{background:transparent;color:var(--dim);padding:7px 9px}\n.btn-quiet:hover{color:var(--err);background:var(--err-bg)}\n.btn svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round}\n.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(84px,1fr));gap:18px 12px}\n.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px;\n display:flex;flex-direction:column;gap:10px;transition:border-color .15s,box-shadow .15s,transform .15s;\n box-shadow:0 1px 2px var(--shadow)}\n.card:hover{border-color:var(--line2);box-shadow:0 6px 16px var(--shadow);transform:translateY(-2px)}\n.card-top{display:flex;align-items:center;gap:10px}\n.card-top img{width:34px;height:34px;border-radius:9px;flex:none;object-fit:cover}\n.card-name{min-width:0;flex:1}\n.card-name b{font-size:14px;font-weight:600;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.card-name .path{color:var(--dim2);font-size:11px;font-family:ui-monospace,Menlo,monospace;\n overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.view-toggle{display:flex;border:1px solid var(--line2);border-radius:8px;overflow:hidden;flex:none}\n.view-toggle button{width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;\n border:none;background:var(--surface2);color:var(--dim);cursor:pointer;border-right:1px solid var(--line2)}\n.view-toggle button:last-child{border-right:none}\n.view-toggle button.active{background:var(--accent);color:#fff}\n.view-toggle svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8}\n.proj-dot{position:absolute;bottom:-1px;right:-1px;width:13px;height:13px;border-radius:50%;\n border:2px solid var(--surface2)}\n.proj-dot.on{background:var(--ok)}\n.proj-dot.off{background:var(--dim2)}\n.proj-dot.err{background:var(--err)}\n.proj-dot.looping{background:var(--err);animation:proj-pulse 1.4s ease-in-out infinite}\n@keyframes proj-pulse{0%,100%{opacity:1}50%{opacity:.35}}\n.proj-menu-wrap{position:absolute;top:2px;right:2px}\n.proj-menu-btn{width:24px;height:24px;border-radius:7px;border:none;background:transparent;color:var(--dim);\n opacity:.55;display:inline-flex;align-items:center;justify-content:center;cursor:pointer;transition:opacity .15s}\n.proj-menu-btn:hover{background:var(--surface);color:var(--txt);opacity:1}\n.proj-menu-btn svg{width:15px;height:15px;fill:currentColor}\n.proj-menu{position:absolute;top:calc(100% + 4px);right:0;width:172px;background:var(--surface);\n border:1px solid var(--line2);border-radius:10px;box-shadow:0 12px 30px var(--shadow);\n display:none;z-index:20;overflow:hidden;padding:6px;text-align:left}\n.proj-menu.show{display:block}\n.proj-menu .acct-item.danger{color:var(--err)}\n.proj-menu .acct-item.danger:hover{background:var(--err-bg)}\n\n/* -------- mode grille : tuiles style icone iOS -------- */\n.proj-tile{position:relative;display:flex;flex-direction:column;align-items:center;gap:6px;\n padding:6px 4px;border-radius:14px;cursor:default;transition:background .15s}\n.proj-tile:hover{background:var(--surface)}\n.proj-tile .proj-link{display:flex;flex-direction:column;align-items:center;gap:6px;width:100%;\n text-decoration:none;color:inherit}\n.proj-tile .proj-icon-wrap{position:relative;width:100%;aspect-ratio:1}\n.proj-tile .proj-icon-wrap img{width:100%;height:100%;border-radius:22%;object-fit:cover;display:block;\n box-shadow:0 2px 6px var(--shadow)}\n.proj-tile .proj-name{font-size:11px;font-weight:500;max-width:100%;overflow:hidden;text-overflow:ellipsis;\n white-space:nowrap;text-align:center}\n.proj-tile.add-tile{color:var(--dim);cursor:pointer}\n.proj-tile.add-tile:hover{color:var(--accent);background:transparent}\n.add-tile-icon{width:100%;aspect-ratio:1;border:2px dashed var(--line2);border-radius:22%;\n display:flex;align-items:center;justify-content:center}\n.proj-tile.add-tile:hover .add-tile-icon{border-color:var(--accent)}\n.add-tile-icon svg{width:30%;height:30%;stroke:currentColor;fill:none;stroke-width:1.6}\n\n/* -------- mode liste -------- */\n.grid.list-mode{display:flex;flex-direction:column;gap:2px}\n.proj-row{position:relative;display:flex;align-items:center;gap:12px;padding:8px 40px 8px 8px;\n border-radius:9px;transition:background .15s}\n.proj-row:hover{background:var(--surface)}\n.proj-row-link{display:flex;align-items:center;gap:12px;flex:1;min-width:0;text-decoration:none;color:inherit}\n.proj-row-icon-wrap{position:relative;flex:none}\n.proj-row-icon-wrap img{width:32px;height:32px;border-radius:9px;object-fit:cover;display:block}\n.proj-row-name{font-size:13.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.proj-row .proj-menu-wrap{top:50%;transform:translateY(-50%);right:8px}\n.proj-row.add-row{cursor:pointer;color:var(--dim)}\n.proj-row.add-row:hover{color:var(--accent)}\n.add-row-icon{width:32px;height:32px;border:2px dashed var(--line2);border-radius:9px;flex:none;\n display:flex;align-items:center;justify-content:center}\n.proj-row.add-row:hover .add-row-icon{border-color:var(--accent)}\n.add-row-icon svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8}\n.empty{border:1px dashed var(--line2);border-radius:var(--r);padding:52px 24px;text-align:center;grid-column:1/-1}\n.empty h3{font-size:15px;font-weight:600;margin-bottom:5px}.empty p{color:var(--dim);font-size:13px}\n.ov{position:fixed;inset:0;background:rgba(10,12,16,.6);display:none;align-items:center;\n justify-content:center;padding:20px;z-index:50}\n.ov.show{display:flex}\n.modal{background:var(--surface);border:1px solid var(--line2);border-radius:14px;width:100%;\n max-width:560px;max-height:88vh;overflow:auto;box-shadow:0 16px 48px var(--shadow)}\n.mh{padding:20px 22px 0}.mh h3{font-size:15px;font-weight:600}\n.mh p{color:var(--dim);font-size:13px;margin-top:3px}\n.mb{padding:18px 22px}\n.mf{padding:14px 22px;display:flex;gap:8px;justify-content:flex-end;border-top:1px solid var(--line)}\nlabel{display:block;font-size:12px;color:var(--dim);margin-bottom:6px;font-weight:500}\n.field{margin-bottom:15px}\ninput,textarea{width:100%;padding:8px 11px;border-radius:8px;border:1px solid var(--line2);\n background:var(--bg-input);color:var(--txt);font:inherit;font-size:13.5px;\n box-shadow:inset 0 1px 2px var(--shadow)}\ninput:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ring)}\n.hint{font-size:11.5px;color:var(--dim2);margin-top:5px}\ncode{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:var(--bg-input);\n border:1px solid var(--line);padding:1px 5px;border-radius:4px}\n.tabs{display:flex;gap:6px;margin-bottom:16px}\n.tab{flex:1;padding:8px;text-align:center;border-radius:8px;border:1px solid var(--line2);\n background:var(--surface2);color:var(--dim);font-size:12.5px;font-weight:500;cursor:pointer}\n.tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}\n.tpls{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:15px}\n.tpl{padding:12px 8px;border-radius:8px;border:1px solid var(--line2);background:var(--surface2);\n text-align:center;cursor:pointer;font-size:12px;font-weight:500;color:var(--dim)}\n.tpl.active{border-color:var(--accent);color:var(--accent);background:var(--accent-ring)}\n.fb{border:1px solid var(--line2);border-radius:8px;overflow:hidden;background:var(--bg-input)}\n.fb-cur{padding:7px 11px;font-size:11.5px;color:var(--dim);background:var(--surface2);\n border-bottom:1px solid var(--line);font-family:ui-monospace,Menlo,monospace;word-break:break-all}\n.fb-l{max-height:150px;overflow:auto}\n.fb-l div{padding:7px 11px;font-size:13px;cursor:pointer;border-bottom:1px solid var(--line)}\n.fb-l div:hover{background:var(--surface2)}\n.suggest{margin-top:6px;font-size:11.5px;color:var(--dim);cursor:pointer}\n.suggest b{color:var(--accent)}\n.alert{background:var(--err-bg);border:1px solid var(--err-border);color:var(--err-txt);\n padding:9px 12px;border-radius:8px;font-size:12.5px;margin-bottom:14px;display:none}\n.logbox{background:var(--bg-input);border:1px solid var(--line);border-radius:var(--r);padding:14px 18px;\n box-shadow:inset 0 1px 4px var(--shadow);\n font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--dim);white-space:pre-wrap;line-height:1.6;\n min-height:320px;max-height:62vh;overflow:auto}\n.settings-card{max-width:560px;margin-bottom:14px}\n.settings-card .field{margin-bottom:0}\n.seg-toggle{display:flex;border:1px solid var(--line2);border-radius:8px;overflow:hidden;max-width:320px}\n.seg-toggle button{flex:1;padding:8px 4px;border:none;background:var(--surface2);color:var(--dim);\n font:inherit;font-size:12.5px;font-weight:500;cursor:pointer;border-right:1px solid var(--line2)}\n.seg-toggle button:last-child{border-right:none}\n.seg-toggle button.active{background:var(--accent);color:#fff}\n.zone{background:var(--surface2);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:16px}\n.zone-title{font-size:11px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.04em;\n margin-bottom:14px}\n.ov-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px}\n.ov-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px;\n box-shadow:0 1px 2px var(--shadow)}\n.ov-card h4{font-size:12.5px;font-weight:600;color:var(--dim);margin-bottom:12px}\n.ov-nums{display:flex;gap:20px}\n.ov-nums div b{display:block;font-size:19px;font-weight:700}\n.ov-nums div span{font-size:11px;color:var(--dim)}\n.healthbar{height:8px;border-radius:20px;overflow:hidden;display:flex;background:var(--line2);margin-bottom:9px;\n box-shadow:inset 0 1px 2px var(--shadow)}\n.healthbar span{height:100%}\n.health-legend{display:flex;gap:12px;font-size:11px;color:var(--dim);flex-wrap:wrap}\n.health-legend i{display:inline-block;width:7px;height:7px;border-radius:2px;margin-right:4px}\n.res-row{display:flex;flex-direction:column;gap:12px}\n.res-label{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-bottom:4px}\n.res-bar{height:6px;border-radius:20px;background:var(--line2);overflow:hidden;box-shadow:inset 0 1px 2px var(--shadow)}\n.res-bar span{display:block;height:100%;background:var(--accent);border-radius:20px}\n.chart-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;\n margin-bottom:12px;box-shadow:0 1px 2px var(--shadow)}\n.chart-card h4{font-size:12.5px;font-weight:600;color:var(--dim);margin-bottom:14px}\n.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}\n.bar-row:last-child{margin-bottom:0}\n.bar-name{width:120px;flex:none;font-size:12px;color:var(--txt);overflow:hidden;text-overflow:ellipsis;\n white-space:nowrap}\n.bar-track{flex:1;height:8px;border-radius:20px;background:var(--line2);overflow:hidden;box-shadow:inset 0 1px 2px var(--shadow)}\n.bar-track span{display:block;height:100%;background:var(--accent);border-radius:20px}\n.bar-val{width:48px;flex:none;text-align:right;font-size:11.5px;color:var(--dim)}\n.spark-wrap{background:var(--bg-input);border-radius:8px;padding:8px 10px}\n.spark-wrap svg{display:block;width:100%;height:60px}\n.log-search{margin:0 22px 10px;position:relative}\n.log-search svg{position:absolute;left:9px;top:50%;transform:translateY(-50%);width:13px;height:13px;\n stroke:var(--dim2);fill:none;stroke-width:2}\n.log-search input{padding-left:30px;font-size:12.5px}\n.chart-empty{color:var(--dim);font-size:12.5px}\n@media (max-width:820px){.ov-grid{grid-template-columns:1fr}}\n@media (max-width:700px){\n  .sidebar{width:60px}\n  .side-item span{display:none}\n  .side-item{justify-content:center;padding:10px}\n  .topbar-brand b{display:none}\n}\n</style></head><body>\n<div class="topbar">\n  <button class="side-toggle-btn" onclick="toggleSidebar()" title="Reduire / etendre le menu">\n    <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><line x1="9" y1="3" x2="9" y2="21"/></svg>\n  </button>\n  <div class="topbar-brand" onclick="showSection(\'overview\')" title="Vue d\'ensemble">\n    <img src="data:image/png;base64,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" alt="CodeLab"><b>CodeLab</b>\n  </div>\n  <div class="spacer"></div>\n  <div class="acct-wrap">\n    <button class="acct-btn" id="acct-btn" onclick="toggleAcctMenu()" title="Compte">\n      <svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8"/></svg></button>\n    <div class="acct-menu" id="acct-menu">\n      <div class="acct-item" onclick="goToSettings()">\n        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/>\n        <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>\n        <span>Parametres</span></div>\n      <div class="acct-item" onclick="doLogout()">\n        <svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>\n        <path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>\n        <span>Deconnexion</span></div>\n    </div>\n  </div>\n</div>\n\n<div class="page-body">\n\n<nav class="sidebar" id="sidebar">\n  <div class="side-nav">\n    <div class="side-item active" data-sec="overview" onclick="showSection(\'overview\')">\n      <svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/></svg>\n      <span>Vue d\'ensemble</span></div>\n    <div class="side-item" data-sec="apps" onclick="showSection(\'apps\')">\n      <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>\n      <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>\n      <span>Projets</span></div>\n  </div>\n</nav>\n\n<main class="main-area">\n  <div class="wrap">\n\n<div id="sec-overview" class="section">\n  <div class="zone">\n    <div class="zone-title">Resume</div>\n    <div class="ov-grid">\n      <div class="ov-card">\n        <h4>Applications</h4>\n        <div class="ov-nums">\n          <div><b id="ov-total">0</b><span>Total</span></div>\n          <div><b id="ov-run">0</b><span>En ligne</span></div>\n          <div><b id="ov-off">0</b><span>Arretees</span></div>\n        </div>\n      </div>\n      <div class="ov-card">\n        <h4>Sante</h4>\n        <div class="healthbar"><span id="hb-on" style="background:var(--ok)"></span><span id="hb-off"\n          style="background:var(--line2)"></span><span id="hb-err" style="background:var(--err)"></span></div>\n        <div class="health-legend">\n          <span><i style="background:var(--ok)"></i>En ligne</span>\n          <span><i style="background:var(--line2)"></i>Arretee</span>\n          <span><i style="background:var(--err)"></i>Erreur</span>\n        </div>\n      </div>\n      <div class="ov-card">\n        <h4>Ressources</h4>\n        <div class="res-row">\n          <div>\n            <div class="res-label"><span>CPU cumule</span><span id="res-cpu-val">0%</span></div>\n            <div class="res-bar"><span id="res-cpu-bar" style="width:0%"></span></div>\n          </div>\n          <div>\n            <div class="res-label"><span>Memoire cumulee</span><span id="res-mem-val">0 Mo</span></div>\n            <div class="res-bar"><span id="res-mem-bar" style="width:0%"></span></div>\n          </div>\n        </div>\n      </div>\n    </div>\n  </div>\n\n  <div class="zone">\n    <div class="zone-title">Activite</div>\n    <div class="chart-card">\n      <h4>CPU par application</h4>\n      <div id="ov-chart-cpu"></div>\n    </div>\n    <div class="chart-card">\n      <h4>Memoire par application</h4>\n      <div id="ov-chart-mem"></div>\n    </div>\n  </div>\n</div>\n\n<div id="sec-apps" class="section" style="display:none">\n  <div class="toolbar">\n    <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>\n      <input id="search" placeholder="Rechercher..." oninput="render()"></div>\n    <div class="spacer"></div>\n    <div class="view-toggle">\n      <button class="view-btn active" data-view="grid" onclick="setViewMode(\'grid\')" title="Grille">\n        <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>\n        <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg></button>\n      <button class="view-btn" data-view="list" onclick="setViewMode(\'list\')" title="Liste">\n        <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg></button>\n    </div>\n  </div>\n  <div class="zone">\n    <div class="zone-title">Projets</div>\n    <div id="grid" class="grid"></div>\n  </div>\n</div>\n\n<div id="sec-settings" class="section" style="display:none">\n  <div class="toolbar"><h2>Parametres du compte</h2></div>\n\n  <div class="zone">\n    <div class="zone-title">Preferences</div>\n    <div class="card settings-card">\n      <div class="card-top"><div class="card-name">\n        <b>Apparence</b>\n        <div class="path">Choisis comment l\'interface s\'affiche.</div>\n      </div></div>\n      <div class="field">\n        <div class="seg-toggle" id="theme-toggle">\n          <button data-t="auto" onclick="setTheme(\'auto\')">Auto</button>\n          <button data-t="light" onclick="setTheme(\'light\')">Clair</button>\n          <button data-t="dark" onclick="setTheme(\'dark\')">Sombre</button>\n        </div>\n      </div>\n    </div>\n  </div>\n\n  <div class="zone">\n    <div class="zone-title">Session</div>\n    <div class="card settings-card">\n      <div class="card-top"><div class="card-name">\n        <b>Session</b>\n        <div class="path">Deconnecte cette session du panneau CodeLab.</div>\n      </div></div>\n      <button class="btn btn-default" onclick="doLogout()">\n        <svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>\n        <path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>Deconnexion</button>\n    </div>\n  </div>\n</div>\n\n  </div>\n</main>\n</div>\n\n<!-- Modal ajout / edition -->\n<div class="ov" id="ov-add"><div class="modal">\n  <div class="mh"><h3 id="a-t">Ajouter une application</h3><p id="a-s"></p></div>\n  <div class="mb"><div class="alert" id="a-e"></div>\n\n    <div class="tabs" id="mode-tabs">\n      <div class="tab active" data-mode="existing" onclick="setMode(\'existing\')">Dossier existant</div>\n      <div class="tab" data-mode="new" onclick="setMode(\'new\')">Nouveau projet</div>\n    </div>\n\n    <div id="tpl-block" style="display:none">\n      <div class="tpls">\n        <div class="tpl active" data-tpl="static" onclick="setTpl(\'static\')">Statique<br>(HTML)</div>\n        <div class="tpl" data-tpl="flask" onclick="setTpl(\'flask\')">Flask<br>(Python)</div>\n        <div class="tpl" data-tpl="node" onclick="setTpl(\'node\')">Node<br>(JS)</div>\n        <div class="tpl" data-tpl="api" onclick="setTpl(\'api\')">API<br>(Python)</div>\n      </div>\n    </div>\n\n    <div class="field"><label>Nom du projet</label>\n      <input id="f-name" placeholder="portfolio" autocomplete="off">\n      <div class="hint">Accessible sur <code id="f-url">/nom/</code></div></div>\n\n    <div class="field" id="browse-block"><label>Dossier</label>\n      <div class="fb"><div class="fb-cur" id="fb-c">/</div><div class="fb-l" id="fb-l"></div></div>\n      <div class="suggest" id="suggest" style="display:none" onclick="applySuggestion()"></div>\n    </div>\n\n    <div class="field"><label>Commande de lancement</label>\n      <input id="f-cmd" placeholder="python -m http.server $PORT" autocomplete="off">\n      <div class="hint">La variable <code>$PORT</code> est fournie : l\'app doit ecouter dessus.</div></div>\n\n    <div class="field"><label>Commande de build (optionnelle)</label>\n      <input id="f-build" placeholder="npm install" autocomplete="off">\n      <div class="hint">Executee a la demande, separement du lancement (menu "..." de la carte).</div></div>\n\n    <div class="field"><label>Limite memoire en Mo (optionnelle)</label>\n      <input id="f-mem" type="number" min="16" placeholder="256">\n      <div class="hint">Le process est arrete automatiquement s\'il tente de la depasser.</div></div>\n  </div>\n  <div class="mf"><button class="btn btn-default" onclick="hide(\'ov-add\')">Annuler</button>\n    <button class="btn btn-primary" id="a-b" onclick="submitAdd()">Ajouter</button></div>\n</div></div>\n\n<!-- Pop-up des logs en direct -->\n<div class="ov" id="ov-logs"><div class="modal">\n  <div class="mh"><h3 id="lg-title">Journal</h3></div>\n  <div class="log-search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>\n    <input id="lg-search" placeholder="Filtrer..." oninput="renderLogFilter()"></div>\n  <div class="mb"><div class="logbox" id="lg-body">Connexion...</div></div>\n  <div class="mf"><button class="btn btn-default" onclick="closeLogsModal()">Fermer</button></div>\n</div></div>\n\n<!-- Pop-up des metriques (historique CPU / memoire) -->\n<div class="ov" id="ov-metrics"><div class="modal">\n  <div class="mh"><h3 id="mt-title">Metriques</h3><p>Sur les ~2 dernieres minutes.</p></div>\n  <div class="mb">\n    <div class="field"><label>CPU (%)</label><div class="spark-wrap" id="mt-cpu"></div></div>\n    <div class="field"><label>Memoire (Mo)</label><div class="spark-wrap" id="mt-mem"></div></div>\n  </div>\n  <div class="mf"><button class="btn btn-default" onclick="hide(\'ov-metrics\')">Fermer</button></div>\n</div></div>\n\n<script>\nconst $=i=>document.getElementById(i), hide=i=>$(i).classList.remove("show");\nconst esc=s=>(s||"").replace(/[&<>"]/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\'}[c]));\n\n/* ---------------- theme ---------------- */\nfunction applyTheme(t){\n  if(t===\'auto\') document.documentElement.removeAttribute(\'data-theme\');\n  else document.documentElement.setAttribute(\'data-theme\', t);\n  document.querySelectorAll(\'#theme-toggle button\').forEach(b=>b.classList.toggle(\'active\', b.dataset.t===t));\n}\nfunction setTheme(t){\n  localStorage.setItem(\'codelab-theme\', t);\n  applyTheme(t);\n}\napplyTheme(localStorage.getItem(\'codelab-theme\')||\'auto\');\n\n/* ---------------- barre laterale reductible ---------------- */\nfunction applySidebar(state){\n  $(\'sidebar\').classList.toggle(\'collapsed\', state===\'collapsed\');\n}\nfunction toggleSidebar(){\n  const next = $(\'sidebar\').classList.contains(\'collapsed\') ? \'expanded\' : \'collapsed\';\n  localStorage.setItem(\'codelab-sidebar\', next);\n  applySidebar(next);\n}\napplySidebar(localStorage.getItem(\'codelab-sidebar\')||\'expanded\');\n\n/* ---------------- menu compte (1er niveau : Parametres / Deconnexion) ---------------- */\nfunction toggleAcctMenu(){\n  $(\'acct-menu\').classList.toggle(\'show\');\n}\nfunction goToSettings(){\n  $(\'acct-menu\').classList.remove(\'show\');\n  showSection(\'settings\');\n}\ndocument.addEventListener(\'click\', e=>{\n  const wrap=document.querySelector(\'.acct-wrap\');\n  if(wrap && !wrap.contains(e.target)) $(\'acct-menu\').classList.remove(\'show\');\n});\n\n/* ---------------- navigation ---------------- */\nfunction showSection(name){\n  document.querySelectorAll(\'.section\').forEach(s=>{ s.style.display = (s.id===\'sec-\'+name) ? \'\' : \'none\'; });\n  document.querySelectorAll(\'.side-item\').forEach(t=>t.classList.toggle(\'active\', t.dataset.sec===name));\n  $(\'acct-btn\').classList.toggle(\'active\', name===\'settings\');\n}\n\nfunction barChart(list, key, unit, maxHint){\n  const items=list.filter(a=>a.running);\n  if(!items.length) return \'<div class="chart-empty">Aucune application en ligne pour le moment.</div>\';\n  const max=Math.max(maxHint, ...items.map(a=>a[key]||0));\n  return items.slice().sort((a,b)=>(b[key]||0)-(a[key]||0)).map(a=>{\n    const v=a[key]||0, pct=max?Math.min(100, v/max*100):0;\n    return `<div class="bar-row"><div class="bar-name" title="${esc(a.name)}">${esc(a.name)}</div>\n     <div class="bar-track"><span style="width:${pct}%"></span></div>\n     <div class="bar-val">${v.toFixed(1)}${unit}</div></div>`;\n  }).join(\'\');\n}\n\nfunction renderOverview(){\n  const total=apps.length, running=apps.filter(a=>a.running).length;\n  const failed=apps.filter(a=>a.failed).length;\n  const stopped=total-running-failed;\n  $(\'ov-total\').textContent=total;\n  $(\'ov-run\').textContent=running;\n  $(\'ov-off\').textContent=stopped+failed;\n  const pct=n=>total?(n/total*100):0;\n  $(\'hb-on\').style.width=pct(running)+\'%\';\n  $(\'hb-off\').style.width=pct(stopped)+\'%\';\n  $(\'hb-err\').style.width=pct(failed)+\'%\';\n\n  const cpuSum=apps.reduce((s,a)=>s+(a.cpu_percent||0),0);\n  const memSum=apps.reduce((s,a)=>s+(a.memory_mb||0),0);\n  $(\'res-cpu-val\').textContent=cpuSum.toFixed(1)+\'%\';\n  $(\'res-cpu-bar\').style.width=Math.min(100,cpuSum)+\'%\';\n  $(\'res-mem-val\').textContent=memSum.toFixed(0)+\' Mo\';\n  $(\'res-mem-bar\').style.width=Math.min(100, memSum/1024*100)+\'%\';\n\n  $(\'ov-chart-cpu\').innerHTML=barChart(apps, \'cpu_percent\', \'%\', 10);\n  $(\'ov-chart-mem\').innerHTML=barChart(apps, \'memory_mb\', \' Mo\', 64);\n}\n\n/* ---------------- data ---------------- */\nlet apps=[];\nasync function refresh(){\n  const r=await fetch(\'/api/apps\');\n  if(r.status===401){ location.href=\'/login\'; return; }\n  apps=(await r.json()).apps;\n  render();\n}\nlet viewMode=localStorage.getItem(\'codelab-view\')||\'grid\';\n\nfunction setViewMode(m){\n  if(viewMode===m) return;\n  viewMode=m;\n  localStorage.setItem(\'codelab-view\', m);\n  document.querySelectorAll(\'.view-btn\').forEach(b=>b.classList.toggle(\'active\', b.dataset.view===m));\n  render();\n}\n\nfunction render(){\n  const q=($(\'search\').value||\'\').toLowerCase();\n  let list=apps.filter(a=>a.name.includes(q));\n  list.sort((a,b)=>a.name.localeCompare(b.name));\n  const isList=viewMode===\'list\';\n  const renderer=isList?cardList:cardGrid;\n  const addTile=isList\n   ?`<div class="proj-row add-row" onclick="openAdd()">\n      <div class="add-row-icon"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></div>\n      <div class="proj-row-name">Nouveau projet</div></div>`\n   :`<div class="proj-tile add-tile" onclick="openAdd()">\n      <div class="add-tile-icon"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></div>\n      <div class="proj-name">Nouveau projet</div></div>`;\n  $(\'grid\').classList.toggle(\'list-mode\', isList);\n  $(\'grid\').innerHTML=list.map(renderer).join(\'\') + addTile;\n  renderOverview();\n}\nfunction menuItems(a){\n  const items=[];\n  if(!a.running) items.push(`<div class="acct-item" onclick="closeCardMenus();openEdit(\'${a.name}\')">\n   <svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>Modifier</div>`);\n  if(a.running) items.push(`<div class="acct-item" onclick="closeCardMenus();restartApp(\'${a.name}\')">\n   <svg viewBox="0 0 24 24"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/>\n   <path d="M3.5 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.5 15"/></svg>Redemarrer</div>`);\n  items.push(`<div class="acct-item" onclick="closeCardMenus();tg(\'${a.name}\')">\n   <svg viewBox="0 0 24 24"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 11-12.8 0"/></svg>${a.running?\'Desactiver\':\'Activer\'}</div>`);\n  if(a.has_build) items.push(`<div class="acct-item" onclick="closeCardMenus();buildApp(\'${a.name}\')">\n   <svg viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>Lancer le build</div>`);\n  if(a.is_git) items.push(`<div class="acct-item" onclick="closeCardMenus();gitPullApp(\'${a.name}\')">\n   <svg viewBox="0 0 24 24"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 012 2v7"/><path d="M6 9v12"/></svg>Git pull</div>`);\n  items.push(`<div class="acct-item" onclick="closeCardMenus();openMetricsModal(\'${a.name}\')">\n   <svg viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="M18 17V9M13 17V5M8 17v-3"/></svg>Metriques</div>`);\n  items.push(`<div class="acct-item" onclick="closeCardMenus();openLogsModal(\'${a.name}\')">\n   <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Voir les logs</div>`);\n  items.push(`<div class="acct-item danger" onclick="closeCardMenus();rm(\'${a.name}\')">\n   <svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>Supprimer</div>`);\n  return items.join(\'\');\n}\nfunction menuBtn(a){\n  return `<div class="proj-menu-wrap">\n   <button class="proj-menu-btn" onclick="toggleCardMenu(event,\'${a.name}\')" title="Actions">\n    <svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg></button>\n   <div class="proj-menu" id="menu-${a.name}">${menuItems(a)}</div>\n  </div>`;\n}\nfunction dotClass(a){\n  if(a.crash_looping) return \'looping\';\n  return a.running?\'on\':(a.failed?\'err\':\'off\');\n}\nfunction cardGrid(a){\n  const dot=dotClass(a);\n  return `<div class="proj-tile">\n   <a class="proj-link" href="/${a.name}/" target="_blank" rel="noopener">\n    <div class="proj-icon-wrap"><img src="/api/icon/${a.name}" alt="">\n     <span class="proj-dot ${dot}" title="${a.crash_looping?\'Plusieurs echecs de suite, redemarrage automatique interrompu\':\'\'}"></span></div>\n    <div class="proj-name" title="${esc(a.name)}">${esc(a.name)}</div>\n   </a>\n   ${menuBtn(a)}\n  </div>`;\n}\nfunction cardList(a){\n  const dot=dotClass(a);\n  return `<div class="proj-row">\n   <a class="proj-row-link" href="/${a.name}/" target="_blank" rel="noopener">\n    <div class="proj-row-icon-wrap"><img src="/api/icon/${a.name}" alt="">\n     <span class="proj-dot ${dot}" title="${a.crash_looping?\'Plusieurs echecs de suite, redemarrage automatique interrompu\':\'\'}"></span></div>\n    <div class="proj-row-name" title="${esc(a.name)}">${esc(a.name)}</div>\n   </a>\n   ${menuBtn(a)}\n  </div>`;\n}\nlet openMenuName=null;\nfunction toggleCardMenu(ev, name){\n  ev.preventDefault(); ev.stopPropagation();\n  const wasOpen=openMenuName===name;\n  closeCardMenus();\n  if(!wasOpen){\n    const el=$(\'menu-\'+name);\n    if(el) el.classList.add(\'show\');\n    openMenuName=name;\n  }\n}\nfunction closeCardMenus(){\n  document.querySelectorAll(\'.proj-menu.show\').forEach(m=>m.classList.remove(\'show\'));\n  openMenuName=null;\n}\ndocument.addEventListener(\'click\', e=>{\n  if(!e.target.closest(\'.proj-menu-wrap\')) closeCardMenus();\n});\nasync function tg(n){ await fetch(\'/api/toggle/\'+n,{method:\'POST\'}); setTimeout(refresh,400); }\nasync function restartApp(n){ await fetch(\'/api/restart/\'+n,{method:\'POST\'}); setTimeout(refresh,500); }\nasync function buildApp(n){\n  const r=await fetch(\'/api/build/\'+n,{method:\'POST\'});\n  const d=await r.json();\n  if(!r.ok){ alert(d.error||\'Le build a echoue.\'); return; }\n  openLogsModal(n);\n}\nasync function gitPullApp(n){\n  const r=await fetch(\'/api/git-pull/\'+n,{method:\'POST\'});\n  const d=await r.json();\n  if(!r.ok){ alert(d.error||\'git pull a echoue.\'); return; }\n  alert(d.output||\'git pull termine.\');\n}\nasync function rm(n){ if(!confirm(\'Supprimer "\'+n+\'" ? Le dossier du projet n\\\'est pas touche.\'))return;\n await fetch(\'/api/app/\'+n,{method:\'DELETE\'}); refresh(); }\nasync function doLogout(){ await fetch(\'/logout\',{method:\'POST\'}); location.href=\'/login\'; }\n\n/* ---------------- modal ajout / edition ---------------- */\nlet mode=\'existing\', tpl=\'static\', cur=\'\', editing=null, ROOT=\'\';\nconst TPL_CMD={static:\'python3 -m http.server $PORT\', flask:\'python3 app.py\', node:\'node index.js\', api:\'python3 app.py\'};\n\nfunction setMode(m){\n  mode=m;\n  document.querySelectorAll(\'#mode-tabs .tab\').forEach(t=>t.classList.toggle(\'active\', t.dataset.mode===m));\n  $(\'tpl-block\').style.display = m===\'new\' ? \'\' : \'none\';\n  $(\'browse-block\').style.display = m===\'new\' ? \'none\' : \'\';\n  if(m===\'new\'){ $(\'f-cmd\').value=TPL_CMD[tpl]; $(\'f-url\').textContent=\'/\'+(slug($(\'f-name\').value)||\'nom\')+\'/\'; }\n}\nfunction setTpl(t){\n  tpl=t;\n  document.querySelectorAll(\'.tpl\').forEach(e=>e.classList.toggle(\'active\', e.dataset.tpl===t));\n  $(\'f-cmd\').value=TPL_CMD[t];\n}\nfunction slug(s){ return (s||\'\').trim().toLowerCase().replace(/[^a-z0-9_-]/g,\'-\'); }\n\nfunction openAdd(){\n  editing=null; mode=\'existing\'; tpl=\'static\';\n  $(\'a-t\').textContent=\'Ajouter une application\';\n  $(\'a-s\').textContent=\'Un dossier existant, ou un nouveau projet cree depuis un modele.\';\n  $(\'a-b\').textContent=\'Ajouter\'; $(\'a-e\').style.display=\'none\';\n  $(\'f-name\').value=\'\'; $(\'f-cmd\').value=\'\'; $(\'f-build\').value=\'\'; $(\'f-mem\').value=\'\';\n  $(\'suggest\').style.display=\'none\';\n  document.querySelectorAll(\'#mode-tabs .tab\').forEach(t=>t.classList.toggle(\'active\', t.dataset.mode===\'existing\'));\n  document.querySelectorAll(\'.tpl\').forEach(e=>e.classList.toggle(\'active\', e.dataset.tpl===\'static\'));\n  setMode(\'existing\');\n  $(\'f-name\').oninput=()=>$(\'f-url\').textContent=\'/\'+(slug($(\'f-name\').value)||\'nom\')+\'/\';\n  browse(ROOT);\n  $(\'ov-add\').classList.add(\'show\'); $(\'f-name\').focus();\n}\nfunction openEdit(name){\n  const a=apps.find(x=>x.name===name); if(!a) return;\n  editing=name; mode=\'existing\';\n  $(\'a-t\').textContent=\'Modifier \'+name;\n  $(\'a-s\').textContent=\'Change le dossier ou la commande de lancement.\';\n  $(\'a-b\').textContent=\'Enregistrer\'; $(\'a-e\').style.display=\'none\';\n  $(\'f-name\').value=name; $(\'f-name\').disabled=true;\n  $(\'f-cmd\').value=a.command;\n  $(\'f-build\').value=a.build_command||\'\'; $(\'f-mem\').value=a.max_memory_mb||\'\';\n  $(\'suggest\').style.display=\'none\';\n  $(\'mode-tabs\').style.display=\'none\'; $(\'tpl-block\').style.display=\'none\'; $(\'browse-block\').style.display=\'\';\n  cur=a.path; $(\'fb-c\').textContent=a.path.replace(ROOT,\'\')||\'/\';\n  browse(a.path);\n  $(\'ov-add\').classList.add(\'show\');\n}\nasync function browse(p){\n  const d=await (await fetch(\'/api/browse?path=\'+encodeURIComponent(p))).json();\n  cur=d.path; $(\'fb-c\').textContent=d.label;\n  let h=d.hasIndex?\'<div style="color:var(--ok)">&check; index.html present dans ce dossier</div>\':\'\';\n  if(d.parent) h+=`<div onclick="browse(\'${d.parent}\')">&#8617; Dossier parent</div>`;\n  h+=d.dirs.map(n=>`<div onclick="browse(\'${d.path}/${n}\')">&#128193; ${esc(n)}</div>`).join(\'\');\n  $(\'fb-l\').innerHTML=h||\'<div style="color:var(--dim);cursor:default">Dossier vide</div>\';\n  if(mode===\'existing\' && !editing){\n    const dd=await (await fetch(\'/api/detect?path=\'+encodeURIComponent(cur))).json();\n    if(dd.command){\n      $(\'suggest\').style.display=\'block\';\n      $(\'suggest\').innerHTML=\'Suggestion detectee : <b>\'+esc(dd.command)+\'</b> (cliquer pour appliquer)\';\n      $(\'suggest\').dataset.cmd=dd.command;\n    } else { $(\'suggest\').style.display=\'none\'; }\n  }\n}\nfunction applySuggestion(){ $(\'f-cmd\').value=$(\'suggest\').dataset.cmd||\'\'; }\nfunction err(m){ const e=$(\'a-e\'); e.textContent=m; e.style.display=\'block\'; }\n\nasync function submitAdd(){\n  const name=$(\'f-name\').value.trim(), c=$(\'f-cmd\').value.trim();\n  const build_command=$(\'f-build\').value.trim();\n  const max_memory_mb=$(\'f-mem\').value ? parseInt($(\'f-mem\').value, 10) : null;\n  if(!name) return err(\'Donne un nom au projet.\');\n  if(!c) return err(\'Indique la commande de lancement.\');\n  $(\'a-e\').style.display=\'none\';\n\n  if(editing){\n    const r=await fetch(\'/api/app/\'+editing,{method:\'PUT\',headers:{\'Content-Type\':\'application/json\'},\n     body:JSON.stringify({path:cur,command:c,build_command,max_memory_mb})});\n    const d=await r.json();\n    if(!r.ok) return err(d.error);\n    $(\'f-name\').disabled=false; $(\'mode-tabs\').style.display=\'\';\n    hide(\'ov-add\'); refresh(); return;\n  }\n\n  if(mode===\'new\'){\n    const r=await fetch(\'/api/create\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},\n     body:JSON.stringify({name,template:tpl,command:c,build_command,max_memory_mb})});\n    const d=await r.json();\n    if(!r.ok) return err(d.error);\n    hide(\'ov-add\'); refresh(); return;\n  }\n\n  const r=await fetch(\'/api/add\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},\n   body:JSON.stringify({name,path:cur,command:c,build_command,max_memory_mb})});\n  const d=await r.json();\n  if(!r.ok) return err(d.error);\n  hide(\'ov-add\'); refresh();\n}\n\n/* ---------------- logs en direct (pop-up, depuis le menu d\'une carte) ---------------- */\nlet logEs=null, logLines=[];\nfunction openLogsModal(name){\n  $(\'lg-title\').textContent=\'Journal — \'+name;\n  $(\'lg-search\').value=\'\';\n  logLines=[];\n  $(\'lg-body\').textContent=\'Connexion...\';\n  $(\'ov-logs\').classList.add(\'show\');\n  if(logEs) logEs.close();\n  logEs=new EventSource(\'/api/logs/\'+name+\'/stream\');\n  let first=true;\n  logEs.onmessage=(e)=>{\n    if(first){ $(\'lg-body\').textContent=\'\'; first=false; }\n    logLines.push(e.data);\n    renderLogFilter();\n  };\n}\nfunction renderLogFilter(){\n  const q=($(\'lg-search\').value||\'\').toLowerCase();\n  const visible=q?logLines.filter(l=>l.toLowerCase().includes(q)):logLines;\n  $(\'lg-body\').textContent=visible.join(\'\\n\');\n  $(\'lg-body\').scrollTop=$(\'lg-body\').scrollHeight;\n}\nfunction closeLogsModal(){\n  $(\'ov-logs\').classList.remove(\'show\');\n  if(logEs){ logEs.close(); logEs=null; }\n}\n$(\'ov-logs\').addEventListener(\'click\', e=>{ if(e.target.id===\'ov-logs\') closeLogsModal(); });\n\n/* ---------------- metriques (pop-up, historique CPU/memoire) ---------------- */\nfunction sparklineSvg(values){\n  if(!values.length) return \'<div class="chart-empty">Pas encore de donnees -- l\\u0027application doit tourner quelques instants.</div>\';\n  const w=460, h=60, pad=4;\n  const max=Math.max(1, ...values);\n  const step=values.length>1?(w-2*pad)/(values.length-1):0;\n  const pts=values.map((v,i)=>{\n    const x=pad+i*step, y=h-pad-(v/max)*(h-2*pad);\n    return x.toFixed(1)+\',\'+y.toFixed(1);\n  }).join(\' \');\n  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">\n   <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2"\n    stroke-linejoin="round" stroke-linecap="round"/></svg>`;\n}\nasync function openMetricsModal(name){\n  $(\'mt-title\').textContent=\'Metriques — \'+name;\n  $(\'mt-cpu\').innerHTML=\'Chargement...\';\n  $(\'mt-mem\').innerHTML=\'Chargement...\';\n  $(\'ov-metrics\').classList.add(\'show\');\n  const r=await fetch(\'/api/metrics/\'+name);\n  const d=await r.json();\n  $(\'mt-cpu\').innerHTML=sparklineSvg(d.points.map(p=>p.cpu));\n  $(\'mt-mem\').innerHTML=sparklineSvg(d.points.map(p=>p.mem));\n}\n\ndocument.addEventListener(\'keydown\',e=>{if(e.key===\'Escape\'){hide(\'ov-add\');hide(\'ov-metrics\');closeLogsModal();$(\'acct-menu\').classList.remove(\'show\');closeCardMenus();}});\ndocument.querySelectorAll(\'.ov\').forEach(o=>o.addEventListener(\'click\',\n e=>{if(e.target===o)o.classList.remove(\'show\')}));\n\ndocument.querySelectorAll(\'.view-btn\').forEach(b=>b.classList.toggle(\'active\', b.dataset.view===viewMode));\n\nROOT=__ROOT__;\nrefresh(); setInterval(refresh,5000);\n</script>\n</body></html>\n'

# ------------------------------ pages --------------------------------

@flask_app.get("/login")
def login_page():
    if is_authed():
        return redirect("/")
    return Response(LOGIN_PAGE, mimetype="text/html")


@flask_app.get("/")
@require_auth
def index():
    return Response(DASHBOARD_PAGE.replace("__ROOT__", json.dumps(ROOT)), mimetype="text/html")


# ------------------------------ proxy --------------------------------

def _proxy(name, sub):
    a = load().get(name)
    if not a:
        return Response(_page("Introuvable", "Aucune application \u00ab " + name + " \u00bb."),
                        404, mimetype="text/html")
    if not is_running(name):
        return Response(_page("Application arretee",
                              "\u00ab " + name + " \u00bb n'est pas demarree.",
                              "Active-la depuis le panneau."), 503, mimetype="text/html")
    url = ("http://127.0.0.1:" + str(a["port"]) + "/"
           + urllib.parse.quote(sub, safe="/"))
    if request.query_string:
        url += "?" + request.query_string.decode()
    body = request.get_data() if request.method in ("POST", "PUT", "PATCH") else None
    req = urllib.request.Request(url, data=body, method=request.method)
    for k, v in request.headers:
        if k.lower() not in HOP and k.lower() != "host":
            req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        data, status, headers = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        data, status, headers = e.read(), e.code, e.headers
    except Exception:
        return Response(_page("Demarrage en cours",
                              "\u00ab " + name + " \u00bb ne repond pas encore.",
                              "Reessaie dans quelques secondes."), 502, mimetype="text/html")
    return Response(data, status, [(k, v) for k, v in headers.items()
                                   if k.lower() not in HOP])


def _from_referer():
    m = re.search(r"://[^/]+/([^/]+)/", request.headers.get("Referer", ""))
    if m and m.group(1) in load() and not request.path.startswith("/" + m.group(1) + "/"):
        return redirect("/" + m.group(1) + request.path, 302)
    return None


def _miss(name):
    return _from_referer() or Response(
        _page("Introuvable", "Aucune application \u00ab " + name + " \u00bb."),
        404, mimetype="text/html")


@flask_app.route("/<n>/", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@flask_app.route("/<n>/<path:sub>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def proxy(n, sub=""):
    return _proxy(n, sub) if n in load() else _miss(n)


@flask_app.route("/<n>")
def proxy_noslash(n):
    return redirect("/" + n + "/", 302) if n in load() else _miss(n)


@flask_app.errorhandler(404)
def not_found(e):
    return _miss(request.path.strip("/"))


def _page(title, msg, extra=""):
    return ("<!doctype html><meta charset=utf-8><title>" + title + "</title>"
            "<body style=\"margin:0;background:#0d1117;color:#c9d1d9;display:flex;"
            "align-items:center;justify-content:center;height:100vh;font:15px/1.6 "
            "-apple-system,Segoe UI,Roboto,sans-serif\"><div style=\"text-align:center\">"
            "<div style=\"font-size:17px;font-weight:600;color:#e6edf3;margin-bottom:6px\">"
            + title + "</div><div style=\"color:#8b949e\">" + msg + "</div>"
            "<div style=\"color:#6e7681;font-size:13px;margin-top:6px\">" + extra + "</div>"
            "<div style=\"margin-top:22px\"><a href=\"/\" style=\"color:#4c8eff;"
            "text-decoration:none;font-size:14px\">Retour au panneau</a></div></div>")


# -------------------------------- main --------------------------------

if __name__ == "__main__":
    bootstrap_secrets()
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(APPS_FILE):
        save({})
    resume()
    start_monitor_thread()
    flask_app.run(host="0.0.0.0",
                  port=int(os.environ.get("MANAGER_PORT", "9001")),
                  threaded=True)
