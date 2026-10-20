"""Journal borné sans adresses, identités, mots de passe ni payload réseau.

Appelé dans un bloc except avec le nom du type de l'exception en cours dans `code`, une entrée
ajoute `where` : les dernières images de la pile (fichier sans dossier, ligne, fonction). Jamais
le message de l'exception ni les variables, qui peuvent contenir adresses, noms ou données réseau.
"""
import json
import os
import platform
from pathlib import Path
import sys
import time
import traceback

VERSION = '0.1.69-game-test'
MAX_FRAMES = 8


def _where(code):
    """Pile courte de l'exception en cours si `code` la nomme ; None sinon."""
    error = sys.exc_info()[1]
    if error is None or type(error).__name__ not in str(code):
        return None
    try:
        frames = traceback.extract_tb(error.__traceback__)[-MAX_FRAMES:]
        return ['%s:%d:%s' % (os.path.basename(frame.filename), frame.lineno, frame.name) for frame in frames]
    except Exception:
        return None


def log(event, code='ok'):
    root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'S4Together' / 'logs'
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'game-client.jsonl'
        if path.exists() and path.stat().st_size > 65536:
            path.replace(root / 'game-client.previous.jsonl')
        entry = {'time': int(time.time()), 'version': VERSION,
            'python': platform.python_version(), 'event': event, 'code': code}
        where = _where(code)
        if where:
            entry['where'] = where
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry) + '\n')
        return True
    except OSError:
        return False
