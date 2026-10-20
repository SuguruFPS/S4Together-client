"""Vitesse native experimentale, active seulement apres presence et capacite serveur.

Une seule requete GAMEPLAY nous appartient. Les pauses UI/SITUATION restent prioritaires.
Aucun changement d'heure absolue, d'alarme, de besoin ou de sauvegarde.
"""
import time


def game_context():
    import services
    from clock import ClockSpeedMode, GameSpeedChangeSource
    return services.game_clock_service(), GameSpeedChangeSource.GAMEPLAY, ClockSpeedMode


_HOLDERS = []


def guard_set_speed(original):
    """Un clic de vitesse (set_clock_speed) vide la pile GAMEPLAY : reprendre aussitot notre maintien."""
    def set_clock_speed(clock, *args, **kwargs):
        result = original(clock, *args, **kwargs)
        for holder in tuple(_HOLDERS):
            try:
                holder.reassert(clock)
            except Exception as error:
                holder.failures += 1
                holder.last_error = type(error).__name__
        return result
    setattr(set_clock_speed, 's4t_original', original)
    return set_clock_speed


def install_guard(cls):
    original = cls.__dict__.get('set_clock_speed')
    if not callable(original) or getattr(original, 's4t_original', None) is not None:
        return False
    cls.set_clock_speed = guard_set_speed(original)
    return True


class GameSpeed:
    def __init__(self, context=game_context):
        self.context = context
        self.clock = self.source = self.request = self.rate = None
        self.active = False
        self.generation = 0
        self.failures = 0
        self.last_error = None
        if self not in _HOLDERS:
            _HOLDERS.append(self)

    def read(self):
        clock, _, _ = self.context()
        rate = int(clock.clock_speed)
        return rate if rate in (0, 1, 2, 3) else None

    def release(self):
        self.active = False
        self.generation += 1
        clock, request, source = self.clock, self.request, self.source
        self.clock = self.source = self.request = self.rate = None
        if clock is not None and request is not None:
            controller = clock.speed_controllers[source]
            for index, item in enumerate(controller):
                if item is request:
                    del controller[index]
                    clock._update_speed(immediate=True)
                    break

    def hold(self, rate):
        clock, source, mode = self.context()
        if clock is not self.clock or rate != self.rate:
            self.release()
        for cls in type(clock).__mro__:
            if 'set_clock_speed' in cls.__dict__:
                install_guard(cls)
                break
        if self.request is None:
            self.active = True
            generation = self.generation
            self.clock, self.source, self.rate = clock, source, rate
            self.request = clock.push_speed(mode(rate), source=source, reason='S4TogetherTerrainSpeed',
                                            validity_check=lambda: self.active and self.generation == generation, immediate=True)
        else:
            self.reassert(clock)

    def reassert(self, clock):
        """Remettre notre requete au sommet (clic visiteur, pile videe) ; ne retirer aucun autre mod."""
        if not self.active or self.request is None or clock is not self.clock:
            return False
        controller = clock.speed_controllers[self.source]
        if controller and controller[-1] is self.request:
            return False
        for index, item in enumerate(controller):
            if item is self.request:
                del controller[index]
                break
        controller.append(self.request)
        clock._update_speed(immediate=True)
        return True


class NativeSpeedSync:
    def __init__(self, read, hold, release, clock=time.monotonic):
        self.read, self.hold, self.release, self.clock = read, hold, release, clock
        self.context = self.pending = self.view = None
        self.disabled = False
        self.held = False
        self.next_get = 0
        self.revision_floor = 0

    def reset(self, manager=None):
        if self.pending is not None and manager is not None:
            manager.forget(self.pending[0])
        if self.held:
            self.release()
        self.context = self.pending = self.view = None
        self.disabled = self.held = False
        self.next_get = 0
        self.revision_floor = 0

    def _request(self, manager, kind, payload):
        self.pending = (manager.request(kind, payload), self.clock(), kind)

    def tick(self, manager, context):
        if getattr(manager, 'snapshot', {}).get('nativeSpeed') != 1:
            context = None
        if (context is not None and (not isinstance(context, tuple) or len(context) != 2
                or any(not isinstance(value, str) or not value for value in context))):
            context = None
        if context != self.context:
            self.reset(manager)
            self.context = context
        if context is None or self.disabled:
            return
        now = self.clock()
        if self.pending is not None and now - self.pending[1] >= 5:
            manager.forget(self.pending[0])
            self.pending = self.view = None
        if self.view is not None:
            if self.view['canControl'] and self.view.get('maxRate') == 1:
                # Un visiteur est sur le terrain : x1 impose aussi au proprietaire jusqu'a son depart.
                self.hold(1)
                self.held = True
            elif self.view['canControl']:
                if self.held:
                    self.release()
                    self.held = False
                rate = self.read()
                if type(rate) is int and rate in (0, 1, 2, 3) and rate != self.view['rate'] and self.pending is None:
                    self._request(manager, 'game.speed.set', {'streamId': context[1],
                        'expectedRevision': self.view['revision'], 'rate': rate})
            else:
                self.hold(self.view['rate'])
                self.held = True
        if self.pending is None and (self.view is None or now >= self.next_get):
            self._request(manager, 'game.speed.get', {'streamId': context[1]})

    def _valid(self, payload):
        return (self.context is not None and isinstance(payload, dict)
                and payload.get('zoneId') == self.context[0]
                and type(payload.get('revision')) is int and 0 <= payload['revision'] <= 9007199254740991
                and type(payload.get('rate')) is int and payload['rate'] in (0, 1, 2, 3)
                and (payload.get('controllerPlayerId') is None or isinstance(payload['controllerPlayerId'], str)))

    def on_messages(self, manager, messages):
        for message in messages:
            payload = message.get('payload')
            if message.get('type') == 'game.speed' and self._valid(payload):
                # Relire les droits depuis un ACK lie a ce flux, jamais les deduire d'un clone.
                if payload['revision'] >= self.revision_floor:
                    self.revision_floor = payload['revision']
                    self.view = None
                    if self.held:
                        self.hold(payload['rate'])
                continue
            if self.pending is None or message.get('id') != self.pending[0] or message.get('type') not in ('ack', 'error'):
                continue
            manager.forget(self.pending[0])
            self.pending = None
            if message.get('ok') is not True:
                self.view = None
                self.disabled = message.get('code') in ('unknown_type', 'speed_disabled')
                if self.disabled and self.held:
                    self.release()
                    self.held = False
                continue
            if self._valid(payload) and type(payload.get('canControl')) is bool:
                if payload['revision'] >= self.revision_floor:
                    self.revision_floor = payload['revision']
                    self.view = dict(payload)
                self.next_get = self.clock() + 2
