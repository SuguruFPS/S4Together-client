"""Apparence native des Sims actifs : publiée au serveur, reçue des autres joueurs (12/10/2026).

Protocole : game.appearance.publish {simId, profile}, game.appearance.list {}, game.appearance.get {playerId},
événement game.appearance {playerId, simId, profile}. Profil : contrat simdata-allowlist-v1 de remote_profile
(seuls les champs d'apparence ; ni nom, ni argent, ni relations). Une seule requête en vol, jamais d'exception
vers le jeu. Export par SimInfo.save_sim(for_cloning=True), qui retourne le message SimData (désassemblage 1.128).
NOT_TESTED_GAME.
"""
import uuid

from .journal import log
from .remote_profile import build_profile, filter_simdata, validate_profile

EVENT = 'game.appearance'
EXPORT_TICKS = 60            # Nouveau relevé de mon apparence (CAS, changement de tenue) une fois par minute.
RETRY_TICKS = 30
MAX_TRANSPORT_BYTES = 44000  # Borne serveur : l'événement tient dans une trame WebSocket de 64 Kio.
MAX_PLAYERS = 16


def game_export(sim_id):
    """Profil d'apparence du Sim `sim_id` de cette partie, ou None. Lecture seule : save_sim ne modifie pas le Sim."""
    try:
        import services
        sim_info = services.sim_info_manager().get(int(sim_id))
        if sim_info is None:
            return None
        data = filter_simdata(sim_info.save_sim(for_cloning=True).SerializeToString())
        if len(data) > MAX_TRANSPORT_BYTES:
            log('appearance_export', 'too_large:' + str(len(data)))
            return None
        return build_profile(data)
    except Exception as error:
        log('appearance_export', type(error).__name__)
        return None


class AppearanceSync:
    """Appelée par AutoLink une fois le personnage lié. `remote` : {playerId: {'simId', 'sha256', 'data'}} validés."""

    def __init__(self, export=game_export, new_id=lambda: str(uuid.uuid4())):
        self.export = export
        self.new_id = new_id
        self.session = None
        self.reset()

    def reset(self):
        # Nouvelle zone ou reconnexion : liste redemandée ; le serveur ignore une republication identique.
        # Le cache est vidé : une apparence reçue d'un autre serveur ou d'une session passée n'est jamais
        # réappliquée ; il n'est reconstruit qu'à partir de la liste validée de la session en cours.
        self.remote = {}
        self.waiting = None
        self.listed = False
        self.published = None
        self.profile = None
        self.export_in = 0
        self.cooldown = 0
        self.wanted = {}
        self.failed = set()

    def own_player(self, manager):
        identity = getattr(manager, 'identity', None)
        return identity.get('playerId') if isinstance(identity, dict) else None

    def _same_session(self, manager):
        """Faux, cache purgé, si le serveur ou le joueur authentifié a changé depuis le dernier appel."""
        session = (getattr(manager, 'endpoint', None), self.own_player(manager))
        if session == self.session:
            return True
        if self.session is not None:
            log('appearance', 'session_changed')
        self.reset()
        self.session = session
        return False

    def tick(self, manager, character, active_sim_id):
        self._same_session(manager)
        if character is None or character.get('status') != 'bound' or self.waiting is not None:
            return None
        if self.cooldown > 0:
            self.cooldown -= 1
            return None
        if not self.listed:
            return self._send(manager, 'game.appearance.list', {})
        if active_sim_id:
            if self.export_in <= 0:
                self.export_in = EXPORT_TICKS
                self.profile = self.export(active_sim_id)
            else:
                self.export_in -= 1
            if self.profile is not None and self.published != (active_sim_id, self.profile['sha256']):
                return self._send(manager, 'game.appearance.publish', {'simId': str(active_sim_id), 'profile': self.profile})
        for player_id in sorted(self.wanted):
            if player_id in self.failed:
                continue
            known = self.remote.get(player_id)
            if known is None or known['sha256'] != self.wanted[player_id]:
                return self._send(manager, 'game.appearance.get', {'playerId': player_id})
        return None

    def _send(self, manager, kind, payload):
        request_id = self.new_id()
        manager.request(kind, payload, request_id)
        self.waiting = (request_id, kind, payload)
        return kind

    def on_messages(self, manager, messages):
        if not self._same_session(manager):
            # Réponses et événements d'une session précédente : rien n'est conservé.
            return
        me = self.own_player(manager)
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict):
                continue
            if message.get('id') is None and message.get('type') == EVENT:
                # Avant la liste de cette session, aucun événement n'entre dans le cache.
                if self.listed:
                    self._store(message.get('payload'), me)
            elif self.waiting is not None and message.get('id') == self.waiting[0]:
                self._reply(message, me)

    def _reply(self, message, me):
        _, kind, payload = self.waiting
        self.waiting = None
        if message.get('ok') is not True:
            code = message.get('code') if isinstance(message.get('code'), str) else 'error'
            log('appearance', kind + ':' + code)
            self.cooldown = RETRY_TICKS
            if kind == 'game.appearance.get':
                self.failed.add(payload['playerId'])
            elif kind == 'game.appearance.publish' and code not in ('appearance_rate_limited', 'game_not_joined', 'storage_unavailable', 'request_limit'):
                # Profil refusé par le serveur : pas de renvoi en boucle de la même apparence.
                self.published = (payload['simId'], payload['profile']['sha256'])
            return
        data = message.get('payload')
        if kind == 'game.appearance.list':
            self.listed = True
            entries = data.get('appearances') if isinstance(data, dict) else None
            self.wanted = {}
            for entry in entries if isinstance(entries, list) else []:
                if (isinstance(entry, dict) and isinstance(entry.get('playerId'), str) and entry['playerId'] != me
                        and isinstance(entry.get('sha256'), str) and len(self.wanted) < MAX_PLAYERS):
                    self.wanted[entry['playerId']] = entry['sha256']
            # Seuls restent les joueurs que la liste de cette session annonce, avec la même empreinte.
            self.remote = {pid: known for pid, known in self.remote.items() if self.wanted.get(pid) == known['sha256']}
        elif kind == 'game.appearance.publish':
            self.published = (payload['simId'], payload['profile']['sha256'])
            log('appearance', 'published')
        elif isinstance(data, dict) and data.get('playerId') == payload['playerId'] and payload['playerId'] in self.wanted:
            self._store(data, me)
        else:
            # Réponse pour un autre joueur que celui demandé, ou joueur absent de la liste : refusée.
            log('appearance', 'get_mismatch')
            self.failed.add(payload['playerId'])

    def _store(self, payload, me):
        if not isinstance(payload, dict) or set(payload) != {'playerId', 'simId', 'profile'}:
            log('appearance', 'event_shape')
            return
        player = payload['playerId']
        if not isinstance(player, str) or not 0 < len(player) <= 128 or player == me:
            return
        data, sha, code = validate_profile(payload['profile'])
        if code is not None:
            log('appearance', 'rejected:' + code)
            self.failed.add(player)
            return
        if player not in self.remote and len(self.remote) >= MAX_PLAYERS:
            return
        self.remote[player] = {'simId': str(payload['simId']), 'sha256': sha, 'data': data}
        self.wanted[player] = sha
        self.failed.discard(player)

    def status(self):
        """Diagnostic sans contenu d'apparence."""
        return {'listed': self.listed, 'published': self.published is not None,
                'waiting': self.waiting[1] if self.waiting else None,
                'remote': sorted(self.remote), 'failed': sorted(self.failed), 'gameReadiness': 'NOT_TESTED_GAME'}
