"""Autonomie de nos propres Sims pendant une visite chez un autre joueur.

Chez un autre joueur, nos Sims n'agissent pas seuls : le joueur les dirige. Après AFK_SECONDS sans
nouvelle action dirigée, l'autonomie revient pour qu'un Sim laissé seul ne meure pas de faim.
Hors visite, ou quand la partie n'est pas liée, rien n'est bloqué.
API 1.128 inspectée statiquement ; comportement en jeu NOT_TESTED_GAME.
"""
import time

AFK_SECONDS = 300


class IdlePolicy:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self):
        self.visiting = False
        self.last_input = None
        self.seen = frozenset()

    def update(self, visiting, user_ids):
        """`user_ids` : identifiants locaux des actions dirigées par le joueur en cours."""
        now = self.clock()
        current = frozenset(user_ids or ())
        if visiting and not self.visiting:
            self.last_input = now  # Arriver chez quelqu'un est une action du joueur.
        elif current - self.seen:
            self.last_input = now
        self.visiting, self.seen = bool(visiting), current

    def afk(self):
        return self.last_input is not None and self.clock() - self.last_input >= AFK_SECONDS

    def blocks(self):
        return self.visiting and not self.afk()

    def status(self):
        return {'visiting': self.visiting, 'afk': self.afk(), 'blocking': self.blocks(),
                'gameReadiness': 'NOT_TESTED_GAME'}


POLICY = IdlePolicy()


def visiting_other_player(household_ids):
    """Vrai si le terrain chargé est le domicile d'un foyer S4T d'un autre joueur."""
    import services
    zone = services.current_zone_id()
    manager = services.household_manager()
    for value in household_ids:
        try:
            household = manager.get(int(value))
        except (TypeError, ValueError):
            continue
        if household is not None and zone and getattr(household, 'home_zone_id', None) == zone:
            return True
    return False
