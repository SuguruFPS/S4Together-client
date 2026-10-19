"""Pause tant que le foyer actif est la copie S4T d'un autre joueur : on ne joue jamais le personnage d'un autre.

La copie reste dans la sauvegarde (aucun Sim supprimé, aucun fichier .save touché) ; rien n'en part vers le
serveur (CharacterSync ne lie ni ne synchronise une copie). La pause empêche seulement de la faire vivre ici.
Même mécanisme que disconnect_guard (clock.py 1.128) : on ne retire que NOTRE requête de vitesse, jamais pop_speed.
"""
from .journal import log

TEXT = ("Ce foyer est la copie S4T du personnage d'un autre joueur : tu ne peux pas le jouer. "
        "Partie en pause. Ouvre « Gérer les mondes » et charge ton propre foyer.")


def game_context():
    import services
    from clock import ClockSpeedMode, GameSpeedChangeSource
    from .character_sync import _is_s4t_copy
    household = services.active_household()
    copy = household is not None and _is_s4t_copy(household)
    return (services.game_clock_service(), str(household.id) if copy else None,
            ClockSpeedMode.PAUSED, GameSpeedChangeSource.UI_MODAL)


class CopyLock:
    def __init__(self, context=game_context, notify=None):
        self.context, self.notify = context, notify
        self.clock, self.request, self.source = None, None, None
        self.locked = None  # Identifiant du foyer copie verrouillé, ou None.

    def _release(self):
        clock, request, source = self.clock, self.request, self.source
        self.locked = None
        if clock is not None and request is not None:
            controller = clock.speed_controllers[source]
            for index, item in enumerate(controller):
                if item is request:
                    del controller[index]
                    clock._update_speed(immediate=True)
                    break
        self.clock, self.request, self.source = None, None, None

    def update(self, loaded):
        """Vrai si la partie est tenue en pause ; jamais d'exception vers l'appelant."""
        try:
            if not loaded:
                self._release()
                return False
            clock, copy, paused, source = self.context()
            if copy is None or clock is None:
                if self.locked is not None:
                    log('copy_lock', 'released')
                self._release()
                return False
            if self.clock is not clock or self.locked != copy:
                self._release()
            self.locked = copy
            controller = clock.speed_controllers[source]
            if self.request is None or not any(item is self.request for item in controller):
                first = self.request is None
                self.clock, self.source = clock, source
                self.request = clock.push_speed(paused, source=source, reason='S4TogetherForeignCopy',
                                                validity_check=lambda: self.locked is not None, immediate=True)
                if first:
                    log('copy_lock', 'paused')
                    if self.notify is not None:
                        self.notify('S4Together', TEXT)
            else:
                clock._update_speed(immediate=True)
            return True
        except Exception as error:
            log('copy_lock', type(error).__name__)
            return False

    def reset(self):
        self._release()
