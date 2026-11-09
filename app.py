#!/usr/bin/env python3
"""GitHub Hub en application de bureau : une vraie fenêtre, sans navigateur.

L'interface (index.html) tourne dans un moteur web intégré (Qt WebEngine,
comme Discord ou VS Code avec Electron). Le serveur interne écoute sur un port
choisi au hasard et s'arrête avec la fenêtre.
"""
import sys
import threading

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

import server

INSTANCE_NAME = "github-hub"


class ExternalPage(QWebEnginePage):
    """Page jetable : récupère l'URL d'un lien target=_blank et l'envoie au navigateur."""

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        QDesktopServices.openUrl(url)
        self.deleteLater()
        return False


class HubPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        # les liens web partent dans le navigateur ; about:srcdoc (README) reste ici
        if url.scheme() in ("http", "https") and url.host() != "127.0.0.1":
            QDesktopServices.openUrl(url)
            return False
        return True

    def createWindow(self, _type):
        return ExternalPage(self.profile(), self)


class Window(QMainWindow):
    def __init__(self, url):
        super().__init__()
        self.setWindowTitle("GitHub Hub")
        self.setWindowIcon(QIcon(str(server.HERE / "logo.svg")))
        self.resize(1320, 880)
        view = QWebEngineView(self)
        # profil nommé = persistant : sous Qt 6 le profil par défaut est « navigation
        # privée » et oubliait les réglages (mode de lancement préféré, panneau ouvert…)
        profile = QWebEngineProfile("github-hub", view)
        view.setPage(HubPage(profile, view))
        view.setUrl(QUrl(url))
        self.setCentralWidget(view)

    def closeEvent(self, event):
        if server.jobs_running():
            answer = QMessageBox.question(
                self, "Tâches en cours",
                "Un téléchargement ou une compilation est en cours.\nFermer quand même ?")
            if answer != QMessageBox.Yes:
                return event.ignore()
        event.accept()

    def bring_to_front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("GitHub Hub")
    app.setDesktopFileName("github-hub")  # relie la fenêtre à son icône du menu

    # une seule fenêtre : un deuxième lancement réveille la première
    probe = QLocalSocket()
    probe.connectToServer(INSTANCE_NAME)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(INSTANCE_NAME)
    single = QLocalServer()
    single.listen(INSTANCE_NAME)

    httpd = server.make_server(0)  # port 0 = un port libre au hasard
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    win = Window(f"http://127.0.0.1:{httpd.server_address[1]}/")
    single.newConnection.connect(lambda: (single.nextPendingConnection(), win.bring_to_front()))
    win.show()
    code = app.exec()
    httpd.shutdown()
    return code


if __name__ == "__main__":
    sys.exit(main())
