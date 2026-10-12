using System.Globalization;

namespace S4Together.Launcher.Core;

/// <summary>Union des parties solo connues ; une limite dépassée bloque le lancement, jamais une troncature.</summary>
public static class SoloProtection
{
    const int MaxIds = 128;
    public static SoloGuard Merge(string characterId, SoloGuard? previous, IEnumerable<LocalSim> unboundSims)
    {
        if (previous is not null && previous.CharacterId != characterId)
            throw new InvalidOperationException("La protection solo appartient à un autre personnage.");
        var households = (previous?.BlockedHouseholdIds ?? []).ToHashSet(StringComparer.Ordinal);
        var sims = (previous?.BlockedSimIds ?? []).ToHashSet(StringComparer.Ordinal);
        foreach (var sim in unboundSims.Where(s => s.Source == "Partie enregistrée"))
        {
            if (sim.HouseholdId > 0) households.Add(sim.HouseholdId.ToString(CultureInfo.InvariantCulture));
            if (sim.SimId > 0) sims.Add(sim.SimId.ToString(CultureInfo.InvariantCulture));
            if (households.Count > MaxIds || sims.Count > MaxIds) throw TooMany();
        }
        if (households.Count > MaxIds || sims.Count > MaxIds) throw TooMany();
        return new(characterId, households.Order(StringComparer.Ordinal).ToArray(), sims.Order(StringComparer.Ordinal).ToArray());
    }
    static InvalidOperationException TooMany() => new("Trop de Sims pour vérifier la protection solo. Choisis le dossier du profil utilisé dans Paramètres.");
}
