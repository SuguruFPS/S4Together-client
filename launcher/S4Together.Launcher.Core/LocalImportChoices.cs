namespace S4Together.Launcher.Core;

/// <summary>Filtre de présentation uniquement : conserve le catalogue complet pour la reprise exacte.</summary>
public static class LocalImportChoices
{
    public static LocalSim PreferActiveSource(LocalSim sim)
    {
        // Le catalogue conserve toutes les sources : une copie récente comme voisin
        // ne doit pas masquer l'ancienne partie où ce Sim est le foyer actif.
        var best = new[] { sim }.Concat(sim.PortraitSources).Where(s => CanShow(s, null))
            .OrderBy(s => s.Source == "Partie enregistrée" ? 0 : 1)
            .ThenByDescending(s => s.SavedAtUtc).FirstOrDefault();
        return best is null ? sim : best with { PortraitSources = sim.PortraitSources };
    }

    public static bool CanShow(LocalSim sim, CharacterRoster? roster)
    {
        // Les animaux restent membres du foyer, jamais un personnage à importer seul.
        if (sim.IsPet) return false;
        // Les indicateurs « joué » et « joueur » peuvent aussi concerner des voisins.
        if (sim.Source == "Partie enregistrée" && !sim.IsActiveHousehold) return false;
        // Un nouveau membre ne transforme pas un foyer multijoueur en foyer solo.
        return roster?.Characters.Any(c => c.Bound && sim.HouseholdId != 0 &&
            c.GameHouseholdId == sim.HouseholdId.ToString(System.Globalization.CultureInfo.InvariantCulture)) != true;
    }
}
