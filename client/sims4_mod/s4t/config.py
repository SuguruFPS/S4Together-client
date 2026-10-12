"""Identités locales dédiées, jamais dans les sauvegardes ou journaux du jeu."""
import hashlib
import json
import os
from pathlib import Path


class ConfigStore:
    def __init__(self, root=None):
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.config')))
        self.root = Path(root) if root is not None else base / 'S4Together' / 'mod'

    def _read(self, name):
        path = self.root / name
        if not path.exists():
            return {}
        if path.stat().st_size > 32768:
            raise ValueError('Configuration trop grande')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            raise ValueError('Configuration locale invalide')
        if not isinstance(value, dict):
            raise ValueError('Configuration locale invalide')
        return value

    def settings(self):
        result = {'endpoint': '', 'name': 'Joueur Sims', 'dlcs': []}
        result.update(self._read('config.json'))
        if (not isinstance(result['name'], str) or not 1 <= len(result['name'].strip()) <= 40 or
                not isinstance(result['dlcs'], list) or len(result['dlcs']) > 256 or
                any(not isinstance(x, str) or len(x) > 24 for x in result['dlcs'])):
            raise ValueError('Configuration locale invalide')
        return result

    def save_settings(self, endpoint, name, allow_lan=False):
        from .websocket import validate_endpoint
        validate_endpoint(endpoint, allow_lan=allow_lan)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise ValueError('Nom joueur invalide')
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / 'config.json'
        temporary = self.root / 'config.new'
        value = {'endpoint': endpoint, 'name': name.strip(), 'dlcs': self.settings()['dlcs'], 'allow_lan': bool(allow_lan)}
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(target))
        finally:
            if temporary.exists():
                temporary.unlink()

    def play_intent_done(self):
        try:
            value = self._read('play-intent.json').get('done')
        except ValueError:
            return None
        return value if isinstance(value, str) else None

    def mark_play_intent_done(self, intent_id):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / 'play-intent.json'
        temporary = path.with_suffix('.tmp')
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump({'done': intent_id}, stream)
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()

    def _marker(self, name, key):
        try:
            value = self._read(name).get(key)
        except ValueError:
            return None
        return value if isinstance(value, str) else None

    def _mark(self, name, key, value):
        try:
            current = self._read(name)
        except ValueError:
            current = {}
        entries = {k: v for k, v in current.items() if isinstance(k, str) and isinstance(v, str)}
        entries.pop(key, None)
        entries[key] = value
        # Borne : les 64 entrées les plus récentes (fichier < 32 Kio).
        entries = dict(list(entries.items())[-64:])
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        temporary = path.with_suffix('.tmp')
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(entries, stream)
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()

    def season_applied(self, key):
        """Saison déjà appliquée pour ce monde serveur et ce foyer, sinon None."""
        return self._marker('season-applied.json', key)

    def mark_season_applied(self, key, season):
        self._mark('season-applied.json', key, season)

    def character_marker(self, key):
        """Action unique déjà faite pour ce personnage (« <id>/funds », « <id>/npc »), sinon None."""
        return self._marker('character-applied.json', key)

    def mark_character(self, key, value):
        self._mark('character-applied.json', key, value)

    @staticmethod
    def _identity_name(endpoint):
        return 'identity-' + hashlib.sha256(endpoint.encode('utf-8')).hexdigest() + '.json'

    def identity(self, endpoint):
        result = self._read(self._identity_name(endpoint))
        if not result:
            return {}
        if (not isinstance(result.get('playerId'), str) or not isinstance(result.get('token'), str) or
                not result['playerId'] or not result['token'] or len(result['token']) > 512):
            raise ValueError('Identité locale invalide')
        return {'playerId': result['playerId'], 'token': result['token']}

    def save_identity(self, endpoint, player_id, token):
        if not isinstance(player_id, str) or not player_id or not isinstance(token, str) or not token:
            raise ValueError('Identité serveur invalide')
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(self.root), 0o700)
        except OSError:
            pass
        path = self.root / self._identity_name(endpoint)
        temporary = path.with_suffix('.tmp')
        # Exclusivité du fichier temporaire ; pas de suivi d'un lien préexistant.
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump({'playerId': player_id, 'token': token}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()
