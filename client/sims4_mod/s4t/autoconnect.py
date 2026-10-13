"""Auto-connexion au pont du launcher et relevé périodique, sur le thread principal du jeu.

API vérifiée dans simulation.zip 1.128 : alarms.add_alarm_real_time(owner, time_span, callback,
repeating, ...), date_and_time.TimeSpan et date_and_time.TICKS_PER_REAL_WORLD_SECOND.
"""
from .journal import log

POLL_SECONDS = 1
RETRY_TICKS = 10
REPORT_TICKS = 60


def game_scheduler(owner, seconds, callback):
    import alarms
    import date_and_time
    span = date_and_time.TimeSpan(int(date_and_time.TICKS_PER_REAL_WORLD_SECOND * seconds))
    return alarms.add_alarm_real_time(owner, span, callback, repeating=True)


def game_cancel(handle):
    import alarms
    alarms.cancel_alarm(handle)


class AutoLink:
    def __init__(self, runtime, load_settings, scheduler=game_scheduler, cancel=game_cancel, play_intent=None, season_sync=None,
                 character_sync=None, presence_sync=None, home_sync=None, appearance_sync=None, remote_households=None,
                 remote_motion=None, interaction_observer=None, interaction_publisher=None):
        self.runtime = runtime
        self.interaction_observer = interaction_observer
        self.interaction_publisher = interaction_publisher
        self.interaction_context = None
        self.interaction_stream = None
        self.appearance_sync = appearance_sync
        self.remote_households = remote_households
        self.remote_motion = remote_motion
        self.presence_sync = presence_sync
        self.home_sync = home_sync
        self.load_settings = load_settings
        self.play_intent = play_intent
        self.season_sync = season_sync
        self.character_sync = character_sync
        self.scheduler = scheduler
        self.cancel = cancel
        self.handle = None
        self.ticks = 0
        self.sent = None

    def on_zone_loaded(self):
        """Vrai si le launcher a demandé l'auto-connexion et que le relevé périodique tourne."""
        try:
            settings = self.load_settings()
        except (ValueError, OSError) as error:
            log('autoconnect_config', type(error).__name__)
            return False
        self._forget_motion()
        self._forget_interactions()
        if settings.get('launcher_bridge') is not True:
            # Solo : mod passif. Un relevé armé quand le launcher était actif ne doit pas survivre.
            self._leave('passive')
            self.stop()
            return False
        if self.play_intent is not None and 'solo_guard' not in settings:
            try:
                self.play_intent.apply(settings)
            except Exception as error:
                log('play_intent', type(error).__name__)
        if self.character_sync is not None:
            # Autre zone, peut-être autre foyer : l'état personnage est redemandé au serveur.
            self.character_sync.reset()
        for sync in (self.home_sync, self.appearance_sync, self.remote_households):
            if sync is not None:
                sync.reset()
        try:
            self._connect_if_idle()
            self._schedule()
        except Exception as error:
            log('autoconnect', type(error).__name__)
            return False
        log('autoconnect', 'started')
        return True

    def _connect_if_idle(self):
        manager, _ = self.runtime()
        if manager.status().get('state') == 'disconnected':
            manager.connect()

    def _retry(self):
        # Launcher lancé après le jeu, ou pont coupé : nouvel essai espacé tant qu'il le demande.
        try:
            if self.load_settings().get('launcher_bridge') is True:
                self._connect_if_idle()
        except (ValueError, OSError, RuntimeError) as error:
            log('autoconnect_retry', type(error).__name__)

    def _season(self, manager):
        if self.season_sync is None:
            return
        try:
            world_id, season = manager.world_season()
            self.season_sync.update(world_id, season)
        except Exception as error:
            log('season', type(error).__name__)

    def _character(self, manager, status, loaded):
        if self.character_sync is None:
            return
        try:
            self.character_sync.tick(manager, bool(status.get('authenticated')), bool(loaded))
        except Exception as error:
            log('character', type(error).__name__)

    def _home(self, manager, status, loaded):
        """Maison publiée au serveur et maisons des autres résolues, une fois le personnage lié."""
        sync = self.character_sync
        if self.home_sync is None or not status.get('authenticated') or not loaded or sync is None:
            return
        try:
            character = sync.character if sync.state == 'bound' else None
            self.home_sync.tick(manager, character, getattr(manager, 'snapshot', None))
        except Exception as error:
            log('home_lot', type(error).__name__)

    def _remote(self, manager, status, observation):
        """Apparences échangées puis foyers « Nom (S4T) » des autres joueurs, partie liée seulement."""
        sync = self.character_sync
        if not status.get('authenticated') or not observation.get('loaded') or sync is None or sync.state != 'bound':
            return
        character = sync.character
        if self.appearance_sync is not None:
            try:
                self.appearance_sync.tick(manager, character, observation.get('activeSimId'))
            except Exception as error:
                log('appearance', type(error).__name__)
        if self.remote_households is None or self.appearance_sync is None:
            return
        try:
            from .home_lots import player_names
            homes = self.home_sync.homes if self.home_sync is not None else []
            self.remote_households.tick(character, player_names(getattr(manager, 'snapshot', None)),
                                        self.appearance_sync.remote, homes)
        except Exception as error:
            log('remote_household', type(error).__name__)

    def _presence(self, manager, status, loaded):
        """Après _character : la présence part au tick même où le personnage devient lié, sans bouton."""
        if self.presence_sync is None:
            return
        try:
            sync = self.character_sync
            if sync is not None and sync.state == 'solo_blocked':
                # Foyer solo : une présence déjà déclarée est retirée, aucune n'est publiée.
                self._leave('solo_blocked')
            character = sync.character if sync is not None and sync.state == 'bound' else None
            self.presence_sync.tick(manager, bool(status.get('authenticated')), bool(loaded), character)
        except Exception as error:
            log('presence', type(error).__name__)

    def _forget_motion(self):
        # Les instances créées sur une zone disparaissent avec elle : oubli sans appel natif.
        if self.remote_motion is None:
            return
        try:
            self.remote_motion.reset()
        except Exception as error:
            log('remote_motion', type(error).__name__)

    def _forget_interactions(self, manager=None):
        # Les interactions suivies meurent avec la zone : nouveau flux, sans appel natif. La requête en vol est
        # oubliée et la file purgée : rien de l'ancienne zone n'est rejoué à la reconnexion.
        self.interaction_context = self.interaction_stream = None
        publisher = self.interaction_publisher
        if publisher is not None:
            try:
                if manager is None and publisher.in_flight():
                    # Une requête en vol prouve que la connexion existe déjà : un mod passif n'en crée aucune ici.
                    manager, _ = self.runtime()
                publisher.reset(manager)
            except Exception as error:
                log('interaction_publisher', type(error).__name__)
        if self.interaction_observer is None:
            return
        try:
            self.interaction_observer.reset()
        except Exception as error:
            log('interaction_observer', type(error).__name__)

    def _interaction_context(self, loaded):
        """(personnage, zone native, foyer) si le personnage multi est lié et sa présence confirmée, sinon None."""
        sync, presence = self.character_sync, self.presence_sync
        if not loaded or sync is None or presence is None or sync.state != 'bound' or presence.state != 'joined':
            return None
        character, key, zone = sync.character, presence.key, presence.zone_id
        binding = character.get('binding') if isinstance(character, dict) else None
        if not isinstance(binding, dict) or not isinstance(zone, str) or not isinstance(key, tuple) or len(key) < 2:
            return None
        household = binding.get('gameHouseholdId')
        if not isinstance(household, str) or key[1] != household:
            return None
        return character.get('characterId'), zone, household

    def _interactions(self, loaded, manager=None, authenticated=False):
        """Relevé en lecture seule des interactions du Sim actif, puis publication au serveur (game.interaction).

        Après _presence : un foyer solo bloqué, un personnage non lié ou une présence retirée ne sont jamais
        relevés ni publiés, et ce qui avait été relevé avant est oublié (ni file d'attente, ni diagnostic).
        Rien n'est rejoué dans le jeu.
        """
        if self.interaction_observer is None:
            return
        try:
            context = self._interaction_context(loaded)
            # Flux serveur confirmé par l'ACK de game.join ; un nouveau join ouvre un nouveau relevé.
            stream = getattr(self.presence_sync, 'stream_id', None) if context is not None else None
        except Exception as error:
            log('interaction_context', type(error).__name__)
            context = stream = None
        if context != self.interaction_context or stream != self.interaction_stream:
            if self.interaction_context is not None:
                self._forget_interactions(manager)
            self.interaction_context, self.interaction_stream = context, stream
        events = []
        if context is not None:
            try:
                self.interaction_observer.tick()
                if self.interaction_publisher is not None:
                    events = self.interaction_observer.drain()
            except Exception as error:
                log('interaction_observer', type(error).__name__)
        if self.interaction_publisher is None or manager is None:
            return
        try:
            flow = (context[0], context[1], stream) if context is not None else None
            self.interaction_publisher.tick(manager, bool(authenticated), flow, events)
        except Exception as error:
            log('interaction_publisher', type(error).__name__)

    def _motion(self, manager):
        """Après _presence, même hors ligne : c'est ce tick qui retire les instances d'un joueur parti."""
        if self.remote_motion is None:
            return
        presence, households, appearance = self.presence_sync, self.remote_households, self.appearance_sync
        zone_id = presence.zone_id if presence is not None and presence.state == 'joined' else None
        identity = getattr(manager, 'identity', None)
        self.remote_motion.tick(manager, zone_id, identity.get('playerId') if isinstance(identity, dict) else None,
                                households.ghosts if households is not None else {},
                                appearance.remote if appearance is not None else {})

    def visibility_diag(self, manager):
        """Mêmes données que s4t.visible, pour le rapport automatique et la commande."""
        diag = {'connection': manager.status()}
        presence = self.presence_sync
        diag['presence'] = presence.status() if presence is not None else {'state': 'not_started'}
        for key, sync in (('appearance', self.appearance_sync), ('remoteHouseholds', self.remote_households),
                          ('remoteMotion', self.remote_motion)):
            diag[key] = sync.status() if sync is not None else {'state': 'not_started'}
        return diag

    def _report(self, manager, status):
        """Diagnostic « se voir » envoyé seul au serveur une fois par minute sur un terrain : personne n'a à taper
        s4t.visible. Réponse ignorée ; un refus (cadence, hors jeu) n'a aucun effet sur la partie."""
        presence = self.presence_sync
        if not status.get('authenticated') or presence is None or presence.state != 'joined':
            return
        from . import visibility
        manager.request('game.visibility.report', visibility.report(self.visibility_diag(manager)))

    def _leave(self, reason):
        # Rien de déclaré : aucune connexion n'est créée pour un mod resté passif.
        if self.presence_sync is None or not self.presence_sync.active():
            return
        try:
            manager, _ = self.runtime()
            self.presence_sync.leave(manager, reason)
        except Exception as error:
            log('presence', type(error).__name__)

    def _flush(self, saved):
        """Personnage et domicile envoyés tout de suite au serveur. Mod passif ou non lié : aucune connexion créée."""
        sync = self.character_sync
        if sync is None or sync.state != 'bound':
            return
        manager, _ = self.runtime()
        if not manager.status().get('authenticated'):
            return
        try:
            sync.flush(manager, saved)
        except Exception as error:
            log('character_flush', type(error).__name__)
        if self.home_sync is None or sync.state != 'bound':
            return
        try:
            self.home_sync.flush(manager, sync.character, getattr(manager, 'snapshot', None))
        except Exception as error:
            log('home_lot_flush', type(error).__name__)

    def on_game_saved(self):
        """Sauvegarde du jeu réussie (manuelle, automatique, voyage, sortie) : le serveur reçoit l'état enregistré."""
        try:
            self._flush(True)
        except Exception as error:
            log('game_saved', type(error).__name__)

    def on_zone_teardown(self):
        """Zone déchargée (voyage, menu principal) : dernier état puis game.leave tant que le réseau répond encore."""
        try:
            self._flush(False)
        except Exception as error:
            log('teardown_flush', type(error).__name__)
        self._leave('teardown')
        self._forget_motion()
        self._forget_interactions()

    def deliver(self, manager, messages):
        """Réponses lues hors de tick() (commande s4t.poll) : sans cela, un ACK personnage serait perdu."""
        for name, sync in (('character', self.character_sync), ('presence', self.presence_sync), ('home_lot', self.home_sync),
                           ('appearance', self.appearance_sync), ('remote_motion', self.remote_motion),
                           ('interaction_publisher', self.interaction_publisher)):
            if sync is None:
                continue
            try:
                sync.on_messages(manager, messages)
            except Exception as error:
                log(name, type(error).__name__)

    def _observe(self, manager, status, observation):
        # Observation envoyée à chaque changement, et de nouveau après chaque (re)connexion.
        seen = (observation.get('loaded'), observation.get('zoneId'), observation.get('activeSimId'))
        if not status.get('authenticated'):
            self.sent = None
        elif seen != self.sent:
            try:
                manager.observe(observation)
            except Exception as error:
                # File pleine ou trop de requêtes en attente : nouvel essai au tick suivant, le reste continue.
                log('autoconnect_observe', type(error).__name__)
                return
            self.sent = seen

    def _schedule(self):
        # Les alarmes non cross_zone meurent avec la zone : on réarme à chaque chargement.
        self.stop()
        self.handle = self.scheduler(self, POLL_SECONDS, self.tick)

    def stop(self):
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        try:
            self.cancel(handle)
        except Exception as error:
            log('autoconnect_cancel', type(error).__name__)

    def tick(self, _handle=None):
        # Une exception ne doit jamais remonter dans la boucle d'alarmes du jeu.
        try:
            manager, adapter = self.runtime()
            # poll() consomme les réponses : elles sont livrées avant toute étape qui peut échouer.
            self.deliver(manager, manager.poll())
            observation = adapter.observe()
            status = manager.status()
            self._observe(manager, status, observation)
            self._character(manager, status, observation.get('loaded'))
            # Avec la garde du launcher, aucune saison serveur ne doit modifier la partie solo.
            guarded = 'solo_guard' in self.load_settings()
            if status.get('authenticated') and observation.get('loaded') and (not guarded or
                    self.character_sync is not None and self.character_sync.state == 'bound'):
                self._season(manager)
            self._home(manager, status, observation.get('loaded'))
            self._remote(manager, status, observation)
            self._presence(manager, status, observation.get('loaded'))
            try:
                self._motion(manager)
            except Exception as error:
                log('remote_motion', type(error).__name__)
            self._interactions(observation.get('loaded'), manager, status.get('authenticated'))
            self.ticks += 1
            if self.ticks % REPORT_TICKS == REPORT_TICKS // 2:
                try:
                    self._report(manager, status)
                except Exception as error:
                    log('visibility_report', type(error).__name__)
            if self.ticks % RETRY_TICKS == 0 and status.get('state') == 'disconnected':
                self._retry()
        except Exception as error:
            log('autoconnect_tick', type(error).__name__)
