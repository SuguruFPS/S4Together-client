"""Besoins locaux des seules copies S4T : jamais les jauges du propriétaire réel ou du joueur local.

Le clone visuel n'a pas sa propre vie de PNJ. Neutraliser ses motifs visibles de base évite
qu'il joue une urgence vessie/faim locale sans rapport avec ce que fait son propriétaire.
Ce module ne transporte aucune jauge et ne modifie ni compétences, ni fonds, ni relations.
"""
import math
import weakref
from .remote_household import parse_description


class ProxyMotiveGuard:
    def __init__(self):
        self.records = {}

    def release(self, sim):
        record = self.records.get(id(sim))
        if record is not None and record[0]() is sim:
            sim.remove_statistic_modifier(record[1])  # Retirer uniquement notre propre handle.
        self.records.pop(id(sim), None)

    def update(self, sim, active_household, modifier):
        for key in [key for key, value in self.records.items() if value[0]() is None]:
            del self.records[key]
        info = sim.sim_info
        home = info.household
        if (type(active_household) is not int or active_household <= 0
                or info.is_selectable is not False or home.id == active_household
                or parse_description(getattr(home, 'description', None)) is None):
            self.release(sim)
            return False
        tracker = info.commodity_tracker
        kinds = set()
        for stat in tracker.get_all_commodities():
            if (getattr(stat, 'core', False) is True and getattr(stat, 'is_visible', False) is True
                    and getattr(stat, 'is_ranked', True) is False and getattr(stat, 'is_skill', True) is False):
                value = getattr(type(stat), 'best_value', None)
                if type(value) in (int, float) and math.isfinite(value):
                    kinds.add(type(stat))
        known = self.records.get(id(sim))
        if known is not None and known[0]() is sim and known[2] == kinds:
            return True
        self.release(sim)
        if not kinds:
            return False
        # Préparer la référence avant toute écriture : une instance non traçable reste intacte.
        reference = weakref.ref(sim)
        handle = sim.add_statistic_modifier(modifier(decay_modifiers={kind: 0 for kind in kinds}))
        try:
            for kind in kinds:
                tracker.set_value(kind, kind.best_value)
        except Exception:
            sim.remove_statistic_modifier(handle)
            raise
        self.records[id(sim)] = (reference, handle, frozenset(kinds))
        return True
