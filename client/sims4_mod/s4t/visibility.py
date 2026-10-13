"""Diagnostic « pourquoi je ne vois pas l'autre joueur » : logique pure, sans identité ni position."""

PRESENCE = {'idle': 'en attente du chargement du terrain', 'joining': 'envoi en cours',
            'blocked': 'bloquée', 'not_started': 'module non démarré'}


def _n(value):
    return value if isinstance(value, int) else 0


def steps(diag):
    """Étapes dans l'ordre de la chaîne : (réussie, libellé, détail). La première en échec explique le blocage."""
    connection = diag.get('connection') or {}
    presence = diag.get('presence') or {}
    motion = diag.get('remoteMotion') or {}
    households = diag.get('remoteHouseholds') or {}
    appearance = diag.get('appearance') or {}
    ghosts = households.get('ghosts') or {}
    rejected = motion.get('rejected') or {}
    state = presence.get('state')
    participants = _n(presence.get('participants'))
    instances, spawned, failed = _n(motion.get('instances')), _n(motion.get('spawned')), _n(motion.get('failed'))
    return [
        (bool(connection.get('authenticated')), 'Connecté au serveur', str(connection.get('state') or 'inconnu')),
        (state == 'joined', 'Ton Sim est annoncé sur ce terrain',
         'zone ' + str(presence.get('zoneId')) if state == 'joined'
         else PRESENCE.get(state, str(state)) + ('' if not presence.get('reason') else ' (' + str(presence['reason']) + ')')),
        (participants > 0, 'Un autre joueur est sur le même terrain',
         '{} joueur(s). Sur deux terrains différents (même voisins, même rue), on ne se voit pas.'.format(participants)),
        (_n(presence.get('remoteStatesSeen')) > 0 or _n(motion.get('poses')) > 0, 'Positions de l\'autre reçues',
         '{} état(s), {} pose(s)'.format(_n(presence.get('remoteStatesSeen')), _n(motion.get('poses')))),
        (bool(appearance.get('remote')), 'Apparence de l\'autre reçue',
         '{} reçue(s), {} en échec'.format(len(appearance.get('remote') or []), len(appearance.get('failed') or []))),
        (bool(ghosts) and not households.get('failed'), 'Copie de son foyer créée chez toi',
         '{} foyer(s), {} en échec'.format(len(ghosts), len(households.get('failed') or []))),
        (instances > 0, 'Son Sim est apparu sur le terrain',
         '{} présent(s), {} apparition(s), {} abandon(s), {} non représenté(s)'.format(
             instances, spawned, failed, _n(motion.get('unrepresented')))),
        (_n(motion.get('routed')) > 0, 'Son Sim se déplace',
         '{} trajet(s), {} bloqué(s){}'.format(_n(motion.get('routed')), _n(motion.get('stuck')),
             '' if not rejected else ', refus : ' + ', '.join('{}×{}'.format(k, v) for k, v in sorted(rejected.items())))),
    ]


def text(diag):
    if diag.get('code') == 'runtime_unavailable':
        return 'Mod pas encore prêt dans cette partie ({}). Charge un foyer puis réessaie.'.format(diag.get('error_class'))
    lines, blocked = [], None
    for ok, label, detail in steps(diag):
        if not ok and blocked is None:
            blocked = label
        lines.append(('OK  ' if ok else 'NON ') + label + ' — ' + detail)
    head = 'Tout est en place.' if blocked is None else 'Bloqué à : ' + blocked
    return head + '\n\n' + '\n'.join(lines) + '\n\nPour vous voir : soyez tous les deux sur le MÊME terrain, attendez 30 s, puis refaites s4t.visible.'


STEP_CODES = ('connected', 'joined', 'other_player', 'positions', 'appearance', 'household', 'instance', 'moving')
COUNTER_MAX = 1000000
LOWER = frozenset('abcdefghijklmnopqrstuvwxyz_')


def report(diag):
    """Charge de game.visibility.report : étapes (bool) et compteurs bornés, ni identité, ni zone, ni position."""
    presence = diag.get('presence') or {}
    motion = diag.get('remoteMotion') or {}
    households = diag.get('remoteHouseholds') or {}
    appearance = diag.get('appearance') or {}
    counters = {
        'participants': presence.get('participants'), 'states': presence.get('remoteStatesSeen'),
        'poses': motion.get('poses'), 'instances': motion.get('instances'), 'spawned': motion.get('spawned'),
        'failed': motion.get('failed'), 'adopted': motion.get('adopted'), 'routed': motion.get('routed'),
        'stuck': motion.get('stuck'), 'unrepresented': motion.get('unrepresented'),
        'appearance_remote': len(appearance.get('remote') or []), 'appearance_failed': len(appearance.get('failed') or []),
        'ghosts': len(households.get('ghosts') or {}), 'ghosts_failed': len(households.get('failed') or []),
    }
    for code, value in sorted((motion.get('rejected') or {}).items()):
        if len(counters) < 32 and isinstance(code, str) and 0 < len(code) <= 25 and all(c in LOWER for c in code):
            counters['rej_' + code] = value
    return {'steps': {code: bool(ok) for code, (ok, _, _) in zip(STEP_CODES, steps(diag))},
            'counters': {k: min(max(_n(v), 0), COUNTER_MAX) for k, v in counters.items()}}
