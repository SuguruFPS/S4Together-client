"""Prototype diagnostic. Compatibilité jeu : NOT_TESTED_GAME."""
import json
import platform
import sys

BUILD_VERSION = "0.1.27-game-test"


def missing_module(error):
    # Noms de dépendances connus seulement : aucun message/chemin utilisateur.
    name = getattr(error, 'name', '')
    allowed = {'ssl', '_ssl', 'socket', '_socket', 'select', 'queue', '_queue',
        'threading', '_thread', 'uuid', 'hashlib', '_hashlib', 'ipaddress',
        'urllib', 'urllib.parse', 'base64', 'hmac', 'struct', '_struct',
        'pathlib', 'json', 'platform', 'time', 'os', 'collections',
        'subprocess', '_winapi', 'multiprocessing', '_multiprocessing',
        'ctypes', '_ctypes', 'math', 'binascii', 'zlib', 'array', 'enum',
        '_random', 'random', '_sha256', '_sha512', '_sha1', '_md5', '_blake2'}
    return name if name in allowed else 'unknown_dependency'


def report():
    """Lecture seule ; aucune donnée de Sim, de foyer ou de sauvegarde."""
    return {
        "mod": "S4Together",
        "version": BUILD_VERSION,
        "python": platform.python_version(),
        "python_magic": "see-build-readiness",
        "game_modules_present": "sims4.commands" in sys.modules,
        "readiness": "NOT_TESTED_GAME",
        "simulation_hooks": False,
        "network_connected": False,
    }


if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, sort_keys=True))
