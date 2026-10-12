using System.Diagnostics;
using System.Text.RegularExpressions;
using Microsoft.Win32;

namespace S4Together.Launcher.Core;

public enum GameStore { EaApp, Steam }

public sealed record GameInstall(GameStore Store, string Path, string? Version)
{
    public string Executable => System.IO.Path.Combine(Path, "Game", "Bin", "TS4_x64.exe");
    public string StoreLabel => Store == GameStore.Steam ? "Steam" : "EA app";
}

/// <summary>Détection en lecture seule de l'installation et du profil Les Sims 4.</summary>
public static partial class GameLocator
{
    public const int SteamAppId = 1222670;
    const int OptionsMaxLines = 2000;

    [GeneratedRegex("\"path\"\\s+\"((?:[^\"\\\\]|\\\\.)*)\"")]
    private static partial Regex SteamPathPattern();

    public static IReadOnlyList<string> SteamLibraryPaths(string vdf) =>
        SteamPathPattern().Matches(vdf).Select(m => m.Groups[1].Value.Replace("\\\\", "\\")).ToList();

    public static IReadOnlyList<GameInstall> FindInstalls(IEnumerable<string>? maxisPaths = null,
        IEnumerable<string>? steamLibraries = null, IEnumerable<string>? defaultPaths = null)
    {
        maxisPaths ??= RegistryMaxisPaths();
        steamLibraries ??= RegistrySteamLibraries();
        defaultPaths ??= DefaultEaPaths();
        var candidates = new List<(GameStore, string)>();
        candidates.AddRange(steamLibraries.Select(l => (GameStore.Steam, Path.Combine(l, "steamapps", "common", "The Sims 4"))));
        candidates.AddRange(maxisPaths.Select(p => (IsUnderSteam(p, steamLibraries) ? GameStore.Steam : GameStore.EaApp, p)));
        candidates.AddRange(defaultPaths.Select(p => (GameStore.EaApp, p)));
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var result = new List<GameInstall>();
        foreach (var (store, raw) in candidates)
        {
            if (string.IsNullOrWhiteSpace(raw)) continue;
            string full;
            try { full = Path.GetFullPath(raw).TrimEnd('\\'); } catch (Exception e) when (e is ArgumentException or NotSupportedException or PathTooLongException) { continue; }
            var exe = Path.Combine(full, "Game", "Bin", "TS4_x64.exe");
            if (!File.Exists(exe) || !seen.Add(full)) continue;
            result.Add(new GameInstall(store, full, FileVersionInfo.GetVersionInfo(exe).FileVersion));
        }
        return result.OrderBy(g => g.Store == GameStore.Steam ? 0 : 1).ToList();
    }

    static bool IsUnderSteam(string path, IEnumerable<string> libraries) =>
        libraries.Any(l => path.StartsWith(l.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase));

    static IEnumerable<string> RegistryMaxisPaths()
    {
        foreach (var key in new[] { @"SOFTWARE\WOW6432Node\Maxis\The Sims 4", @"SOFTWARE\Maxis\The Sims 4" })
        {
            using var k = Registry.LocalMachine.OpenSubKey(key);
            if (k?.GetValue("Install Dir") is string dir) yield return dir;
        }
    }

    static IEnumerable<string> RegistrySteamLibraries()
    {
        using var k = Registry.CurrentUser.OpenSubKey(@"Software\Valve\Steam");
        if (k?.GetValue("SteamPath") is not string steam) return [];
        var vdf = Path.Combine(steam.Replace('/', '\\'), "steamapps", "libraryfolders.vdf");
        try { return File.Exists(vdf) ? SteamLibraryPaths(File.ReadAllText(vdf)) : [steam]; }
        catch (IOException) { return [steam]; }
    }

