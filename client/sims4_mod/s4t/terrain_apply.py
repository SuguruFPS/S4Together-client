"""Application des objets d'un terrain distant sur sa copie locale (13/10/2026).

Écrit dans la partie : objets créés, déplacés (suppression puis création) et supprimés, uniquement sur le terrain
chargé quand il appartient à un foyer « Nom (S4T) » — jamais au foyer actif, jamais en mode construction. Le
contrôle du foyer propriétaire attendu est fait par TerrainSync avant l'appel, et refait ici contre le foyer actif.

API vérifiée par désassemblage 1.128 (lecture seule), jamais exécutée en jeu :
objects.system.create_object(definition_or_id, ...) rend None pour une définition absente (pack ou CC manquant) ;
ClientObjectMixin.move_to(**overrides) = self.location = self._location.clone(**overrides), et
sims4.math.Location.clone accepte translation, orientation, routing_surface ;
routing.SurfaceIdentifier(primary_id, secondary_id, surface_type), routing.SurfaceType.SURFACETYPE_WORLD ;
sims4.math.Vector3, sims4.math.angle_to_yaw_quaternion(angle) ; ClientObjectMixin.scale (setter) ;
GameObject.set_household_owner_id(new_owner_id) ; BaseObject.destroy(self, source, cause) ;
services.active_household_id(), services.current_zone(), Zone.id, Zone.lot.zone_owner_household_id,
services.object_manager().valid_objects().

Une définition absente chez ce joueur (create_object rend None) est ignorée : le reste du terrain est appliqué.
Toute autre erreur de création ou de pose annule le plan sans rien retirer.

Limites prouvées : murs, sols, toits, relief et papiers peints sont un blob natif sans setter Python, ils ne sont
pas reproduits ; un objet posé sur un autre (parented = 1) n'est pas recréé, son emplacement de slot n'étant pas
transporté. NOT_TESTED_GAME.
"""
import collections
import threading

from .journal import log

MAX_CHANGES = 1000
POSITION_MM = 2
ROTATION_MRAD = 2
SCALE_UNITS = 1
TURN_MRAD = 6283
CAUSE = 'S4Together : terrain distant synchronisé'


def _near(a, b):
    """Même objet à l'arrondi près : la relecture d'un objet posé ne rend pas exactement la ligne demandée."""
    turn = abs(a[4] - b[4])
    return (all(abs(a[i] - b[i]) <= POSITION_MM for i in (1, 2, 3)) and min(turn, TURN_MRAD - turn) <= ROTATION_MRAD
            and abs(a[6] - b[6]) <= SCALE_UNITS)


def reduce_plan(plan):
    """Retire du plan les paires suppression/création qui désignent le même objet à l'arrondi près."""
    create = collections.defaultdict(list)
    for row in plan['create']:
        create[(row[0], row[5], row[7])].append(row)
    remove = []
    for row in plan['remove']:
        bucket = create.get((row[0], row[5], row[7])) or []
        match = next((index for index, other in enumerate(bucket) if _near(row, other)), None)
        if match is None:
            remove.append(row)
        else:
            del bucket[match]
    return {'create': sorted(row for bucket in create.values() for row in bucket), 'remove': sorted(remove)}


class TerrainApplier:
    """apply(zone_id, plan) -> True si le terrain local a été rapproché du plan, False si rien n'a été tenté."""

    def __init__(self, services, row_of, create, place, thread_id=None):
        self.services = services
        self.row_of = row_of
        self.create = create
        self.place = place
        self.thread_id = threading.get_ident() if thread_id is None else thread_id
        self.last = None

    def _target(self, zone_id):
        """Foyer propriétaire du terrain chargé si l'écriture y est permise, sinon None."""
        zone = self.services.current_zone()
        if zone is None or str(zone.id) != zone_id:
            return None
        building = zone.is_in_build_buy
        if callable(building):
            building = building()
        owner = zone.lot.zone_owner_household_id
        if building or type(owner) is not int or owner <= 0 or owner == self.services.active_household_id():
            return None
        return owner

    def apply(self, zone_id, plan):
        if threading.get_ident() != self.thread_id:
            raise RuntimeError('Écriture du jeu interdite hors thread principal')
        owner = self._target(zone_id)
        if owner is None:
            log('terrain_apply', 'refused')
            return False
        plan = reduce_plan(plan)
        if len(plan['create']) + len(plan['remove']) > 2 * MAX_CHANGES:
            log('terrain_apply', 'too_large')
            return False
        targets = self._targets(plan['remove'])
        made, skipped, missing = [], 0, 0
        for row in plan['create']:
            if row[7] == 1:
                skipped += 1
                continue
            item = self._create(row, zone_id, owner)
            if item is None:
                # Pack ou contenu absent chez ce joueur : cet objet manque, le reste du terrain est appliqué.
                skipped += 1
                missing += 1
                continue
            if item is False:
                self._destroy(made)  # Aucun meuble existant retiré avant réussite des remplacements.
                self.last = {'created': 0, 'removed': 0, 'skipped': skipped + 1}
                return False
            made.append(item)
        if self._target(zone_id) != owner:
            self._destroy(made)
            return False
        removed = self._destroy(targets)
        self.last = {'created': len(made), 'removed': removed, 'skipped': skipped}
        log('terrain_apply', 'created:%d removed:%d skipped:%d missing:%d' % (len(made), removed, skipped, missing))
        return True

    def _targets(self, rows):
        if not rows:
            return []
        wanted = collections.Counter(tuple(row) for row in rows if row[7] == 0)
        targets = []
        # Liste figée avant toute destruction : on ne modifie pas le gestionnaire pendant son parcours.
        for item in list(self.services.object_manager().valid_objects()):
            try:
                row = self.row_of(item)
            except Exception:
                row = None
            key = tuple(row) if row else None
            if key is not None and wanted[key] > 0:
                wanted[key] -= 1
                targets.append(item)
        return targets

    @staticmethod
    def _destroy(targets):
        removed = 0
        for item in targets:
            try:
                item.destroy(source=item, cause=CAUSE)
                removed += 1
            except Exception as error:
                log('terrain_remove', type(error).__name__)
        return removed

    def _create(self, row, zone_id, owner):
        """Objet posé, None si la définition n'existe pas chez ce joueur, False pour toute autre erreur."""
        item = None
        try:
            item = self.create(int(row[0]))
            if item is None:
                return None
            self.place(item, int(zone_id), row)
            item.set_household_owner_id(owner)
            return item
        except Exception as error:
            log('terrain_create', type(error).__name__)
            if item is not None:
                # Objet créé mais non posé : il ne doit pas rester à l'origine du monde.
                try:
                    item.destroy(source=item, cause=CAUSE)
                except Exception as cleanup:
                    log('terrain_create', type(cleanup).__name__)
            return False


def game_applier(reader):
    import routing
    import services
    import sims4.math
    from objects.system import create_object

    def place(item, zone_id, row):
        surface = routing.SurfaceIdentifier(zone_id, row[5], routing.SurfaceType.SURFACETYPE_WORLD)
        item.move_to(translation=sims4.math.Vector3(row[1] / 1000.0, row[2] / 1000.0, row[3] / 1000.0),
                     orientation=sims4.math.angle_to_yaw_quaternion(row[4] / 1000.0), routing_surface=surface)
        if row[6] != 1000:
            item.scale = row[6] / 1000.0

    return TerrainApplier(services, reader._row, create_object, place)
