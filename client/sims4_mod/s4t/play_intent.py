"""Intention « Jouer » du launcher, appliquée une seule fois après le chargement d'une zone.

Le menu principal est piloté par le client C++ (spike #137) : aucune API Python n'y charge une partie.
Le launcher guide donc le joueur (« Nouvelle partie » mène nativement à CAS, « Reprendre » charge la
dernière partie) ; le mod constate l'intention et, pour create_sim seulement, ouvre CAS sur le foyer chargé.

API vérifiée dans simulation.zip 1.128 : services.get_first_client(), Client.id, Client.active_sim, et
server_commands/cas_commands.modify_in_cas_with_household_id qui envoie
sims4.commands.client_cheat('sims.exit2caswithhouseholdid {} {}'.format(sim.id, sim.household_id), connexion).
"""
import re

from .journal import log

MODES = ('new_player', 'resume', 'create_sim')
_INTENT_ID = re.compile(r'^[0-9a-f]{32}$')


def parse(settings):
    value = settings.get('play_intent')
    if not isinstance(value, dict):
        return None
    mode, intent_id = value.get('mode'), value.get('id')
    if mode not in MODES or not isinstance(intent_id, str) or not _INTENT_ID.match(intent_id):
        return None
    return mode, intent_id


def game_open_cas():
    import services
    import sims4.commands
    client = services.get_first_client()
    sim = getattr(client, 'active_sim', None) if client is not None else None
    if sim is None:
        return False
    sims4.commands.client_cheat('sims.exit2caswithhouseholdid {} {}'.format(sim.id, sim.household_id), client.id)
    return True


class PlayIntentRunner:
    def __init__(self, store, open_cas=game_open_cas):
        self.store = store
        self.open_cas = open_cas

    def apply(self, settings):
        """Résultat appliqué, ou None si aucune intention nouvelle."""
        intent = parse(settings)
        if intent is None:
            return None
        mode, intent_id = intent
        if self.store.play_intent_done() == intent_id:
            return None
        # Marquée avant l'action : un échec ne doit jamais renvoyer le joueur dans CAS en boucle.
        self.store.mark_play_intent_done(intent_id)
        if mode != 'create_sim':
            log('play_intent', mode)
            return mode
        opened = self.open_cas()
        log('play_intent', 'cas_opened' if opened else 'cas_no_active_sim')
        return 'cas' if opened else 'no_active_sim'