    static IEnumerable<string> DefaultEaPaths()
    {
        foreach (var root in new[] { Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86) })
        {
            if (string.IsNullOrEmpty(root)) continue;
            yield return Path.Combine(root, "EA Games", "The Sims 4");
            yield return Path.Combine(root, "Origin Games", "The Sims 4");
        }
    }

    /// <summary>Racines Documents possibles : le dossier Documents de Windows peut avoir été déplacé.</summary>
    public static IReadOnlyList<string> DocumentsCandidates()
    {
        var list = new List<string> { Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments) };
        var profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        list.Add(Path.Combine(profile, "Documents"));
        list.Add(Path.Combine(profile, "OneDrive", "Documents"));
        foreach (var drive in DriveInfo.GetDrives())
            if (drive.DriveType == DriveType.Fixed && drive.IsReady) list.Add(Path.Combine(drive.RootDirectory.FullName, "Documents"));
        return list.Where(p => !string.IsNullOrEmpty(p)).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
    }

    /// <summary>Dossiers Mods des profils : tout sous-dossier d'Electronic Arts contenant Options.ini (nom localisé).</summary>
    public static IReadOnlyList<string> FindModsFolders(IEnumerable<string>? documents = null)
    {
        var result = new List<string>();
        foreach (var root in documents ?? DocumentsCandidates())
        {
            var ea = Path.Combine(root, "Electronic Arts");
            if (!Directory.Exists(ea)) continue;
            foreach (var profile in Directory.EnumerateDirectories(ea).Order(StringComparer.OrdinalIgnoreCase))
                if (File.Exists(Path.Combine(profile, "Options.ini"))) result.Add(Path.Combine(profile, "Mods"));
        }
        return result.Distinct(StringComparer.OrdinalIgnoreCase).ToList();
    }

    /// <summary>Chaîne vide si le dossier est un dossier Mods de profil valide, sinon la raison en français.</summary>
    public static string ValidateModsFolder(string? mods)
    {
        if (string.IsNullOrWhiteSpace(mods)) return "Choisis le dossier Mods de ton profil Les Sims 4.";
        string full;
        try { full = Path.GetFullPath(mods).TrimEnd('\\'); } catch (Exception e) when (e is ArgumentException or NotSupportedException or PathTooLongException) { return "Chemin invalide."; }
        if (!string.Equals(Path.GetFileName(full), "Mods", StringComparison.OrdinalIgnoreCase)) return "Le dossier choisi doit s'appeler Mods.";
        var parent = Path.GetDirectoryName(full);
        if (parent is null || !File.Exists(Path.Combine(parent, "Options.ini"))) return "Ce dossier Mods n'est pas dans un profil Les Sims 4 (Options.ini introuvable à côté).";
        if (Directory.Exists(full) && new DirectoryInfo(full).Attributes.HasFlag(FileAttributes.ReparsePoint)) return "Dossier redirigé refusé : choisis le dossier réel.";
        return "";
    }

    public sealed record ModSettings(bool ModsEnabled, bool ScriptModsEnabled)
    {
        public string Problem => !ModsEnabled
            ? "Le contenu personnalisé est désactivé : Options > Options du jeu > Autre > Activer le contenu personnalisé et les mods, puis redémarrer le jeu."
            : !ScriptModsEnabled
                ? "Les mods de script sont désactivés : Options > Options du jeu > Autre > Mods de script autorisés, puis redémarrer le jeu."
                : "";
    }

    /// <summary>Lecture seule d'Options.ini (bornée).</summary>
    public static ModSettings ReadModSettings(string mods)
    {
        var options = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(mods).TrimEnd('\\'))!, "Options.ini");
        bool modsDisabled = false, scripts = false;
        foreach (var line in File.ReadLines(options).Take(OptionsMaxLines))
        {
            var parts = line.Split('=', 2);
            if (parts.Length != 2) continue;
            var key = parts[0].Trim().ToLowerInvariant();
            var value = parts[1].Trim();
            if (key == "modsdisabled") modsDisabled = value == "1";
            else if (key == "scriptmodsenabled") scripts = value == "1";
        }
        return new ModSettings(!modsDisabled, scripts);
    }

    [GeneratedRegex("^(EP|GP|SP|FP)\\d{2}$")]
    private static partial Regex PackPattern();

    /// <summary>Packs installés (dossiers EP01, GP02, SP03, FP01… à la racine du jeu), triés.</summary>
    public static IReadOnlyList<string> DetectPacks(string installPath)
    {
        if (!Directory.Exists(installPath)) return [];
        return Directory.EnumerateDirectories(installPath).Select(System.IO.Path.GetFileName).OfType<string>()
            .Where(n => PackPattern().IsMatch(n)).Order(StringComparer.Ordinal).ToList();
    }

    /// <summary>Plafond du serveur (DLCService.MaxDlcs) : une installation complète, kits compris, tient dessous.</summary>
    public const int MaxPacksSent = 256;

    static int PackRank(string pack) => pack[..2] switch { "EP" => 0, "FP" => 1, "GP" => 2, _ => 3 };

    /// <summary>Packs annoncés au serveur : extensions, Free Packs et packs de jeu d'abord (ce sont eux qui conditionnent les terrains), kits ensuite, plafond respecté.</summary>
    public static IReadOnlyList<string> PacksForServer(IEnumerable<string> packs) =>
        packs.Where(p => PackPattern().IsMatch(p)).Distinct(StringComparer.Ordinal)
            .OrderBy(PackRank).ThenBy(p => p, StringComparer.Ordinal).Take(MaxPacksSent).ToList();

    public static string PacksSummary(IReadOnlyList<string> packs) =>
        string.Join(" · ", new[] { "EP", "GP", "SP", "FP" }.Select(k => (k, n: packs.Count(p => p.StartsWith(k, StringComparison.Ordinal)))).Where(x => x.n > 0).Select(x => $"{x.k} {x.n}"));

    public static bool IsGameRunning() =>
        Process.GetProcessesByName("TS4_x64").Length > 0 || Process.GetProcessesByName("TS4").Length > 0;
}
