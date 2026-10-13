"""Commandes exécutées par la console du jeu, adaptation sur thread principal."""
import json
from .diagnostic import report, missing_module
from .journal import log

_manager = None
_adapter = None
_password = ''
_auto = None

def ConnectionManager():
    # Une dépendance réseau manquante ne doit pas bloquer le diagnostic du mod.
    from .connection import ConnectionManager as implementation
    return implementation()

def GameAdapter():
    from .game_adapter import GameAdapter as implementation
    return implementation()

def configure_and_connect(address, port, name, password):
    global _password
    from .network_config import endpoint_for
    manager, _ = runtime()
    endpoint, lan = endpoint_for(address, port)
    if not isinstance(password, str) or len(password) > 128:
        raise ValueError('Mot de passe invalide')
    manager.config.save_settings(endpoint, name, allow_lan=lan)
    _password = password
    manager.connect(endpoint, name, password)
    log('connect', 'started')


def auto_link():
    global _auto
    if _auto is None:
        from .autoconnect import AutoLink
        from .config import ConfigStore
        from .play_intent import PlayIntentRunner
        from .season_sync import SeasonSync
        from .season_phase import SeasonPhaseSync
        from .character_sync import CharacterSync
        from .presence_sync import PresenceSync
        from .home_lots import HomeLotSync
        from .appearance_sync import AppearanceSync
        from .remote_household import RemoteHouseholds
        from .remote_motion import RemoteMotion
        from .interaction_observer import GameInteractions, InteractionObserver, validated_clones, validated_remote_sims
        from .interaction_publisher import InteractionPublisher
        # Lectures différées : en solo, ni adaptateur ni connexion ne sont créés.
        motion = RemoteMotion()
        presence = PresenceSync(lambda: runtime()[1].presence(), lambda sim_ids: runtime()[1].poses(sim_ids),
                                remote=motion)
        appearance, households = AppearanceSync(), RemoteHouseholds()
        # Un clone n'est nommé comme cible que si son foyer porte l'empreinte de l'apparence reçue.
        # Le Sim visé n'est relié au Sim natif distant que par la correspondance écrite après un Locate réussi.
        interactions = InteractionObserver(GameInteractions(
            clones=lambda: validated_clones(households.ghosts, appearance.remote),
            remote_sims=lambda: validated_remote_sims(households.ghosts, appearance.remote, motion.mapping)))
        def terrain_factory():
            from .terrain_adapter import game_reader, game_events
            from .terrain_sync import TerrainSync
            from .terrain_apply import game_applier
            reader = game_reader()
            applier = game_applier(reader)
            sync = TerrainSync(reader.locate, reader.read, applier.apply)
            return sync, game_events(sync.mark_dirty)
        from .offline_guard import OfflineGuard
        def native_probe():
            from . import native_probe as probe
            probe.write(probe.inspect())
        _auto = AutoLink(runtime, lambda: ConfigStore().settings(), play_intent=PlayIntentRunner(ConfigStore()),
                         season_sync=SeasonSync(ConfigStore()), character_sync=CharacterSync(ConfigStore()),
                         presence_sync=presence, home_sync=HomeLotSync(),
                         appearance_sync=appearance, remote_households=households,
                         remote_motion=motion, interaction_observer=interactions,
                         interaction_publisher=InteractionPublisher(peer_streams=lambda: dict(motion.streams)),
                         terrain_factory=terrain_factory, offline_guard=OfflineGuard(), season_phase=SeasonPhaseSync(), native_probe=native_probe)
    return _auto


def runtime():
    global _manager, _adapter
    if _manager is None:
        _manager = ConnectionManager()
        _adapter = GameAdapter()
    return _manager, _adapter


