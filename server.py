#!/usr/bin/env python3
"""GitHub Hub : télécharge, compile, lance et met à jour des dépôts GitHub.

Ce module contient toute la logique et l'API interne. L'appli de bureau
(app.py) l'ouvre dans sa propre fenêtre ; il reste lançable seul pour déboguer :
  python3 server.py            -> http://127.0.0.1:8765
Variables d'environnement :
  HUB_DIR       dossier où ranger les dépôts (défaut ~/GitHubHub)
  HUB_PORT      port HTTP en mode autonome (défaut 8765)
  GITHUB_TOKEN  facultatif, relève la limite de l'API GitHub
"""
import functools
import html
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB_DIR = Path(os.environ.get("HUB_DIR", Path.home() / "GitHubHub")).expanduser()
REPOS_DIR = HUB_DIR / "repos"
CACHE_FILE = HUB_DIR / "cache.json"
FAVORITES_FILE = HUB_DIR / "favorites.json"
PORT = int(os.environ.get("HUB_PORT", "8765"))
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")
PAGE_TTL = 30 * 60        # pages tendances / README en mémoire
META_TTL = 6 * 3600       # infos des dépôts, gardées sur disque

jobs = {}            # id -> {repo, action, state, log, started}
jobs_lock = threading.Lock()
busy_repos = set()   # un seul job à la fois par dépôt
running = {}         # repo -> Popen des programmes lancés
page_cache = {}      # url -> (timestamp, data)
meta_lock = threading.Lock()

# Catégories : l'ordre départage les égalités. Les mots sont des débuts de
# mots cherchés dans le nom, la description, les topics et le langage.
CATEGORIES = [
    {"id": "jeux", "name": "Jeux vidéo", "icon": "🎮", "color": "#a371f7",
     "words": ["game", "gaming", "emulat", "ps2", "ps3", "ps4", "ps5", "playstation", "nintendo",
               "switch", "xbox", "retro", "steam", "godot", "unity", "unreal", "minecraft",
               "roguelike", "decomp", "porting", "speedrun", "mod", "rom"]},
    {"id": "ia", "name": "IA", "icon": "🤖", "color": "#2f81f7",
     "words": ["ai", "llm", "agent", "gpt", "claude", "openai", "gemini", "rag", "mcp", "diffusion",
               "transformer", "inference", "machine learning", "deep learning", "neural", "chatbot",
               "prompt", "copilot", "codex", "vibe", "skill", "model"]},
    {"id": "creation", "name": "Création", "icon": "🎨", "color": "#db61a2",
     "words": ["video", "photo", "image", "audio", "music", "sound", "design", "3d", "animation",
               "render", "font", "drawing", "paint", "camera", "media", "podcast", "adobe", "figma",
               "art", "svg", "editing"]},
    {"id": "securite", "name": "Sécurité", "icon": "🛡️", "color": "#f85149",
     "words": ["security", "secur", "reverse", "exploit", "pentest", "malware", "ctf", "binary",
               "vulnerab", "firewall", "osint", "privacy", "encrypt", "hack", "ghidra", "fuzz"]},
    {"id": "outils", "name": "Outils", "icon": "🧰", "color": "#d29922",
     "words": ["cli", "terminal", "utility", "productivity", "self-host", "selfhost", "docker",
               "linux", "backup", "download", "launcher", "manager", "extension", "android",
               "windows", "macos", "browser", "note", "archive", "desktop", "homelab", "monitor",
               "vpn", "proxy", "dns", "home assistant", "automation"]},
    {"id": "dev", "name": "Développement", "icon": "💻", "color": "#3fb950",
     "words": ["framework", "library", "sdk", "compiler", "language", "database", "api", "devtool",
               "debug", "test", "lint", "ide", "editor", "react", "web", "typescript", "rust", "git",
               "package", "orm", "http", "backend", "frontend", "kubernetes", "server"]},
]
OTHER = {"id": "autres", "name": "Autres", "icon": "📦", "color": "#8b949e", "words": []}

# Topics GitHub cherchés pour remplir une catégorie (6 au plus : la recherche
# GitHub refuse plus de 5 « OR » par requête).
CATEGORY_TOPICS = {
    "jeux": ["game", "emulator", "gaming", "godot", "minecraft", "modding"],
    "ia": ["llm", "ai-agents", "machine-learning", "claude-code", "mcp", "generative-ai"],
    "creation": ["video-editor", "image-editor", "audio", "3d", "design", "animation"],
    "securite": ["security", "reverse-engineering", "pentest", "malware", "osint", "privacy"],
    "outils": ["cli", "self-hosted", "productivity", "utility", "desktop-app", "automation"],
    "dev": ["framework", "developer-tools", "compiler", "database", "sdk", "library"],
}
# fenêtre de création des projets récents, selon la période choisie
CATEGORY_WINDOW = {"daily": 14, "weekly": 30, "monthly": 90}
MIN_STARS = 100


# ---------------------------------------------------------------- GitHub
def http_get(url, accept="application/vnd.github+json"):
    hit = page_cache.get(url)
    if hit and time.time() - hit[0] < PAGE_TTL:
        return hit[1]
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "github-hub-local"})
    if os.environ.get("GITHUB_TOKEN") and "api.github.com" in url:
        req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode()
    data = json.loads(raw) if accept.endswith("json") else raw
    page_cache[url] = (time.time(), data)
    return data


def load_meta_cache():
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


meta_cache = load_meta_cache()   # repo -> {"t": timestamp, ...infos}


def load_favorites():
    try:
        return json.loads(FAVORITES_FILE.read_text())
    except Exception:
        return {}


def save_favorites():
    FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAVORITES_FILE.write_text(json.dumps(favorites, indent=1))


# repo -> {"added": timestamp, "category": id} ; gardés sur disque, même pour un
# projet qui n'est plus en tendance
favorites = load_favorites()


