using System.Globalization;
using System.Text.Json.Nodes;

namespace S4Together.Launcher.Core;

/// <summary>Plafonds de sanction accordés par le serveur au rôle courant (<c>sanctionLimits</c>).</summary>
public sealed record SanctionLimits(int MaxDurationSeconds, bool PermanentBan)
{
    public static readonly SanctionLimits None = new(0, false);

    public static SanctionLimits FromJson(JsonObject? limits)
    {
        if (limits is null) return None;
        var max = limits["maxDurationSeconds"] is JsonValue m && m.TryGetValue<int>(out var seconds) && seconds > 0 ? seconds : 0;
        var permanent = limits["permanentBan"] is JsonValue p && p.TryGetValue<bool>(out var b) && b;
        return new SanctionLimits(max, permanent);
    }
}

/// <summary>Durée proposée ; <c>Seconds</c> nul = bannissement définitif.</summary>
public sealed record SanctionDuration(int? Seconds, string Label);

public sealed record SanctionEntry(string Id, string TargetPlayerId, string Kind, string Reason, string ActorRole,
    DateTimeOffset AppliedAt, DateTimeOffset? ExpiresAt, bool Active);

/// <summary>
/// Sanctions admin (<c>admin.sanction.apply|revoke|list</c>). Le launcher ne propose que ce que le serveur déclare
/// pour le rôle courant ; le serveur revérifie hiérarchie, durée et motif à chaque requête.
/// </summary>
public static class AdminSanctions
{
    public const int ReasonMaxLength = 1000;
    public static readonly IReadOnlyList<string> Kinds = ["warn", "mute", "build_suspend", "kick", "ban"];

    static readonly SanctionDuration[] Presets =
    [
        new(600, "10 minutes"), new(3600, "1 heure"), new(86400, "24 heures"),
        new(604800, "7 jours"), new(2592000, "30 jours"), new(31536000, "1 an"),
    ];

    public static string KindLabel(string kind) => kind switch
    {
        "warn" => "Avertissement",
        "mute" => "Silence dans le tchat",
        "build_suspend" => "Construction suspendue",
        "kick" => "Expulsion",
        "ban" => "Bannissement",
        _ => kind,
    };

    /// <summary>Le serveur exige une durée pour mute et build_suspend, la refuse pour warn et kick.</summary>
    public static bool RequiresDuration(string kind) => kind is "mute" or "build_suspend";
    public static bool AcceptsDuration(string kind) => kind is "mute" or "build_suspend" or "ban";

    public static IReadOnlyList<SanctionDuration> Durations(string kind, SanctionLimits limits)
    {
        if (!AcceptsDuration(kind)) return [];
        var list = Presets.Where(d => d.Seconds <= limits.MaxDurationSeconds).ToList();
        if (kind == "ban" && limits.PermanentBan) list.Add(new SanctionDuration(null, "Définitif"));
        return list;
    }

    /// <summary>Charge utile exacte attendue par le serveur ; <see cref="ArgumentException"/> en français si incomplète.</summary>
    public static JsonObject ApplyPayload(string targetPlayerId, string kind, SanctionDuration? duration, string? reason)
    {
        if (!Kinds.Contains(kind)) throw new ArgumentException("Type de sanction inconnu.");
        var motive = (reason ?? "").Trim();
        if (motive.Length == 0) throw new ArgumentException("Indique un motif : il est enregistré dans l'audit du serveur.");
        if (motive.Length > ReasonMaxLength) throw new ArgumentException($"Motif trop long ({ReasonMaxLength} caractères au plus).");
        if (AcceptsDuration(kind) && duration is null) throw new ArgumentException("Choisis une durée.");
        if (RequiresDuration(kind) && duration?.Seconds is null) throw new ArgumentException("Cette sanction doit avoir une durée.");
        var payload = new JsonObject { ["targetPlayerId"] = targetPlayerId, ["kind"] = kind, ["reason"] = motive };
        if (AcceptsDuration(kind) && duration?.Seconds is int seconds) payload["durationSeconds"] = seconds;
        return payload;
    }

    public static IReadOnlyList<SanctionEntry> FromList(ServerReply? reply)
    {
        if (reply is not { Ok: true } || reply.Payload?["sanctions"] is not JsonArray items) return [];
        var list = new List<SanctionEntry>();
        foreach (var item in items.OfType<JsonObject>())
        {
            if (Text(item, "sanctionId") is not { } id || Text(item, "targetPlayerId") is not { } target
                || Text(item, "kind") is not { } kind || Date(item["appliedAt"]) is not { } applied) continue;
            list.Add(new SanctionEntry(id, target, kind, Text(item, "reason") ?? "", Text(item, "actorRole") ?? "",
                applied, Date(item["expiresAt"]), item["active"] is JsonValue a && a.TryGetValue<bool>(out var active) && active));
        }
        return list;
    }

    /// <summary>Ligne lisible : « Silence dans le tchat · jusqu'au 12/10/2026 14:00 · en cours · motif ».</summary>
    public static string Describe(SanctionEntry entry, TimeZoneInfo? zone = null)
    {
        zone ??= TimeZoneInfo.Local;
        string Local(DateTimeOffset d) => TimeZoneInfo.ConvertTime(d, zone).ToString("dd/MM/yyyy HH:mm", CultureInfo.InvariantCulture);
        var instant = entry.Kind is "warn" or "kick";
        var when = instant ? Local(entry.AppliedAt) : entry.ExpiresAt is { } end ? "jusqu'au " + Local(end) : "définitif";
        var state = instant ? "" : entry.Active ? " · en cours" : " · terminée";
        return $"{KindLabel(entry.Kind)} · {when}{state} · {entry.Reason}";
    }

    /// <summary>Message d'un refus <c>banned</c> : le serveur joint l'échéance (UTC) ou null si définitif.</summary>
    public static string BannedMessage(JsonObject? payload, TimeZoneInfo? zone = null)
    {
        if (Date(payload?["expiresAt"]) is not { } end) return "Tu es banni définitivement de ce serveur.";
        var local = TimeZoneInfo.ConvertTime(end, zone ?? TimeZoneInfo.Local);
        return $"Tu es banni de ce serveur jusqu'au {local.ToString("dd/MM/yyyy HH:mm", CultureInfo.InvariantCulture)}.";
    }

    static string? Text(JsonObject item, string key) => item[key] is JsonValue v && v.TryGetValue<string>(out var s) && s.Length > 0 ? s : null;

    static DateTimeOffset? Date(JsonNode? node) =>
        node is JsonValue v && v.TryGetValue<string>(out var s)
        && DateTimeOffset.TryParse(s, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var d) ? d : null;
}
