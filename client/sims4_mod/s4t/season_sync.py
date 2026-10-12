"""Saison initiale commune (#138) appliquée au jeu, une seule fois par monde serveur et par foyer.

Le serveur reste l'autorité : le mod n'applique que l'état « chosen » reçu (snapshot ou diffusion
season.state / season.chosen). Ensuite la saison avance normalement dans la partie du joueur.

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
_WORLD_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


def chosen_season(state):
    """Nom serveur de la saison choisie, ou None si l'état n'est pas un « chosen » v1 valide."""
    if not isinstance(state, dict) or state.get('v') != 1 or state.get('status') != 'chosen':
        return None
    season = state.get('season')
    return season if season in GAME_SEASONS else None


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

    def update(self, world_id, state):
        """Résultat (chaîne) ou None si rien à faire. Jamais d'exception vers la boucle du jeu."""
        season = chosen_season(state)
        if season is None or not isinstance(world_id, str) or not _WORLD_ID.match(world_id):
            return None
        household = self.household()
        if household is None:
            return None
        key = world_id + '/' + household
        # Un nouveau choix après réinitialisation admin (autre chosenAt) est appliqué à nouveau.
        chosen_at = state.get('chosenAt')
        token = season + '@' + (chosen_at if isinstance(chosen_at, str) else '')
        if self.store.season_applied(key) == token:
            return None
        current = self.current()
        if current is None:
            # Pas de Saisons dans cette partie : on ne marque rien, le pack peut être ajouté plus tard.
            log('season', 'pack_unavailable')
            return 'pack_unavailable'
        # Marquée avant l'action : un échec ne doit jamais réappliquer la saison en boucle.
        self.store.mark_season_applied(key, token)
        if current == GAME_SEASONS[season]:
            log('season', 'already_' + season)
            return 'already'
        applied = self.apply_season(season)
        log('season', ('applied_' + season) if applied else 'pack_unavailable')
        return 'applied' if applied else 'pack_unavailable'
