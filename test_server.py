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


def faux_programme(nom, script):
    """Un faux programme `nom` (script sh) dans un dossier à mettre en tête du PATH."""
    dossier = Path(tempfile.mkdtemp(dir=server.HUB_DIR))
    (dossier / nom).write_text("#!/bin/sh\n" + script)
    (dossier / nom).chmod(0o755)
    return dossier


def serveur(test):
    """Un serveur du hub sur un port libre, arrêté à la fin du test ; renvoie le port."""
    httpd = server.make_server(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    test.addCleanup(httpd.server_close)
    test.addCleanup(httpd.shutdown)
    return httpd.server_address[1]


def requete(port, chemin, corps=None):
    """GET (sans corps) ou POST (avec corps) comme la fenêtre du hub ; renvoie (statut, JSON)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    entetes = {"Host": f"127.0.0.1:{port}", "X-Hub": "1", "Content-Type": "application/json"}
    if corps is None:
        c.request("GET", chemin, headers=entetes)
    else:
        c.request("POST", chemin, json.dumps(corps), entetes)
    r = c.getresponse()
    reponse = (r.status, json.loads(r.read() or b"null"))
    c.close()
    return reponse


def attendre_fichier(fichier, delai=10):
    fin = time.time() + delai
    while not fichier.exists() and time.time() < fin:
        time.sleep(0.05)


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

    def test_terminal_du_dossier_repond_une_erreur(self):
        # avant : le serveur coupait la connexion sans répondre, et le bouton ne faisait rien
        faux_depot("test/sans-terminal-api", {"LISEZMOI": "rien\n"})
        statut, reponse = requete(serveur(self), "/api/terminal", {"repo": "test/sans-terminal-api"})
        self.assertEqual(statut, 400)
        self.assertIn("Aucun terminal", reponse["error"])

    def test_ouvrir_le_dossier_sans_xdg_open_repond_une_erreur(self):
        faux_depot("test/sans-xdg-open", {"LISEZMOI": "rien\n"})
        statut, reponse = requete(serveur(self), "/api/open", {"repo": "test/sans-xdg-open"})
        self.assertEqual(statut, 400)
        self.assertIn("xdg-open", reponse["error"])

    def test_dossier_d_un_projet_non_installe(self):
        statut, reponse = requete(serveur(self), "/api/open", {"repo": "test/jamais-installe"})
        self.assertEqual(statut, 400)
        self.assertIn("pas installé", reponse["error"])


class TerminalParDefaut(unittest.TestCase):
    """Sans konsole ni xterm mais avec xdg-terminal-exec (Bazzite, Fedora, Ubuntu récents…),
    le terminal par défaut du bureau sert au mode Terminal et aux raccourcis."""

    def setUp(self):
        self.path_avant = os.environ["PATH"]
        self.sortie = Path(tempfile.mkdtemp(dir=server.HUB_DIR)) / "terminal.txt"
        faux = faux_programme("xdg-terminal-exec", f"""printf '%s\\n' "$PWD" "$*" > '{self.sortie}.tmp'
mv '{self.sortie}.tmp' '{self.sortie}'
""")
        os.environ["PATH"] = f"{faux}:" + outils_seulement("git", "make", "env", "du", "true",
                                                            "printf", "mv")

    def tearDown(self):
        os.environ["PATH"] = self.path_avant

    def test_terminal_du_dossier(self):
        path = faux_depot("test/xdg-terminal", {"LISEZMOI": "rien\n"})
        with warnings.catch_warnings():  # terminal lancé sans l'attendre (voulu)
            warnings.simplefilter("ignore", ResourceWarning)
            statut, reponse = requete(serveur(self), "/api/terminal", {"repo": "test/xdg-terminal"})
            self.assertEqual(statut, 200, reponse)
            attendre_fichier(self.sortie)
        dossier, arguments = self.sortie.read_text().splitlines()
        self.assertEqual(Path(dossier).resolve(), path.resolve())
        self.assertIn(f"--dir={path}", arguments)

    def test_raccourci_possible(self):
        faux_depot("test/xdg-raccourci", {"Makefile": "all:\n\ttrue\n"})
        job = attendre(server.start_job("test/xdg-raccourci", "build"))
        self.assertEqual(job["state"], "ok", "\n".join(job["log"]))
        self.assertTrue(server.local_status("test/xdg-raccourci")["launchable"])


class VerificationDesMisesAJour(unittest.TestCase):
    def setUp(self):
        self.path_avant, self.delai_avant = os.environ["PATH"], server.FETCH_TIMEOUT

    def tearDown(self):
        os.environ["PATH"], server.FETCH_TIMEOUT = self.path_avant, self.delai_avant

    def test_un_depot_injoignable_ne_bloque_pas_la_verification(self):
        # avant : un seul git fetch trop long faisait échouer toute la requête, sans réponse,
        # et le bouton « Vérifier les mises à jour » restait grisé
        faux_depot("test/bloque", {"LISEZMOI": "rien\n"})
        vrai_git = shutil.which("git")
        faux = faux_programme("git", f"""case "$*" in *bloque*fetch*) exec sleep 600 ;; esac
