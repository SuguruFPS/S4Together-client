"""Jour de saison commun : le début de la saison locale est décalé de jours entiers pour afficher le même jour
que le serveur (13/10/2026). L'horloge du jeu (sim_now), l'heure du jour et la vitesse ne sont jamais modifiées.

Le serveur reste l'autorité : l'état « chosen » annonce `progressPermille` (0..999, part écoulée de la saison
courante) et `seasonDays`. La saison elle-même reste l'affaire de season_sync ; ce module n'agit que lorsque la
saison du jeu est déjà celle du serveur.

API vérifiée par désassemblage de simulation.zip et generated.zip (Python 3.7) — voir docs/SEASON_PHASE_NATIVE.md :
services.season_service() (None sans EP05) ; SeasonService.season, .season_content (Season, None avant le
chargement de zone), .season_length (TimeSpan) ; Season.start_time ; services.time_service().sim_now ;
DateAndTime(ticks), DateAndTime.absolute_ticks() ; TimeSpan.in_ticks() ; date_and_time.sim_ticks_per_day().
Application : même séquence que la commande native seasons.shift_season_by_weeks (seasons/season_commands.py),
avec un début au jour près : reset_region_season_params(), _set_season_start_time(start) (interne, seul moyen de
poser un début quelconque), weather_service().reset_forecasts(), handle_season_content_updated().
"""
from .journal import log
from .season_sync import GAME_SEASONS

# Part de jour sous laquelle deux jours affichés différents sont tolérés (jeu et serveur de part et d'autre
# d'un changement de jour) : sans elle, chaque minuit local et chaque changement de jour serveur recalerait.
TOLERANCE_DAY_FRACTION = 0
# update() est appelé une fois par seconde : délais avant la lecture suivante.
SETTLE_CALLS = 5
RETRY_CALLS = 10
FAILED_CALLS = 300
MAX_SEASON_DAYS = 28


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def chosen_phase(state):
    """(saison serveur, progressPermille, seasonDays), ou None si l'état n'annonce pas de phase valide."""
    if not isinstance(state, dict) or state.get('v') != 1 or state.get('status') != 'chosen':
        return None
    season = state.get('season')
    if season not in GAME_SEASONS:
        return None
    current = state.get('current')
    permille = state.get('progressPermille')
    days = state.get('seasonDays')
    if not _whole(permille) or not 0 <= permille <= 999 or not _whole(days) or not 1 <= days <= MAX_SEASON_DAYS:
        return None
    return (current if current in GAME_SEASONS else season), permille, days


def plan(now, start, length, ticks_per_day, permille):
    """Décision pure, en ticks du jeu : ('aligned' | 'near' | 'apply' | 'out_of_range' | 'invalid', début).

    La progression serveur est rapportée à la durée locale de la saison ; seul l'indice du jour est imposé.
    """
    values = (now, start, length, ticks_per_day, permille)
    if not all(_whole(v) for v in values) or now < 0 or start < 0 or length <= 0 or ticks_per_day <= 0 \
            or not 0 <= permille <= 999:
        return 'invalid', None
    server = permille * length // 1000
    local = now - start
    day = server // ticks_per_day
    if local // ticks_per_day == day:
        return 'aligned', None
    if abs(local - server) < TOLERANCE_DAY_FRACTION * ticks_per_day:
        return 'near', None
    new_start = now - now % ticks_per_day - day * ticks_per_day
    if new_start < 0:
        # season_start_time est un uint64 (UI_pb2.SeasonUpdate) : partie trop jeune pour reculer le début.
        return 'out_of_range', None
    return 'apply', new_start


def game_read():
    """Relevé natif en ticks, ou None sans pack Saisons ou avant le chargement de la zone."""
    import services
    import date_and_time
    service = services.season_service()
    if service is None:
        return None
    content = service.season_content
    length = service.season_length
    if content is None or length is None:
        return None
    return {'season': getattr(service.season, 'name', None),
            'now': services.time_service().sim_now.absolute_ticks(),
            'start': content.start_time.absolute_ticks(),
            'length': length.in_ticks(),
            'ticksPerDay': date_and_time.sim_ticks_per_day()}


def game_apply(start):
    import services
    from date_and_time import DateAndTime
    service = services.season_service()
    if service is None:
        return False
    service.reset_region_season_params()
    service._set_season_start_time(DateAndTime(start))
    weather = services.weather_service()
    if weather is not None:
        weather.reset_forecasts()
    service.handle_season_content_updated()
    return True


class SeasonPhaseSync:
    def __init__(self, read=game_read, apply=game_apply):
        self.read = read
        self.apply_start = apply
        self.wait = 0
        self.failed = None
        self.mismatch = None

    def update(self, state):
        """Résultat (chaîne) ou None si rien à faire. Jamais d'exception vers la boucle du jeu."""
        phase = chosen_phase(state)
        if phase is None:
            return None
        if self.wait > 0:
            self.wait -= 1
            return None
        try:
            return self._update(*phase)
        except Exception as error:
            # API interne du jeu : un patch peut la retirer. Journalisé, puis espacé.
            self.wait = FAILED_CALLS
            log('season_phase', 'error_' + type(error).__name__)
            return 'error'

    def _update(self, season, permille, days):
        snapshot = self.read()
        if snapshot is None:
            self.wait = RETRY_CALLS
            log('season_phase', 'pack_unavailable')
            return 'pack_unavailable'
        if snapshot.get('season') != GAME_SEASONS[season]:
            # season_sync pose d'abord la saison du serveur ; le jour est aligné ensuite.
            return None
        now, start = snapshot.get('now'), snapshot.get('start')
        length, ticks_per_day = snapshot.get('length'), snapshot.get('ticksPerDay')
        action, new_start = plan(now, start, length, ticks_per_day, permille)
        if action in ('aligned', 'near'):
            self.failed = None
            return None
        if action != 'apply':
            self.wait = FAILED_CALLS
            log('season_phase', action)
            return action
        local_days = length // ticks_per_day
        if local_days != days and self.mismatch != (local_days, days):
            self.mismatch = (local_days, days)
            log('season_phase', 'length_mismatch_%d_%d' % (local_days, days))
        # Une application qui n'a rien changé n'est pas rejouée à chaque délai de stabilisation.
        target = (season, new_start)
        self.wait = FAILED_CALLS if self.failed == target else SETTLE_CALLS
        applied = self.apply_start(new_start)
        self.failed = target
        day = (now - new_start) // ticks_per_day + 1
        log('season_phase', ('applied_day_%d_of_%d' % (day, local_days)) if applied else 'pack_unavailable')
        return 'applied' if applied else 'pack_unavailable'