def meta_from_api(i):
    return {"t": time.time(), "stars": i["stargazers_count"], "forks": i["forks_count"],
            "description": i["description"] or "", "language": i["language"],
            "topics": i.get("topics", []), "license": (i.get("license") or {}).get("spdx_id"),
            "pushed_at": i.get("pushed_at"), "created_at": i.get("created_at"),
            "homepage": i.get("homepage") or "",
            "issues": i.get("open_issues_count")}


def fetch_meta(repos):
    """Infos de plusieurs dépôts via UNE recherche par paquet (repo:a repo:b …).

    L'API REST classique limite à 60 appels/heure sans compte ; la recherche
    groupée en fait tenir une dizaine par appel. Les infos sont gardées 6 h sur disque.
    """
    stale = [r for r in dict.fromkeys(repos) if REPO_RE.match(r)
             and time.time() - meta_cache.get(r, {}).get("t", 0) > META_TTL]
    chunks, cur = [], ""
    for r in stale:
        part = f"repo:{r} "
        if len(cur) + len(part) > 240:
            chunks.append(cur)
            cur = ""
        cur += part
    if cur:
        chunks.append(cur)
    for q in chunks:
        url = ("https://api.github.com/search/repositories?per_page=100&q="
               + urllib.parse.quote(q.strip()))
        try:
            for i in http_get(url).get("items", []):
                meta_cache[i["full_name"]] = meta_from_api(i)
        except Exception as e:
            if any(code in str(e) for code in ("403", "429")) or "urlopen" in str(e):
                break  # limite atteinte ou hors ligne : on garde les infos en cache
            # sinon (requête refusée pour ce paquet seulement) : on tente les paquets suivants
    if chunks:
        with meta_lock:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(meta_cache))
    return {r: meta_cache.get(r, {}) for r in repos}


