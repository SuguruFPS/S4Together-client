using System.Globalization;

namespace S4Together.Launcher.Core;

/// <summary>Union des parties solo connues ; une limite dépassée bloque le lancement, jamais une troncature.</summary>
public static class SoloProtection
{
    const int MaxIds = 1024; // sous le plafond du mod (solo_guard.MAX_IDS = 2048)
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
        return new(characterId, households.Order(StringComparer.Ordinal).ToArray(), sims.Order(StringComparer.Ordinal).ToArray(), previous?.AllowedSaveSlotId);
    }

    /// <summary>Sauvegardes locales qu'un personnage en attente peut reprendre : une par fichier, la plus récente d'abord.</summary>
    public static IReadOnlyList<SaveChoice> Saves(IEnumerable<LocalSim> sims) => sims
        .Where(s => s.Source == "Partie enregistrée" && IsSlotId(s.SaveId))
        .GroupBy(s => s.FilePath, StringComparer.OrdinalIgnoreCase)
        .Select(g => new SaveChoice(g.First().SaveId, g.First().SaveName, g.Key, g.Max(s => s.SavedAtUtc),
            g.Select(s => s.Household).Where(h => h.Length > 0).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray()))
        .OrderByDescending(c => c.SavedAtUtc).ToArray();

    /// <summary>Même format que le mod (solo_guard._NATIVE) : décimal sans zéro initial, 1 à 20 chiffres.</summary>
    public static bool IsSlotId(string value) =>
        value.Length is > 0 and <= 20 && value[0] != '0' && value.All(char.IsAsciiDigit);
    static InvalidOperationException TooMany() => new("Trop de Sims pour vérifier la protection solo. Choisis le dossier du profil utilisé dans Paramètres.");
}

public sealed record SaveChoice(string SlotId, string Name, string FilePath, DateTime SavedAtUtc, IReadOnlyList<string> Households)
{
    public string Label => $"{Name} — {string.Join(", ", Households)} — {SavedAtUtc.ToLocalTime():dd/MM/yyyy HH:mm}";
}
