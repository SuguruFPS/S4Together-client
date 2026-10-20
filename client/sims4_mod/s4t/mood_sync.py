"""Humeur et pensées (moodlets) du Sim actif : publiées au serveur, rejouées sur la copie chez les autres (20/10/2026).

Protocole : game.mood.publish {simId, mood, buffs}, game.mood.list {}, événement game.mood {playerId, simId, mood,
buffs}. Identifiants = guid64 du tuning, en chaînes décimales ; 32 pensées au plus. Le serveur ne connaît pas le
tuning : c'est ce mod qui décide quoi rejouer. Seules les pensées visibles, porteuses d'une émotion, sans loot à
l'ajout ou au retrait, sans buff de remplacement ni commodity propriétaire, et hors liste des pensées mortelles sont
ajoutées à la copie, avec apply_buff_loot=False et remove_on_zone_unload=True. Nous ne retirons que ce que nous avons
ajouté. API vérifiée dans le bytecode 1.128 (BuffComponent.add_buff/remove_buff_by_type/has_buff/get_mood/__iter__,
SimInfo.Buffs). Une seule requête en vol, jamais d'exception vers le jeu. NOT_TESTED_GAME.
"""
import re
import time
import uuid

from .journal import log

EVENT = 'game.mood'
MAX_BUFFS = 32
MAX_PLAYERS = 16
EXPORT_SECONDS = 5.0     # Relevé de mon humeur ; le serveur refuse un changement plus fréquent que toutes les 4 s.
LIST_SECONDS = 60.0      # Liste redemandée : rattrape un événement perdu pendant une coupure.
RETRY_SECONDS = 30.0
ACK_SECONDS = 15.0
APPLY_SECONDS = 2.0
SOFT_CODES = ('mood_rate_limited', 'game_not_joined', 'storage_unavailable', 'request_limit')
# Pensées qui peuvent tuer ou déclencher une conséquence : jamais rejouées sur une copie.
DANGEROUS = ('hysteri', 'enraged', 'mortified', 'death', 'dying', 'electrocut', 'freez', 'overheat', 'starv')
_NATIVE = re.compile(r'^[0-9]{1,20}$')


def native_id(value):
    return isinstance(value, str) and _NATIVE.match(value) is not None


def replayable(buff_type):
    """Vrai si ajouter ce buff à une copie n'a pas d'effet de bord connu."""
    if buff_type is None or not getattr(buff_type, 'visible', False) or getattr(buff_type, 'mood_type', None) is None:
        return False
    for name in ('_loot_on_addition', '_loot_on_removal', '_add_buff_on_remove', '_owning_commodity'):
        if getattr(buff_type, name, None):
            return False
    name = str(getattr(buff_type, '__name__', '') or '').lower()
    return not any(word in name for word in DANGEROUS)


