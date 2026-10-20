"""Météo commune (#166, 20/10/2026) : même prévision du jour et même suite d'événements météo pour tous les joueurs
d'un même quartier, tirées d'une graine dérivée du jour serveur. L'heure du jour reste locale à chaque partie.

Le serveur n'envoie rien de nouveau : le jour serveur se déduit de l'état de saison « chosen » déjà diffusé
(`chosenAt`, `period`, `dayIndex`). Le jeu tire ses prévisions et ses événements avec le module `random`
global (sims4.random.weighted_random_item / random.choice) : le mod fixe cette graine le temps d'un tirage, puis
rend l'état aléatoire d'origine au jeu.

API vérifiée par désassemblage de simulation.zip / core.zip (Python 3.7) :
services.weather_service() (None sans EP05) ; WeatherService._forecasts (liste de la région courante, propriété) ;
WeatherService._send_new_weather_event() (populate_forecasts(1) puis start_weather_event du forecast[0]) ;
WeatherService.populate_forecasts(num_days) ; WeatherService.update() tire une nouvelle prévision au changement de
jour local ; weather.weather_service.get_street_or_region_id_with_weather_tuning() ; forecast.guid64 ;
sims4.random importe le module `random` de la bibliothèque standard.
"""
import hashlib
import random

from .journal import log
from .season_sync import GAME_SEASONS

# update() est appelé une fois par seconde.
RETRY_CALLS = 10
FAILED_CALLS = 300
# Une partie ne recale pas la météo plus d'une fois par jour serveur et par prévision locale changée.
MAX_APPLIES_PER_DAY = 3

_STATE = {'day': None, 'events': 0}


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def server_day(state):
    """Clé stable du jour serveur, ou None si l'état de saison ne la donne pas."""
    if not isinstance(state, dict) or state.get('v') != 1 or state.get('status') != 'chosen':
        return None
    chosen_at, period, day = state.get('chosenAt'), state.get('period'), state.get('dayIndex')
    if state.get('season') not in GAME_SEASONS or not isinstance(chosen_at, str) or not chosen_at:
        return None
    if not _whole(period) or period < 0 or not _whole(day) or not 0 <= day <= 27:
        return None
    return '%s|%d|%d' % (chosen_at, period, day)


def seed_for(day, region, label):
    digest = hashlib.sha256(('s4t-weather|%s|%s|%s' % (day, region, label)).encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big')


def seeded(seed, call):
    """Exécute call() avec le module random fixé sur seed, puis rend l'état d'origine au jeu."""
    saved = random.getstate()
    random.seed(seed)
    try:
        return call()
    finally:
        random.setstate(saved)


def _region():
    from weather.weather_service import get_street_or_region_id_with_weather_tuning
    return get_street_or_region_id_with_weather_tuning()


def guard_new_event(original, region=_region):
    """Chaque nouvel événement météo du jour serveur est tiré avec une graine commune (jour, quartier, rang)."""
    def _send_new_weather_event(self, *args, **kwargs):
        day = _STATE['day']
        if day is None:
            return original(self, *args, **kwargs)
        try:
            place = region()
        except Exception:
            place = None
        _STATE['events'] += 1
        return seeded(seed_for(day, place, 'event-%d' % _STATE['events']), lambda: original(self, *args, **kwargs))
    _send_new_weather_event.s4t_original = original
    return _send_new_weather_event


def install_guard(cls):
    original = cls.__dict__.get('_send_new_weather_event')
    if not callable(original) or getattr(original, 's4t_original', None) is not None:
        return False
    cls._send_new_weather_event = guard_new_event(original)
    return True


def game_read():
    """(quartier, guid de la prévision du jour) ou None sans pack Saisons / avant la zone."""
    import services
    service = services.weather_service()
    if service is None:
        return None
    install_guard(type(service))
    if getattr(service, '_street_or_region_id', None) is None:
        return None
    forecasts = service._forecasts
    today = forecasts[0] if forecasts else None
    return _region(), getattr(today, 'guid64', None)


def game_apply():
    """Retire les prévisions locales puis tire celles du jour serveur (graine posée par le garde)."""
    import services
    service = services.weather_service()
    if service is None:
        return False
    install_guard(type(service))
    service._forecasts.clear()
    service._send_new_weather_event()
    return True


class WeatherSync:
    def __init__(self, read=game_read, apply=game_apply):
        self.read = read
        self.apply_weather = apply
        self.wait = 0
        self.applied = None
        self.applies = 0

    def update(self, state):
        """Résultat (chaîne) ou None si rien à faire. Jamais d'exception vers la boucle du jeu."""
        day = server_day(state)
        if day is None:
            _STATE['day'] = None
            return None
        if _STATE['day'] != day:
            _STATE['day'], _STATE['events'] = day, 0
        if self.wait > 0:
            self.wait -= 1
            return None
        try:
            return self._update(day)
        except Exception as error:
            self.wait = FAILED_CALLS
            log('weather_sync', 'error_' + type(error).__name__)
            return 'error'

    def _update(self, day):
        snapshot = self.read()
        if snapshot is None:
            self.wait = RETRY_CALLS
            return None
        region, forecast = snapshot
        if self.applied is not None and self.applied[:2] == (day, region):
            if self.applied[2] == forecast:
                return None
            # Changement de jour local ou prévision retirée par le jeu : le jour serveur est reposé, sans boucle.
            if self.applies >= MAX_APPLIES_PER_DAY:
                return None
        else:
            self.applies = 0
        _STATE['events'] = 0
        if not self.apply_weather():
            self.wait = RETRY_CALLS
            return 'pack_unavailable'
        self.applies += 1
        after = self.read()
        self.applied = (day, region, after[1] if after else None)
        log('weather_sync', 'applied')
        return 'applied'