def scrape_trending(since, language):
    """github.com/trending n'a pas d'API : on lit la page HTML."""
    url = "https://github.com/trending"
    if language:
        url += "/" + urllib.parse.quote(language.lower())
    page = http_get(url + "?since=" + since, accept="text/html")
    items = []
    for a in page.split('<article class="Box-row">')[1:]:
        name = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+)"', a)
        if not name:
            continue
        desc = re.search(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', a, re.S)
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', a)
        stars = re.search(r'/stargazers"[^>]*>.*?</svg>\s*([\d,]+)', a, re.S)
        gained = re.search(r'([\d,]+) stars? (?:today|this week|this month)', a)
        items.append({
            "repo": name.group(1),
            "description": html.unescape(re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else "",
            "language": lang.group(1).strip() if lang else None,
            "stars": int(stars.group(1).replace(",", "")) if stars else None,
            "gained": int(gained.group(1).replace(",", "")) if gained else None,
        })
    return items


def search_new(days, language):
    """Projets créés récemment, triés par étoiles (API de recherche GitHub)."""
    since = (date.today() - timedelta(days=days)).isoformat()
    q = f"created:>{since}" + (f" language:{language}" if language else "")
    url = ("https://api.github.com/search/repositories?sort=stars&order=desc&per_page=60&q="
           + urllib.parse.quote(q))
    items = http_get(url).get("items", [])
    for i in items:
        meta_cache[i["full_name"]] = meta_from_api(i)
    return [{"repo": i["full_name"], "gained": None} for i in items]


def trending(source, language):
    periods = {"daily": 1, "weekly": 7, "monthly": 30}
    if source in periods:
        try:
            items = scrape_trending(source, language)
            if items:
                return items
        except Exception:
            pass  # page indisponible : on se rabat sur l'API
        return search_new(periods[source], language)
    return search_new(int(source.removeprefix("new")), language)


def trending_items(source, language):
    """Tendances générales, complétées (topics, forks…) et classées par catégorie."""
    items = trending(source, language)
    metas = fetch_meta([i["repo"] for i in items])
    for i in items:
        # la page tendances donne les étoiles du moment : elles priment
        i.update({k: v for k, v in metas[i["repo"]].items() if i.get(k) in (None, "")})
        with_category(i)
    return items


def category_trending(cat_id, source, language):
    """Une catégorie : ses tendances générales, puis les projets récents les plus étoilés.

    La page github.com/trending ne liste qu'une vingtaine de dépôts, souvent tous
    d'IA : sans ce complément, les autres catégories restaient vides.
    """
    general = [i for i in trending_items(source, language) if i["category"] == cat_id]
    topics = CATEGORY_TOPICS.get(cat_id)
    if not topics:
        return general
    since = (date.today() - timedelta(days=CATEGORY_WINDOW.get(source, 30))).isoformat()
    # un plancher d'étoiles écarte les dépôts jetables (souvent des appâts à malware)
    q = (" OR ".join(topics) + f" in:topics created:>{since} stars:>={MIN_STARS}"
         + (f" language:{language}" if language else ""))
    url = ("https://api.github.com/search/repositories?sort=stars&order=desc&per_page=30&q="
           + urllib.parse.quote(q))
    seen = {i["repo"] for i in general}
    try:
        found = http_get(url).get("items", [])
    except Exception:
        if general:
            return general  # quota de recherche épuisé : on montre au moins les tendances
        raise
    for i in found:
        meta_cache[i["full_name"]] = meta_from_api(i)
        if i["full_name"] not in seen:
            general.append({"repo": i["full_name"], "gained": None, "category": cat_id,
                            **meta_cache[i["full_name"]]})
    return general


_mine_cache = {}


def my_repos():
    """Tes propres dépôts, privés compris, lus avec l'outil `gh` déjà connecté à ton compte.

    Passer par `gh` évite d'avoir à donner un jeton au hub, et sa requête GraphQL
    ne consomme pas le quota de recherche (10/min).
    """
    if _mine_cache and time.time() - _mine_cache["t"] < 10 * 60:
        return _mine_cache["data"]
    if not shutil.which("gh"):
        raise RuntimeError("L'outil « gh » n'est pas installé (paquet github-cli ou gh).")
    fields = ("nameWithOwner,description,stargazerCount,forkCount,primaryLanguage,isPrivate,"
              "isFork,pushedAt,createdAt,homepageUrl,licenseInfo,repositoryTopics")
    p = subprocess.run(["gh", "repo", "list", "--limit", "200", "--json", fields],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError("gh n'a pas pu lister tes dépôts : " + (p.stderr.strip() or "connecte-toi avec « gh auth login »"))
    login = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True,
                           text=True, timeout=30).stdout.strip()
    items = []
    for r in json.loads(p.stdout):
        item = {"repo": r["nameWithOwner"], "description": r["description"] or "",
                "stars": r["stargazerCount"], "forks": r["forkCount"],
                "language": (r["primaryLanguage"] or {}).get("name"),
                "topics": [t["name"] for t in r["repositoryTopics"] or []],
                "pushed_at": r["pushedAt"], "created_at": r["createdAt"],
                "homepage": r["homepageUrl"] or "",
                "license": ((r["licenseInfo"] or {}).get("key") or "").upper() or None,
                "private": r["isPrivate"], "fork": r["isFork"], "mine": True, "gained": None,
                "category": catalog_entry(r["nameWithOwner"]).get("category")}
        items.append(with_category(item))
    data = {"login": login, "items": items}
    _mine_cache.update(t=time.time(), data=data)
    return data


SEARCH_SORTS = {"best": "", "stars": "stars", "updated": "updated"}


def search_github(query, sort="best", language="", page=1):
    """Recherche sur tout GitHub. La requête accepte la syntaxe GitHub :
    language:rust, stars:>1000, topic:emulator, user:storytold, in:readme…"""
    q = query.strip() + (f" language:{language}" if language else "")
    params = {"q": q, "per_page": 30, "page": page}
    if SEARCH_SORTS.get(sort):
        params.update(sort=SEARCH_SORTS[sort], order="desc")
    d = http_get("https://api.github.com/search/repositories?" + urllib.parse.urlencode(params))
    items = []
    for i in d.get("items", []):
        meta_cache[i["full_name"]] = meta_from_api(i)
        items.append(with_category({"repo": i["full_name"], "gained": None, **meta_cache[i["full_name"]],
                                    "category": catalog_entry(i["full_name"]).get("category")}))
    # GitHub ne donne jamais plus de 1 000 résultats par recherche
    return {"total": d.get("total_count", 0), "items": items,
            "more": page * 30 < min(d.get("total_count", 0), 1000)}


def days_since(iso):
    if not iso:
        return None
    created = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return max(1, (datetime.now(timezone.utc) - created).days)


def buzz():
    """Les phénomènes du moment, classés par étoiles gagnées par jour.

    Deux sources : les gros succès de l'année (vitesse moyenne depuis leur création)
    et les explosions récentes vues sur github.com/trending (gain de la semaine /
    du mois), ce qui attrape aussi un vieux projet qui repart d'un coup.
    """
    found = {}
    # les plus gros totaux de l'année, puis les plus gros des 60 derniers jours : un
    # projet de 9 jours à 4 000 ★/jour n'entre pas encore dans le top de l'année
    searches = [(365, 10000), (60, 3000)]
    results = []
    for days, min_stars in searches:
        since = (date.today() - timedelta(days=days)).isoformat()
        url = ("https://api.github.com/search/repositories?sort=stars&order=desc&per_page=50&q="
               + urllib.parse.quote(f"created:>{since} stars:>{min_stars}"))
        results += http_get(url).get("items", [])
    for i in results:
        if i["full_name"] in found:
            continue
        meta_cache[i["full_name"]] = meta_from_api(i)
        age = days_since(i["created_at"])
        found[i["full_name"]] = {"repo": i["full_name"], "age_days": age,
                                 "lifetime": i["stargazers_count"] / age, "recent": 0, "week": None}
    for source, days in (("weekly", 7), ("monthly", 30)):
        try:
            for t in scrape_trending(source, ""):
                if not t["gained"]:
                    continue
                e = found.setdefault(t["repo"], {"repo": t["repo"], "age_days": None,
                                                 "lifetime": 0, "recent": 0, "week": None})
                e["recent"] = max(e["recent"], t["gained"] / days)
                if source == "weekly":
                    e["week"] = t["gained"]
        except Exception:
            pass  # page tendances indisponible : on garde le classement de l'année
    ranked = sorted(found.values(), key=lambda e: -max(e["lifetime"], e["recent"]))[:30]
    metas = fetch_meta([e["repo"] for e in ranked])
    items = []
    for rank, e in enumerate(ranked, 1):
        # le gain de la semaine est affiché dans le bandeau buzz, pas comme « gain du jour »
        item = {"repo": e["repo"], **metas[e["repo"]], "gained": None,
                "category": catalog_entry(e["repo"]).get("category")}  # ta sélection prime
        age = e["age_days"] or days_since(item.get("created_at"))
        score = max(e["lifetime"], e["recent"])
        reason = ("Nouveau phénomène" if age and age <= 45
                  else "Explosion récente" if e["recent"] >= 1.5 * e["lifetime"]
                  else "Carton de l'année")
        item["buzz"] = {"rank": rank, "per_day": round(score), "age_days": age,
                        "week": e["week"], "reason": reason}
        items.append(with_category(item))
    return items


def readme_html(repo):
    """README tel que GitHub l'affiche, extrait de la page du dépôt (sans quota d'API)."""
    page = http_get(f"https://github.com/{repo}", accept="text/html")
    start = page.find('<article class="markdown-body')
    if start < 0:
        return ""
    return page[start:page.find("</article>", start) + len("</article>")]


def category_matches(item):
    """[(id_catégorie, nb de mots-clés trouvés)], du plus au moins pertinent."""
    text = " ".join([item["repo"].replace("/", " ").replace("-", " "), item.get("description") or "",
                     " ".join(item.get("topics") or []), item.get("language") or ""]).lower()
    found = []
    for cat in CATEGORIES:
        hits = sum(1 for w in cat["words"] if re.search(r"(?<![a-z0-9])" + re.escape(w), text))
        if hits:
            found.append((cat["id"], hits))
    return sorted(found, key=lambda m: -m[1])


def choose_category(matches):
    """Choisit LA catégorie d'un dépôt parmi celles qui correspondent.

    matches : liste [(id_catégorie, nb_mots_clés)], déjà triée du plus grand
    au plus petit nombre de mots-clés, dans l'ordre de CATEGORIES en cas d'égalité.
    Retourne un id de catégorie (ou OTHER["id"] si la liste est vide).
    """
    # TODO(human)
    return matches[0][0] if matches else OTHER["id"]


def public_categories():
    return [{k: c[k] for k in ("id", "name", "icon", "color")} for c in CATEGORIES + [OTHER]]


# ---------------------------------------------------------------- recettes
_catalog_cache = {}


def load_catalog():
    mtime = (HERE / "catalog.json").stat().st_mtime
    if _catalog_cache.get("mtime") != mtime:
        cat = json.loads((HERE / "catalog.json").read_text())
        _catalog_cache.update(mtime=mtime, data=cat, by_repo={r["repo"]: r for r in cat["repos"]})
    return _catalog_cache


def catalog_entry(repo):
    return load_catalog()["by_repo"].get(repo, {})


def repo_path(repo):
    return REPOS_DIR / repo.replace("/", "__")


def cargo_binaries(path):
    out = path / "target" / "release"
    if not out.is_dir():
        return []
    return sorted(p for p in out.iterdir()
                  if p.is_file() and os.access(p, os.X_OK) and "." not in p.name)


def recipe(repo):
    """Comment compiler et lancer ce dépôt : catalogue d'abord, sinon on devine."""
    path, entry = repo_path(repo), catalog_entry(repo)
    r = {"build": entry.get("build"), "run": entry.get("run"),
         "terminal": entry.get("terminal", False), "platform": entry.get("platform"),
         "built_check": entry.get("built_check"), "cli": entry.get("cli"),
         "web": entry.get("web"), "args_hint": entry.get("args_hint", ""), "modes": []}
    if r["platform"] == "windows":
        r["modes"] = ["web"] if r["web"] else []
        return r
    guessed_binary = False
    if not r["build"]:
        if (path / "Cargo.toml").exists():
            r["build"] = ["cargo", "build", "--release"]
        elif (path / "package.json").exists():
            r["build"] = ["npm", "install"]
        elif (path / "CMakeLists.txt").exists():
            r["build"] = ["sh", "-c", "cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --parallel"]
            r["requires"] = ["cmake"]
        elif (path / "Makefile").exists():
            r["build"] = ["make"]
    if not r["run"]:
        bins = cargo_binaries(path)
        name = repo.split("/")[1].lower()
        pick = next((b for b in bins if b.name.lower() == name), bins[0] if len(bins) == 1 else None)
        if pick:
            # binaire deviné : souvent un outil en ligne de commande -> terminal
            r["run"], r["terminal"] = [str(pick.relative_to(path))], True
            r["cli"], guessed_binary = r["run"], True
        elif (path / "package.json").exists() and '"start"' in (path / "package.json").read_text():
            r["run"], r["terminal"] = ["npm", "start"], True
        else:
            # rien de reconnu : « Lancer » ouvre un terminal dans le dossier
            r["run"], r["terminal"] = [os.environ.get("SHELL", "sh")], True
    # seulement un chemin DANS le projet (target/release/x) : le terminal de secours
    # (/bin/zsh) existe toujours et faisait croire que tout était compilé
    if r["run"] and not r["built_check"] and "/" in r["run"][0] and not r["run"][0].startswith("/"):
        r["built_check"] = r["run"][0]
    r["requires"] = entry.get("requires") or r.get("requires") or (
        [r["build"][0]] if r["build"] and r["build"][0] != "sh" else [])
    # Modes de lancement proposés, le premier étant celui par défaut :
    #   window   = le programme tel quel, sans terminal (interface graphique)
    #   terminal = dans Konsole
    #   hub      = outil en ligne de commande piloté depuis un formulaire du hub
    #   web      = la version web officielle, dans le navigateur
    modes = []
    if r["run"] and (not r["terminal"] or guessed_binary):
        modes.append("window")
    if r["run"]:
        modes.append("terminal")
    if r["cli"]:
        modes.append("hub")
    if r["web"]:
        modes.append("web")
    preferred = entry.get("default_mode") or ("terminal" if guessed_binary else None)
    if preferred in modes:
        modes.remove(preferred)
        modes.insert(0, preferred)
    r["modes"] = modes
    return r


def missing_tools(r):
    return [t for t in r.get("requires", []) if not shutil.which(t)]


# ---------------------------------------------------------------- git local
def git(path, *args, timeout=600):
    p = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def is_running(repo):
    p = running.get(repo)
    return p is not None and p.poll() is None


# jetons que les projets compilés ou lancés n'ont pas à voir : un script npm
# « postinstall » ou un build.rs pourrait les lire et les envoyer ailleurs
SECRET_VARS = ("GITHUB_TOKEN", "GH_TOKEN")


def child_env():
    return {k: v for k, v in os.environ.items() if k not in SECRET_VARS}


BUILT_MARKER = "github-hub-built"  # dans .git/ : invisible pour git, contient le commit compilé


def is_built(path, r):
    if not r["build"]:
        return True
    marker = path / ".git" / BUILT_MARKER
    if marker.exists():
        # compilé, mais pour quel commit ? Après une mise à jour dont la recompilation
        # a échoué, le repère (et l'ancien binaire) datent de la version d'avant
        _, head = git(path, "rev-parse", "HEAD")
        return marker.read_text().strip() == head and (
            not r["built_check"] or (path / r["built_check"]).exists())
    if r["built_check"]:
        return (path / r["built_check"]).exists()
    # projets npm installés avant l'ajout du marqueur
    return r["build"][0] == "npm" and (path / "node_modules").is_dir()


def local_status(repo):
    path = repo_path(repo)
    if not (path / ".git").exists():
        entry = catalog_entry(repo)
        return {"installed": False, "platform": entry.get("platform"), "web": entry.get("web")}
    _, head = git(path, "log", "-1", "--format=%h|%cs|%s")
    _, dirty = git(path, "status", "--porcelain", "--untracked-files=no")
    code, counts = git(path, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    ahead = behind = None
    if code == 0:
        ahead, behind = (int(x) for x in counts.split())
    sha, day, subject = (head.split("|", 2) + ["", ""])[:3]
    r = recipe(repo)
    return {"installed": True, "head": {"sha": sha, "date": day, "subject": subject},
            "dirty": bool(dirty), "ahead": ahead, "behind": behind, "path": str(path),
            "platform": r["platform"], "can_build": bool(r["build"]),
            "can_run": any(m != "web" for m in r["modes"]), "modes": r["modes"],
            "web": r["web"], "args_hint": r["args_hint"],
            "built": is_built(path, r),
            "missing": missing_tools(r), "running": is_running(repo),
            "size": disk_size(repo), "shortcuts": shortcut_state(repo),
            "launchable": bool(shortcut_command(repo, path, r))}


# ---------------------------------------------------------------- poids sur le disque
# Dossiers produits par la compilation (jamais suivis par git) : effaçables sans risque,
# le hub sait les reconstruire.
BUILD_DIRS = ["target", "node_modules", "build", "build-relinker"]
size_cache = {}       # repo -> {"total": octets, "build": octets, "t": timestamp}
sizing = set()        # calculs en cours (en arrière-plan)
size_lock = threading.Lock()


def du_bytes(*paths):
    paths = [str(p) for p in paths if Path(p).exists()]
    if not paths:
        return 0
    p = subprocess.run(["du", "-sbc", *paths], capture_output=True, text=True, timeout=300)
    last = p.stdout.strip().splitlines()[-1:] or ["0"]
    return int(last[0].split()[0])


def build_dirs(path):
    """Dossiers de compilation présents ET ignorés par git (jamais un dossier du projet)."""
    found = []
    for name in BUILD_DIRS:
        d = path / name
        if d.is_dir() and not git(path, "ls-files", "--", name)[1]:
            found.append(d)
    return found


def compute_size(repo):
    path = repo_path(repo)
    try:
        info = {"total": du_bytes(path), "build": du_bytes(*build_dirs(path)), "t": time.time()}
        size_cache[repo] = info
    finally:
        with size_lock:
            sizing.discard(repo)


def disk_size(repo):
    """Poids en cache ; le calcul (du sur des Go) se fait en arrière-plan, jamais en attente."""
    info = size_cache.get(repo)
    if not info or time.time() - info["t"] > 15 * 60:
        with size_lock:
            if repo not in sizing:
                sizing.add(repo)
                threading.Thread(target=compute_size, args=(repo,), daemon=True).start()
    return info


# ---------------------------------------------------------------- raccourcis
APPS_DIR = Path.home() / ".local" / "share" / "applications"
ICONS_DIR = HUB_DIR / "icons"


@functools.cache
def desktop_dir():
    """~/Bureau en français, ~/Desktop en anglais : c'est xdg-user-dir qui le sait."""
    if shutil.which("xdg-user-dir"):
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True).stdout.strip()
        if out:
            return Path(out)
    return Path.home() / "Desktop"


def refresh_menu():
    if shutil.which("kbuildsycoca6"):
        subprocess.run(["kbuildsycoca6"], capture_output=True)


def shortcut_name(repo):
    return "github-hub-" + re.sub(r"[^A-Za-z0-9_.-]", "-", repo.replace("/", "__")) + ".desktop"


def shortcut_state(repo):
    name = shortcut_name(repo)
    return {"desktop": (desktop_dir() / name).exists(), "menu": (APPS_DIR / name).exists()}


def desktop_quote(arg):
    """Guillemets au format .desktop (spécification freedesktop « Exec »).

    Deux niveaux : la règle des guillemets d'Exec (\\" \\$ …), puis l'échappement
    général des valeurs .desktop, qui double chaque barre oblique inverse. Et % est
    réservé aux codes %f %u… : un % littéral s'écrit %%.
    """
    arg = arg.replace("%", "%%")
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg):
        return arg
    quoted = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return quoted.replace("\\", "\\\\")


def shortcut_command(repo, path, r=None):
    """La commande lancée par un raccourci, hors du hub : fenêtre si possible, sinon terminal."""
    r = r or recipe(repo)
    if not r["run"] or r["platform"] == "windows" or not is_built(path, r):
        return None
    cmd = list(r["run"])
    if "/" in cmd[0] and not cmd[0].startswith("/"):
        cmd[0] = str(path / cmd[0])
    if "window" in r["modes"]:
        return cmd
    if "terminal" in r["modes"]:
        try:
            return terminal_cmd(cmd, path)
        except RuntimeError:  # ni konsole ni xterm (bureau GNOME…) : pas de raccourci possible
            return None
    return None


def find_icon(repo, path):
    """Icône du projet si elle existe (hicolor, logo, icon…), sinon l'avatar de l'auteur."""
    best, best_score = None, -1
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", *BUILD_DIRS, "vendor", "third_party", "3rdparty")]
        if root.count(os.sep) - str(path).count(os.sep) > 6:
            dirs[:] = []
            continue
        for f in files:
            low = f.lower()
            if not low.endswith((".svg", ".png")):
                continue
            where = (root + "/" + low).lower()
            if not any(k in where for k in ("icon", "logo", "hicolor")) or "favicon" in low:
                continue
            score = (40 if "app-icon" in where or "hicolor" in where else 0) + \
                    (30 if low.endswith(".svg") else 0) + (20 if "256" in where or "512" in where else 0) + \
                    (10 if repo.split("/")[1].lower() in low else 0)
            if score > best_score:
                best, best_score = os.path.join(root, f), score
    if best:
        return best
    owner = repo.split("/")[0]
    avatar = ICONS_DIR / f"{owner}.png"
    try:
        if not avatar.exists():
            ICONS_DIR.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(f"https://github.com/{owner}.png?size=256", timeout=15) as resp:
                avatar.write_bytes(resp.read())
        return str(avatar)
    except Exception:
        return "application-x-executable"


def kde_favorite(name, on):
    """Épingle (ou retire) une appli dans les favoris du lanceur KDE, comme « Ajouter aux favoris »."""
    if not shutil.which("qdbus6"):
        return
    method = "LinkResourceToActivity" if on else "UnlinkResourceFromActivity"
    subprocess.run(["qdbus6", "org.kde.ActivityManager", "/ActivityManager/Resources/Linking",
                    f"org.kde.ActivityManager.ResourcesLinking.{method}",
                    "org.kde.plasma.favorites.applications", f"applications:{name}", ":global"],
                   capture_output=True, timeout=10)


def set_shortcut(repo, kind, on):
    path, name = repo_path(repo), shortcut_name(repo)
    target = (desktop_dir() if kind == "desktop" else APPS_DIR) / name
    if not on:
        if target.exists():
            target.unlink()
            if kind == "menu":
                kde_favorite(name, False)
                refresh_menu()
        return
    cmd = shortcut_command(repo, path)
    if not cmd:
        raise RuntimeError("Ce projet ne peut pas se lancer hors du hub (pas encore compilé, "
                           "ou outil sans interface).")
    entry = catalog_entry(repo)
    meta = meta_cache.get(repo, {})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join([
        "[Desktop Entry]", "Type=Application",
        f"Name={entry.get('title', repo.split('/')[1]).split(' — ')[0]}",
        f"Comment={(entry.get('summary') or meta.get('description') or repo)[:160]}",
        "Exec=" + " ".join(desktop_quote(a) for a in cmd),
        f"Path={path}", f"Icon={find_icon(repo, path)}", "Terminal=false",
        "Categories=Utility;", f"X-GitHub-Hub-Repo={repo}", ""]))
    target.chmod(0o755)  # KDE n'exécute un raccourci du bureau que s'il est exécutable
    if kind == "menu":
        refresh_menu()
        kde_favorite(name, True)