class GameMoods:
    """Accès au jeu. Chaque méthode rend None en cas d'échec, sans lever."""

    def active(self):
        try:
            import services
            sim_info = services.active_sim_info()
            return str(sim_info.sim_id) if sim_info is not None else None
        except Exception as error:
            log('mood_active', type(error).__name__)
            return None

    def export(self, sim_id):
        """(humeur, [pensées]) du Sim `sim_id`, ou None. Lecture seule."""
        try:
            import services
            sim_info = services.sim_info_manager().get(int(sim_id))
            component = sim_info.Buffs if sim_info is not None else None
            if component is None:
                return None
            buffs = set()
            for buff in component:
                buff_type = getattr(buff, 'buff_type', None) or type(buff)
                guid = getattr(buff_type, 'guid64', None)
                if getattr(buff_type, 'visible', False) and getattr(buff_type, 'mood_type', None) is not None and guid:
                    buffs.add(str(int(guid)))
            mood = component.get_mood()
            return str(int(getattr(mood, 'guid64', 0) or 0)), sorted(buffs)[:MAX_BUFFS]
        except Exception as error:
            log('mood_export', type(error).__name__)
            return None

    def apply(self, sim_id, wanted, owned):
        """Pose `wanted` (guid) sur la copie `sim_id`. Rend l'ensemble des pensées que nous y avons posées, ou None."""
        try:
            import services
            import sims4.resources
            sim_info = services.sim_info_manager().get(int(sim_id))
            component = sim_info.Buffs if sim_info is not None else None
            if component is None:
                return None
            tuning = services.get_instance_manager(sims4.resources.Types.BUFF)
            types = {}
            for guid in wanted:
                buff_type = tuning.get(int(guid))
                if replayable(buff_type):
                    types[guid] = buff_type
            kept = set()
            for guid in owned:
                if guid in types:
                    continue
                buff_type = tuning.get(int(guid))
                if buff_type is not None and component.has_buff(buff_type):
                    component.remove_buff_by_type(buff_type, on_destroy=False)
            for guid, buff_type in sorted(types.items()):
                if component.has_buff(buff_type):
                    if guid in owned:
                        kept.add(guid)
                    # Sinon : pensée native de la copie, elle ne nous appartient pas.
                    continue
                component.add_buff(buff_type, apply_buff_loot=False, remove_on_zone_unload=True)
                if component.has_buff(buff_type):
                    kept.add(guid)
            return kept
        except Exception as error:
            log('mood_apply', type(error).__name__)
            return None


