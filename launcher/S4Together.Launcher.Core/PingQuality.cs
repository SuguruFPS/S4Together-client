namespace S4Together.Launcher.Core;

/// <summary>Qualité perçue de la latence affichée dans le launcher.</summary>
public enum PingLevel { Unknown, Good, Fair, Poor }

/// <summary>Classe un aller-retour heartbeat et le formate pour l'affichage.</summary>
public static class PingQuality
{
    /// <summary>En dessous : jeu fluide.</summary>
    public const long GoodBelowMs = 80;
    /// <summary>En dessous : retard perceptible mais jouable ; au-dessus : mauvais.</summary>
    public const long FairBelowMs = 150;

    public static PingLevel Classify(long? milliseconds) => milliseconds switch
    {
        null or < 0 => PingLevel.Unknown,
        < GoodBelowMs => PingLevel.Good,
        < FairBelowMs => PingLevel.Fair,
        _ => PingLevel.Poor,
    };

    public static string Format(long? milliseconds) =>
        milliseconds is null or < 0 ? "" : $"{milliseconds} ms";
}
