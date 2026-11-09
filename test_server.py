"""Tests du serveur interne (bibliothèque standard seulement, sans PySide6).

    python3 -m unittest -v

Les dépôts de test sont créés dans un dossier temporaire (HUB_DIR) et rien ne
part sur Internet.
"""
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ["HUB_DIR"] = tempfile.mkdtemp(prefix="github-hub-test-")
import server  # noqa: E402  (HUB_DIR doit être fixé avant l'import)


def tearDownModule():
    shutil.rmtree(server.HUB_DIR, ignore_errors=True)


def git(path, *args):
    subprocess.run(["git", "-C", str(path), "-c", "user.name=test", "-c", "user.email=test@test",
                    *args], check=True, capture_output=True)


def faux_depot(repo, fichiers):
    """Crée un dépôt git local à l'endroit où le hub range `repo`, avec un commit."""
    path = server.repo_path(repo)
    path.mkdir(parents=True)
    for nom, contenu in fichiers.items():
        (path / nom).write_text(contenu)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "départ")
    return path


def nouveau_commit(path):
    (path / "NOUVEAU.md").write_text("une nouvelle version\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "nouvelle version")


def attendre(job_id, delai=30):
    fin = time.time() + delai
    while server.jobs[job_id]["state"] == "running":
        if time.time() > fin:
            raise TimeoutError("\n".join(server.jobs[job_id]["log"]))
        time.sleep(0.05)
    return server.jobs[job_id]


class OrigineDesRequetes(unittest.TestCase):
    """Le serveur n'obéit qu'à sa propre fenêtre, même via DNS rebinding."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = server.make_server(0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def requete(self, methode, chemin, host, corps=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        entetes = {"Host": host, "X-Hub": "1", "Content-Type": "application/json"}
        c.request(methode, chemin, json.dumps(corps) if corps is not None else None, entetes)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_post_venant_d_un_autre_site_refuse(self):
        statut = self.requete("POST", "/api/favorite", f"site-pirate.example:{self.port}",
                              {"repo": "pirate/test", "on": True})
        self.assertEqual(statut, 403)
        self.assertNotIn("pirate/test", server.favorites)

    def test_get_venant_d_un_autre_site_refuse(self):
        self.assertEqual(self.requete("GET", "/api/jobs", f"site-pirate.example:{self.port}"), 403)

    def test_requetes_locales_acceptees(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.requete("GET", "/api/jobs", host), 200)


class JetonsGitHub(unittest.TestCase):
    def setUp(self):
        self.avant = {k: os.environ.get(k) for k in ("GITHUB_TOKEN", "GH_TOKEN")}
        os.environ["GITHUB_TOKEN"] = "jeton-secret-github"
        os.environ["GH_TOKEN"] = "jeton-secret-gh"

    def tearDown(self):
        for k, v in self.avant.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_la_compilation_ne_voit_pas_les_jetons(self):
        path = faux_depot("test/jeton", {"Makefile": "all:\n\tenv > env.txt\n"})
        job = attendre(server.start_job("test/jeton", "build"))
        self.assertEqual(job["state"], "ok", "\n".join(job["log"]))
        variables = dict(l.split("=", 1) for l in (path / "env.txt").read_text().splitlines() if "=" in l)
        self.assertIn("PATH", variables)
        # on ne compare que les noms : un échec n'affiche pas tout l'environnement
        visibles = [k for k, v in variables.items() if "jeton-secret" in v]
        self.assertEqual(visibles, [], "jetons visibles par la compilation")



if __name__ == "__main__":
    unittest.main()
