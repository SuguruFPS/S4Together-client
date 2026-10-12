"""Réception, cache et diagnostic des apparences de Sims distants (spike rendu distant, 12/10/2026).

Désactivé par défaut et branché nulle part : aucun message `game.appearance` n'existe encore dans le protocole.
Ce module ne crée, ne déplace et ne supprime aucun Sim ; il ne touche ni au Sim local, ni au foyer, ni à l'argent,
ni aux besoins, ni à la sauvegarde. Le spawn reste bloqué (voir SPAWN_BLOCKERS et
docs/REMOTE_RENDER_SPIKE_2026-10-12.md). NOT_TESTED_GAME.
"""
import re

from .journal import log
from .remote_profile import GAME_VERSION, validate_profile

ENABLED = False
EVENT = 'game.appearance'
MAX_SIMS_PER_PLAYER = 8
MAX_ENTRIES = 64
MAX_PLAYER_ID = 128
_NATIVE = re.compile(r'^[0-9]{1,20}$')
_ZONE = re.compile(r'^native:[0-9]{1,20}:[0-9]{1,20}$')

# Ce qui interdit aujourd'hui de créer le Sim distant. Tant qu'un seul subsiste, rien n'est instancié.
SPAWN_BLOCKERS = (
    'appearance_transport_absent',   # ni export côté mod, ni message serveur game.appearance
    'native_import_unverified',      # SimInfo.load_sim_info / SimSpawner.spawn_sim jamais exécutés avec ce profil
    'save_exclusion_unverified',     # un SimInfo créé serait écrit par SimInfoManager.save dans la partie solo
    'canonical_lot_unproven',        # worldId/lotId natifs de deux sauvegardes ne prouvent pas le même terrain
)


class RemoteAppearanceCache:
    """Cache borné des apparences reçues pour la zone courante. Une entrée appartient au joueur qui l'a publiée."""

    def __init__(self, enabled=ENABLED, game_version=GAME_VERSION):
        self.enabled = bool(enabled)
        self.game_version = game_version
        self.zone_id = None
        self.own_player_id = None
        self.own_sim_ids = frozenset()
        self.participants = frozenset()
        self.entries = {}     # (playerId, simId) -> (sha256, octets)
        self.owners = {}      # simId -> playerId
        self.received = self.duplicates = self.replaced = self.dropped = 0
        self.rejected = {}

    # -- Cycle de vie : voyage, départ d'un joueur, déconnexion ------------------------------------------

    def sync(self, zone_id, participants, own_sim_ids, own_player_id=None):
        """À appeler à chaque tick avec l'état confirmé de PresenceSync. Changement de zone : tout est oublié."""
        if zone_id != self.zone_id:
            self.clear('zone')
            self.zone_id = zone_id if isinstance(zone_id, str) and _ZONE.match(zone_id) else None
        self.own_player_id = own_player_id
        self.own_sim_ids = frozenset(own_sim_ids or ())
        self.participants = frozenset(participants or ())
        for key in [k for k in self.entries if k[0] not in self.participants or k[1] in self.own_sim_ids]:
            self._drop(key)

    def clear(self, reason):
        """Voyage, game.leave, déconnexion ou arrêt : aucune apparence ne survit à la zone qui l'a reçue."""
        if self.entries:
            log('remote_render', 'clear:' + reason)
        self.dropped += len(self.entries)
        self.entries, self.owners = {}, {}
        self.zone_id = None
        self.participants = frozenset()

    def _drop(self, key):
        if self.entries.pop(key, None) is not None:
            self.dropped += 1
            if self.owners.get(key[1]) == key[0]:
                del self.owners[key[1]]

    # -- Réception --------------------------------------------------------------------------------------

    def on_messages(self, messages):
        for message in messages if isinstance(messages, list) else []:
            if isinstance(message, dict) and message.get('id') is None and message.get('type') == EVENT:
                self.receive(message.get('payload'))

    def receive(self, payload):
        """Valide puis met en cache une apparence. Retourne 'cached', 'duplicate', 'replaced' ou un code de refus."""
        code = self._receive(payload)
        if code in ('cached', 'duplicate', 'replaced'):
            self.received += 1
        else:
            self.rejected[code] = self.rejected.get(code, 0) + 1
        return code

    def _receive(self, payload):
        if not self.enabled:
            return 'disabled'
        if not isinstance(payload, dict) or set(payload) != {'playerId', 'zoneId', 'simId', 'profile'}:
            return 'event_shape'
        player, sim = payload['playerId'], payload['simId']
        if not isinstance(player, str) or not 1 <= len(player) <= MAX_PLAYER_ID:
            return 'event_shape'
        if not isinstance(sim, str) or not _NATIVE.match(sim) or sim.strip('0') == '':
            return 'event_shape'
        if self.zone_id is None or payload['zoneId'] != self.zone_id:
            return 'zone_mismatch'
        if player == self.own_player_id:
            return 'own_player'
        if player not in self.participants:
            return 'not_participant'
        if sim in self.own_sim_ids:
            # Une identité native étrangère égale à celle d'un Sim local ne le remplace jamais.
            return 'local_sim_collision'
        if self.owners.get(sim, player) != player:
            return 'sim_owner_conflict'
        data, sha, code = validate_profile(payload['profile'], self.game_version)
        if code is not None:
            return code
        key = (player, sim)
        known = self.entries.get(key)
        if known is not None and known[0] == sha:
            self.duplicates += 1
            return 'duplicate'
        if known is None:
            if sum(1 for k in self.entries if k[0] == player) >= MAX_SIMS_PER_PLAYER:
                return 'player_limit'
            if len(self.entries) >= MAX_ENTRIES:
                return 'cache_full'
        else:
            self.replaced += 1
        self.entries[key] = (sha, data)
        self.owners[sim] = player
        return 'cached' if known is None else 'replaced'

    def get(self, player_id, sim_id):
        """Octets SimData d'apparence validés, ou None. Seul point de lecture pour un futur import natif."""
        entry = self.entries.get((player_id, sim_id))
        return entry[1] if entry is not None else None

    # -- Diagnostic -------------------------------------------------------------------------------------

    def status(self):
        """État pour s4t.diagnostic : ni identité de joueur, ni identité de Sim, ni contenu d'apparence."""
        return {'enabled': self.enabled, 'remoteRendering': 'blocked', 'spawn': 'not_attempted',
                'blockers': list(SPAWN_BLOCKERS), 'zoneId': self.zone_id, 'cached': len(self.entries),
                'bytes': sum(len(entry[1]) for entry in self.entries.values()), 'received': self.received,
                'duplicates': self.duplicates, 'replaced': self.replaced, 'dropped': self.dropped,
                'rejected': dict(self.rejected), 'gameReadiness': 'NOT_TESTED_GAME'}