def execute(action, *args):
    global _password
    if action == 'visible':
        from . import visibility
        diag = execute('diagnostic')
        keep = {k: diag.get(k) for k in ('code', 'error_class', 'connection', 'presence', 'appearance',
                                          'remoteHouseholds', 'remoteMotion')}
        _save_visibility(keep)
        return {'ok': True, 'visible': visibility.text(diag), 'steps': [list(s) for s in visibility.steps(diag)]}
    if action == 'diagnostic':
        value = report()
        try:
            manager, adapter = runtime()
            value['observation'] = adapter.observe()
            value['connection'] = manager.status()
            value['network_connected'] = value['connection']['authenticated']
            presence = getattr(_auto, 'presence_sync', None)
            value['presence'] = presence.status() if presence is not None else {'state': 'not_started'}
            home = getattr(_auto, 'home_sync', None)
            value['homes'] = home.status() if home is not None else {'state': 'not_started'}
            for key, name in (('appearance', 'appearance_sync'), ('remoteHouseholds', 'remote_households'),
                              ('remoteMotion', 'remote_motion'), ('interactions', 'interaction_observer'),
                              ('interactionPublisher', 'interaction_publisher'), ('terrain', 'terrain_sync')):
                sync = getattr(_auto, name, None)
                value[key] = sync.status() if sync is not None else {'state': 'not_started'}
            character = getattr(_auto, 'character_sync', None)
            value['soloGuard'] = character.solo_status() if character is not None else {'guard': 'not_started'}
        except Exception as error:
            value['code'] = 'runtime_unavailable'
            value['error_class'] = type(error).__name__
            log('diagnostic_runtime', type(error).__name__)
            if isinstance(error, ImportError):
                value['missing_module'] = missing_module(error)
                log('missing_module', value['missing_module'].replace('.', '_'))
        return value
    if action == 'native':
        # Lecture seule : noms des attributs des modules natifs, écrits dans le dossier des journaux. Aucun appel.
        from . import native_probe
        found = native_probe.inspect()
        native_probe.write(found)
        return native_probe.summary(found)
    if action in ('menu', 'configure'):
        from .native_ui import connection_form
        settings = {'endpoint': '', 'name': 'Joueur Sims'}
        try:
            from .config import ConfigStore
            settings = ConfigStore().settings()
        except (ImportError, ValueError, OSError) as error:
            log('menu_config', type(error).__name__)
        ok = connection_form(settings, configure_and_connect)
        return {'ok': ok, 'code': 'native_form' if ok else 'native_ui_unavailable',
            'message': 'Charger un foyer en mode Vie avant s4t.menu'}
    if action == 'interactions':
        # Diagnostic local : état de l'observateur, de la publication et derniers événements. La commande ne crée
        # ni connexion ni envoi.
        observer = getattr(_auto, 'interaction_observer', None)
        publisher = getattr(_auto, 'interaction_publisher', None)
        value = observer.status() if observer is not None else {'state': 'not_started'}
        if observer is not None and publisher is not None:
            value['publisher'] = publisher.status()
            value['serverLink'] = value['publisher']['state']
        return value
    manager, adapter = runtime()
    if action == 'connect':
        return {'requestId': manager.connect(*(args + ((_password,) if _password else ()))), 'message': 'Connexion lancée ; utiliser s4t.poll'}
    if action == 'poll':
        messages = manager.poll()
        if _auto is not None:
            _auto.deliver(manager, messages)
        observation = adapter.observe()
        # Pas d'affichage de payload brut contenant des données d'identité.
        summaries = [{'type': m.get('type'), 'ok': m.get('ok'), 'code': m.get('code')}
                     for m in messages]
        chats = [m['payload'].get('text', '') for m in messages
                 if m.get('type') == 'chat' and isinstance(m.get('payload'), dict)]
        return {'received': summaries, 'chat': chats, 'observation': observation, 'connection': manager.status()}
    if action == 'status':
        return manager.status()
    if action == 'disconnect':
        if _auto is not None:
            _auto.stop()
        manager.disconnect()
        _password = ''
        return manager.status()
    if action == 'observe':
        return {'requestId': manager.observe(adapter.observe())}
    if action == 'chat':
        if not args or not isinstance(args[0], str) or not 1 <= len(args[0]) <= 500:
            raise ValueError('Message invalide')
        return {'requestId': manager.request('chat', {'text': args[0]})}
    if action == 'snapshot':
        return {'requestId': manager.request('snapshot')}
    if action == 'season':
        from .season_sync import chosen_season, game_current
        world_id, state = manager.world_season()
        try:
            current = game_current()
        except Exception as error:
            current = None
            log('season_read', type(error).__name__)
        return {'server': state.get('status') if isinstance(state, dict) else None,
                'serverSeason': chosen_season(state), 'gameSeason': current,
                'packAvailable': current is not None, 'connection': manager.status()}
    if action == 'queue':
        from .queue_diag import game_read
        guard = _auto.offline_guard if _auto is not None else None
        return {'queue': game_read(), 'offlineGuard': guard.status() if guard is not None else None}
    if action == 'character':
        return _character_status(manager)
    if action == 'terrain':
        sync = getattr(_auto, 'terrain_sync', None)
        return {'terrain': sync.status() if sync is not None else {'state': 'not_started'}, 'connection': manager.status()}
    if action == 'retry':
        return {'requestId': manager.retry(args[0])}
    raise ValueError('Commande inconnue')


def _character_status(manager):
    """Vérification en jeu de T3 : état du mod, personnage serveur et foyer actif réellement observé."""
    sync = getattr(_auto, 'character_sync', None)
    if sync is None:
        return {'state': 'not_started', 'connection': manager.status()}
    character = sync.character if isinstance(sync.character, dict) else {}
    binding = character.get('binding') if isinstance(character.get('binding'), dict) else {}
    try:
        observed = sync.household() or {}
    except Exception as error:
        observed = {}
        log('character_read', type(error).__name__)
    return {'state': sync.state, 'name': character.get('name'), 'status': character.get('status'),
            'revision': character.get('revision'), 'boundHousehold': binding.get('gameHouseholdId'),
            'gameHousehold': observed.get('householdId'), 'gameFunds': observed.get('funds'),
            'soloGuard': sync.solo_status(), 'connection': manager.status()}