def pull_plan(dirty, ahead, behind):
    """Commandes git pour rattraper le serveur sans perdre de travail local."""
    plan = [["stash", "push", "-m", "github-hub: avant mise à jour"]] if dirty else []
    # avec des commits locaux, on les rejoue par-dessus la version du serveur
    plan.append(["rebase", "@{upstream}"] if ahead else ["merge", "--ff-only", "@{upstream}"])
    if dirty:
        plan.append(["stash", "pop"])
    return plan


def terminal_cmd(cmd, cwd):
    if shutil.which("konsole"):
        return ["konsole", "--workdir", str(cwd), "-e", *cmd]
    if shutil.which("xterm"):
        return ["xterm", "-e", *cmd]
    raise RuntimeError("Aucun terminal trouvé (konsole ou xterm).")


# ---------------------------------------------------------------- jobs
def run_job(job_id, repo, action):
    job = jobs[job_id]
    log = job["log"].append
    opts = job.get("opts") or {}

    def stream(cmd, cwd=None, stoppable=False):
        log("$ " + shlex.join(cmd))
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", start_new_session=stoppable,
                             env=child_env())
        if stoppable:
            running[repo] = p  # le bouton « Arrêter » peut l'interrompre
        with p.stdout:
            for line in p.stdout:
                log(line.rstrip())
        return p.wait()

    def build():
        r = recipe(repo)
        if (path / ".gitmodules").exists() and catalog_entry(repo).get("submodules", True):
            # les sous-modules sont d'autres dépôts inclus dans le projet : sans eux le
            # projet est incomplet et sa compilation échoue. --depth 1 : leur dernière
            # version seulement, 8 à la fois
            log("── Sous-modules ──")
            code = stream(["git", "-C", str(path), "submodule", "update", "--init", "--recursive",
                           "--depth", "1", "--jobs", "8", "--progress"])
            if code != 0:
                return code
        if r["platform"] == "windows":
            log("Ce projet ne se compile que sous Windows : compilation ignorée.")
            return 0
        if not r["build"]:
            log("Aucune compilation nécessaire.")
            return 0
        if missing_tools(r):
            raise RuntimeError("outil(s) manquant(s) : " + ", ".join(missing_tools(r))
                               + " (à installer avec le gestionnaire de paquets du système)")
        log("── Compilation (ça peut prendre plusieurs minutes la première fois) ──")
        code = stream(r["build"], cwd=path)
        if code == 0:
            _, head = git(path, "rev-parse", "HEAD")
            (path / ".git" / BUILT_MARKER).write_text(head)
        return code

    def local_cmd(cmd):
        cmd = list(cmd)
        if "/" in cmd[0]:
            cmd[0] = str(path / cmd[0])
        return cmd

    def launch():
        r = recipe(repo)
        mode = opts.get("mode") or (r["modes"] or [None])[0]
        if mode not in r["modes"] or mode == "web":
            raise RuntimeError("Ce mode de lancement n'est pas disponible pour ce projet.")
        if is_running(repo):
            log("Déjà lancé.")
            return 0
        if mode == "hub":
            # outil en ligne de commande : sa sortie s'affiche dans le panneau Activité
            args = shlex.split(opts.get("args") or "")
            log("── Exécution dans le hub ──")
            return stream(local_cmd(r["cli"]) + args, cwd=path, stoppable=True)
        cmd = local_cmd(r["run"])
        if mode == "terminal":
            cmd = terminal_cmd(cmd, path)
        log("$ " + " ".join(cmd))
        # nouvelle session : le programme survit à la fermeture du hub
        running[repo] = subprocess.Popen(cmd, cwd=path, start_new_session=True, env=child_env(),
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("Lancé.")
        return 0

    try:
        path = repo_path(repo)
        code = 0
        if action == "install":
            if path.exists():
                raise RuntimeError("Déjà installé.")
            REPOS_DIR.mkdir(parents=True, exist_ok=True)
            log("── Téléchargement ──")
            # clone superficiel : seulement la dernière version, sans l'historique.
            # Mesuré sur AnyPS5 : 2× plus rapide et .git 4× plus léger qu'un clone partiel,
            # et les mises à jour (fetch + merge) fonctionnent pareil
            code = stream(["git", "clone", "--depth", "1", "--progress",
                           f"https://github.com/{repo}.git", str(path)])
            if code == 0:
                code = build()
        elif action == "update":
            _, before = git(path, "rev-parse", "HEAD")
            code = stream(["git", "-C", str(path), "fetch", "--prune"])
            st = local_status(repo)
            if code == 0 and st["behind"]:
                for cmd in pull_plan(st["dirty"], st["ahead"], st["behind"]):
                    code = stream(["git", "-C", str(path), *cmd])
                    if code != 0:
                        log("Mise à jour interrompue. Si tu avais des modifs, elles sont dans « git stash list ».")
                        break
            elif code == 0:
                log("Déjà à jour.")
            _, after = git(path, "rev-parse", "HEAD")
            if code == 0 and (after != before or not st["built"]):
                code = build()
        elif action == "build":
            code = build()
        elif action == "run":
            if not local_status(repo)["built"]:
                code = build()
            if code == 0:
                code = launch()
        elif action == "remove":
            for kind in ("desktop", "menu"):
                set_shortcut(repo, kind, False)  # un raccourci vers un dossier effacé ne sert à rien
            shutil.rmtree(path)
            log(f"Supprimé : {path}")
        elif action == "clean":
            dirs = build_dirs(path)
            freed = du_bytes(*dirs)
            for d in dirs:
                log(f"Efface {d.name}/")
                shutil.rmtree(d)
            (path / ".git" / BUILT_MARKER).unlink(missing_ok=True)
            log(f"{freed / 1e9:.2f} Go libérés. Le projet sera recompilé au prochain lancement.")
        else:
            raise RuntimeError("Action inconnue.")
        job["state"] = "ok" if code == 0 else "error"
    except Exception as e:
        log(f"Erreur : {e}")
        job["state"] = "error"
    finally:
        job["ended"] = time.time()
        size_cache.pop(repo, None)  # le poids a pu changer : recalcul au prochain affichage
        with jobs_lock:
            busy_repos.discard(repo)


def start_job(repo, action, opts=None):
    with jobs_lock:
        if repo in busy_repos:
            return None
        busy_repos.add(repo)
        job_id = uuid.uuid4().hex[:8]
        jobs[job_id] = {"id": job_id, "repo": repo, "action": action, "opts": opts or {},
                        "state": "running", "log": [], "started": time.time()}
    threading.Thread(target=run_job, args=(job_id, repo, action), daemon=True).start()
    return job_id


def pick_file(save=False):
    """Fenêtre native de choix de fichier (KDE, sinon GNOME) ; renvoie le chemin ou ""."""
    home = str(Path.home())
    if shutil.which("kdialog"):
        cmd = ["kdialog", "--getsavefilename" if save else "--getopenfilename", home]
    elif shutil.which("zenity"):
        cmd = ["zenity", "--file-selection"] + (["--save"] if save else [])
    else:
        return ""
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def stop_program(repo):
    p = running.get(repo)
    if p and p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM)


