"""
CodeLab app-manager — panneau de gestion + reverse proxy, sur un port unique.

  http://<IP>:9001/            panneau CodeLab (cette page)
  http://<IP>:9001/<projet>/   le projet, servi via proxy interne
  http://<IP>:9001/health      sonde du HEALTHCHECK Docker

Autonome : aucune dependance a Supervisor. Les processus sont geres ici.
Seule dependance externe : Flask.

Les chemins sont pilotes par variables d'environnement pour rester alignes
sur les points de montage declares dans docker-compose.yml :
  config/app-manager -> /opt/codelab/app-manager      (APP_MANAGER_DIR)
  data/app-manager   -> /var/lib/codelab/app-manager  (APP_MANAGER_STATE)
  workspace          -> /workspace                    (APP_MANAGER_ROOT)
Le code est en lecture seule ; apps.json et les journaux vivent dans data.
Les valeurs par defaut sont celles fixees dans l'image : ne les changer ici
qu'en changeant aussi le compose et le Dockerfile.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, redirect, request

#  Le CODE et l'ETAT sont separes.
#    APP_MANAGER_DIR    ou vit ce fichier          -> config/app-manager (ro)
#    APP_MANAGER_STATE  apps.json et les journaux  -> data/app-manager
#  L'usage du panneau n'a rien a faire dans la configuration : ce qu'on edite
#  et ce que le programme produit ne se rangent pas au meme endroit.
CONFIG_DIR = os.environ.get("APP_MANAGER_DIR", "/opt/codelab/app-manager")
STATE_DIR = os.environ.get("APP_MANAGER_STATE", "/var/lib/codelab/app-manager")
APPS_FILE = os.path.join(STATE_DIR, "apps.json")
LOG_DIR = os.path.join(STATE_DIR, "logs")
ROOT = os.environ.get("APP_MANAGER_ROOT", "/workspace")
PORT_MIN, PORT_MAX = 9101, 9140
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
       "proxy-authenticate", "proxy-authorization", "te", "trailers"}

flask_app = Flask(__name__)
procs = {}          # nom -> subprocess.Popen
lock = threading.Lock()


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
    procs.pop(name, None)
    apps = load()
    if name in apps:
        apps[name]["enabled"] = False
        save(apps)


def resume():
    """Relance au demarrage du conteneur les apps qui etaient actives."""
    for name, a in load().items():
        if a.get("enabled"):
            start(name)


# ------------------------------ API --------------------------------

@flask_app.get("/health")
def health():
    """Cible du HEALTHCHECK Docker. Route statique : elle passe avant la
    regle attrape-tout /<name>."""
    return Response("ok\n", mimetype="text/plain")


@flask_app.get("/api/apps")
def api_apps():
    apps = load()
    out = []
    for name in sorted(apps):
        a = apps[name]
        run = is_running(name)
        out.append({
            "name": name, "path": a["path"], "command": a["command"],
            "port": a["port"], "running": run,
            "failed": bool(a.get("enabled")) and not run,
        })
    return jsonify({"apps": out})


@flask_app.get("/api/browse")
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


@flask_app.post("/api/add")
def api_add():
    d = request.get_json(force=True)
    name = re.sub(r"[^a-z0-9_-]", "-", (d.get("name") or "").strip().lower()).strip("-")
    path = (d.get("path") or "").strip()
    command = (d.get("command") or "").strip()
    apps = load()

    if not name:
        return jsonify({"error": "Le nom est obligatoire."}), 400
    if name in ("api", "static", "health"):
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


@flask_app.post("/api/toggle/<name>")
def api_toggle(name):
    if name not in load():
        return jsonify({"error": "Application inconnue."}), 404
    stop(name) if is_running(name) else start(name)
    return jsonify({"ok": True})


@flask_app.delete("/api/app/<name>")
def api_delete(name):
    stop(name)
    apps = load()
    apps.pop(name, None)
    save(apps)
    return jsonify({"ok": True})


@flask_app.get("/api/logs/<name>")
def api_logs(name):
    f = os.path.join(LOG_DIR, name + ".log")
    if not os.path.exists(f):
        return jsonify({"lines": ["Aucun journal pour le moment."]})
    with open(f, errors="replace") as fh:
        lines = [l.rstrip() for l in fh.readlines()[-120:]]
    return jsonify({"lines": lines or ["Journal vide."]})


# ------------------------------ PROXY ------------------------------

def _proxy(name, sub):
    a = load().get(name)
    if not a:
        return Response(_page("Introuvable", "Aucune application « " + name + " »."),
                        404, mimetype="text/html")
    if not is_running(name):
        return Response(_page("Application arretee",
                              "« " + name + " » n'est pas demarree.",
                              "Active-la depuis le panneau."), 503, mimetype="text/html")
    # Flask livre "sub" DECODE : "data/Apres ski/photo 1.jpg" arrive avec ses
    # espaces et ses accents. Reinjecte tel quel dans une URL, http.client
    # echoue -- espace interdit dans la ligne de requete, non-ASCII impossible
    # a encoder. On re-encode donc avant de transmettre. safe="/" preserve la
    # hierarchie des dossiers.
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
                              "« " + name + " » ne repond pas encore.",
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
        _page("Introuvable", "Aucune application « " + name + " »."),
        404, mimetype="text/html")


@flask_app.route("/<name>/", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@flask_app.route("/<name>/<path:sub>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def proxy(name, sub=""):
    return _proxy(name, sub) if name in load() else _miss(name)


@flask_app.route("/<name>")
def proxy_noslash(name):
    return redirect("/" + name + "/", 302) if name in load() else _miss(name)


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


# -------------------------------- UI --------------------------------

PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CodeLab · Applications</title>
<style>
:root{--bg:#0d1117;--surface:#161b22;--surface2:#1c2129;--line:#262c36;--line2:#30363d;
 --txt:#e6edf3;--dim:#8b949e;--dim2:#6e7681;--accent:#316dca;--accent-h:#3d7dda;
 --ok:#3fb950;--err:#f85149;--r:8px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.5 -apple-system,BlinkMacSystemFont,
 "Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.topbar{border-bottom:1px solid var(--line);background:var(--surface);padding:0 24px}
.topbar-in{max-width:900px;margin:0 auto;height:56px;display:flex;align-items:center}
.brand{font-size:14px;font-weight:600}.brand span{color:var(--dim2);font-weight:400}
.wrap{max-width:900px;margin:0 auto;padding:28px 24px 60px}
.bar{display:flex;align-items:baseline;gap:12px;margin-bottom:18px}
h2{font-size:16px;font-weight:600}.muted{color:var(--dim);font-size:13px}
.spacer{margin-left:auto}
.btn{display:inline-flex;align-items:center;gap:7px;border:1px solid transparent;cursor:pointer;
 font:inherit;font-size:13px;font-weight:500;border-radius:var(--r);padding:7px 14px;
 transition:background .15s,border-color .15s,color .15s;text-decoration:none}
.btn-primary{background:var(--accent);color:#fff;border-color:#3f7fd9}
.btn-primary:hover{background:var(--accent-h)}
.btn-default{background:var(--surface2);color:var(--txt);border-color:var(--line2)}
.btn-default:hover{background:#242a33}
.btn-quiet{background:transparent;color:var(--dim);padding:7px 9px}
.btn-quiet:hover{color:var(--err);background:rgba(248,81,73,.1)}
.icon{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8;
 stroke-linecap:round;stroke-linejoin:round}
.table{border:1px solid var(--line);border-radius:var(--r);overflow:hidden;background:var(--surface)}
.tr{display:flex;align-items:center;gap:14px;padding:13px 16px;border-bottom:1px solid var(--line)}
.tr:last-child{border-bottom:none}.tr:hover{background:#1a1f27}
.cell{min-width:0;flex:1}
.name{font-weight:600;font-size:14px;display:flex;align-items:center;gap:8px}
.pill{font-size:11px;font-weight:500;padding:2px 8px;border-radius:20px;border:1px solid}
.pill.on{color:var(--ok);border-color:rgba(63,185,80,.4);background:rgba(63,185,80,.1)}
.pill.off{color:var(--dim);border-color:var(--line2);background:var(--surface2)}
.pill.err{color:var(--err);border-color:rgba(248,81,73,.4);background:rgba(248,81,73,.1)}
.path{color:var(--dim);font-size:12px;margin-top:3px;font-family:ui-monospace,Menlo,monospace;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.acts{display:flex;align-items:center;gap:6px;flex:none}
.url{color:var(--dim);font-size:12px;font-family:ui-monospace,Menlo,monospace}
.url a{color:var(--accent);text-decoration:none}.url a:hover{text-decoration:underline}
.sw{position:relative;width:38px;height:21px;border-radius:20px;border:none;flex:none;
 background:var(--line2);cursor:pointer;transition:background .2s}
.sw.on{background:var(--ok)}
.sw:after{content:"";position:absolute;top:3px;left:3px;width:15px;height:15px;border-radius:50%;
 background:#fff;transition:left .2s ease}
.sw.on:after{left:20px}
.empty{border:1px solid var(--line);border-radius:var(--r);background:var(--surface);
 padding:52px 24px;text-align:center}
.empty h3{font-size:15px;font-weight:600;margin-bottom:5px}
.ov{position:fixed;inset:0;background:rgba(1,4,9,.8);display:none;align-items:center;
 justify-content:center;padding:20px;z-index:40}
.ov.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--line2);border-radius:12px;width:100%;
 max-width:540px;max-height:88vh;overflow:auto;box-shadow:0 16px 48px rgba(1,4,9,.85)}
.mh{padding:20px 22px 0}.mh h3{font-size:15px;font-weight:600}
.mh p{color:var(--dim);font-size:13px;margin-top:3px}
.mb{padding:18px 22px}
.mf{padding:14px 22px;display:flex;gap:8px;justify-content:flex-end;border-top:1px solid var(--line)}
label{display:block;font-size:12px;color:var(--dim);margin-bottom:6px;font-weight:500}
.field{margin-bottom:15px}
input{width:100%;padding:8px 11px;border-radius:6px;border:1px solid var(--line2);
 background:#0d1117;color:var(--txt);font:inherit;font-size:13.5px}
input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(49,109,202,.25)}
.hint{font-size:11.5px;color:var(--dim2);margin-top:5px}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:#0d1117;
 border:1px solid var(--line);padding:1px 5px;border-radius:4px}
.fb{border:1px solid var(--line2);border-radius:6px;overflow:hidden;background:#0d1117}
.fb-cur{padding:7px 11px;font-size:11.5px;color:var(--dim);background:var(--surface2);
 border-bottom:1px solid var(--line);font-family:ui-monospace,Menlo,monospace;word-break:break-all}
.fb-l{max-height:160px;overflow:auto}
.fb-l div{padding:7px 11px;font-size:13px;cursor:pointer;border-bottom:1px solid #1b2028}
.fb-l div:hover{background:#161b22}
.ok{color:var(--ok)}
.recap{border:1px solid var(--line);border-radius:8px;background:#0d1117;padding:14px 16px}
.recap div{display:flex;gap:14px;padding:5px 0;font-size:13px}
.recap b{color:var(--dim);font-weight:500;width:84px;flex:none;font-size:12.5px}
.recap span{word-break:break-all;font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.alert{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.35);color:#ff9b95;
 padding:9px 12px;border-radius:6px;font-size:12.5px;margin-bottom:14px;display:none}
.logs{background:#0d1117;border:1px solid var(--line);border-radius:6px;padding:12px;
 font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:var(--dim);
 max-height:360px;overflow:auto;white-space:pre-wrap}
</style></head><body>
<div class="topbar"><div class="topbar-in">
  <div class="brand">CodeLab <span>/ Applications</span></div></div></div>
<div class="wrap">
  <div class="bar"><h2>Applications</h2><div class="muted" id="count"></div>
    <div class="spacer"></div>
    <button class="btn btn-primary" onclick="openAdd()">
      <svg class="icon" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>Ajouter</button>
  </div>
  <div id="list"></div>
</div>

<div class="ov" id="ov-add"><div class="modal">
  <div class="mh"><h3 id="a-t">Ajouter une application</h3>
    <p id="a-s">Indique le dossier du projet et la commande de lancement.</p></div>
  <div class="mb"><div class="alert" id="a-e"></div>
    <div id="s1">
      <div class="field"><label>Nom du projet</label>
        <input id="f-name" placeholder="portfolio" autocomplete="off">
        <div class="hint">Accessible sur <code id="f-url">/nom/</code></div></div>
      <div class="field"><label>Dossier</label>
        <div class="fb"><div class="fb-cur" id="fb-c">/</div><div class="fb-l" id="fb-l"></div></div></div>
      <div class="field"><label>Commande de lancement</label>
        <input id="f-cmd" placeholder="python -m http.server $PORT" autocomplete="off">
        <div class="hint">La variable <code>$PORT</code> est fournie : l'app doit ecouter dessus.</div></div>
    </div>
    <div id="s2" style="display:none">
      <div class="recap">
        <div><b>Nom</b><span id="r-n"></span></div>
        <div><b>Dossier</b><span id="r-p"></span></div>
        <div><b>Commande</b><span id="r-c"></span></div>
        <div><b>Adresse</b><span id="r-u"></span></div></div>
      <p class="hint" style="margin-top:12px">Creee a l'arret : demarre-la avec son interrupteur.</p>
    </div>
  </div>
  <div class="mf"><button class="btn btn-default" onclick="hide('ov-add')">Annuler</button>
    <button class="btn btn-primary" id="a-b" onclick="step2()">Continuer</button></div>
</div></div>

<div class="ov" id="ov-log"><div class="modal">
  <div class="mh"><h3 id="l-t">Journal</h3></div>
  <div class="mb"><div class="logs" id="l-b"></div></div>
  <div class="mf"><button class="btn btn-default" onclick="hide('ov-log')">Fermer</button></div>
</div></div>

<script>
const $=i=>document.getElementById(i), hide=i=>$(i).classList.remove("show");
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let cur="";
async function refresh(){
  const a=(await (await fetch("/api/apps")).json()).apps;
  $("count").textContent=a.length?a.filter(x=>x.running).length+" active(s) sur "+a.length:"";
  $("list").innerHTML=a.length?'<div class="table">'+a.map(row).join("")+'</div>':
   '<div class="empty"><h3>Aucune application</h3><div class="muted">Ajoute ton premier projet avec le bouton Ajouter.</div></div>';
}
function row(a){
  const p=a.running?'<span class="pill on">En ligne</span>':
   (a.failed?'<span class="pill err">Arret imprevu</span>':'<span class="pill off">Arretee</span>');
  return `<div class="tr">
   <button class="sw ${a.running?'on':''}" onclick="tg('${a.name}')" title="Activer / desactiver"></button>
   <div class="cell"><div class="name">${esc(a.name)}${p}</div><div class="path">${esc(a.path)}</div></div>
   <div class="url">${a.running?`<a href="/${a.name}/" target="_blank">/${a.name}/</a>`:`/${a.name}/`}</div>
   <div class="acts"><button class="btn btn-default" onclick="lg('${a.name}')">Journal</button>
    <button class="btn btn-quiet" onclick="rm('${a.name}')" title="Supprimer">
     <svg class="icon" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
    </button></div></div>`;
}
async function tg(n){ await fetch("/api/toggle/"+n,{method:"POST"}); setTimeout(refresh,500); }
async function rm(n){ if(!confirm("Supprimer « "+n+" » ? Le dossier du projet n'est pas touche."))return;
 await fetch("/api/app/"+n,{method:"DELETE"}); refresh(); }
async function lg(n){ $("l-t").textContent="Journal — "+n; $("l-b").textContent="Chargement...";
 $("ov-log").classList.add("show");
 $("l-b").textContent=(await (await fetch("/api/logs/"+n)).json()).lines.join("\n"); }
function openAdd(){
  $("s1").style.display=""; $("s2").style.display="none";
  $("a-b").textContent="Continuer"; $("a-b").onclick=step2;
  $("a-t").textContent="Ajouter une application";
  $("a-s").textContent="Indique le dossier du projet et la commande de lancement.";
  $("a-e").style.display="none"; $("f-name").value=""; $("f-cmd").value="";
  $("f-name").oninput=()=>$("f-url").textContent="/"+
   ($("f-name").value.trim().toLowerCase().replace(/[^a-z0-9_-]/g,"-")||"nom")+"/";
  browse(__ROOT__); $("ov-add").classList.add("show"); $("f-name").focus();
}
async function browse(p){
  const d=await (await fetch("/api/browse?path="+encodeURIComponent(p))).json();
  cur=d.path; $("fb-c").textContent=d.label;
  let h=d.hasIndex?'<div class="ok">✓ index.html present dans ce dossier</div>':"";
  if(d.parent) h+=`<div onclick="browse('${d.parent}')">↩ Dossier parent</div>`;
  h+=d.dirs.map(n=>`<div onclick="browse('${d.path}/${n}')">📁 ${esc(n)}</div>`).join("");
  $("fb-l").innerHTML=h||'<div class="muted" style="cursor:default">Dossier vide</div>';
}
function err(m){ const e=$("a-e"); e.textContent=m; e.style.display="block"; }
function step2(){
  const n=$("f-name").value.trim(), c=$("f-cmd").value.trim();
  if(!n) return err("Donne un nom au projet.");
  if(!c) return err("Indique la commande de lancement.");
  $("a-e").style.display="none";
  const s=n.toLowerCase().replace(/[^a-z0-9_-]/g,"-");
  $("r-n").textContent=s; $("r-p").textContent=cur; $("r-c").textContent=c;
  $("r-u").textContent=location.origin+"/"+s+"/";
  $("s1").style.display="none"; $("s2").style.display="";
  $("a-t").textContent="Confirmer"; $("a-s").textContent="Verifie avant de valider.";
  $("a-b").textContent="Ajouter"; $("a-b").onclick=send;
}
async function send(){
  const r=await fetch("/api/add",{method:"POST",headers:{"Content-Type":"application/json"},
   body:JSON.stringify({name:$("f-name").value,path:cur,command:$("f-cmd").value})});
  const d=await r.json();
  if(!r.ok){ $("s1").style.display=""; $("s2").style.display="none";
   $("a-b").textContent="Continuer"; $("a-b").onclick=step2; return err(d.error); }
  hide("ov-add"); refresh();
}
document.addEventListener("keydown",e=>{if(e.key==="Escape"){hide("ov-add");hide("ov-log");}});
document.querySelectorAll(".ov").forEach(o=>o.addEventListener("click",
 e=>{if(e.target===o)o.classList.remove("show")}));
refresh(); setInterval(refresh,4000);
</script></body></html>
"""


@flask_app.get("/")
def index():
    # __ROOT__ est remplace au rendu : le navigateur de dossiers part de la
    # meme racine que l'API, meme si APP_MANAGER_ROOT est redefini.
    return Response(PAGE.replace("__ROOT__", json.dumps(ROOT)),
                    mimetype="text/html")


if __name__ == "__main__":
    # LOG_DIR est sous STATE_DIR : le creer cree aussi le parent si le volume
    # monte est vide au premier demarrage.
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(APPS_FILE):
        save({})
    resume()
    flask_app.run(host="0.0.0.0",
                  port=int(os.environ.get("MANAGER_PORT", "9001")),
                  threaded=True)
