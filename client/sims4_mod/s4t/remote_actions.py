"""Rejeu borné d'assise/TV observée, sur le seul proxy prouvé du joueur émetteur.

Entrée exclusivement issue d'InteractionPublisher (session/zone/flux serveur validés).
Aucune interaction sociale, invitation, téléportation ou action sur un Sim jouable.
API 1.128 inspectée statiquement ; exécution native NOT_TESTED_GAME.
"""
from collections import OrderedDict

from .journal import log

ENDING = frozenset(('finished', 'user_canceled', 'canceled', 'failed', 'reset', 'unknown', 'unobserved'))


class RemoteActions:
    def __init__(self, motion, adapter=None):
        self.motion = motion
        self.adapter = adapter if adapter is not None else GameActions(motion.adapter)
        self.seen, self.active, self.pending = OrderedDict(), {}, {}
        self.started = self.ended = self.refused = 0

    def _actor(self, observation):
        motion, event = self.motion, observation['event']
        player = observation['playerId']
        if (player == motion.own or player not in motion.participants or observation['zoneId'] != motion.zone_id
                or motion.streams.get(player) != observation['streamId']):
            return None
        key = (player, event['actorSimId'])
        mapped, record = motion.mapping.get(key), motion.owned.get(player)
        if mapped is None or record is None or mapped != (record['household'], record['sim']):
            return None
        instance = record['instance']()
        pose = motion.poses.get(key)
        return (player, record['household'], instance, pose) if instance is not None and pose is not None else None

    def _stop(self, player):
        item = self.active.get(player)
        if item is not None:
            try:
                self.adapter.stop(item['actor'], item['handle'])
                self.ended += 1
            except Exception as error:
                log('remote_actions', 'stop:' + type(error).__name__)
                return False  # Ne jamais superposer une autre action à un handle dont la fin est incertaine.
            self.active.pop(player, None)
        return True

    def tick(self, observations):
        for player, item in list(self.active.items()):
            actor = self._actor(item['observation'])
            if actor is None or actor[2] is not item['actor'][2]:
                self._stop(player)
        for observation in list(observations)[-8:]:
            event = observation['event']
            key = (observation['playerId'], observation['streamId'], event['streamId'], event['interactionId'])
            if observation['revision'] <= self.seen.get(key, 0):
                continue
            player = observation['playerId']
            actor = self._actor(observation)
            if actor is None and event['state'] not in ENDING:
                # L'apparence ou le mobilier peut arriver après l'événement. La même observation reste
                # réessayable tant que son flux est confirmé ; une fin plus récente la condamne ci-dessous.
                self.refused += 1
                continue
            self.seen[key] = observation['revision']
            self.seen.move_to_end(key)
            while len(self.seen) > 128:
                self.seen.popitem(last=False)
            if event['state'] in ENDING:
                self.pending.pop(key, None)
                if self.active.get(player, {}).get('key') == key:
                    self._stop(player)
                continue
            if actor is None:
                continue
            if (event['state'] != 'running' or event['origin'] != 'user' or event['visible'] is not True
                    or event['target']['kind'] != 'object'):
                continue
            if self.active.get(player, {}).get('key') != key:
                for old in list(self.pending):
                    if old[0] == player:
                        del self.pending[old]
                if len(self.pending) >= 64:
                    self.refused += 1
                    continue
                self.pending[key] = (observation, 0)
        # Un appel natif maximum par tick, aucun événement social rejoué.
        for key, (observation, attempts) in list(self.pending.items()):
            actor = self._actor(observation)
            if actor is None:
                del self.pending[key]
                continue
            if not self._stop(actor[0]):
                break
            try:
                handle = self.adapter.start(actor, observation['event'])
            except Exception as error:
                handle = None
                log('remote_actions', 'start:' + type(error).__name__)
            if handle is not None:
                self.active[actor[0]] = {'key': key, 'actor': actor, 'handle': handle, 'observation': observation}
                del self.pending[key]
                self.started += 1
            elif attempts >= 4:
                del self.pending[key]
                self.refused += 1
            else:
                self.pending[key] = (observation, attempts + 1)
            break

    def reset(self):
        for player in list(self.active):
            self._stop(player)
        self.seen.clear()
        self.pending.clear()

    def status(self):
        return {'started': self.started, 'ended': self.ended, 'refused': self.refused,
                'active': len(self.active), 'pending': len(self.pending), 'scope': 'seated_and_tv_object_only',
                'gameReadiness': 'NOT_TESTED_GAME'}


class GameActions:
    def __init__(self, motion):
        self.motion = motion

    def start(self, actor, event):
        import math
        import services
        import sims4.resources
        from interactions.context import InteractionContext
        from interactions.priority import Priority
        player, household, instance, pose = actor
        sim = self.motion._exact(player, household, instance)
        # Le tuning reçu n'est jamais exécuté librement : posture assise ou classe TV native.
        affordance = services.get_instance_manager(sims4.resources.Types.INTERACTION).get(int(event['affordanceId']))
        posture = getattr(affordance, 'provided_posture_type', None)
        seated = posture is not None and getattr(posture, 'name', None) == 'sit'
        television = False
        if not seated and isinstance(affordance, type):
            try:
                from objects.electronics.television import WatchSuperInteraction
                television = issubclass(affordance, WatchSuperInteraction)
            except ImportError:
                pass
        target_info = event['target']
        placement = target_info.get('placement')
        if (not seated and not television) or (television and placement is None):
            return None
        if placement is not None and not self._valid_placement(placement):
            return None
        context = InteractionContext(sim, InteractionContext.SOURCE_SCRIPT_WITH_USER_INTENT, Priority.High)
        candidates, count = [], 0
        for obj in services.object_manager().valid_objects():
            count += 1
            if count > 1000:
                return None
            if (getattr(obj, 'is_sim', True) or not obj.is_on_active_lot()
                    or str(obj.definition.id) != target_info['definitionId'] or not obj.supports_affordance(affordance)):
                continue
            targets = tuple(obj.parts or ()) if target_info['part'] else (obj,)
            if len(targets) > 64:
                return None
            for target in targets:
                level = placement['level'] if placement is not None else pose.get('level')
                if target.routing_surface.secondary_id != level:
                    continue
                pos = target.position
                origin = {name: placement[name] / 1000. for name in ('x', 'y', 'z')} if placement is not None else pose['position']
                distance = math.hypot(pos.x - origin['x'], pos.z - origin['z'])
                tolerance = .02 if placement is not None else 1.5
                if distance <= tolerance and abs(pos.y - origin['y']) <= tolerance:
                    candidates.append((distance, target))
        candidates.sort(key=lambda item: item[0])
        if not candidates or len(candidates) > 1 and (placement is not None or candidates[1][0] - candidates[0][0] < .25):
            return None  # Siège absent ou ambigu : aucun choix arbitraire.
        result = sim.push_super_affordance(affordance, candidates[0][1], context)
        return getattr(result, 'interaction', None) if result else None

    @staticmethod
    def _valid_placement(value):
        return (isinstance(value, dict) and set(value) == {'x', 'y', 'z', 'level'}
                and all(type(value[name]) is int and -1000000000 <= value[name] <= 1000000000
                        for name in ('x', 'y', 'z'))
                and type(value['level']) is int and -8 <= value['level'] <= 16)

    def stop(self, actor, interaction):
        from interactions.interaction_finisher import FinishingType
        self.motion._exact(actor[0], actor[1], actor[2])
        interaction.cancel(FinishingType.USER_CANCEL, cancel_reason_msg='S4Together: owner action ended')
