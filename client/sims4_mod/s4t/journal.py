"""Journal borné sans adresses, identités, mots de passe ni payload réseau."""
import json
import os
import platform
from pathlib import Path
import time

VERSION = '0.1.42-game-test'

def log(event, code='ok'):
    root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'S4Together' / 'logs'
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'game-client.jsonl'
        if path.exists() and path.stat().st_size > 65536:
            path.replace(root / 'game-client.previous.jsonl')
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'time': int(time.time()), 'version': VERSION,
                'python': platform.python_version(), 'event': event, 'code': code}) + '\n')
        return True
    except OSError:
        return False