def installed_repos():
    if not REPOS_DIR.exists():
        return []
    # seuls les dossiers « auteur__projet » viennent du hub ; un autre dossier rangé là
    # (un worktree git, par exemple) n'est pas un dépôt GitHub et faisait échouer les infos
    names = (p.name.replace("__", "/", 1) for p in REPOS_DIR.iterdir() if (p / ".git").exists())
    return [n for n in names if REPO_RE.match(n)]


def jobs_running():
    return any(j["state"] == "running" for j in jobs.values())


def with_category(item):
    if not item.get("category"):
        item["category"] = choose_category(category_matches(item))
    return item


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def from_hub(self):
        """Refuse toute requête adressée à un autre nom que 127.0.0.1 / localhost.

        Sinon une page web piégée peut faire pointer son propre nom de domaine vers
        127.0.0.1 (« DNS rebinding ») : le navigateur la croit chez elle, la laisse
        envoyer X-Hub et lire les réponses, donc installer et lancer n'importe quoi.
        """
        port = self.server.server_address[1]
        if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return True
        self.send_json({"error": "interdit"}, 403)
        return False

    def do_GET(self):
        if not self.from_hub():
            return
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/logo.svg":
            body = (HERE / "logo.svg").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/catalog":
            curated = [dict(r, curated=True) for r in load_catalog()["data"]["repos"]]
            listed = {r["repo"] for r in curated}
            extra = dict.fromkeys(r for r in installed_repos() + list(favorites) if r not in listed)
            entries = curated + [{"repo": r} for r in extra]
            metas = fetch_meta([e["repo"] for e in entries])
            for e in entries:
                e.update(metas[e["repo"]])
                e["local"] = local_status(e["repo"])
                fav = favorites.get(e["repo"])
                e["favorite"] = fav is not None
                if fav and not e.get("category"):
                    e["category"] = fav.get("category")  # celle vue au moment de l'ajout
                with_category(e)
            self.send_json({"categories": public_categories(), "repos": entries,
                            "favorites": list(favorites)})
        elif url.path == "/api/trending":
            try:
                source, language = qs.get("source", ["daily"])[0], qs.get("language", [""])[0]
                cat_id = qs.get("category", [""])[0]
                items = (category_trending(cat_id, source, language) if cat_id
                         else trending_items(source, language))
                for i in items:
                    i["local"] = local_status(i["repo"])
                self.send_json({"categories": public_categories(), "items": items})
            except Exception as e:
                limited = any(code in str(e) for code in ("403", "429"))
                self.send_json({"error": "GitHub limite les recherches sans compte (10 par minute) : "
                                         "nouvel essai automatique dans une minute." if limited else str(e),
                                "retry": limited}, 502)
        elif url.path == "/api/mine":
            if qs.get("refresh"):
                _mine_cache.clear()
            try:
                data = my_repos()
                items = [dict(i, local=local_status(i["repo"])) for i in data["items"]]
                self.send_json({"categories": public_categories(), "login": data["login"], "items": items})
            except Exception as e:
                self.send_json({"error": str(e)}, 502)
        elif url.path == "/api/search":
            query = qs.get("q", [""])[0]
            if not query.strip():
                return self.send_json({"error": "Tape quelque chose à chercher."}, 400)
            try:
                res = search_github(query, qs.get("sort", ["best"])[0], qs.get("language", [""])[0],
                                    max(1, min(34, int(qs.get("page", ["1"])[0]))))
                for i in res["items"]:
                    i["local"] = local_status(i["repo"])
                self.send_json({"categories": public_categories(), **res})
            except Exception as e:
                limited = any(code in str(e) for code in ("403", "429"))
                self.send_json({"error": "GitHub limite les recherches sans compte (10 par minute) : "
                                         "réessaie dans une minute." if limited
                                         else "Recherche refusée par GitHub (vérifie la syntaxe) : " + str(e),
                                "retry": limited}, 502)
        elif url.path == "/api/buzz":
            try:
                items = buzz()
                for i in items:
                    i["local"] = local_status(i["repo"])
                self.send_json({"categories": public_categories(), "items": items})
            except Exception as e:
                limited = any(code in str(e) for code in ("403", "429"))
                self.send_json({"error": "GitHub limite les recherches sans compte (10 par minute) : "
                                         "nouvel essai automatique dans une minute." if limited else str(e),
                                "retry": limited}, 502)
        elif url.path == "/api/readme":
            repo = qs.get("repo", [""])[0]
            if not REPO_RE.match(repo):
                return self.send_json({"error": "nom de dépôt invalide"}, 400)
            try:
                self.send_json({"html": readme_html(repo)})
            except Exception as e:
                self.send_json({"error": str(e)}, 502)
        elif url.path == "/api/jobs":
            with jobs_lock:
                self.send_json(sorted(jobs.values(), key=lambda j: -j["started"])[:30])
        else:
            self.send_json({"error": "introuvable"}, 404)

    def do_POST(self):
        if not self.from_hub():
            return
        # un site web tiers ne peut pas ajouter cet en-tête sans pré-vol CORS,
        # qu'on ne valide jamais : protège contre les requêtes forgées
        if self.headers.get("X-Hub") != "1":
            return self.send_json({"error": "interdit"}, 403)
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/check":
            with ThreadPoolExecutor(6) as pool:
                list(pool.map(lambda r: git(repo_path(r), "fetch", "--prune", timeout=120),
                              installed_repos()))
            return self.send_json({r: local_status(r) for r in installed_repos()})
        if self.path == "/api/update-all":
            ids = [start_job(r, "update") for r in installed_repos()]
            return self.send_json({"jobs": [i for i in ids if i]})
        if self.path == "/api/favorite":
            repo = data.get("repo", "")
            if not REPO_RE.match(repo):
                return self.send_json({"error": "nom de dépôt invalide"}, 400)
            if data.get("on"):
                favorites[repo] = {"added": time.time(), "category": data.get("category")}
            else:
                favorites.pop(repo, None)
            save_favorites()
            return self.send_json({"favorites": list(favorites)})
        if self.path == "/api/pick":
            return self.send_json({"path": pick_file(data.get("save", False))})
        repo = data.get("repo", "")
        if not REPO_RE.match(repo):
            return self.send_json({"error": "nom de dépôt invalide"}, 400)
        if self.path == "/api/open":
            subprocess.Popen(["xdg-open", str(repo_path(repo))])
            return self.send_json({"ok": True})
        if self.path == "/api/shortcut":
            if data.get("kind") not in ("desktop", "menu"):
                return self.send_json({"error": "type de raccourci inconnu"}, 400)
            try:
                set_shortcut(repo, data["kind"], bool(data.get("on")))
                return self.send_json({"ok": True, "shortcuts": shortcut_state(repo)})
            except Exception as e:
                return self.send_json({"error": str(e)}, 400)
        if self.path == "/api/terminal":
            subprocess.Popen(terminal_cmd([os.environ.get("SHELL", "sh")], repo_path(repo)),
                             start_new_session=True)
            return self.send_json({"ok": True})
        if self.path == "/api/stop":
            stop_program(repo)
            return self.send_json({"ok": True})
        action = self.path.removeprefix("/api/")
        if action not in ("install", "update", "build", "run", "remove", "clean"):
            return self.send_json({"error": "introuvable"}, 404)
        opts = {"mode": data.get("mode"), "args": data.get("args", "")} if action == "run" else None
        job_id = start_job(repo, action, opts)
        if not job_id:
            return self.send_json({"error": "une tâche tourne déjà sur ce dépôt"}, 409)
        self.send_json({"job": job_id})


def make_server(port):
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    print(f"GitHub Hub -> http://127.0.0.1:{PORT}   (dépôts dans {REPOS_DIR})")
    make_server(PORT).serve_forever()