class MoodSync:
    """Synchro sociale d'AutoLink : tick(manager, character), on_messages(manager, messages).

    `remote_sims()` : validated_remote_sims(...) = {(foyer local, Sim local): (joueur, Sim distant)}.
    """

    def __init__(self, game=None, remote_sims=lambda: {}, clock=time.monotonic, new_id=lambda: str(uuid.uuid4())):
        self.game = game if game is not None else GameMoods()
        self.remote_sims = remote_sims
        self.clock = clock
        self.new_id = new_id
        self.session = None
        self.owned = {}       # Sim local -> pensées posées par nous. Gardé d'une session à l'autre pour les retirer.
        self.disabled = False
        self.reset()

    def reset(self):
        self.remote = {}      # joueur -> {'simId', 'mood', 'buffs'}
        self.waiting = None
        self.waiting_at = 0.0
        self.listed = False
        self.list_at = 0.0
        self.export_at = 0.0
        self.apply_at = 0.0
        self.cooldown_until = 0.0
        self.published = None
        self.applied = {}     # Sim local -> (joueur, pensées voulues)

    def own_player(self, manager):
        identity = getattr(manager, 'identity', None)
        return identity.get('playerId') if isinstance(identity, dict) else None

    def _same_session(self, manager):
        session = (getattr(manager, 'endpoint', None), self.own_player(manager))
        if session == self.session:
            return True
        if self.session is not None:
            log('mood', 'session_changed')
        self.reset()
        self.session = session
        return False

    def tick(self, manager, character):
        self._same_session(manager)
        if self.disabled or not isinstance(character, dict) or character.get('status') != 'bound':
            return None
        now = self.clock()
        self._apply(now)
        if self.waiting is not None:
            if now - self.waiting_at < ACK_SECONDS:
                return None
            log('mood', 'ack_timeout:' + self.waiting[1])
            self.waiting = None
        if now < self.cooldown_until:
            return None
        if not self.listed or now >= self.list_at:
            return self._send(manager, 'game.mood.list', {}, now)
        if now < self.export_at:
            return None
        self.export_at = now + EXPORT_SECONDS
        sim_id = self.game.active()
        exported = self.game.export(sim_id) if sim_id else None
        if exported is None:
            return None
        mood, buffs = exported
        payload = {'simId': sim_id, 'mood': mood, 'buffs': list(buffs)}
        if self.published == (sim_id, mood, tuple(buffs)):
            return None
        return self._send(manager, 'game.mood.publish', payload, now)

    def _send(self, manager, kind, payload, now):
        request_id = self.new_id()
        manager.request(kind, payload, request_id)
        self.waiting = (request_id, kind, payload)
        self.waiting_at = now
        return kind

    def on_messages(self, manager, messages):
        if not self._same_session(manager):
            return
        me = self.own_player(manager)
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict):
                continue
            if message.get('id') is None and message.get('type') == EVENT:
                if self.listed:
                    self._store(message.get('payload'), me)
            elif self.waiting is not None and message.get('id') == self.waiting[0]:
                self._reply(message, me)

    def _reply(self, message, me):
        _, kind, payload = self.waiting
        self.waiting = None
        now = self.clock()
        if message.get('ok') is not True:
            code = message.get('code') if isinstance(message.get('code'), str) else 'error'
            log('mood', kind + ':' + code)
            if code == 'unknown_type':
                # Serveur sans humeurs communes (avant 0.1.50) : plus aucune requête.
                self.disabled = True
                return
            self.cooldown_until = now + RETRY_SECONDS
            if kind == 'game.mood.publish' and code not in SOFT_CODES:
                # Refus durable (Sim non lié, forme) : pas de renvoi en boucle du même état.
                self.published = (payload['simId'], payload['mood'], tuple(payload['buffs']))
            return
        data = message.get('payload')
        if kind == 'game.mood.list':
            self.listed = True
            self.list_at = now + LIST_SECONDS
            entries = data.get('moods') if isinstance(data, dict) else None
            self.remote = {}
            for entry in entries if isinstance(entries, list) else []:
                self._store(entry, me)
        else:
            self.published = (payload['simId'], payload['mood'], tuple(payload['buffs']))

    def _store(self, payload, me):
        if not isinstance(payload, dict) or set(payload) != {'playerId', 'simId', 'mood', 'buffs'}:
            log('mood', 'event_shape')
            return
        player, buffs = payload['playerId'], payload['buffs']
        if not isinstance(player, str) or not 0 < len(player) <= 128 or player == me:
            return
        if (not native_id(payload['simId']) or not native_id(payload['mood']) or not isinstance(buffs, list)
                or len(buffs) > MAX_BUFFS or not all(native_id(b) for b in buffs)):
            log('mood', 'event_values')
            return
        if player not in self.remote and len(self.remote) >= MAX_PLAYERS:
            return
        self.remote[player] = {'simId': payload['simId'], 'mood': payload['mood'], 'buffs': frozenset(buffs)}

    def _apply(self, now):
        if now < self.apply_at:
            return
        self.apply_at = now + APPLY_SECONDS
        try:
            links = self.remote_sims() or {}
        except Exception as error:
            log('mood_links', type(error).__name__)
            links = {}
        targets = {}
        for local, remote in links.items():
            if type(local) is not tuple or len(local) != 2 or type(remote) is not tuple or len(remote) != 2:
                continue
            player, remote_sim = remote
            known = self.remote.get(player)
            if known is not None and known['simId'] == remote_sim:
                targets[local[1]] = (player, known['buffs'])
        # Copie qui n'est plus reliée (joueur parti, Sim changé) : on retire nos pensées.
        for sim_id in [s for s in self.owned if s not in targets]:
            if self.game.apply(sim_id, frozenset(), self.owned[sim_id]) is not None:
                del self.owned[sim_id]
            self.applied.pop(sim_id, None)
        for sim_id, wanted in sorted(targets.items()):
            if self.applied.get(sim_id) == wanted:
                continue
            kept = self.game.apply(sim_id, wanted[1], self.owned.get(sim_id, set()))
            if kept is None:
                continue
            self.applied[sim_id] = wanted
            if kept:
                self.owned[sim_id] = set(kept)
            else:
                self.owned.pop(sim_id, None)

    def status(self):
        """Diagnostic sans identifiant de pensée."""
        return {'listed': self.listed, 'published': self.published is not None, 'disabled': self.disabled,
                'waiting': self.waiting[1] if self.waiting else None, 'remote': sorted(self.remote),
                'copies': len(self.applied), 'owned': sum(len(v) for v in self.owned.values()),
                'gameReadiness': 'NOT_TESTED_GAME'}