exec '{vrai_git}' "$@"
""")
        os.environ["PATH"] = f"{faux}:{os.environ['PATH']}"
        server.FETCH_TIMEOUT = 1
        statut, reponse = requete(serveur(self), "/api/check", {})
        self.assertEqual(statut, 200)
        self.assertTrue(reponse["test/bloque"]["installed"])


class JournauxDesTaches(unittest.TestCase):
    def test_seules_les_nouvelles_lignes_sont_envoyees(self):
        # le panneau Activité redemande /api/jobs toutes les 0,8 s : avant, chaque appel
        # renvoyait les journaux complets (227 Ko pour une compilation de 3 000 lignes)
        faux_depot("test/bavard", {"Makefile": "all:\n\t@seq 1 3000\n"})
        job_id = server.start_job("test/bavard", "build")
        nb = len(attendre(job_id)["log"])
        port = serveur(self)

        def journal(have=""):
            statut, taches = requete(port, "/api/jobs" + have)
            self.assertEqual(statut, 200)
            return next(j for j in taches if j["id"] == job_id)

        tout = journal()  # première fois : tout le journal
        self.assertEqual((tout["log_from"], len(tout["log"])), (0, nb))
        suite = journal(f"?have={job_id}:{nb - 10}")
        self.assertEqual((suite["log_from"], suite["log"]), (nb - 10, tout["log"][-10:]))
        rien = journal(f"?have={job_id}:{nb}")
        self.assertEqual((rien["log_from"], rien["log"]), (nb, []))


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

    def env_vu_par(self, programme, route):
        """Environnement reçu par `programme` (konsole, xdg-open…) que le hub lance via `route`."""
        faux_depot(f"test/{programme}", {"LISEZMOI": "rien\n"})
        # un faux programme, trouvé en premier dans le PATH, note l'environnement reçu
        sortie = Path(tempfile.mkdtemp(dir=server.HUB_DIR)) / "env.txt"
        faux = faux_programme(programme, f"env > '{sortie}.tmp' && mv '{sortie}.tmp' '{sortie}'\n")
        os.environ["PATH"] = f"{faux}:{os.environ['PATH']}"
        port = serveur(self)
        # le hub lance ces programmes sans les attendre (voulu) : Python émet un ResourceWarning
        # quand l'objet Popen disparaît alors que le faux programme tourne encore
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            self.assertEqual(requete(port, route, {"repo": f"test/{programme}"})[0], 200)
            attendre_fichier(sortie)
        return sortie

    def test_le_terminal_du_dossier_ne_voit_pas_les_jetons(self):
        sortie = self.env_vu_par("konsole", "/api/terminal")
        self.assertEqual(self.jetons_visibles(sortie), [], "jetons visibles dans le terminal")

    def test_le_gestionnaire_de_fichiers_ne_voit_pas_les_jetons(self):
        sortie = self.env_vu_par("xdg-open", "/api/open")
        self.assertEqual(self.jetons_visibles(sortie), [], "jetons visibles par « Ouvrir le dossier »")

    def git_qui_note_son_env(self, sortie):
        """Un faux git : note son environnement pour clone et fetch, puis passe la main au vrai
        (sauf clone, qui échoue aussitôt : les tests ne vont pas sur Internet)."""
        vrai_git = shutil.which("git")
        faux = faux_programme("git", f"""for a in "$@"; do
  case "$a" in clone|fetch) env > '{sortie}' ;; esac
  case "$a" in clone) exit 1 ;; esac
done
exec '{vrai_git}' "$@"
""")
        os.environ["PATH"] = f"{faux}:{os.environ['PATH']}"

    def test_le_telechargement_garde_les_jetons(self):
        # git n'exécute pas le code du projet ; un dépôt privé peut avoir besoin du jeton
        sortie = Path(tempfile.mkdtemp(dir=server.HUB_DIR)) / "env.txt"
        self.git_qui_note_son_env(sortie)
        attendre(server.start_job("test/prive", "install"))
        self.assertEqual(sorted(self.jetons_visibles(sortie)), ["GH_TOKEN", "GITHUB_TOKEN"])

    def test_la_mise_a_jour_garde_les_jetons(self):
        origine = Path(tempfile.mkdtemp(dir=server.HUB_DIR)) / "origine.git"
        git(origine.parent, "init", "-q", "--bare", str(origine))
        path = faux_depot("test/prive-maj", {"LISEZMOI": "rien\n"})
        git(path, "remote", "add", "origin", str(origine))
        git(path, "push", "-q", "-u", "origin", "HEAD")
        sortie = Path(tempfile.mkdtemp(dir=server.HUB_DIR)) / "env.txt"
        self.git_qui_note_son_env(sortie)
        job = attendre(server.start_job("test/prive-maj", "update"))
        self.assertEqual(job["state"], "ok", "\n".join(job["log"]))
        self.assertEqual(sorted(self.jetons_visibles(sortie)), ["GH_TOKEN", "GITHUB_TOKEN"])


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

    def test_statut_ne_relit_le_commit_qu_une_fois(self):
        faux_depot("test/une-fois", {"Makefile": "all:\n\ttrue\n"})
        attendre(server.start_job("test/une-fois", "build"))
        appels = []
        vrai_git = server.git

        def git_compte(path, *args, **kw):
            appels.append(args)
            return vrai_git(path, *args, **kw)

        server.git = git_compte
        try:
            statut = server.local_status("test/une-fois")
        finally:
            server.git = vrai_git
        self.assertTrue(statut["built"])
        # « compilé » sert deux fois (statut et raccourci) : un seul git rev-parse HEAD
        self.assertEqual(appels.count(("rev-parse", "HEAD")), 1)


if __name__ == "__main__":
    unittest.main()
