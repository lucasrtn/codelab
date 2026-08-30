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
codelab-postgres, pour consultation directe depuis le disque du ZimaOS
(nom sans "." pour rester visible dans le navigateur de fichiers ZimaOS).

Interface : "Vue d'ensemble" (accueil, stats + sante + actions rapides),
"Projets" (grille complete des applications), "Logs". Apparence et
identifiants deplaces dans le menu compte (avatar en haut a droite), plus
un onglet Parametres a part.
"""
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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


# --------------------------- bootstrap secrets ---------------------------

def upsert_shared_env(pairs):
    """Ecrit/met a jour des paires CLE=valeur dans credentials.env, partage
    avec codelab-postgres (/DATA/AppData/codelab/config/credentials.env sur
    le disque du ZimaOS -- pas de "." en tete de nom, le navigateur de
    fichiers ZimaOS ne propose pas d'afficher les fichiers caches). Ne touche
    qu'aux cles listees ici, laisse celles d'un autre service (ex.
    POSTGRES_*) intactes -- meme logique que le cote codelab-postgres dans
    docker-compose.yml, pour que les deux cohabitent sans jamais s'ecraser
    l'un l'autre."""
    try:
        os.makedirs(SHARED_CONFIG_DIR, exist_ok=True)
        existing = []
        if os.path.exists(SHARED_ENV_FILE):
            with open(SHARED_ENV_FILE) as f:
                existing = f.readlines()
        keys = set(pairs.keys())
        kept = [line for line in existing
                if not any(line.startswith(k + "=") for k in keys)]
        with open(SHARED_ENV_FILE, "w") as f:
            f.writelines(kept)
            for k, v in pairs.items():
                f.write(f"{k}={v}\n")
        os.chmod(SHARED_ENV_FILE, 0o600)
    except OSError as e:
        # Le volume partage n'est peut-etre pas monte (ex. test local sans
        # docker-compose) -- ne bloque jamais le demarrage du service pour
        # ca, c'est une commodite, pas une dependance critique.
        print(f"[app-manager] .env partage non ecrit ({e}), ignore.", flush=True)


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

    upsert_shared_env({
        "APP_MANAGER_URL": "http://<IP-ZimaOS>:9001",
        "APP_MANAGER_ADMIN_PASSWORD": admin_password() or "",
    })


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
    os.makedirs(LOG_DIR, exist_ok=True)
    out = open(os.path.join(LOG_DIR, name + ".log"), "ab", buffering=0)
    env = dict(os.environ, PORT=str(a["port"]), PYTHONUNBUFFERED="1")
    with lock:
        procs[name] = subprocess.Popen(
            ["bash", "-lc", a["command"]],
            cwd=a["path"], env=env, stdout=out, stderr=out,
            start_new_session=True)
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
    apps = load()
    if name in apps:
        apps[name]["enabled"] = False
        save(apps)


def resume():
    for name, a in load().items():
        if a.get("enabled"):
            start(name)


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
        out.append({
            "name": name, "path": a["path"], "command": a["command"],
            "port": a["port"], "running": run,
            "failed": bool(a.get("enabled")) and not run,
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

TEMPLATES = {
    "static": {"file": "index.html", "content": STATIC_TEMPLATE, "args": 2,
               "command": "python3 -m http.server $PORT"},
    "flask": {"file": "app.py", "content": FLASK_TEMPLATE, "args": 1,
              "command": "python3 app.py"},
    "node": {"file": "index.js", "content": NODE_TEMPLATE, "args": 1,
             "command": "node index.js"},
}


@flask_app.post("/api/create")
@require_auth
def api_create():
    d = request.get_json(force=True)
    name = valid_name(d.get("name"))
    tpl_name = d.get("template") or "static"
    command = (d.get("command") or "").strip()

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

    apps[name] = {"path": path, "command": command, "port": port, "enabled": False}
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
    save(apps)
    return jsonify({"ok": True})


@flask_app.post("/api/toggle/<n>")
@require_auth
def api_toggle(n):
    if n not in load():
        return jsonify({"error": "Application inconnue."}), 404
    stop(n) if is_running(n) else start(n)
    return jsonify({"ok": True})


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


LOGIN_PAGE = '<!doctype html>\n<html lang="fr"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>CodeLab &middot; Connexion</title>\n<style>\n:root{\n  --bg:#f6f7f9; --surface:#ffffff; --surface2:#f0f1f3; --bg-input:#f0f1f3;\n  --line:#e2e4e8; --line2:#d7dae0; --txt:#1c2129; --dim:#5b6472; --dim2:#8891a0;\n  --accent:#316dca; --accent-h:#2a5fb0; --accent-ring:rgba(49,109,202,.18);\n  --ok:#1a7f37; --ok-bg:rgba(26,127,55,.1); --ok-border:rgba(26,127,55,.3);\n  --err:#cf222e; --err-bg:rgba(207,34,46,.08); --err-txt:#a4030f; --err-border:rgba(207,34,46,.28);\n  --shadow:rgba(28,33,41,.08); --r:10px;\n}\n@media (prefers-color-scheme: dark){\n  :root:not([data-theme="light"]):not([data-theme="dark"]){\n    --bg:#0d1117; --surface:#161b22; --surface2:#1c2129; --bg-input:#0d1117;\n    --line:#262c36; --line2:#30363d; --txt:#e6edf3; --dim:#8b949e; --dim2:#6e7681;\n    --accent:#4c8eff; --accent-h:#3d7dda; --accent-ring:rgba(76,142,255,.22);\n    --ok:#3fb950; --ok-bg:rgba(63,185,80,.1); --ok-border:rgba(63,185,80,.35);\n    --err:#f85149; --err-bg:rgba(248,81,73,.1); --err-txt:#ff9b95; --err-border:rgba(248,81,73,.35);\n    --shadow:rgba(1,4,9,.6);\n  }\n}\n[data-theme="dark"]{\n  --bg:#0d1117; --surface:#161b22; --surface2:#1c2129; --bg-input:#0d1117;\n  --line:#262c36; --line2:#30363d; --txt:#e6edf3; --dim:#8b949e; --dim2:#6e7681;\n  --accent:#4c8eff; --accent-h:#3d7dda; --accent-ring:rgba(76,142,255,.22);\n  --ok:#3fb950; --ok-bg:rgba(63,185,80,.1); --ok-border:rgba(63,185,80,.35);\n  --err:#f85149; --err-bg:rgba(248,81,73,.1); --err-txt:#ff9b95; --err-border:rgba(248,81,73,.35);\n  --shadow:rgba(1,4,9,.6);\n}\n*{box-sizing:border-box;margin:0;padding:0}\nbody{background:var(--bg);color:var(--txt);\n  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;\n  -webkit-font-smoothing:antialiased; transition:background .15s,color .15s}\n\nbody{min-height:100vh;display:flex;align-items:center;justify-content:center}\n.card{width:100%;max-width:340px;padding:32px 28px;background:var(--surface);\n border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 32px var(--shadow)}\n.brand{display:flex;align-items:center;gap:10px;margin-bottom:22px}\n.brand .dot{width:10px;height:10px;border-radius:50%;background:var(--accent)}\n.brand span{font-size:15px;font-weight:600}\nlabel{display:block;font-size:12px;color:var(--dim);margin-bottom:6px;font-weight:500}\ninput{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--line2);\n background:var(--bg-input);color:var(--txt);font:inherit;font-size:14px}\ninput:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ring)}\nbutton{width:100%;margin-top:16px;padding:10px;border:none;border-radius:8px;\n background:var(--accent);color:#fff;font:inherit;font-size:14px;font-weight:600;cursor:pointer}\nbutton:hover{background:var(--accent-h)}\nbutton:disabled{opacity:.6;cursor:default}\n.err{margin-top:12px;padding:9px 12px;border-radius:8px;font-size:12.5px;\n background:var(--err-bg);color:var(--err-txt);border:1px solid var(--err-border);display:none}\n.hint{margin-top:18px;font-size:11.5px;color:var(--dim2);line-height:1.5}\ncode{font-family:ui-monospace,Menlo,monospace;background:var(--bg-input);padding:1px 5px;border-radius:4px}\n</style></head><body>\n<div class="card">\n  <div class="brand"><div class="dot"></div><span>CodeLab</span></div>\n  <label>Mot de passe</label>\n  <input type="password" id="pw" autofocus autocomplete="current-password">\n  <button id="go" onclick="submit()">Se connecter</button>\n  <div class="err" id="err"></div>\n  <div class="hint">Mot de passe genere automatiquement au premier demarrage. Pour le retrouver :\n  <br><code>docker exec codelab-app-manager cat /var/lib/codelab/app-manager/admin_password</code></div>\n</div>\n<script>\nconst pw=document.getElementById(\'pw\'), err=document.getElementById(\'err\'), go=document.getElementById(\'go\');\nasync function submit(){\n  err.style.display=\'none\'; go.disabled=true; go.textContent=\'Connexion...\';\n  try{\n    const r=await fetch(\'/login\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},\n      body:JSON.stringify({password:pw.value})});\n    const d=await r.json();\n    if(r.ok){ location.href=\'/\'; return; }\n    err.textContent=d.error||\'Erreur.\'; err.style.display=\'block\';\n  }catch(e){ err.textContent=\'Erreur reseau.\'; err.style.display=\'block\'; }\n  go.disabled=false; go.textContent=\'Se connecter\';\n}\npw.addEventListener(\'keydown\',e=>{if(e.key===\'Enter\')submit();});\n</script>\n</body></html>\n'

DASHBOARD_PAGE = '<!doctype html>\n<html lang="fr"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>CodeLab &middot; App Manager</title>\n<style>\n:root{\n  --bg:#f6f7f9; --surface:#ffffff; --surface2:#f0f1f3; --bg-input:#f0f1f3;\n  --line:#e2e4e8; --line2:#d7dae0; --txt:#1c2129; --dim:#5b6472; --dim2:#8891a0;\n  --accent:#316dca; --accent-h:#2a5fb0; --accent-ring:rgba(49,109,202,.18);\n  --ok:#1a7f37; --ok-bg:rgba(26,127,55,.1); --ok-border:rgba(26,127,55,.3);\n  --err:#cf222e; --err-bg:rgba(207,34,46,.08); --err-txt:#a4030f; --err-border:rgba(207,34,46,.28);\n  --shadow:rgba(28,33,41,.08); --r:10px;\n}\n@media (prefers-color-scheme: dark){\n  :root:not([data-theme="light"]):not([data-theme="dark"]){\n    --bg:#0d1117; --surface:#161b22; --surface2:#1c2129; --bg-input:#0d1117;\n    --line:#262c36; --line2:#30363d; --txt:#e6edf3; --dim:#8b949e; --dim2:#6e7681;\n    --accent:#4c8eff; --accent-h:#3d7dda; --accent-ring:rgba(76,142,255,.22);\n    --ok:#3fb950; --ok-bg:rgba(63,185,80,.1); --ok-border:rgba(63,185,80,.35);\n    --err:#f85149; --err-bg:rgba(248,81,73,.1); --err-txt:#ff9b95; --err-border:rgba(248,81,73,.35);\n    --shadow:rgba(1,4,9,.6);\n  }\n}\n[data-theme="dark"]{\n  --bg:#0d1117; --surface:#161b22; --surface2:#1c2129; --bg-input:#0d1117;\n  --line:#262c36; --line2:#30363d; --txt:#e6edf3; --dim:#8b949e; --dim2:#6e7681;\n  --accent:#4c8eff; --accent-h:#3d7dda; --accent-ring:rgba(76,142,255,.22);\n  --ok:#3fb950; --ok-bg:rgba(63,185,80,.1); --ok-border:rgba(63,185,80,.35);\n  --err:#f85149; --err-bg:rgba(248,81,73,.1); --err-txt:#ff9b95; --err-border:rgba(248,81,73,.35);\n  --shadow:rgba(1,4,9,.6);\n}\n*{box-sizing:border-box;margin:0;padding:0}\nbody{background:var(--bg);color:var(--txt);\n  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;\n  -webkit-font-smoothing:antialiased; transition:background .15s,color .15s}\n\n.topbar{border-bottom:1px solid var(--line);background:var(--surface);padding:0 20px;position:sticky;top:0;z-index:30}\n.topbar-in{max-width:1080px;margin:0 auto;height:58px;display:flex;align-items:center;gap:14px}\n.brand{display:flex;align-items:center;gap:9px;flex:none}\n.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--accent)}\n.brand b{font-size:14.5px;font-weight:600}.brand span{color:var(--dim2);font-size:13px}\n.search{flex:1;max-width:340px;position:relative}\n.search input{width:100%;padding:7px 12px 7px 32px;border-radius:8px;border:1px solid var(--line2);\n background:var(--bg-input);color:var(--txt);font:inherit;font-size:13px}\n.search input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ring)}\n.search svg{position:absolute;left:9px;top:50%;transform:translateY(-50%);width:14px;height:14px;\n stroke:var(--dim2);fill:none;stroke-width:2}\n.spacer{flex:1}\n.icon-btn{width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;\n border-radius:8px;border:1px solid var(--line2);background:var(--surface2);color:var(--dim);\n cursor:pointer;flex:none}\n.icon-btn:hover{color:var(--txt);border-color:var(--dim2)}\n.icon-btn svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8}\n.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px}\n.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:22px}\n.stat{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px}\n.stat b{display:block;font-size:21px;font-weight:700;margin-bottom:2px}\n.stat span{color:var(--dim);font-size:12px}\n.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:16px}\nh2{font-size:15px;font-weight:600}\nselect{padding:7px 10px;border-radius:8px;border:1px solid var(--line2);background:var(--surface);\n color:var(--txt);font:inherit;font-size:12.5px}\n.btn{display:inline-flex;align-items:center;gap:7px;border:1px solid transparent;cursor:pointer;\n font:inherit;font-size:13px;font-weight:500;border-radius:8px;padding:7px 14px;text-decoration:none}\n.btn-primary{background:var(--accent);color:#fff}\n.btn-primary:hover{background:var(--accent-h)}\n.btn-default{background:var(--surface2);color:var(--txt);border-color:var(--line2)}\n.btn-default:hover{border-color:var(--dim2)}\n.btn-quiet{background:transparent;color:var(--dim);padding:7px 9px}\n.btn-quiet:hover{color:var(--err);background:var(--err-bg)}\n.btn svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round}\n.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}\n.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px;\n display:flex;flex-direction:column;gap:10px;transition:border-color .15s}\n.card:hover{border-color:var(--line2)}\n.card-top{display:flex;align-items:center;gap:10px}\n.card-top img{width:34px;height:34px;border-radius:9px;flex:none;object-fit:cover}\n.card-name{min-width:0;flex:1}\n.card-name b{font-size:14px;font-weight:600;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.card-name .path{color:var(--dim2);font-size:11px;font-family:ui-monospace,Menlo,monospace;\n overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.pill{font-size:10.5px;font-weight:600;padding:2px 7px;border-radius:20px;border:1px solid;flex:none}\n.pill.on{color:var(--ok);border-color:var(--ok-border);background:var(--ok-bg)}\n.pill.off{color:var(--dim);border-color:var(--line2);background:var(--surface2)}\n.pill.err{color:var(--err);border-color:var(--err-border);background:var(--err-bg)}\n.metrics{display:flex;gap:14px;font-size:11.5px;color:var(--dim)}\n.metrics b{color:var(--txt);font-weight:600}\n.card-acts{display:flex;align-items:center;gap:6px;margin-top:2px}\n.sw{position:relative;width:36px;height:20px;border-radius:20px;border:none;flex:none;\n background:var(--line2);cursor:pointer;transition:background .2s}\n.sw.on{background:var(--ok)}\n.sw:after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;\n background:#fff;transition:left .2s ease}\n.sw.on:after{left:18px}\n.empty{border:1px dashed var(--line2);border-radius:var(--r);padding:52px 24px;text-align:center;grid-column:1/-1}\n.empty h3{font-size:15px;font-weight:600;margin-bottom:5px}.empty p{color:var(--dim);font-size:13px}\n.ov{position:fixed;inset:0;background:rgba(10,12,16,.6);display:none;align-items:center;\n justify-content:center;padding:20px;z-index:50}\n.ov.show{display:flex}\n.modal{background:var(--surface);border:1px solid var(--line2);border-radius:14px;width:100%;\n max-width:560px;max-height:88vh;overflow:auto;box-shadow:0 16px 48px var(--shadow)}\n.mh{padding:20px 22px 0}.mh h3{font-size:15px;font-weight:600}\n.mh p{color:var(--dim);font-size:13px;margin-top:3px}\n.mb{padding:18px 22px}\n.mf{padding:14px 22px;display:flex;gap:8px;justify-content:flex-end;border-top:1px solid var(--line)}\nlabel{display:block;font-size:12px;color:var(--dim);margin-bottom:6px;font-weight:500}\n.field{margin-bottom:15px}\ninput,textarea{width:100%;padding:8px 11px;border-radius:8px;border:1px solid var(--line2);\n background:var(--bg-input);color:var(--txt);font:inherit;font-size:13.5px}\ninput:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ring)}\n.hint{font-size:11.5px;color:var(--dim2);margin-top:5px}\ncode{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:var(--bg-input);\n border:1px solid var(--line);padding:1px 5px;border-radius:4px}\n.tabs{display:flex;gap:6px;margin-bottom:16px}\n.tab{flex:1;padding:8px;text-align:center;border-radius:8px;border:1px solid var(--line2);\n background:var(--surface2);color:var(--dim);font-size:12.5px;font-weight:500;cursor:pointer}\n.tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}\n.tpls{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:15px}\n.tpl{padding:12px 8px;border-radius:8px;border:1px solid var(--line2);background:var(--surface2);\n text-align:center;cursor:pointer;font-size:12px;font-weight:500;color:var(--dim)}\n.tpl.active{border-color:var(--accent);color:var(--accent);background:var(--accent-ring)}\n.fb{border:1px solid var(--line2);border-radius:8px;overflow:hidden;background:var(--bg-input)}\n.fb-cur{padding:7px 11px;font-size:11.5px;color:var(--dim);background:var(--surface2);\n border-bottom:1px solid var(--line);font-family:ui-monospace,Menlo,monospace;word-break:break-all}\n.fb-l{max-height:150px;overflow:auto}\n.fb-l div{padding:7px 11px;font-size:13px;cursor:pointer;border-bottom:1px solid var(--line)}\n.fb-l div:hover{background:var(--surface2)}\n.suggest{margin-top:6px;font-size:11.5px;color:var(--dim);cursor:pointer}\n.suggest b{color:var(--accent)}\n.alert{background:var(--err-bg);border:1px solid var(--err-border);color:var(--err-txt);\n padding:9px 12px;border-radius:8px;font-size:12.5px;margin-bottom:14px;display:none}\n.logpanel{position:fixed;top:0;right:0;bottom:0;width:min(560px,100vw);background:var(--surface);\n border-left:1px solid var(--line);box-shadow:-16px 0 48px var(--shadow);z-index:60;\n display:flex;flex-direction:column;transform:translateX(100%);transition:transform .2s ease}\n.logpanel.show{transform:translateX(0)}\n.lp-h{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px}\n.lp-h b{font-size:14px;flex:1}\n.lp-status{width:7px;height:7px;border-radius:50%;background:var(--dim2)}\n.lp-status.live{background:var(--ok)}\n.lp-body{flex:1;overflow:auto;background:var(--bg-input);padding:14px 18px;\n font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--dim);white-space:pre-wrap;line-height:1.6}\n.segbar{border-bottom:1px solid var(--line);background:var(--surface)}\n.segbar-in{max-width:1080px;margin:0 auto;padding:0 20px;display:flex;gap:22px}\n.seg-tab{padding:11px 2px;font-size:13.5px;font-weight:500;color:var(--dim);cursor:pointer;\n border-bottom:2px solid transparent;white-space:nowrap;user-select:none}\n.seg-tab.active{color:var(--txt);border-bottom-color:var(--accent)}\n.seg-tab:hover{color:var(--txt)}\n.logbox{background:var(--bg-input);border:1px solid var(--line);border-radius:var(--r);padding:14px 18px;\n font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--dim);white-space:pre-wrap;line-height:1.6;\n min-height:320px;max-height:62vh;overflow:auto}\n.settings-card{max-width:560px;margin-bottom:14px}\n.settings-card .field{margin-bottom:0}\n.pw-row{display:flex;gap:8px}\n.pw-row input{flex:1;font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--dim)}\n\n.acct-btn{width:32px;height:32px;border-radius:50%;border:1px solid var(--line2);background:var(--accent);\n color:#fff;font-size:12.5px;font-weight:600;display:inline-flex;align-items:center;justify-content:center;\n cursor:pointer;flex:none}\n.acct-wrap{position:relative;flex:none}\n.acct-menu{position:absolute;top:calc(100% + 8px);right:0;width:300px;background:var(--surface);\n border:1px solid var(--line2);border-radius:12px;box-shadow:0 12px 36px var(--shadow);\n display:none;z-index:40;overflow:hidden}\n.acct-menu.show{display:block}\n.acct-head{padding:14px 16px;border-bottom:1px solid var(--line)}\n.acct-head b{font-size:13.5px;display:block}\n.acct-head span{font-size:11.5px;color:var(--dim2);font-family:ui-monospace,Menlo,monospace}\n.acct-sec{padding:12px 16px;border-bottom:1px solid var(--line)}\n.acct-sec:last-child{border-bottom:none}\n.acct-sec-title{font-size:11px;font-weight:600;color:var(--dim2);text-transform:uppercase;\n letter-spacing:.03em;margin-bottom:9px}\n.seg-toggle{display:flex;border:1px solid var(--line2);border-radius:8px;overflow:hidden}\n.seg-toggle button{flex:1;padding:6px 4px;border:none;background:var(--surface2);color:var(--dim);\n font:inherit;font-size:12px;font-weight:500;cursor:pointer;border-right:1px solid var(--line2)}\n.seg-toggle button:last-child{border-right:none}\n.seg-toggle button.active{background:var(--accent);color:#fff}\n.acct-menu .pw-row input{font-size:11px}\n.acct-menu .btn{width:100%;justify-content:center}\n\n.ov-grid{display:grid;grid-template-columns:1.1fr 1fr 1fr;gap:12px;margin-bottom:12px}\n.ov-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px}\n.ov-card h4{font-size:12.5px;font-weight:600;color:var(--dim);margin-bottom:12px}\n.ov-nums{display:flex;gap:20px}\n.ov-nums div b{display:block;font-size:19px;font-weight:700}\n.ov-nums div span{font-size:11px;color:var(--dim)}\n.healthbar{height:8px;border-radius:20px;overflow:hidden;display:flex;background:var(--line2);margin-bottom:9px}\n.healthbar span{height:100%}\n.health-legend{display:flex;gap:12px;font-size:11px;color:var(--dim);flex-wrap:wrap}\n.health-legend i{display:inline-block;width:7px;height:7px;border-radius:2px;margin-right:4px}\n.ov-quick{display:flex;flex-direction:column;gap:7px}\n.ov-quick button{width:100%;justify-content:flex-start}\n@media (max-width:820px){.ov-grid{grid-template-columns:1fr}}\n</style></head><body>\n<div class="topbar"><div class="topbar-in">\n  <div class="brand"><div class="dot"></div><b>CodeLab</b><span>App Manager</span></div>\n  <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>\n    <input id="search" placeholder="Rechercher..." oninput="render()"></div>\n  <div class="spacer"></div>\n\n  <div class="acct-wrap">\n    <button class="acct-btn" onclick="toggleAcctMenu()" id="acct-btn">A</button>\n    <div class="acct-menu" id="acct-menu">\n      <div class="acct-head"><b>Administrateur</b><span id="acct-root"></span></div>\n\n      <div class="acct-sec">\n        <div class="acct-sec-title">Apparence</div>\n        <div class="seg-toggle" id="theme-toggle">\n          <button data-t="auto" onclick="setTheme(\'auto\')">Auto</button>\n          <button data-t="light" onclick="setTheme(\'light\')">Clair</button>\n          <button data-t="dark" onclick="setTheme(\'dark\')">Sombre</button>\n        </div>\n      </div>\n\n      <div class="acct-sec">\n        <div class="acct-sec-title">Identifiants</div>\n        <div class="pw-row">\n          <input id="pw-cmd" readonly value="cat /DATA/AppData/codelab/config/credentials.env">\n          <button class="btn btn-default" id="pw-copy-btn" onclick="copyPwCmd()">Copier</button>\n        </div>\n      </div>\n\n      <div class="acct-sec">\n        <button class="btn btn-default" onclick="doLogout()">\n          <svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>\n          <path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>Deconnexion</button>\n      </div>\n    </div>\n  </div>\n</div></div>\n\n<div class="segbar"><div class="segbar-in">\n  <div class="seg-tab active" data-sec="overview" onclick="showSection(\'overview\')">Vue d\'ensemble</div>\n  <div class="seg-tab" data-sec="apps" onclick="showSection(\'apps\')">Projets</div>\n  <div class="seg-tab" data-sec="logs" onclick="showSection(\'logs\')">Logs</div>\n</div></div>\n\n<div class="wrap">\n<div id="sec-overview" class="section">\n  <div class="ov-grid">\n    <div class="ov-card">\n      <h4>Applications</h4>\n      <div class="ov-nums">\n        <div><b id="ov-total">0</b><span>Total</span></div>\n        <div><b id="ov-run">0</b><span>En ligne</span></div>\n        <div><b id="ov-off">0</b><span>Arretees</span></div>\n      </div>\n    </div>\n    <div class="ov-card">\n      <h4>Sante</h4>\n      <div class="healthbar"><span id="hb-on" style="background:var(--ok)"></span><span id="hb-off"\n        style="background:var(--line2)"></span><span id="hb-err" style="background:var(--err)"></span></div>\n      <div class="health-legend">\n        <span><i style="background:var(--ok)"></i>En ligne</span>\n        <span><i style="background:var(--line2)"></i>Arretee</span>\n        <span><i style="background:var(--err)"></i>Erreur</span>\n      </div>\n    </div>\n    <div class="ov-card">\n      <h4>Actions rapides</h4>\n      <div class="ov-quick">\n        <button class="btn btn-primary" onclick="openAdd()">\n          <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>Ajouter une app</button>\n        <button class="btn btn-default" onclick="showSection(\'apps\')">\n          <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>\n          <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>Voir les projets</button>\n        <button class="btn btn-default" onclick="showSection(\'logs\')">\n          <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Voir les logs</button>\n      </div>\n    </div>\n  </div>\n</div>\n\n<div id="sec-apps" class="section" style="display:none">\n  <div class="stats">\n    <div class="stat"><b id="s-total">0</b><span>Applications</span></div>\n    <div class="stat"><b id="s-run">0</b><span>En ligne</span></div>\n    <div class="stat"><b id="s-cpu">0%</b><span>CPU cumule</span></div>\n    <div class="stat"><b id="s-mem">0 Mo</b><span>Memoire cumulee</span></div>\n  </div>\n\n  <div class="toolbar">\n    <h2>Applications</h2>\n    <div class="spacer"></div>\n    <select id="sort" onchange="render()">\n      <option value="name">Trier : nom</option>\n      <option value="status">Trier : statut</option>\n      <option value="cpu">Trier : CPU</option>\n    </select>\n    <button class="btn btn-primary" onclick="openAdd()">\n      <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>Ajouter</button>\n  </div>\n  <div id="grid" class="grid"></div>\n</div>\n\n<div id="sec-logs" class="section" style="display:none">\n  <div class="toolbar">\n    <h2>Journal en direct</h2>\n    <div class="spacer"></div>\n    <select id="log-app-select" onchange="onLogAppChange()">\n      <option value="">Choisir une application...</option>\n    </select>\n  </div>\n  <div class="logbox" id="log-inline-body">Selectionne une application ci-dessus.</div>\n</div>\n</div>\n\n<!-- Modal ajout / edition -->\n<div class="ov" id="ov-add"><div class="modal">\n  <div class="mh"><h3 id="a-t">Ajouter une application</h3><p id="a-s"></p></div>\n  <div class="mb"><div class="alert" id="a-e"></div>\n\n    <div class="tabs" id="mode-tabs">\n      <div class="tab active" data-mode="existing" onclick="setMode(\'existing\')">Dossier existant</div>\n      <div class="tab" data-mode="new" onclick="setMode(\'new\')">Nouveau projet</div>\n    </div>\n\n    <div id="tpl-block" style="display:none">\n      <div class="tpls">\n        <div class="tpl active" data-tpl="static" onclick="setTpl(\'static\')">Statique<br>(HTML)</div>\n        <div class="tpl" data-tpl="flask" onclick="setTpl(\'flask\')">Flask<br>(Python)</div>\n        <div class="tpl" data-tpl="node" onclick="setTpl(\'node\')">Node<br>(JS)</div>\n      </div>\n    </div>\n\n    <div class="field"><label>Nom du projet</label>\n      <input id="f-name" placeholder="portfolio" autocomplete="off">\n      <div class="hint">Accessible sur <code id="f-url">/nom/</code></div></div>\n\n    <div class="field" id="browse-block"><label>Dossier</label>\n      <div class="fb"><div class="fb-cur" id="fb-c">/</div><div class="fb-l" id="fb-l"></div></div>\n      <div class="suggest" id="suggest" style="display:none" onclick="applySuggestion()"></div>\n    </div>\n\n    <div class="field"><label>Commande de lancement</label>\n      <input id="f-cmd" placeholder="python -m http.server $PORT" autocomplete="off">\n      <div class="hint">La variable <code>$PORT</code> est fournie : l\'app doit ecouter dessus.</div></div>\n  </div>\n  <div class="mf"><button class="btn btn-default" onclick="hide(\'ov-add\')">Annuler</button>\n    <button class="btn btn-primary" id="a-b" onclick="submitAdd()">Ajouter</button></div>\n</div></div>\n\n<!-- Panneau de logs en direct -->\n<div class="logpanel" id="logpanel">\n  <div class="lp-h"><div class="lp-status" id="lp-status"></div><b id="lp-title">Journal</b>\n    <button class="icon-btn" onclick="closeLogs()">\n      <svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>\n  <div class="lp-body" id="lp-body"></div>\n</div>\n\n<script>\nconst $=i=>document.getElementById(i), hide=i=>$(i).classList.remove("show");\nconst esc=s=>(s||"").replace(/[&<>"]/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\'}[c]));\n\n/* ---------------- theme ---------------- */\nfunction applyTheme(t){\n  if(t===\'auto\') document.documentElement.removeAttribute(\'data-theme\');\n  else document.documentElement.setAttribute(\'data-theme\', t);\n  document.querySelectorAll(\'#theme-toggle button\').forEach(b=>b.classList.toggle(\'active\', b.dataset.t===t));\n}\nfunction setTheme(t){\n  localStorage.setItem(\'codelab-theme\', t);\n  applyTheme(t);\n}\napplyTheme(localStorage.getItem(\'codelab-theme\')||\'auto\');\n\n/* ---------------- menu compte ---------------- */\nfunction toggleAcctMenu(){\n  $(\'acct-menu\').classList.toggle(\'show\');\n}\ndocument.addEventListener(\'click\', e=>{\n  const wrap=document.querySelector(\'.acct-wrap\');\n  if(wrap && !wrap.contains(e.target)) $(\'acct-menu\').classList.remove(\'show\');\n});\n\n/* ---------------- navigation par onglets ---------------- */\nfunction showSection(name){\n  document.querySelectorAll(\'.section\').forEach(s=>{ s.style.display = (s.id===\'sec-\'+name) ? \'\' : \'none\'; });\n  document.querySelectorAll(\'.seg-tab\').forEach(t=>t.classList.toggle(\'active\', t.dataset.sec===name));\n  if(name===\'logs\') populateLogSelect();\n}\n\nfunction renderOverview(){\n  const total=apps.length, running=apps.filter(a=>a.running).length;\n  const failed=apps.filter(a=>a.failed).length;\n  const stopped=total-running-failed;\n  $(\'ov-total\').textContent=total;\n  $(\'ov-run\').textContent=running;\n  $(\'ov-off\').textContent=stopped+failed;\n  const pct=n=>total?(n/total*100):0;\n  $(\'hb-on\').style.width=pct(running)+\'%\';\n  $(\'hb-off\').style.width=pct(stopped)+\'%\';\n  $(\'hb-err\').style.width=pct(failed)+\'%\';\n}\n\nfunction populateLogSelect(){\n  const sel=$(\'log-app-select\');\n  const cur=sel.value;\n  sel.innerHTML=\'<option value="">Choisir une application...</option>\'\n    + apps.map(a=>`<option value="${esc(a.name)}">${esc(a.name)}</option>`).join(\'\');\n  if(cur && apps.some(a=>a.name===cur)) sel.value=cur;\n}\n\nlet inlineEs=null;\nfunction onLogAppChange(){\n  const name=$(\'log-app-select\').value;\n  if(inlineEs){ inlineEs.close(); inlineEs=null; }\n  if(!name){ $(\'log-inline-body\').textContent=\'Selectionne une application ci-dessus.\'; return; }\n  $(\'log-inline-body\').textContent=\'Connexion...\';\n  inlineEs=new EventSource(\'/api/logs/\'+name+\'/stream\');\n  let first=true;\n  inlineEs.onmessage=(e)=>{\n    if(first){ $(\'log-inline-body\').textContent=\'\'; first=false; }\n    $(\'log-inline-body\').textContent+=e.data+\'\\n\';\n    $(\'log-inline-body\').scrollTop=$(\'log-inline-body\').scrollHeight;\n  };\n}\n\nfunction copyPwCmd(){\n  const el=$(\'pw-cmd\');\n  el.select();\n  navigator.clipboard.writeText(el.value).then(()=>{\n    const btn=$(\'pw-copy-btn\');\n    const old=btn.textContent;\n    btn.textContent=\'Copie\';\n    setTimeout(()=>{ btn.textContent=old; }, 1500);\n  });\n}\n\n/* ---------------- data ---------------- */\nlet apps=[];\nasync function refresh(){\n  const r=await fetch(\'/api/apps\');\n  if(r.status===401){ location.href=\'/login\'; return; }\n  apps=(await r.json()).apps;\n  render();\n}\nfunction render(){\n  const q=($(\'search\').value||\'\').toLowerCase();\n  const sort=$(\'sort\').value;\n  let list=apps.filter(a=>a.name.includes(q));\n  list.sort((a,b)=>{\n    if(sort===\'status\') return (b.running-a.running) || a.name.localeCompare(b.name);\n    if(sort===\'cpu\') return (b.cpu_percent-a.cpu_percent) || a.name.localeCompare(b.name);\n    return a.name.localeCompare(b.name);\n  });\n  $(\'s-total\').textContent=apps.length;\n  $(\'s-run\').textContent=apps.filter(a=>a.running).length;\n  $(\'s-cpu\').textContent=apps.reduce((s,a)=>s+(a.cpu_percent||0),0).toFixed(1)+\'%\';\n  $(\'s-mem\').textContent=apps.reduce((s,a)=>s+(a.memory_mb||0),0).toFixed(0)+\' Mo\';\n  $(\'grid\').innerHTML=list.length?list.map(card).join(\'\'):\n   `<div class="empty"><h3>${q?\'Aucun resultat\':\'Aucune application\'}</h3>\n    <p>${q?\'Essaie un autre terme de recherche.\':\'Ajoute ton premier projet avec le bouton Ajouter.\'}</p></div>`;\n  populateLogSelect();\n  renderOverview();\n}\nfunction card(a){\n  const p=a.running?\'<span class="pill on">En ligne</span>\':\n   (a.failed?\'<span class="pill err">Arret imprevu</span>\':\'<span class="pill off">Arretee</span>\');\n  const metrics=a.running?`<div class="metrics"><span>CPU <b>${a.cpu_percent.toFixed(1)}%</b></span>\n   <span>Mem <b>${a.memory_mb.toFixed(0)} Mo</b></span></div>`:\'\';\n  return `<div class="card">\n   <div class="card-top"><img src="/api/icon/${a.name}" alt="">\n    <div class="card-name"><b title="${esc(a.name)}">${esc(a.name)}</b>\n     <div class="path" title="${esc(a.path)}">${esc(a.path)}</div></div>\n    ${p}</div>\n   ${metrics}\n   <div class="card-acts">\n    <button class="sw ${a.running?\'on\':\'\'}" onclick="tg(\'${a.name}\')" title="Activer / desactiver"></button>\n    <div class="spacer"></div>\n    ${a.running?\'\':`<button class="icon-btn" onclick="openEdit(\'${a.name}\')" title="Modifier">\n     <svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg></button>`}\n    <button class="icon-btn" onclick="openLogs(\'${a.name}\')" title="Journal">\n     <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg></button>\n    <button class="icon-btn" onclick="rm(\'${a.name}\')" title="Supprimer">\n     <svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg></button>\n   </div></div>`;\n}\nasync function tg(n){ await fetch(\'/api/toggle/\'+n,{method:\'POST\'}); setTimeout(refresh,400); }\nasync function rm(n){ if(!confirm(\'Supprimer "\'+n+\'" ? Le dossier du projet n\\\'est pas touche.\'))return;\n await fetch(\'/api/app/\'+n,{method:\'DELETE\'}); refresh(); }\nasync function doLogout(){ await fetch(\'/logout\',{method:\'POST\'}); location.href=\'/login\'; }\n\n/* ---------------- modal ajout / edition ---------------- */\nlet mode=\'existing\', tpl=\'static\', cur=\'\', editing=null, ROOT=\'\';\nconst TPL_CMD={static:\'python3 -m http.server $PORT\', flask:\'python3 app.py\', node:\'node index.js\'};\n\nfunction setMode(m){\n  mode=m;\n  document.querySelectorAll(\'#mode-tabs .tab\').forEach(t=>t.classList.toggle(\'active\', t.dataset.mode===m));\n  $(\'tpl-block\').style.display = m===\'new\' ? \'\' : \'none\';\n  $(\'browse-block\').style.display = m===\'new\' ? \'none\' : \'\';\n  if(m===\'new\'){ $(\'f-cmd\').value=TPL_CMD[tpl]; $(\'f-url\').textContent=\'/\'+(slug($(\'f-name\').value)||\'nom\')+\'/\'; }\n}\nfunction setTpl(t){\n  tpl=t;\n  document.querySelectorAll(\'.tpl\').forEach(e=>e.classList.toggle(\'active\', e.dataset.tpl===t));\n  $(\'f-cmd\').value=TPL_CMD[t];\n}\nfunction slug(s){ return (s||\'\').trim().toLowerCase().replace(/[^a-z0-9_-]/g,\'-\'); }\n\nfunction openAdd(){\n  editing=null; mode=\'existing\'; tpl=\'static\';\n  $(\'a-t\').textContent=\'Ajouter une application\';\n  $(\'a-s\').textContent=\'Un dossier existant, ou un nouveau projet cree depuis un modele.\';\n  $(\'a-b\').textContent=\'Ajouter\'; $(\'a-e\').style.display=\'none\';\n  $(\'f-name\').value=\'\'; $(\'f-cmd\').value=\'\'; $(\'suggest\').style.display=\'none\';\n  document.querySelectorAll(\'#mode-tabs .tab\').forEach(t=>t.classList.toggle(\'active\', t.dataset.mode===\'existing\'));\n  document.querySelectorAll(\'.tpl\').forEach(e=>e.classList.toggle(\'active\', e.dataset.tpl===\'static\'));\n  setMode(\'existing\');\n  $(\'f-name\').oninput=()=>$(\'f-url\').textContent=\'/\'+(slug($(\'f-name\').value)||\'nom\')+\'/\';\n  browse(ROOT);\n  $(\'ov-add\').classList.add(\'show\'); $(\'f-name\').focus();\n}\nfunction openEdit(name){\n  const a=apps.find(x=>x.name===name); if(!a) return;\n  editing=name; mode=\'existing\';\n  $(\'a-t\').textContent=\'Modifier \'+name;\n  $(\'a-s\').textContent=\'Change le dossier ou la commande de lancement.\';\n  $(\'a-b\').textContent=\'Enregistrer\'; $(\'a-e\').style.display=\'none\';\n  $(\'f-name\').value=name; $(\'f-name\').disabled=true;\n  $(\'f-cmd\').value=a.command; $(\'suggest\').style.display=\'none\';\n  $(\'mode-tabs\').style.display=\'none\'; $(\'tpl-block\').style.display=\'none\'; $(\'browse-block\').style.display=\'\';\n  cur=a.path; $(\'fb-c\').textContent=a.path.replace(ROOT,\'\')||\'/\';\n  browse(a.path);\n  $(\'ov-add\').classList.add(\'show\');\n}\nasync function browse(p){\n  const d=await (await fetch(\'/api/browse?path=\'+encodeURIComponent(p))).json();\n  cur=d.path; $(\'fb-c\').textContent=d.label;\n  let h=d.hasIndex?\'<div style="color:var(--ok)">&check; index.html present dans ce dossier</div>\':\'\';\n  if(d.parent) h+=`<div onclick="browse(\'${d.parent}\')">&#8617; Dossier parent</div>`;\n  h+=d.dirs.map(n=>`<div onclick="browse(\'${d.path}/${n}\')">&#128193; ${esc(n)}</div>`).join(\'\');\n  $(\'fb-l\').innerHTML=h||\'<div style="color:var(--dim);cursor:default">Dossier vide</div>\';\n  if(mode===\'existing\' && !editing){\n    const dd=await (await fetch(\'/api/detect?path=\'+encodeURIComponent(cur))).json();\n    if(dd.command){\n      $(\'suggest\').style.display=\'block\';\n      $(\'suggest\').innerHTML=\'Suggestion detectee : <b>\'+esc(dd.command)+\'</b> (cliquer pour appliquer)\';\n      $(\'suggest\').dataset.cmd=dd.command;\n    } else { $(\'suggest\').style.display=\'none\'; }\n  }\n}\nfunction applySuggestion(){ $(\'f-cmd\').value=$(\'suggest\').dataset.cmd||\'\'; }\nfunction err(m){ const e=$(\'a-e\'); e.textContent=m; e.style.display=\'block\'; }\n\nasync function submitAdd(){\n  const name=$(\'f-name\').value.trim(), c=$(\'f-cmd\').value.trim();\n  if(!name) return err(\'Donne un nom au projet.\');\n  if(!c) return err(\'Indique la commande de lancement.\');\n  $(\'a-e\').style.display=\'none\';\n\n  if(editing){\n    const r=await fetch(\'/api/app/\'+editing,{method:\'PUT\',headers:{\'Content-Type\':\'application/json\'},\n     body:JSON.stringify({path:cur,command:c})});\n    const d=await r.json();\n    if(!r.ok) return err(d.error);\n    $(\'f-name\').disabled=false; $(\'mode-tabs\').style.display=\'\';\n    hide(\'ov-add\'); refresh(); return;\n  }\n\n  if(mode===\'new\'){\n    const r=await fetch(\'/api/create\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},\n     body:JSON.stringify({name,template:tpl,command:c})});\n    const d=await r.json();\n    if(!r.ok) return err(d.error);\n    hide(\'ov-add\'); refresh(); return;\n  }\n\n  const r=await fetch(\'/api/add\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},\n   body:JSON.stringify({name,path:cur,command:c})});\n  const d=await r.json();\n  if(!r.ok) return err(d.error);\n  hide(\'ov-add\'); refresh();\n}\n\n/* ---------------- logs en direct ---------------- */\nlet es=null;\nfunction openLogs(name){\n  $(\'lp-title\').textContent=\'Journal — \'+name;\n  $(\'lp-body\').textContent=\'Connexion...\';\n  $(\'lp-status\').classList.remove(\'live\');\n  $(\'logpanel\').classList.add(\'show\');\n  if(es) es.close();\n  es=new EventSource(\'/api/logs/\'+name+\'/stream\');\n  let first=true;\n  es.onopen=()=>$(\'lp-status\').classList.add(\'live\');\n  es.onmessage=(e)=>{\n    if(first){ $(\'lp-body\').textContent=\'\'; first=false; }\n    $(\'lp-body\').textContent+=e.data+\'\\\\n\';\n    $(\'lp-body\').scrollTop=$(\'lp-body\').scrollHeight;\n  };\n  es.onerror=()=>$(\'lp-status\').classList.remove(\'live\');\n}\nfunction closeLogs(){\n  $(\'logpanel\').classList.remove(\'show\');\n  if(es){ es.close(); es=null; }\n}\n\ndocument.addEventListener(\'keydown\',e=>{if(e.key===\'Escape\'){hide(\'ov-add\');closeLogs();$(\'acct-menu\').classList.remove(\'show\');}});\ndocument.querySelectorAll(\'.ov\').forEach(o=>o.addEventListener(\'click\',\n e=>{if(e.target===o)o.classList.remove(\'show\')}));\n\nROOT=__ROOT__;\n$(\'acct-root\').textContent=ROOT;\nrefresh(); setInterval(refresh,5000);\n</script>\n</body></html>\n'

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
    flask_app.run(host="0.0.0.0",
                  port=int(os.environ.get("MANAGER_PORT", "9001")),
                  threaded=True)