def _save_visibility(value):
    import os, time
    from pathlib import Path
    root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'S4Together' / 'logs'
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / 'visibility-last.json').write_text(json.dumps(dict(value, time=int(time.time())), ensure_ascii=False,
                                                              sort_keys=True, default=str), encoding='utf-8')
    except OSError as error:
        log('visibility_save', type(error).__name__)


def _output(action, connection, *args):
    log('command_' + action, 'entered')
    try:
        value = execute(action, *args)
    except (ValueError, RuntimeError, OSError, ImportError):
        value = {'ok': False, 'code': 'command_refused', 'message': 'Commande refusée ; vérifier état et configuration locale'}
    log('command_' + action, value.get('code', 'ok'))
    try:
        game_commands.Output(connection)(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except Exception as error:
        log('console_output', type(error).__name__)
    if action == 'visible':
        from .native_ui import dialog
        dialog('S4Together — Se voir en jeu', value.get('visible') or value.get('message', ''))
    if action in ('diagnostic', 'status', 'poll'):
        from .native_ui import dialog
        status = value if value.get('code') == 'runtime_unavailable' else value.get('connection', value)
        dialog('S4Together — Diagnostic', 'Script exécuté.\n' + json.dumps(status, ensure_ascii=False, sort_keys=True))


try:
    import sims4.commands as game_commands
except ImportError:
    game_commands = None
else:
    @game_commands.Command('s4t.ping', command_type=game_commands.CommandType.Live)
    def ping(_connection=None):
        # Preuve sans réseau, configuration, observation ni dialogue natif.
        log('command_ping', 'entered')
        try:
            game_commands.Output(_connection)('S4Together : script actif — PONG')
            log('command_ping', 'output_sent')
        except Exception as error:
            log('console_output', type(error).__name__)
        from .native_ui import dialog
        dialog('S4Together — PONG', 'La commande est exécutée dans le jeu. Aucun réseau requis.')

    @game_commands.Command('s4t.menu', command_type=game_commands.CommandType.Live)
    def menu(_connection=None):
        _output('menu', _connection)

    @game_commands.Command('s4t.configure', command_type=game_commands.CommandType.Live)
    def configure(_connection=None):
        _output('configure', _connection)

    @game_commands.Command('s4t.diagnostic', command_type=game_commands.CommandType.Live)
    def diagnostic(_connection=None):
        _output('diagnostic', _connection)

    @game_commands.Command('s4t.visible', command_type=game_commands.CommandType.Live)
    def visible(_connection=None):
        _output('visible', _connection)

    @game_commands.Command('s4t.connect', command_type=game_commands.CommandType.Live)
    def connect(endpoint: str = '', name: str = '', _connection=None):
        _output('connect', _connection, endpoint or None, name or None)

    @game_commands.Command('s4t.poll', command_type=game_commands.CommandType.Live)
    def poll(_connection=None):
        _output('poll', _connection)

    @game_commands.Command('s4t.status', command_type=game_commands.CommandType.Live)
    def status(_connection=None):
        _output('status', _connection)

    @game_commands.Command('s4t.disconnect', command_type=game_commands.CommandType.Live)
    def disconnect(_connection=None):
        _output('disconnect', _connection)

    @game_commands.Command('s4t.observe', command_type=game_commands.CommandType.Live)
    def observe(_connection=None):
        _output('observe', _connection)

    @game_commands.Command('s4t.chat', command_type=game_commands.CommandType.Live)
    def chat(text: str = '', _connection=None):
        _output('chat', _connection, text)

    @game_commands.Command('s4t.snapshot', command_type=game_commands.CommandType.Live)
    def snapshot(_connection=None):
        _output('snapshot', _connection)

    @game_commands.Command('s4t.season', command_type=game_commands.CommandType.Live)
    def season(_connection=None):
        _output('season', _connection)

    @game_commands.Command('s4t.queue', command_type=game_commands.CommandType.Live)
    def queue(_connection=None):
        _output('queue', _connection)

    @game_commands.Command('s4t.interactions', command_type=game_commands.CommandType.Live)
    def interactions(_connection=None):
        _output('interactions', _connection)

    @game_commands.Command('s4t.terrain', command_type=game_commands.CommandType.Live)
    def terrain(_connection=None):
        _output('terrain', _connection)

    @game_commands.Command('s4t.native', command_type=game_commands.CommandType.Live)
    def native(_connection=None):
        _output('native', _connection)

    @game_commands.Command('s4t.character', command_type=game_commands.CommandType.Live)
    def character(_connection=None):
        _output('character', _connection)

    @game_commands.Command('s4t.retry', command_type=game_commands.CommandType.Live)
    def retry(request_id: str = '', _connection=None):
        _output('retry', _connection, request_id)
