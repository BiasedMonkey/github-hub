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
import warnings
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


def outils_seulement(*noms):
    """Un PATH qui ne contient que ces outils (liens vers les vrais), ni konsole ni xterm."""
    dossier = Path(tempfile.mkdtemp(dir=server.HUB_DIR))
    for nom in noms:
        (dossier / nom).symlink_to(shutil.which(nom))
    return str(dossier)


class SansTerminal(unittest.TestCase):
    """Sans konsole ni xterm (bureau GNOME…), le statut d'un projet s'affiche quand même."""

    def setUp(self):
        self.path_avant = os.environ["PATH"]
        os.environ["PATH"] = outils_seulement("git", "make", "env", "du", "true")

    def tearDown(self):
        os.environ["PATH"] = self.path_avant

    def test_statut_d_un_projet_compile_sans_terminal(self):
        faux_depot("test/sans-terminal", {"Makefile": "all:\n\ttrue\n"})
        job = attendre(server.start_job("test/sans-terminal", "build"))
        self.assertEqual(job["state"], "ok", "\n".join(job["log"]))
        statut = server.local_status("test/sans-terminal")
        self.assertTrue(statut["built"])
        self.assertFalse(statut["launchable"])  # pas de raccourci possible sans terminal


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
        cls.httpd.server_close()

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
        self.avant = {k: os.environ.get(k) for k in ("GITHUB_TOKEN", "GH_TOKEN", "PATH")}
        os.environ["GITHUB_TOKEN"] = "jeton-secret-github"
        os.environ["GH_TOKEN"] = "jeton-secret-gh"

    def tearDown(self):
        for k, v in self.avant.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def jetons_visibles(self, fichier_env):
        """Noms des variables qui contiennent un jeton (un échec n'affiche pas tout l'environnement)."""
        variables = dict(l.split("=", 1) for l in fichier_env.read_text().splitlines() if "=" in l)
        self.assertIn("PATH", variables)
        return [k for k, v in variables.items() if "jeton-secret" in v]

    def test_la_compilation_ne_voit_pas_les_jetons(self):
        path = faux_depot("test/jeton", {"Makefile": "all:\n\tenv > env.txt\n"})
        job = attendre(server.start_job("test/jeton", "build"))
        self.assertEqual(job["state"], "ok", "\n".join(job["log"]))
        self.assertEqual(self.jetons_visibles(path / "env.txt"), [], "jetons visibles par la compilation")

    def test_le_terminal_du_dossier_ne_voit_pas_les_jetons(self):
        faux_depot("test/terminal", {"LISEZMOI": "rien\n"})
        # un faux konsole, trouvé en premier dans le PATH, note l'environnement reçu
        faux = Path(tempfile.mkdtemp(dir=server.HUB_DIR))
        sortie = faux / "env.txt"
        (faux / "konsole").write_text(f"#!/bin/sh\nenv > '{sortie}.tmp' && mv '{sortie}.tmp' '{sortie}'\n")
        (faux / "konsole").chmod(0o755)
        os.environ["PATH"] = f"{faux}:{os.environ['PATH']}"
        httpd = server.make_server(0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        # /api/terminal lance konsole sans l'attendre (voulu) : Python émet un ResourceWarning
        # quand l'objet Popen disparaît alors que le faux konsole tourne encore
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/api/terminal", json.dumps({"repo": "test/terminal"}),
                      {"Host": f"127.0.0.1:{port}", "X-Hub": "1", "Content-Type": "application/json"})
            self.assertEqual(c.getresponse().status, 200)
            c.close()
            fin = time.time() + 10
            while not sortie.exists() and time.time() < fin:
                time.sleep(0.05)
        self.assertEqual(self.jetons_visibles(sortie), [], "jetons visibles dans le terminal")


class RepereDeCompilation(unittest.TestCase):
    def test_compile_puis_nouveau_commit_n_est_plus_compile(self):
        path = faux_depot("test/repere", {"Makefile": "all:\n\ttrue\n"})
        job = attendre(server.start_job("test/repere", "build"))
        self.assertEqual(job["state"], "ok", "\n".join(job["log"]))
        self.assertTrue(server.local_status("test/repere")["built"])
        nouveau_commit(path)  # ex. une mise à jour dont la recompilation a échoué
        self.assertFalse(server.local_status("test/repere")["built"])

    def test_ancien_binaire_d_un_autre_commit_ne_compte_pas(self):
        path = faux_depot("test/binaire", {"Cargo.toml": "[package]\nname = \"binaire\"\n"})
        binaire = path / "target" / "release" / "binaire"
        binaire.parent.mkdir(parents=True)
        binaire.write_text("#!/bin/sh\n")
        binaire.chmod(0o755)
        _, ancien = server.git(path, "rev-parse", "HEAD")
        (path / ".git" / server.BUILT_MARKER).write_text(ancien)
        self.assertTrue(server.local_status("test/binaire")["built"])
        nouveau_commit(path)  # le binaire est resté, mais il vient de l'ancien commit
        self.assertFalse(server.local_status("test/binaire")["built"])


if __name__ == "__main__":
    unittest.main()
