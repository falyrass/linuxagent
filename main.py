#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek Agent — agent de chat + terminal pour Linux Mint (Python 3 / PyQt6)

  • Gauche  : terminal (vrai shell bash dans un pty, intégré à la fenêtre)
  • Droite  : barre latérale de chat (streaming, bouton Arrêter, historique SQLite)
  • Avant chaque exécution de l'agent : point de restauration (snapshot git local, instantané, sans sudo)
  • Toutes les 20 commandes : boîte de dialogue « Restaurer / Continuer »

Prérequis : python3, PyQt6, git  (sudo apt install git).
Lancement : python3 main.py
"""

import codecs
import fcntl
import hashlib
import json
import os
import pty
import re
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import threading
import termios
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEventLoop, QMetaObject, QSocketNotifier, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────┐
# │  CLÉ API : elle n'est plus dans ce fichier.                             │
# │  Copiez  config_secret.example.py  en  config_secret.py  (même dossier  │
# │  que ce script) et collez-y votre clé DeepSeek.                         │
# │  config_secret.py est dans .gitignore : il ne sera jamais publié.       │
# │  (Alternative : variable d'environnement DEEPSEEK_API_KEY)              │
# └─────────────────────────────────────────────────────────────────────────┘
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from config_secret import DEEPSEEK_API_KEY      # <-- défini dans config_secret.py
except ImportError:
    DEEPSEEK_API_KEY = ""

DEEPSEEK_MODEL = "deepseek-flash"                       # nom du modèle
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"

DEEPSEEK_THINKING = "disabled"   # "enabled" = mode réflexion (plus lent) ; None = ne pas envoyer le paramètre

COMMAND_LIMIT = 20          # après N commandes : boîte « Restaurer / Continuer »
MAX_AGENT_STEPS = 12        # nombre max d'allers-retours agent <-> terminal par message
COMMAND_TIMEOUT = 120       # secondes avant d'interrompre une commande (Ctrl+C)
OUTPUT_LIMIT = 6000         # nb max de caractères de sortie renvoyés à l'IA
SNAPSHOT_PER_COMMAND = False  # False : 1 snapshot par série d'actions ; True : 1 snapshot par commande/écriture

DATA_DIR = Path.home() / ".deepseek_agent"
TMP_DIR = DATA_DIR / "tmp"
CONFIG_FILE = DATA_DIR / "config.json"
DB_FILE = DATA_DIR / "agent.db"
SNAPSHOT_DIR = DATA_DIR / "snapshots"   # dépôts git des points de restauration (hors de votre projet)
# Dossiers lourds exclus des points de restauration (pour rester rapide)
SNAPSHOT_EXCLUDES = ["node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", ".cache/", "target/", "dist/", "build/", "*.iso", "*.zip", "*.tar*", "*.mp4", "*.mkv"]
for _d in (DATA_DIR, TMP_DIR, SNAPSHOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def get_api_key():
    """Retourne la clé API (config_secret.py, sinon variable d'environnement DEEPSEEK_API_KEY)."""
    key = (DEEPSEEK_API_KEY or "").strip()
    if not key or key.startswith("COLLEZ_"):
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return key or None

def fetch_balance():
    """Retourne (texte_affichage, dict_brut) du solde DeepSeek, ou (message_erreur, None)."""
    key = get_api_key()
    if not key:
        return "Solde : clé API manquante", None
    req = urllib.request.Request(
        DEEPSEEK_BALANCE_URL,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return f"Solde : erreur HTTP {e.code}", None
    except Exception as e:
        return f"Solde : indisponible ({type(e).__name__})", None
    infos = data.get("balance_infos") or []
    if not infos:
        return "Solde : inconnu", data
    parts = []
    for b in infos:
        cur = b.get("currency", "?")
        tot = b.get("total_balance", "?")
        parts.append(f"{tot} {cur}")
    return "Solde : " + " · ".join(parts), data



def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def system_prompt(workdir):
    return f"""Tu es un agent de développement et d'administration système intégré à une application de bureau sous Linux Mint.
Dossier de travail : {workdir}
Réponds en français, de façon concise.

Tu disposes de 3 actions, à écrire EXACTEMENT avec ces balises :

1) Exécuter une commande bash dans le terminal de l'utilisateur :
<exec>
commande(s) bash
</exec>

2) Écrire (créer ou écraser) un fichier :
<write path="chemin/du/fichier">
contenu complet du fichier
</write>

3) Lire un fichier ou lister un dossier :
<read path="chemin"/>

Règles :
- N'utilise ces balises que pour agir réellement. Pour simplement montrer du code, utilise des blocs markdown.
- Après tes actions, l'application te renvoie les résultats dans un message « [Résultats d'exécution] » : analyse-les, puis continue ou conclus.
- Quand la tâche est terminée, réponds SANS aucune balise d'action.
- Les commandes doivent être non interactives (options -y, pas d'éditeur, pas de pager) et se terminer seules (max {COMMAND_TIMEOUT} s).
- Pas de sudo (aucun mot de passe ne sera saisi pour toi).
- Les chemins relatifs sont relatifs au dossier de travail.
- Un point de restauration du dossier de travail est créé automatiquement avant tes actions ; évite quand même les commandes destructrices sauf demande explicite.
"""


# ═══════════════════════════════════════════════════════════════════════════
#  BASE DE DONNÉES (SQLite) : chats, messages, points de restauration
# ═══════════════════════════════════════════════════════════════════════════

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self):
        self.con = sqlite3.connect(DB_FILE)
        self.con.row_factory = sqlite3.Row
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS chats(
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT);
            CREATE TABLE IF NOT EXISTS restore_points(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, comment TEXT,
                created_at TEXT, chat_id INTEGER);
        """)
        self.con.commit()

    def new_chat(self, title="Nouveau chat"):
        cur = self.con.execute(
            "INSERT INTO chats(title, created_at, updated_at) VALUES(?,?,?)", (title, now(), now()))
        self.con.commit()
        return cur.lastrowid

    def list_chats(self):
        return self.con.execute("SELECT * FROM chats ORDER BY updated_at DESC, id DESC").fetchall()

    def last_chat_id(self):
        row = self.con.execute("SELECT id FROM chats ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
        return row["id"] if row else None

    def messages(self, chat_id):
        rows = self.con.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def add_message(self, chat_id, role, content):
        self.con.execute(
            "INSERT INTO messages(chat_id, role, content, created_at) VALUES(?,?,?,?)",
            (chat_id, role, content, now()))
        self.con.execute("UPDATE chats SET updated_at=? WHERE id=?", (now(), chat_id))
        self.con.commit()

    def set_title(self, chat_id, title):
        self.con.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
        self.con.commit()

    def delete_chat(self, chat_id):
        self.con.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        self.con.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        self.con.commit()

    def add_restore_point(self, name, comment, chat_id):
        self.con.execute(
            "INSERT INTO restore_points(name, comment, created_at, chat_id) VALUES(?,?,?,?)",
            (name, comment, now(), chat_id))
        self.con.commit()

    def restore_points(self):
        return self.con.execute("SELECT * FROM restore_points ORDER BY id DESC").fetchall()


# ═══════════════════════════════════════════════════════════════════════════
#  THREADS : API DeepSeek (streaming) et commandes git
# ═══════════════════════════════════════════════════════════════════════════

class ChatWorker(QThread):
    chunk = pyqtSignal(str)          # morceau de texte reçu
    completed = pyqtSignal(str)      # texte complet (ou partiel si arrêté)
    failed = pyqtSignal(str, str)    # message d'erreur, texte partiel

    def __init__(self, messages, parent=None):
        super().__init__(parent)
        self.messages = messages
        self.stopped = False
        self._resp = None

    def stop(self):
        self.stopped = True
        try:
            if self._resp is not None:
                self._resp.close()
        except Exception:
            pass

    def run(self):
        full = ""
        payload = {"model": DEEPSEEK_MODEL, "messages": self.messages, "stream": True}
        if DEEPSEEK_THINKING in ("enabled", "disabled"):
            payload["thinking"] = {"type": DEEPSEEK_THINKING}
        req = urllib.request.Request(
            DEEPSEEK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # La clé API est lue ici via get_api_key() (voir DEEPSEEK_API_KEY en haut du fichier)
                "Authorization": f"Bearer {get_api_key()}",
            },
            method="POST",
        )
        try:
            self._resp = urllib.request.urlopen(req, timeout=120)
            for raw in self._resp:
                if self.stopped:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {})
                except Exception:
                    continue
                piece = delta.get("content") or ""   # (reasoning_content ignoré)
                if piece:
                    full += piece
                    self.chunk.emit(piece)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:600]
            self.failed.emit(f"Erreur HTTP {e.code} : {body}", full)
            return
        except Exception as e:
            if self.stopped:
                self.completed.emit(full)
            else:
                self.failed.emit(f"Erreur réseau : {e}", full)
            return
        self.completed.emit(full)


class CmdWorker(QThread):
    """Exécute une suite de commandes (listes d'arguments) hors du thread UI ; s'arrête à la première erreur."""
    done = pyqtSignal(int, str)

    def __init__(self, commands, timeout=300):
        super().__init__()
        self.commands, self.timeout = commands, timeout

    def run(self):
        out = ""
        try:
            for args in self.commands:
                p = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout)
                out += (p.stdout or "") + (p.stderr or "")
                if p.returncode != 0:
                    self.done.emit(p.returncode, out)
                    return
            self.done.emit(0, out)
        except Exception as e:
            self.done.emit(-1, str(e))


def run_blocking(worker):
    """Lance un CmdWorker et attend sans figer l'interface."""
    loop, res = QEventLoop(), {}

    def finished(rc, out):
        res["rc"], res["out"] = rc, out
        loop.quit()

    worker.done.connect(finished)
    worker.start()
    loop.exec()
    worker.wait()
    return res.get("rc", -1), res.get("out", "")


# ═══════════════════════════════════════════════════════════════════════════
#  TERMINAL INTÉGRÉ (vrai shell bash dans un pty)
# ═══════════════════════════════════════════════════════════════════════════

ANSI = ["#1e1e1e", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
        "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff"]
FG = "#d4d4d4"


def color256(n):
    if n < 16:
        return ANSI[n]
    if n < 232:
        n -= 16
        f = lambda v: 0 if v == 0 else 55 + v * 40
        return "#%02x%02x%02x" % (f(n // 36), f((n // 6) % 6), f(n % 6))
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


# bashrc dédié : charge ~/.bashrc puis définit __da, la fonction qui exécute un
# script de l'agent dans CE shell et signale la fin par une séquence OSC invisible.
RC_CONTENT = r'''
[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"
__da() { . "$HOME/.deepseek_agent/tmp/c$1.sh"; local rc=$?; printf '\033]777;DA;%s;%s\007' "$1" "$rc"; }
'''

KEYMAP = {
    Qt.Key.Key_Return.value: "\r", Qt.Key.Key_Enter.value: "\r",
    Qt.Key.Key_Backspace.value: "\x7f", Qt.Key.Key_Tab.value: "\t",
    Qt.Key.Key_Escape.value: "\x1b",
    Qt.Key.Key_Up.value: "\x1b[A", Qt.Key.Key_Down.value: "\x1b[B",
    Qt.Key.Key_Right.value: "\x1b[C", Qt.Key.Key_Left.value: "\x1b[D",
    Qt.Key.Key_Home.value: "\x1b[H", Qt.Key.Key_End.value: "\x1b[F",
    Qt.Key.Key_Delete.value: "\x1b[3~",
}


class TerminalWidget(QPlainTextEdit):
    shell_exited = pyqtSignal()

    TOKEN = re.compile(
        r"\x1b\[(?P<p>[0-9;:?<>=!]*)[ -/]*(?P<f>[@-~])"     # séquences CSI
        r"|\x1b\](?P<osc>.*?)(?:\x07|\x1b\\)"                # séquences OSC
        r"|\x1b[()][0-9A-Za-z]"
        r"|\x1b[=>78MDEHc]"
        r"|(?P<ctl>[\x00-\x1a\x1c-\x1f\x7f])"                # caractères de contrôle
        r"|(?P<txt>[^\x00-\x1f\x1b\x7f]+)",                  # texte
        re.S,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("terminal")
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setMaximumBlockCount(5000)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(10)
        self.setFont(font)

        self._cur = QTextCursor(self.document())
        self._col = 0
        self._fmt = self._default_fmt()
        self._pending = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._master = None
        self._proc = None
        self._notifier = None
        self._cmd_id = 0
        self._waiting = None

        self._caret = QFrame(self.viewport())
        self._caret.setStyleSheet("background: rgba(212,212,212,150);")
        self._caret.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.verticalScrollBar().valueChanged.connect(self._update_caret)

    # ── Shell / pty ─────────────────────────────────────────────────────
    def start_shell(self, cwd):
        self.stop_shell()
        rc = DATA_DIR / "bashrc"
        rc.write_text(RC_CONTENT, encoding="utf-8")
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        master, slave = pty.openpty()

        def preexec():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self._proc = subprocess.Popen(
            ["bash", "--rcfile", str(rc), "-i"], stdin=slave, stdout=slave, stderr=slave,
            cwd=cwd, env=env, preexec_fn=preexec, close_fds=True)
        os.close(slave)
        self._master = master
        self._decoder.reset()
        self._notifier = QSocketNotifier(master, QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._on_readable)
        self._update_winsize()

    def stop_shell(self):
        if self._notifier:
            self._notifier.setEnabled(False)
            self._notifier = None
        if self._proc:
            try:
                os.killpg(self._proc.pid, signal.SIGHUP)
            except Exception:
                pass
            self._proc = None
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None

    def send(self, s):
        if self._master is None:
            return
        try:
            os.write(self._master, s.encode("utf-8"))
        except OSError:
            pass

    def _on_readable(self, *_):
        try:
            data = os.read(self._master, 65536)
        except OSError:
            data = b""
        if not data:
            self.stop_shell()
            self.info("[Le shell s'est terminé — cliquez sur « Relancer »]", "#f14c4c")
            if self._waiting:
                self._waiting["rc"] = -2
                self._waiting["loop"].quit()
            self.shell_exited.emit()
            return
        self.feed(self._decoder.decode(data))

    def _update_winsize(self):
        if self._master is None:
            return
        fm = self.fontMetrics()
        cols = max(20, self.viewport().width() // max(1, fm.horizontalAdvance("M")))
        rows = max(5, self.viewport().height() // max(1, fm.lineSpacing()))
        try:
            fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    # ── Exécution de commandes de l'agent dans ce terminal ──────────────
    def run_command(self, cmd, timeout=COMMAND_TIMEOUT):
        """Exécute `cmd` dans le shell visible ; retourne (code_retour, sortie)."""
        if self._master is None:
            return -2, "Le terminal n'est pas démarré."
        self._cmd_id += 1
        cid = str(self._cmd_id)
        (TMP_DIR / f"c{cid}.sh").write_text(cmd.rstrip() + "\n", encoding="utf-8")

        lines = cmd.strip().splitlines()
        for i, line in enumerate(lines[:12]):
            self.info(("▶ Agent : " if i == 0 else "         ") + line, "#569cd6")
        if len(lines) > 12:
            self.info(f"         … ({len(lines) - 12} lignes de plus)", "#569cd6")

        start = self.document().characterCount() - 1
        loop = QEventLoop(self)
        self._waiting = {"id": cid, "loop": loop, "rc": None}
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_timeout)
        timer.start(timeout * 1000)
        self.send("\x15")                 # Ctrl+U : efface une éventuelle saisie en cours
        self.send(f"__da {cid}\r")
        loop.exec()
        timer.stop()
        rc = self._waiting["rc"] if self._waiting else -3
        timed_out = bool(self._waiting and self._waiting.get("timeout"))
        end_pos = self._waiting.get("end") if self._waiting else None
        self._waiting = None

        doc = self.document()
        cur = QTextCursor(doc)
        cur.setPosition(min(start, max(0, doc.characterCount() - 1)))
        if end_pos is not None and end_pos >= cur.position():
            cur.setPosition(min(end_pos, max(0, doc.characterCount() - 1)), QTextCursor.MoveMode.KeepAnchor)
        else:
            cur.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        out = cur.selectedText().replace("\u2029", "\n")
        out = "\n".join(l.rstrip() for l in out.splitlines() if not re.search(r"__da \d+\s*$", l)).strip()
        if len(out) > OUTPUT_LIMIT:
            out = "[… sortie tronquée …]\n" + out[-OUTPUT_LIMIT:]
        if timed_out:
            out += f"\n[Délai de {timeout} s dépassé : commande interrompue]"
        return rc, out

    def _on_timeout(self):
        if self._waiting:
            self.send("\x03")
            self._waiting["rc"] = -1
            self._waiting["timeout"] = True
            self._waiting["loop"].quit()

    def abort(self):
        """Interrompt la commande en cours (bouton Arrêter)."""
        if self._waiting:
            self.send("\x03")
            self._waiting["rc"] = -3
            self._waiting["loop"].quit()

    # ── Émulation de terminal (ANSI simplifié) ──────────────────────────
    @staticmethod
    def _default_fmt():
        f = QTextCharFormat()
        f.setForeground(QColor(FG))
        return f

    def info(self, text, color="#569cd6"):
        """Affiche une ligne d'information locale (pas envoyée au shell)."""
        if self._col != 0:
            self._control("\n")
        saved = self._fmt
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        self._fmt = fmt
        self._put(text)
        self._control("\n")
        self._fmt = saved
        self._scroll_end()
        self._update_caret()

    def feed(self, text):
        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        buf = self._pending + text
        self._pending = ""
        pos, n = 0, len(buf)
        while pos < n:
            m = self.TOKEN.match(buf, pos)
            if not m:
                if buf[pos] == "\x1b" and n - pos < 512:
                    self._pending = buf[pos:]      # séquence incomplète : on attend la suite
                    break
                pos += 1
                continue
            pos = m.end()
            if m.group("txt") is not None:
                self._put(m.group("txt"))
            elif m.group("ctl") is not None:
                self._control(m.group("ctl"))
            elif m.group("f") is not None:
                self._csi(m.group("p"), m.group("f"))
            elif m.group("osc") is not None:
                self._osc(m.group("osc"))
        if at_bottom:
            self._scroll_end()
        self._update_caret()

    def _scroll_end(self):
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _line(self):
        blk = self.document().lastBlock()
        return blk.position(), blk.length() - 1

    def _put(self, s):
        c = self._cur
        start, length = self._line()
        if self._col > length:                               # comble avec des espaces
            c.setPosition(start + length)
            c.insertText(" " * (self._col - length), self._fmt)
            length = self._col
        c.setPosition(start + self._col)
        over = min(len(s), length - self._col)
        if over > 0:                                         # écrase les caractères existants
            c.setPosition(start + self._col + over, QTextCursor.MoveMode.KeepAnchor)
        c.insertText(s, self._fmt)
        self._col += len(s)

    def _control(self, ch):
        if ch == "\n":
            self._cur.movePosition(QTextCursor.MoveOperation.End)
            self._cur.insertBlock()
            self._col = 0
        elif ch == "\r":
            self._col = 0
        elif ch == "\b":
            self._col = max(0, self._col - 1)
        elif ch == "\t":
            self._col += 8 - (self._col % 8)

    def _csi(self, params, final):
        if params and params[0] in "?<>=!":
            return
        nums = [int(x) if x.isdigit() else 0 for x in re.split(r"[;:]", params)] if params else []

        def arg(i=0, default=1):
            return nums[i] if len(nums) > i and nums[i] > 0 else default

        c = self._cur
        if final == "m":
            self._sgr(nums or [0])
        elif final == "K":
            mode = nums[0] if nums else 0
            start, length = self._line()
            if mode == 0 and self._col < length:
                c.setPosition(start + self._col)
                c.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
                c.removeSelectedText()
            elif mode == 1:
                k = min(self._col, length)
                c.setPosition(start)
                c.setPosition(start + k, QTextCursor.MoveMode.KeepAnchor)
                c.insertText(" " * k, self._fmt)
            elif mode == 2:
                c.setPosition(start)
                c.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
                c.removeSelectedText()
        elif final == "J":
            if (nums[0] if nums else 0) in (2, 3):
                self.clear()
                self._col = 0
        elif final == "C":
            self._col += arg()
        elif final == "D":
            self._col = max(0, self._col - arg())
        elif final == "G":
            self._col = max(0, arg() - 1)
        elif final in ("H", "f"):
            self._col = max(0, arg(1) - 1)
        elif final == "P":
            start, length = self._line()
            if self._col < length:
                c.setPosition(start + self._col)
                c.setPosition(start + min(self._col + arg(), length), QTextCursor.MoveMode.KeepAnchor)
                c.removeSelectedText()
        elif final == "@":
            start, length = self._line()
            c.setPosition(start + min(self._col, length))
            c.insertText(" " * arg(), self._fmt)
        elif final == "X":
            start, length = self._line()
            if self._col < length:
                k = min(arg(), length - self._col)
                c.setPosition(start + self._col)
                c.setPosition(start + self._col + k, QTextCursor.MoveMode.KeepAnchor)
                c.insertText(" " * k, self._fmt)

    def _sgr(self, nums):
        fmt = QTextCharFormat(self._fmt)
        i = 0
        while i < len(nums):
            n = nums[i]
            if n == 0:
                fmt = self._default_fmt()
            elif n == 1:
                fmt.setFontWeight(QFont.Weight.Bold)
            elif n == 22:
                fmt.setFontWeight(QFont.Weight.Normal)
            elif n == 3:
                fmt.setFontItalic(True)
            elif n == 23:
                fmt.setFontItalic(False)
            elif n == 4:
                fmt.setFontUnderline(True)
            elif n == 24:
                fmt.setFontUnderline(False)
            elif 30 <= n <= 37:
                fmt.setForeground(QColor(ANSI[n - 30]))
            elif n == 39:
                fmt.setForeground(QColor(FG))
            elif 40 <= n <= 47:
                fmt.setBackground(QColor(ANSI[n - 40]))
            elif n == 49:
                fmt.clearBackground()
            elif 90 <= n <= 97:
                fmt.setForeground(QColor(ANSI[n - 90 + 8]))
            elif 100 <= n <= 107:
                fmt.setBackground(QColor(ANSI[n - 100 + 8]))
            elif n in (38, 48):
                color = None
                if i + 2 < len(nums) and nums[i + 1] == 5:
                    color = color256(nums[i + 2])
                    i += 2
                elif i + 4 < len(nums) and nums[i + 1] == 2:
                    color = "#%02x%02x%02x" % tuple(min(255, v) for v in nums[i + 2:i + 5])
                    i += 4
                if color:
                    (fmt.setForeground if n == 38 else fmt.setBackground)(QColor(color))
            i += 1
        self._fmt = fmt

    def _osc(self, osc):
        # Séquence privée envoyée par __da à la fin d'une commande de l'agent
        # Le contenu OSC reçu est de la forme "777;DA;<id>;<code retour>"
        if osc.startswith("777;DA;"):
            parts = osc.split(";")
            try:
                cid, rc = parts[2], int(parts[3])
            except (IndexError, ValueError):
                return
            if self._waiting and self._waiting["id"] == cid:
                # position de fin de la sortie (avant l'invite qui suit dans le même paquet)
                self._waiting["end"] = self.document().characterCount() - 1
                self._waiting["rc"] = rc
                self._waiting["loop"].quit()

    # ── Curseur visuel ──────────────────────────────────────────────────
    def _update_caret(self, *_):
        blk = self.document().lastBlock()
        length = blk.length() - 1
        c = QTextCursor(blk)
        c.setPosition(blk.position() + min(self._col, length))
        r = self.cursorRect(c)
        w = self.fontMetrics().horizontalAdvance("M")
        extra = max(0, self._col - length) * w
        self._caret.setGeometry(r.x() + extra, r.y(), w, r.height())
        self._caret.show()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_winsize()
        self._update_caret()

    # ── Clavier ─────────────────────────────────────────────────────────
    def focusNextPrevChild(self, nxt):
        return False        # Tab est envoyé au shell

    def keyPressEvent(self, e):
        key = e.key()
        mods = e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if ctrl and shift and key == Qt.Key.Key_C.value:
            self.copy()
        elif ctrl and shift and key == Qt.Key.Key_V.value:
            self.send(QApplication.clipboard().text().replace("\n", "\r"))
        elif key in (Qt.Key.Key_PageUp.value, Qt.Key.Key_PageDown.value):
            sb = self.verticalScrollBar()
            step = sb.pageStep() * (-1 if key == Qt.Key.Key_PageUp.value else 1)
            sb.setValue(sb.value() + step)
        elif ctrl and Qt.Key.Key_A.value <= key <= Qt.Key.Key_Z.value:
            self.send(chr(key - Qt.Key.Key_A.value + 1))      # Ctrl+C, Ctrl+D, Ctrl+L…
        elif key in KEYMAP:
            self.send(KEYMAP[key])
        elif e.text():
            self.send(e.text())
        self._scroll_end()


# ═══════════════════════════════════════════════════════════════════════════
#  OUTILS DE L'AGENT : analyse des actions demandées par l'IA
# ═══════════════════════════════════════════════════════════════════════════

ACTION_RE = re.compile(
    r'<exec>(?P<cmd>.*?)</exec>'
    r'|<write\s+path="(?P<wpath>[^"]+)">(?P<content>.*?)</write>'
    r'|<read\s+path="(?P<rpath>[^"]+)"\s*/?>(?:\s*</read>)?',
    re.S)


def parse_actions(text):
    actions = []
    for m in ACTION_RE.finditer(text):
        if m.group("cmd") is not None:
            if m.group("cmd").strip():
                actions.append({"kind": "exec", "cmd": m.group("cmd").strip()})
        elif m.group("wpath"):
            content = m.group("content")
            if content.startswith("\n"):
                content = content[1:]
            actions.append({"kind": "write", "path": m.group("wpath"), "content": content})
        elif m.group("rpath"):
            actions.append({"kind": "read", "path": m.group("rpath")})
    return actions


def describe(a):
    if a["kind"] == "exec":
        first = a["cmd"].splitlines()[0]
        return "Commande : " + (first[:90] + ("…" if len(first) > 90 or "\n" in a["cmd"] else ""))
    if a["kind"] == "write":
        return f"Écriture du fichier : {a['path']}"
    return f"Lecture : {a['path']}"


def to_display_markdown(text):
    """Transforme les balises d'action en blocs markdown lisibles dans le chat."""
    text = re.sub(r"<exec>(.*?)</exec>",
                  lambda m: "\n**▶ Commande**\n```bash\n" + m.group(1).strip("\n") + "\n```\n", text, flags=re.S)
    text = re.sub(r'<write\s+path="([^"]+)">(.*?)</write>',
                  lambda m: f"\n**📝 Écriture : `{m.group(1)}`**\n```\n" + m.group(2).strip("\n") + "\n```\n",
                  text, flags=re.S)
    text = re.sub(r'<read\s+path="([^"]+)"\s*/?>(?:\s*</read>)?',
                  lambda m: f"\n**📖 Lecture : `{m.group(1)}`**\n", text)
    # balises non encore fermées (streaming en cours)
    text = text.replace("<exec>", "\n**▶ Commande**\n```bash\n")
    text = re.sub(r'<write\s+path="([^"]+)">', lambda m: f"\n**📝 Écriture : `{m.group(1)}`**\n```\n", text)
    return text


# ═══════════════════════════════════════════════════════════════════════════
#  WIDGETS DU CHAT
# ═══════════════════════════════════════════════════════════════════════════

class AutoTextBrowser(QTextBrowser):
    """QTextBrowser sans scroll dont la hauteur suit le contenu."""

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setOpenExternalLinks(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDefaultStyleSheet("a{color:#4fc1ff;} code{background-color:#1e1e1e;}")
        self.document().documentLayout().documentSizeChanged.connect(self._fit)

    def _fit(self, *_):
        w = self.viewport().width()
        if w <= 0:
            return
        doc = self.document()
        if abs(doc.textWidth() - w) > 0.5:
            doc.setTextWidth(w)
        h = int(doc.size().height()) + 2
        if h != self.height():
            self.setFixedHeight(h)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


class Bubble(QFrame):
    TITLES = {"user": "Vous", "assistant": "DeepSeek", "tool": "Résultats d'exécution"}
    NAMES = {"user": "bubbleUser", "assistant": "bubbleAssistant", "tool": "bubbleTool"}

    def __init__(self, role, text=""):
        super().__init__()
        self.role = role
        self.setObjectName(self.NAMES[role])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(4)
        head = QLabel(self.TITLES[role])
        head.setObjectName("bubbleTitle")
        self.body = AutoTextBrowser()
        lay.addWidget(head)
        lay.addWidget(self.body)
        self.set_text(text)

    def set_text(self, text):
        if self.role == "user":
            self.body.setPlainText(text)
        elif self.role == "tool":
            self.body.setMarkdown("```\n" + text + "\n```")
        else:
            self.body.setMarkdown(to_display_markdown(text))
        QTimer.singleShot(0, self.body._fit)


class InputBox(QPlainTextEdit):
    submit = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("input")
        self.setPlaceholderText("Écrivez votre message…  (Entrée = envoyer, Maj+Entrée = nouvelle ligne)")
        self.textChanged.connect(self._resize)
        self._resize()

    def _resize(self):
        lines = max(1, int(self.document().size().height()))
        self.setFixedHeight(min(150, max(46, lines * self.fontMetrics().lineSpacing() + 24)))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return.value, Qt.Key.Key_Enter.value) and \
                not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submit.emit()
            return
        super().keyPressEvent(e)


class RestoreDialog(QDialog):
    """Choix d'un point de restauration. `locked` : impossible de fermer sans choisir."""

    def __init__(self, parent, points, message, locked, cancel_text):
        super().__init__(parent)
        self.setWindowTitle("Points de restauration")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._locked = locked
        self.choice = None
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lbl = QLabel(message)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        self.combo = QComboBox()
        for p in points:
            self.combo.addItem(f"{p['created_at']}  —  {p['comment']}  [{p['name']}]", p["name"])
        lay.addWidget(self.combo)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_restore = QPushButton("↺ Restaurer")
        self.btn_restore.setObjectName("danger")
        self.btn_restore.setEnabled(bool(points))
        self.btn_continue = QPushButton(cancel_text)
        self.btn_continue.setObjectName("primary")
        self.btn_restore.clicked.connect(lambda: self._done("restore"))
        self.btn_continue.clicked.connect(lambda: self._done("continue"))
        row.addWidget(self.btn_restore)
        row.addWidget(self.btn_continue)
        lay.addLayout(row)
        if not points:
            self.combo.addItem("(aucun point de restauration enregistré)")
            self.combo.setEnabled(False)

    def selected_name(self):
        return self.combo.currentData()

    def _done(self, choice):
        self.choice = choice
        self.accept()

    def reject(self):
        if not self._locked:
            super().reject()


# ═══════════════════════════════════════════════════════════════════════════
#  FENÊTRE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════

QSS = """
* { font-family: "Inter","Ubuntu","Noto Sans",sans-serif; font-size: 10pt; color: #d4d4d4; }
QMainWindow, QWidget#root, QWidget#chatContainer, QDialog, QMessageBox, QInputDialog { background: #1e1e1e; }
QWidget#panelHeader { background: #252526; border-bottom: 1px solid #333333; }
QLabel#panelTitle { font-weight: 600; font-size: 9pt; color: #bbbbbb; letter-spacing: 1px; }
QLabel#muted { color: #858585; font-size: 9pt; }
QLabel#balance { color: #4ec9b0; font-size: 9.5pt; font-weight: 600; }
QPushButton#balanceRefresh { padding: 1px 4px; font-size: 11pt; min-width: 20px; }
QPlainTextEdit#terminal { background: #181818; border: none; padding: 8px; selection-background-color: #264f78; }
QPushButton { background: #333337; border: 1px solid #3c3c3c; border-radius: 6px; padding: 5px 10px; }
QPushButton:hover { background: #3e3e42; }
QPushButton:disabled { color: #6b6b6b; background: #2a2a2d; }
QPushButton#primary { background: #0e639c; border: none; color: white; font-weight: 600; padding: 8px 16px; }
QPushButton#primary:hover { background: #1177bb; }
QPushButton#primary[busy="true"], QPushButton#danger { background: #a1260d; border: none; color: white; font-weight: 600; padding: 8px 16px; }
QPushButton#primary[busy="true"]:hover, QPushButton#danger:hover { background: #c0391e; }
QComboBox { background: #3c3c3c; border: 1px solid #454545; border-radius: 6px; padding: 5px 8px; }
QComboBox QAbstractItemView { background: #252526; border: 1px solid #454545; selection-background-color: #094771; }
QPlainTextEdit#input { background: #2a2a2d; border: 1px solid #3c3c3c; border-radius: 10px; padding: 8px; }
QPlainTextEdit#input:focus { border: 1px solid #0e639c; }
QFrame#bubbleUser { background: #0b4f7d; border-radius: 12px; }
QFrame#bubbleAssistant { background: #2b2b2f; border-radius: 12px; }
QFrame#bubbleTool { background: #1c2a21; border: 1px solid #2e4a38; border-radius: 12px; }
QLabel#bubbleTitle { color: #9cdcfe; font-size: 8pt; font-weight: 600; }
QTextBrowser { background: transparent; border: none; }
QScrollArea { border: none; background: #1e1e1e; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #424242; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #5a5a5a; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle:horizontal { background: #424242; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QSplitter::handle { background: #2d2d2d; }
QToolTip { background: #252526; color: #d4d4d4; border: 1px solid #454545; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mint Agent")
        self.resize(1400, 860)
        self.db = Database()
        self.cfg = load_config()
        self.workdir = self.cfg.get("workdir") or str(Path.home())
        self.chat_id = None
        self.messages = []
        self.cmd_counter = 0
        self._busy = False
        self._stop = False
        self._worker = None
        self._autoscroll = True
        self._build_ui()
        self.refresh_balance()

    # ── Construction de l'interface ─────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter)

        # --- Panneau gauche : terminal -----------------------------------
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        head = QWidget()
        head.setObjectName("panelHeader")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 8, 12, 8)
        t = QLabel("TERMINAL")
        t.setObjectName("panelTitle")
        self.dir_label = QLabel("")
        self.dir_label.setObjectName("muted")
        self.counter_label = QLabel("")
        self.counter_label.setObjectName("muted")
        btn_restart = QPushButton("↻ Relancer")
        btn_restart.clicked.connect(self.restart_shell)
        hl.addWidget(t)
        hl.addWidget(self.dir_label, 1)
        hl.addWidget(self.counter_label)
        hl.addWidget(btn_restart)
        self.terminal = TerminalWidget()
        ll.addWidget(head)
        ll.addWidget(self.terminal, 1)

        # --- Panneau droit : chat ----------------------------------------
        right = QWidget()
        right.setMinimumWidth(400)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rhead = QWidget()
        rhead.setObjectName("panelHeader")
        rhl = QVBoxLayout(rhead)
        rhl.setContentsMargins(12, 8, 12, 10)
        rhl.setSpacing(8)
        title = QLabel(f"CHAT  ·  {DEEPSEEK_MODEL}")
        title.setObjectName("panelTitle")
        rhl.addWidget(title)
        # Solde DeepSeek (mis à jour au démarrage et au clic)
        balance_row = QHBoxLayout()
        balance_row.setContentsMargins(0, 0, 0, 0)
        balance_row.setSpacing(6)
        self.balance_label = QLabel("Solde : …")
        self.balance_label.setObjectName("balance")
        self.balance_label.setTextFormat(Qt.TextFormat.PlainText)
        self.balance_label.setToolTip("Solde DeepSeek")
        self.btn_balance = QPushButton("↻")
        self.btn_balance.setObjectName("balanceRefresh")
        self.btn_balance.setToolTip("Mettre à jour le solde")
        self.btn_balance.setFixedWidth(26)
        self.btn_balance.clicked.connect(self.refresh_balance)
        balance_row.addWidget(self.balance_label)
        balance_row.addWidget(self.btn_balance)
        balance_row.addStretch(1)
        rhl.addLayout(balance_row)
        row1 = QHBoxLayout()
        self.chat_combo = QComboBox()
        self.chat_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.chat_combo.setMinimumContentsLength(18)
        self.chat_combo.currentIndexChanged.connect(self.on_chat_selected)
        self.btn_new = QPushButton("＋ Nouveau")
        self.btn_new.clicked.connect(lambda: self.new_chat())
        self.btn_del = QPushButton("🗑")
        self.btn_del.setToolTip("Supprimer ce chat")
        self.btn_del.clicked.connect(self.delete_current_chat)
        row1.addWidget(self.chat_combo, 1)
        row1.addWidget(self.btn_new)
        row1.addWidget(self.btn_del)
        rhl.addLayout(row1)
        row2 = QHBoxLayout()
        self.btn_restore = QPushButton("↺ Restaurer…")
        self.btn_restore.setToolTip("Restaurer le dossier de projet à un point de restauration")
        self.btn_restore.clicked.connect(self.manual_restore)
        self.confirm_box = QCheckBox("Confirmer avant exécution")
        self.confirm_box.setChecked(self.cfg.get("confirm", True))
        self.confirm_box.toggled.connect(self._save_confirm)
        row2.addWidget(self.btn_restore)
        row2.addStretch(1)
        row2.addWidget(self.confirm_box)
        rhl.addLayout(row2)
        rl.addWidget(rhead)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.chat_container = QWidget()
        self.chat_container.setObjectName("chatContainer")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(12, 12, 12, 12)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch(1)
        self.scroll.setWidget(self.chat_container)
        sb = self.scroll.verticalScrollBar()
        sb.rangeChanged.connect(lambda mn, mx: sb.setValue(mx) if self._autoscroll else None)
        sb.valueChanged.connect(lambda v: setattr(self, "_autoscroll", v >= sb.maximum() - 20))
        rl.addWidget(self.scroll, 1)

        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setContentsMargins(14, 2, 14, 2)
        rl.addWidget(self.status)

        bottom = QWidget()
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(12, 4, 12, 12)
        self.input = InputBox()
        self.input.submit.connect(self.on_send_clicked)
        self.send_btn = QPushButton("Envoyer ➤")
        self.send_btn.setObjectName("primary")
        self.send_btn.setProperty("busy", False)
        self.send_btn.clicked.connect(self.on_send_clicked)
        bl.addWidget(self.input, 1)
        bl.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        rl.addWidget(bottom)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([860, 540])
        self.update_counter_label()

    # ── Démarrage ───────────────────────────────────────────────────────
    def startup(self):
        # Dossier de travail par défaut (aucun dialogue au démarrage)
        default_dir = "/home/falyrass/htdocs/linuxagent"
        if os.path.isdir(default_dir):
            self.workdir = default_dir
        elif not os.path.isdir(self.workdir):
            self.workdir = str(Path.home())
        self.cfg["workdir"] = self.workdir
        save_config(self.cfg)
        self.dir_label.setText(f"📁 {self.workdir}")
        self.dir_label.setToolTip(self.workdir)
        self.restart_shell()
        cid = self.db.last_chat_id()          # rouvre par défaut le dernier chat
        if cid is None:
            self.new_chat(force=True)
        else:
            self.load_chat(cid)
        self.terminal.setFocus()

    def restart_shell(self):
        self.terminal.clear()
        self.terminal._col = 0
        self.terminal.start_shell(self.workdir)

    def _save_confirm(self, checked):
        self.cfg["confirm"] = checked
        save_config(self.cfg)

    # ── Gestion des chats ───────────────────────────────────────────────
    def refresh_chat_list(self):
        self.chat_combo.blockSignals(True)
        self.chat_combo.clear()
        idx = 0
        for i, row in enumerate(self.db.list_chats()):
            self.chat_combo.addItem(f"{row['title']}   ·   {row['updated_at'][5:16]}", row["id"])
            if row["id"] == self.chat_id:
                idx = i
        self.chat_combo.setCurrentIndex(idx)
        self.chat_combo.blockSignals(False)

    def on_chat_selected(self, idx):
        cid = self.chat_combo.itemData(idx)
        if cid and cid != self.chat_id and not self._busy:
            self.load_chat(cid)

    def clear_bubbles(self):
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def add_bubble(self, role, text):
        b = Bubble(role, text)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            h.addSpacing(40)
        h.addWidget(b)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        return b

    def greeting(self):
        txt = (f"Bonjour 👋  Dossier de travail : `{self.workdir}`\n\n"
               "Décrivez ce que vous voulez faire : je peux lire et écrire des fichiers et exécuter des "
               "commandes dans le terminal. Un point de restauration du dossier est créé avant chaque exécution.")
        if get_api_key() is None:
            txt += ("\n\n⚠️ **Clé API manquante** : copiez `config_secret.example.py` en `config_secret.py` "
                    "et collez-y votre clé DeepSeek, puis relancez.")
        self.add_bubble("assistant", txt)

    def new_chat(self, force=False):
        if self._busy:
            return
        if not force and self.chat_id is not None and not self.messages:
            self.input.setFocus()
            return
        self.chat_id = self.db.new_chat()
        self.messages = []
        self.clear_bubbles()
        self.refresh_chat_list()
        self.greeting()
        self.input.setFocus()

    def load_chat(self, cid):
        self.chat_id = cid
        self.messages = self.db.messages(cid)
        self.clear_bubbles()
        self._autoscroll = True
        if not self.messages:
            self.greeting()
        for m in self.messages:
            self.add_bubble(m["role"], m["content"])
        self.refresh_chat_list()

    def delete_current_chat(self):
        if self._busy or self.chat_id is None:
            return
        if QMessageBox.question(self, "Supprimer", "Supprimer définitivement ce chat ?") != \
                QMessageBox.StandardButton.Yes:
            return
        self.db.delete_chat(self.chat_id)
        cid = self.db.last_chat_id()
        if cid is None:
            self.new_chat(force=True)
        else:
            self.load_chat(cid)

    def add_message(self, role, content, show=True):
        self.messages.append({"role": role, "content": content})
        self.db.add_message(self.chat_id, role, content)
        if show:
            self.add_bubble(role, content)
        if role == "user" and len([m for m in self.messages if m["role"] == "user"]) == 1:
            title = content.strip().splitlines()[0][:40] or "Nouveau chat"
            self.db.set_title(self.chat_id, title)
            self.refresh_chat_list()

    # ── Envoi / boucle de l'agent ───────────────────────────────────────
    def set_busy(self, busy):
        self._busy = busy
        self.send_btn.setText("■ Arrêter" if busy else "Envoyer ➤")
        self.send_btn.setProperty("busy", busy)
        self.send_btn.style().unpolish(self.send_btn)
        self.send_btn.style().polish(self.send_btn)
        for w in (self.chat_combo, self.btn_new, self.btn_del, self.btn_restore):
            w.setEnabled(not busy)

    def refresh_balance(self):
        """Récupère le solde DeepSeek en tâche de fond et met à jour le label."""
        print("[balance] refresh_balance appelé", flush=True)
        if getattr(self, "_balance_busy", False):
            return
        self._balance_busy = True
        def worker():
            txt, raw = fetch_balance()
            self._pending_balance = (txt, raw)
            self._balance_busy = False
            try:
                QMetaObject.invokeMethod(
                    self, "apply_balance", Qt.ConnectionType.QueuedConnection)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot()
    def apply_balance(self):
        if hasattr(self, "_pending_balance") and hasattr(self, "balance_label"):
            txt, raw = self._pending_balance
            print("[balance] résultat:", txt, flush=True)
            self.balance_label.setText(txt)
            if raw:
                infos = raw.get("balance_infos") or []
                det = "\n".join(
                    f"{b.get('currency','?')} : total {b.get('total_balance','?')} "
                    f"(offert {b.get('granted_balance','?')}, rechargé {b.get('topped_up_balance','?')})"
                    for b in infos)
                self.balance_label.setToolTip("Cliquer pour rafraîchir\n" + det)
            else:
                self.balance_label.setToolTip("Cliquer pour rafraîchir")

    def set_status(self, text):
        self.status.setText(text)

    def on_send_clicked(self):
        if self._busy:
            self._stop = True
            if self._worker:
                self._worker.stop()
            self.terminal.abort()
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        if get_api_key() is None:
            self.add_bubble("assistant", "⚠️ **Clé API manquante.** Copiez `config_secret.example.py` en "
                                         "`config_secret.py`, collez-y votre clé DeepSeek, puis relancez.")
            return
        self.input.clear()
        self._stop = False
        self._autoscroll = True
        self.add_message("user", text)
        self.set_busy(True)
        try:
            self.agent_loop()
        finally:
            self.set_busy(False)
            self.set_status("")
            self.input.setFocus()

    def build_api_messages(self):
        msgs = [{"role": "system", "content": system_prompt(self.workdir)}]
        for m in self.messages:
            if m["role"] == "tool":
                msgs.append({"role": "user", "content": "[Résultats d'exécution]\n" + m["content"]})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    def agent_loop(self):
        for _ in range(MAX_AGENT_STEPS):
            text, status = self.stream_reply()
            if text.strip():
                self.add_message("assistant", text, show=False)
            if status != "ok" or self._stop:
                return
            actions = parse_actions(text)
            if not actions:
                return
            results, keep_going = self.execute_actions(actions)
            self.add_message("tool", results)
            if not keep_going or self._stop:
                return
        self.add_bubble("assistant", f"⏸ Limite de {MAX_AGENT_STEPS} étapes atteinte. Dites « continue » pour poursuivre.")

    def stream_reply(self):
        """Affiche la réponse de l'IA en streaming ; retourne (texte, statut)."""
        bubble = self.add_bubble("assistant", "…")
        worker = ChatWorker(self.build_api_messages(), self)
        self._worker = worker
        state = {"text": "", "dirty": False}
        result = {"status": "error", "text": ""}
        loop = QEventLoop(self)

        def on_chunk(p):
            state["text"] += p
            state["dirty"] = True

        def on_done(t):
            result.update(status="stopped" if worker.stopped else "ok", text=t)
            loop.quit()

        def on_fail(msg, partial):
            result.update(status="error", text=partial, error=msg)
            loop.quit()

        def flush():                       # rafraîchit la bulle ~12 fois par seconde
            if state["dirty"]:
                state["dirty"] = False
                bubble.set_text(state["text"])

        worker.chunk.connect(on_chunk)
        worker.completed.connect(on_done)
        worker.failed.connect(on_fail)
        timer = QTimer(self)
        timer.timeout.connect(flush)
        timer.start(80)
        self.set_status("💬 DeepSeek répond…")
        worker.start()
        loop.exec()
        timer.stop()
        worker.wait()
        self._worker = None
        text = result["text"]
        if result["status"] == "error":
            bubble.set_text((text + "\n\n" if text else "") + "⚠️ " + result.get("error", "Erreur inconnue"))
        elif result["status"] == "stopped":
            bubble.set_text((text or "") + "\n\n*⏹ interrompu*")
        else:
            bubble.set_text(text)
        return text, result["status"]

    # ── Exécution des actions (avec points de restauration) ─────────────
    def execute_actions(self, actions):
        """Retourne (texte_résultats, continuer ?)."""
        if self.confirm_box.isChecked():
            desc = "\n".join("• " + describe(a) for a in actions)
            r = QMessageBox.question(
                self, "Confirmer l'exécution",
                "L'agent souhaite effectuer :\n\n" + desc + "\n\nAutoriser ?")
            if r != QMessageBox.StandardButton.Yes:
                return "L'utilisateur a refusé l'exécution de ces actions.", False

        results, snapshot_done = [], False
        for a in actions:
            if self._stop:
                results.append("Interrompu par l'utilisateur.")
                return "\n\n".join(results), False
            if a["kind"] == "read":
                results.append(self.do_read(a["path"]))
                continue

            # Point de restauration AVANT toute exécution/écriture
            if SNAPSHOT_PER_COMMAND or not snapshot_done:
                if not self.make_restore_point(describe(a)):
                    results.append("Aucun point de restauration créé : actions annulées par sécurité.")
                    return "\n\n".join(results), False
                snapshot_done = True

            if a["kind"] == "write":
                results.append(self.do_write(a["path"], a["content"]))
            else:
                self.set_status("⚙️ Exécution dans le terminal…")
                rc, out = self.terminal.run_command(a["cmd"])
                results.append(f"$ {a['cmd']}\n[code de sortie : {rc}]\n{out}")
                self.cmd_counter += 1
                self.update_counter_label()
                if self.cmd_counter >= COMMAND_LIMIT:
                    if self.ask_continue_or_restore() == "restore":
                        results.append("L'utilisateur a restauré un point de restauration : "
                                       "l'état du système a été ramené en arrière.")
                        return "\n\n".join(results), False
        return "\n\n".join(results), True

    def resolve_path(self, p):
        path = Path(p).expanduser()
        return path if path.is_absolute() else Path(self.workdir) / path

    def do_read(self, p):
        path = self.resolve_path(p)
        try:
            if path.is_dir():
                listing = "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in path.iterdir()))
                return f"[Dossier {path}]\n{listing[:OUTPUT_LIMIT]}"
            data = path.read_text(encoding="utf-8", errors="replace")
            extra = "\n[… tronqué …]" if len(data) > OUTPUT_LIMIT else ""
            return f"[Fichier {path}]\n{data[:OUTPUT_LIMIT]}{extra}"
        except Exception as e:
            return f"[Lecture impossible : {path}] {e}"

    def do_write(self, p, content):
        path = self.resolve_path(p)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self.terminal.info(f"📝 Fichier écrit : {path}", "#6a9955")
            return f"Fichier écrit : {path} ({len(content.encode('utf-8'))} octets)"
        except Exception as e:
            return f"Écriture impossible : {path} — {e}"

    def update_counter_label(self):
        self.counter_label.setText(f"Commandes : {self.cmd_counter}/{COMMAND_LIMIT}")

    # ── Points de restauration : dépôt git local (rapide, sans sudo) ────
    def snapshot_dir(self):
        return SNAPSHOT_DIR / hashlib.sha1(self.workdir.encode("utf-8")).hexdigest()[:12]

    def git(self, *args):
        """Commande git qui sauvegarde le dossier de projet dans un dépôt séparé (n'altère pas votre .git)."""
        return ["git", f"--git-dir={self.snapshot_dir()}", f"--work-tree={self.workdir}",
                "-c", "user.name=DeepSeek Agent", "-c", "user.email=agent@localhost",
                "-c", "core.autocrlf=false", *args]

    def ensure_repo(self):
        gd = self.snapshot_dir()
        if not (gd / "HEAD").exists():
            subprocess.run(["git", "init", "--bare", "--quiet", str(gd)], check=True, capture_output=True)
        (gd / "info").mkdir(exist_ok=True)
        (gd / "info" / "exclude").write_text("\n".join(SNAPSHOT_EXCLUDES) + "\n", encoding="utf-8")

    def make_restore_point(self, description):
        if not shutil.which("git"):
            QMessageBox.critical(self, "git introuvable",
                                 "Installez git pour les points de restauration :\n\nsudo apt install git")
            return False
        try:
            self.ensure_repo()
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible d'initialiser les points de restauration :\n{e}")
            return False
        comment = f"{description}"[:120]
        self.set_status("📸 Point de restauration…")
        rc, out = run_blocking(CmdWorker([
            self.git("add", "-A"),
            self.git("commit", "--allow-empty", "--no-verify", "-q", "-m", comment),
            self.git("rev-parse", "--short", "HEAD"),
        ]))
        self.set_status("")
        if rc != 0:
            r = QMessageBox.question(
                self, "Échec du point de restauration",
                "Le point de restauration n'a pas pu être créé :\n\n" + out[-800:] +
                "\n\nContinuer SANS point de restauration ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            return r == QMessageBox.StandardButton.Yes
        name = out.strip().splitlines()[-1].strip()
        self.db.add_restore_point(name, comment, self.chat_id)
        self.terminal.info(f"📸 Point de restauration : {name}", "#6a9955")
        return True

    def ask_continue_or_restore(self):
        """Boîte de dialogue déclenchée après COMMAND_LIMIT commandes."""
        dlg = RestoreDialog(
            self,
            self.db.restore_points(),
            f"<b>{self.cmd_counter} commandes</b> ont été exécutées depuis la dernière décision.<br><br>"
            "Souhaitez-vous <b>restaurer</b> un point de restauration (choisissez-le ci-dessous) "
            "ou <b>continuer</b> ?",
            locked=True, cancel_text="▶ Continuer")
        dlg.exec()
        if dlg.choice == "restore" and self.do_restore(dlg.selected_name()):
            self.cmd_counter = 0
            self.update_counter_label()
            return "restore"
        self.cmd_counter = 0
        self.update_counter_label()
        return "continue"

    def manual_restore(self):
        dlg = RestoreDialog(self, self.db.restore_points(),
                            "Choisissez le point de restauration à rétablir :",
                            locked=False, cancel_text="Annuler")
        dlg.exec()
        if dlg.choice == "restore":
            self.do_restore(dlg.selected_name())

    def do_restore(self, name):
        if not name:
            return False
        r = QMessageBox.warning(
            self, "Confirmer la restauration",
            f"Restaurer le dossier\n{self.workdir}\nà l'état du point « {name} » ?\n\n"
            "Les modifications faites depuis seront perdues et les fichiers créés depuis seront supprimés.\n"
            "Les changements effectués hors de ce dossier (paquets installés, fichiers système…) "
            "ne sont pas annulés.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return False
        self.set_status("↺ Restauration…")
        rc, out = run_blocking(CmdWorker([
            self.git("read-tree", "-u", "--reset", name),   # remet les fichiers suivis dans l'état du point
            self.git("clean", "-fdq"),                      # supprime les fichiers créés depuis
        ]))
        self.set_status("")
        if rc == 0:
            self.terminal.info(f"↺ Dossier restauré au point {name}", "#dcdcaa")
            QMessageBox.information(self, "Restauration terminée", f"Le dossier a été restauré au point {name}.")
            return True
        QMessageBox.critical(self, "Échec de la restauration", out[-800:])
        return False

    # ── Fermeture ───────────────────────────────────────────────────────
    def closeEvent(self, e):
        self._stop = True
        if self._worker:
            self._worker.stop()
        self.terminal.abort()
        self.terminal.stop_shell()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    # Nom d'application (barre des tâches / notifications)
    app.setApplicationName("Mint Agent")
    app.setApplicationDisplayName("Mint Agent")
    app.setDesktopFileName("Mint Agent")
    # Icône globale de l'application
    _icon_path = Path(__file__).resolve().parent / "logomint_icon.png"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    win = MainWindow()
    if _icon_path.exists():
        win.setWindowIcon(QIcon(str(_icon_path)))
    win.show()
    QTimer.singleShot(0, win.startup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
