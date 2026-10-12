using System.ComponentModel;
using System.Windows.Media;
using S4Together.Launcher.Core;

namespace S4Together.Launcher;

/// <summary>Présentation locale ; les métadonnées originales restent séparées de l'identité serveur.</summary>
public sealed class LocalSimCard(LocalSim? data) : INotifyPropertyChanged
{
    public LocalSim? Data { get; } = data;
    public bool IsNew => Data is null;
    public string Name => Data?.Name ?? "Nouveau personnage";
    public string Household => Data?.Household ?? "Créer un Sim dans Les Sims 4";
    public string SaveName => Data?.SaveName ?? "";
    public DateTime SavedAtUtc => Data?.SavedAtUtc ?? DateTime.MinValue;
    ImageSource? portrait;
    public ImageSource? Portrait { get => portrait; set { portrait = value; PropertyChanged?.Invoke(this, new(nameof(Portrait))); } }
    public event PropertyChangedEventHandler? PropertyChanged;
}
