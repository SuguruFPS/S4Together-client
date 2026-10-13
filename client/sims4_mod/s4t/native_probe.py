"""Inventaire en lecture seule des modules natifs du jeu : noms d'attributs, aucun appel."""
import importlib
import json
import os
from pathlib import Path

# Modules natifs dont les exports réels ne sont pas visibles dans le bytecode livré
# (docs/TERRAIN_NATIVE_ARCHITECTURE_SPIKE.md, « Portée des affirmations d'absence »).
MODULES = ('_buildbuy', '_persistence_module', '_zone', '_terrain', '_lot', '_world')
# Noms qui pourraient désigner une voie d'architecture ; un nom n'est pas une preuve de contrat.
HINTS = ('blueprint', 'architecture', 'wall', 'roof', 'floor', 'foundation', 'terrain', 'template',
         'lot', 'apply', 'import', 'load', 'save', 'serialize', 'house_desc')
MAX_NAMES = 2000


def inspect(load=importlib.import_module):
    found = {}
    for name in MODULES:
        try:
            module = load(name)
        except Exception as error:  # Module absent de cette version du jeu : à consigner, pas à masquer.
            found[name] = {'available': False, 'error': type(error).__name__}
            continue
        names = sorted(n for n in dir(module) if not n.startswith('__'))[:MAX_NAMES]
        found[name] = {'available': True, 'count': len(names), 'names': names,
                       'candidates': [n for n in names if any(h in n.lower() for h in HINTS)]}
    return found


def write(found, root=None):
    root = Path(root) if root is not None else \
        Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'S4Together' / 'logs'
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'native-probe.json'
    temporary = root / 'native-probe.json.tmp'
    temporary.write_text(json.dumps(found, ensure_ascii=False, sort_keys=True, indent=1), encoding='utf-8')
    temporary.replace(path)
    return path


def summary(found):
    modules = {name: ({'count': value['count'], 'candidates': len(value['candidates'])}
                      if value.get('available') else {'error': value.get('error')})
               for name, value in found.items()}
    return {'ok': True, 'code': 'native_probe', 'modules': modules, 'file': 'native-probe.json',
            'message': 'Lecture seule : envoyer le fichier native-probe.json du dossier logs'}
