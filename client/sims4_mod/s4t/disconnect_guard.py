"""Pause de sécurité de la partie multijoueur liée quand le pont authentifié disparaît.

APIs 1.128 vérifiées dans clock.py : push_speed retourne la SpeedRequest ; le contrôleur est une
liste de requêtes. On retire uniquement CETTE instance, jamais pop_speed qui pourrait retirer
la pause d'un autre mod ou du jeu. Aucune sauvegarde .save n'est ouverte ou remplacée.
"""
from .journal import log


def game_context():
    import services
    from clock import ClockSpeedMode, GameSpeedChangeSource
    household = services.active_household()
    return (services.game_clock_service(), str(household.id) if household is not None else None,
            ClockSpeedMode.PAUSED, GameSpeedChangeSource.UI_MODAL)


class DisconnectGuard:
    def __init__(self, context=game_context, notify=None):
        self.context, self.notify = context, notify
        self.household = None
        self.clock, self.request, self.source = None, None, None
        self.blocked = False

    def _release(self):
        clock, request, source = self.clock, self.request, self.source
        self.blocked = False
        if clock is not None and request is not None:
            controller = clock.speed_controllers[source]
            for index, item in enumerate(controller):
                if item is request:
                    del controller[index]
                    clock._update_speed(immediate=True)
                    break
        self.clock, self.request, self.source = None, None, None

    def update(self, authenticated, loaded, character, joined):
        try:
            clock, household, paused, source = self.context()
            binding = character.get('binding') if isinstance(character, dict) else None
            ready = (authenticated is True and joined is True and isinstance(binding, dict)
                     and binding.get('gameHouseholdId') == household and character.get('status') == 'bound')
            if ready:
                self._release()
                self.household = household
                return False
            if authenticated is True and not self.blocked:
                return False  # Un voyage ordinaire et une première liaison ne sont pas une coupure.
            if not loaded or self.household is None or household != self.household or clock is None:
                self._release()
                if loaded and household != self.household:
                    self.household = None
                return False
            if self.clock is not clock:
                self._release()
            controller = clock.speed_controllers[source]
            if self.request is None or not any(item is self.request for item in controller):
                self.blocked = True
                self.clock, self.source = clock, source
                self.request = clock.push_speed(paused, source=source, reason='S4TogetherNetworkSafety',
                                                validity_check=lambda: self.blocked, immediate=True)
                log('network_safety', 'paused')
                if self.notify is not None:
                    self.notify('S4Together', 'Connexion interrompue : partie multijoueur en pause. Reconnecte le launcher pour reprendre depuis le serveur.')
            else:
                clock._update_speed(immediate=True)
            return True
        except Exception as error:
            log('network_safety', type(error).__name__)
            return False

    def reset(self):
        try:
            self._release()
        finally:
            self.household = None
