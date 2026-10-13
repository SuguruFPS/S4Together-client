"""Saison commune (#138) : la partie est ramenée sur la saison courante du serveur à chaque écart (13/10/2026).

Le serveur reste l'autorité : le mod n'applique que l'état « chosen » reçu (snapshot ou diffusion
season.state / season.chosen). `current` est la saison courante, qui avance sur l'horloge du serveur ; `season`
(le choix initial) ne sert que si un ancien serveur n'annonce pas `current`. L'heure du jour et la vitesse restent
celles de chaque partie : quand le calendrier local change de saison tout seul, il est réaligné au contrôle suivant.

API vérifiée par désassemblage de simulation.zip (1.128) :
services.season_service() -> getattr(service_manager, 'season_service', None) (None sans EP05) ;
SeasonService.season (SeasonType) ; SeasonService.reset_region_season_params() ;
SeasonService.set_season(season_type, source, interp_time) ; services.weather_service().reset_forecasts() ;
seasons.seasons_enums.SeasonType SUMMER=0 FALL=1 WINTER=2 SPRING=3, SeasonSetSource.CHEAT.
Même séquence que la commande native seasons.set_season (seasons/season_commands.py).
"""
import re

from .journal import log

GAME_SEASONS = {'summer': 'SUMMER', 'autumn': 'FALL', 'winter': 'WINTER', 'spring': 'SPRING'}
# update() est appelé une fois par seconde : délais entre deux lectures après une application ou un échec.
RETRY_CALLS = 10
FAILED_CALLS = 300
_WORLD_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


def chosen_season(state):
    """Nom serveur de la saison choisie, ou None si l'état n'est pas un « chosen » v1 valide."""
    if not isinstance(state, dict) or state.get('v') != 1 or state.get('status') != 'chosen':
        return None
    season = state.get('season')
    if season not in GAME_SEASONS:
        return None
    current = state.get('current')
    return current if current in GAME_SEASONS else season


def game_current():
    """Nom du SeasonType courant, ou None sans pack Saisons."""
    import services
    service = services.season_service()
    if service is None:
        return None
    current = service.season
    return getattr(current, 'name', None)


def game_apply(season):
    import services
    from seasons.seasons_enums import SeasonType, SeasonSetSource
    service = services.season_service()
    if service is None:
        return False
    service.reset_region_season_params()
    service.set_season(getattr(SeasonType, GAME_SEASONS[season]), SeasonSetSource.CHEAT, None)
    weather = services.weather_service()
    if weather is not None:
        weather.reset_forecasts()
    return True


def game_household_id():
    import services
    value = services.active_household_id()
    return str(value) if isinstance(value, int) and value > 0 else None


class SeasonSync:
    def __init__(self, store, current=game_current, apply=game_apply, household=game_household_id):
        self.store = store
        self.current = current
        self.apply_season = apply
        self.household = household
        self.wait = 0
        self.failed = None

    def update(self, world_id, state):
        """Résultat (chaîne) ou None si rien à faire. Jamais d'exception vers la boucle du jeu."""
        season = chosen_season(state)
        if season is None or not isinstance(world_id, str) or not _WORLD_ID.match(world_id):
            return None
        household = self.household()
        if household is None:
            return None
        if self.wait > 0:
            self.wait -= 1
            return None
        key = world_id + '/' + household
        current = self.current()
        if current is None:
            # Pas de Saisons dans cette partie : relu de temps en temps, le pack peut être ajouté plus tard.
            self.wait = RETRY_CALLS
            log('season', 'pack_unavailable')
            return 'pack_unavailable'
        if current == GAME_SEASONS[season]:
            self.failed = None
            if self.store.season_applied(key) != season:
                self.store.mark_season_applied(key, season)
                log('season', 'already_' + season)
                return 'already'
            return None
        # Écart avec le serveur. Une application qui n'a rien changé n'est pas rejouée à chaque seconde.
        self.wait = RETRY_CALLS
        if self.failed == (key, season):
            self.wait = FAILED_CALLS
        applied = self.apply_season(season)
        self.failed = (key, season)
        if applied:
            self.store.mark_season_applied(key, season)
        log('season', ('applied_' + season) if applied else 'pack_unavailable')
        return 'applied' if applied else 'pack_unavailable'
