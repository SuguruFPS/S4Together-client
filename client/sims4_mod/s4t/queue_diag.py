"""Diagnostic « mes actions ne partent pas » : file et interactions en cours du Sim actif (13/10/2026).

Lecture seule, à la demande (commande s4t.queue) : rien n'est annulé, poussé ni modifié. Sert à voir, au moment du
blocage, quelle interaction tient la tête de la file et si sa cible est le Sim d'un autre joueur (foyer « Nom (S4T) »).

API vérifiée par désassemblage 1.128 : Sim.queue (InteractionQueue : itérable, .running, .transition_in_progress),
Sim.si_state (itérable), Interaction.affordance.__name__, .pipeline_progress, .is_user_directed, .is_finishing,
.priority, .target, .visible ; BaseObject.is_sim, SimInfo.household.name. NOT_TESTED_GAME.
"""
MAX_ROWS = 20
SUFFIX = ' (S4T)'


def _row(interaction):
    row = {}
    for key, read in (('affordance', lambda: interaction.affordance.__name__),
                      ('progress', lambda: str(interaction.pipeline_progress)),
                      ('priority', lambda: str(interaction.priority)),
                      ('user', lambda: bool(interaction.is_user_directed)),
                      ('finishing', lambda: bool(interaction.is_finishing)),
                      ('visible', lambda: bool(interaction.visible)),
                      ('target', lambda: _target(interaction.target))):
        try:
            row[key] = read()
        except Exception as error:
            row[key] = 'unreadable:' + type(error).__name__
    return row


def _target(target):
    if target is None:
        return 'none'
    if getattr(target, 'is_sim', False) is not True:
        return 'object'
    name = getattr(getattr(target.sim_info, 'household', None), 'name', None)
    return 'remote_player_sim' if isinstance(name, str) and name.endswith(SUFFIX) else 'local_sim'


def read(sim):
    """État de la file de `sim`, ou None sans Sim actif. Aucune identité native dans le résultat."""
    if sim is None:
        return None
    queue = sim.queue
    queued = list(queue) if queue is not None else []
    running = queue.running if queue is not None else None
    states = list(sim.si_state) if sim.si_state is not None else []
    return {'transitionInProgress': bool(queue.transition_in_progress) if queue is not None else None,
            'running': _row(running) if running is not None else None,
            'queued': [_row(item) for item in queued[:MAX_ROWS]], 'queuedCount': len(queued),
            'active': [_row(item) for item in states[:MAX_ROWS]], 'activeCount': len(states),
            'gameReadiness': 'NOT_TESTED_GAME'}


def game_read():
    import services
    return read(services.get_active_sim())
