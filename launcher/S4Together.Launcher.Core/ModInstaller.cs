using System.Security.Cryptography;
using System.Text.Json;

namespace S4Together.Launcher.Core;

public enum InstallState { Absent, UpToDate, OldVersion, Modified, Duplicate }

public sealed record ModPackage(string ScriptPath, string Sha256, string Version);

public sealed class InstallRefusedException(string message) : Exception(message);

/// <summary>Installation, mise à jour, réparation et retrait du .ts4script, uniquement dans le dossier Mods choisi.</summary>
public sealed class ModInstaller(string backupRoot, Func<bool>? gameRunning = null)
{
    public const string TargetFolder = "S4Together";
    public const string ScriptName = "S4Together-GameClient.ts4script";
    public const string ManifestName = ".s4t-install.json";
    static readonly string[] KnownScripts = ["S4Together-diagnostic-prototype.ts4script", ScriptName];
    readonly Func<bool> gameRunning = gameRunning ?? GameLocator.IsGameRunning;

    public static string FileHash(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexStringLower(SHA256.HashData(stream));
    }

    /// <summary>Paquet fourni avec le launcher : refusé si l'empreinte ne correspond pas à mod-readiness.json.</summary>
    public static ModPackage LoadPackage(string folder)
    {
        using var readiness = JsonDocument.Parse(File.ReadAllText(Path.Combine(folder, "mod-readiness.json")));
        var expected = readiness.RootElement.GetProperty("sha256").GetString() ?? "";
        var version = readiness.RootElement.TryGetProperty("version", out var v) ? v.GetString() ?? "inconnue" : "inconnue";
        var script = Path.Combine(folder, ScriptName);
        if (!File.Exists(script) || !string.Equals(FileHash(script), expected, StringComparison.OrdinalIgnoreCase))
            throw new InstallRefusedException("Le mod fourni avec le launcher est altéré : retélécharge le launcher.");
        return new ModPackage(script, expected.ToLowerInvariant(), version);
    }

    static string Root(string mods) => Path.GetFullPath(mods).TrimEnd('\\');

    List<string> LegacyCopies(string mods)
    {
        var root = Root(mods);
        var target = Path.Combine(root, TargetFolder, ScriptName);
        return Directory.EnumerateFiles(root, "*.ts4script", SearchOption.AllDirectories)
            .Where(f => KnownScripts.Contains(Path.GetFileName(f), StringComparer.OrdinalIgnoreCase))
            .Where(f => !string.Equals(Path.GetFullPath(f), target, StringComparison.OrdinalIgnoreCase))
            .ToList();
    }

    public InstallState State(string mods, string expectedSha)
    {
        var root = Root(mods);
        var target = Path.Combine(root, TargetFolder, ScriptName);
        var legacy = Directory.Exists(root) ? LegacyCopies(root) : [];
        if (!File.Exists(target)) return legacy.Count > 0 ? InstallState.OldVersion : InstallState.Absent;
        if (legacy.Count > 0) return InstallState.Duplicate;
        var actual = FileHash(target);
        if (string.Equals(actual, expectedSha, StringComparison.OrdinalIgnoreCase)) return InstallState.UpToDate;
        var manifest = Path.Combine(root, TargetFolder, ManifestName);
        if (File.Exists(manifest))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(manifest));
                if (string.Equals(doc.RootElement.GetProperty("sha256").GetString(), actual, StringComparison.OrdinalIgnoreCase)) return InstallState.OldVersion;
            }
            catch (Exception e) when (e is JsonException or KeyNotFoundException or InvalidOperationException) { return InstallState.Modified; }
        }
        return InstallState.Modified;
    }

    public static string Label(InstallState state) => state switch
    {
        InstallState.Absent => "Mod non installé.",
        InstallState.UpToDate => "Mod installé et à jour.",
        InstallState.OldVersion => "Ancienne version du mod : mise à jour disponible.",
        InstallState.Modified => "Le fichier du mod a été modifié : réparation conseillée.",
        InstallState.Duplicate => "Plusieurs copies du mod : réparation nécessaire (une seule copie doit rester).",
        _ => "",
    };

    void Guard(string mods)
    {
        if (gameRunning()) throw new InstallRefusedException("Ferme Les Sims 4 avant d'installer ou de retirer le mod.");
        var reason = GameLocator.ValidateModsFolder(mods);
        if (reason.Length > 0) throw new InstallRefusedException(reason);
    }

    static void RefuseRedirected(string root, string file)
    {
        for (var dir = new DirectoryInfo(Path.GetDirectoryName(file)!); dir is not null && dir.FullName.TrimEnd('\\').Length >= root.Length; dir = dir.Parent)
            if (dir.Attributes.HasFlag(FileAttributes.ReparsePoint)) throw new InstallRefusedException("Ancien mod dans un dossier redirigé : opération refusée.");
    }

    /// <summary>Installe ou répare : sauvegarde puis retire les anciennes copies, copie la version vérifiée. Aucun autre mod ni sauvegarde touché.</summary>
    public string Install(string mods, ModPackage package)
    {
        Guard(mods);
        var root = Root(mods);
        Directory.CreateDirectory(root);
        var targetDir = Path.Combine(root, TargetFolder);
        if (Directory.Exists(targetDir) && new DirectoryInfo(targetDir).Attributes.HasFlag(FileAttributes.ReparsePoint))
            throw new InstallRefusedException("Dossier redirigé refusé : choisis le dossier réel.");
        var old = LegacyCopies(root);
        var target = Path.Combine(targetDir, ScriptName);
        if (File.Exists(target)) old.Add(target);
        old.ForEach(f => RefuseRedirected(root, f));
        var backup = Path.Combine(backupRoot, DateTime.UtcNow.ToString("yyyyMMddHHmmss") + "-" + Guid.NewGuid().ToString("N")[..8]);
        foreach (var file in old)
        {
            Directory.CreateDirectory(backup);
            File.Copy(file, Path.Combine(backup, Guid.NewGuid().ToString("N") + ".ts4script"));
            File.Delete(file);
        }
        Directory.CreateDirectory(targetDir);
        var temporary = target + ".new";
        File.Copy(package.ScriptPath, temporary, true);
        if (!string.Equals(FileHash(temporary), package.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            File.Delete(temporary);
            throw new InstallRefusedException("Copie du mod corrompue : installation annulée.");
        }
        File.Move(temporary, target, true);
        File.WriteAllText(Path.Combine(targetDir, ManifestName),
            JsonSerializer.Serialize(new { version = package.Version, sha256 = package.Sha256, file = ScriptName }));
        return old.Count > 0 ? backup : "";
    }

    /// <summary>Mise à jour automatique d'une seule copie suivie et intacte, avec remplacement atomique.</summary>
    public bool UpdateManaged(string mods, ModPackage package)
    {
        Guard(mods);
        var root=Root(mods);var target=Path.Combine(root,TargetFolder,ScriptName);
        var manifest=Path.Combine(root,TargetFolder,ManifestName);
        if(!File.Exists(target)||!File.Exists(manifest)||State(mods,package.Sha256)!=InstallState.OldVersion)return false;
        RefuseRedirected(root,target);RefuseRedirected(root,manifest);
        using(var installed=JsonDocument.Parse(File.ReadAllText(manifest)))
        {
            if(!installed.RootElement.TryGetProperty("version",out var stored)||stored.ValueKind!=JsonValueKind.String)return false;
            var installedVersion=stored.GetString()?.Split('-')[0];
            if(!Version.TryParse(installedVersion,out var previous)||!Version.TryParse(package.Version.Split('-')[0],out var next)||next<previous)return false;
        }
        if(FileHash(package.ScriptPath)!=package.Sha256)throw new InstallRefusedException("Le mod fourni est altéré : ancienne version conservée.");
        var backup=Path.Combine(backupRoot,"auto-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(backup);
        var saved=Path.Combine(backup,ScriptName);var savedManifest=Path.Combine(backup,ManifestName);
        File.Copy(target,saved);File.Copy(manifest,savedManifest);
        var pending=target+".new-"+Guid.NewGuid().ToString("N");var pendingManifest=manifest+".new-"+Guid.NewGuid().ToString("N");
        var replaced=false;
        try
        {
            File.Copy(package.ScriptPath,pending);
            if(FileHash(pending)!=package.Sha256)throw new InstallRefusedException("Copie du mod altérée : ancienne version conservée.");
            File.WriteAllText(pendingManifest,JsonSerializer.Serialize(new{version=package.Version,sha256=package.Sha256,file=ScriptName}));
            Guard(mods);
            if(FileHash(target)!=FileHash(saved))throw new InstallRefusedException("Le mod a changé pendant sa mise à jour.");
            File.Replace(pending,target,null);replaced=true;
            File.Move(pendingManifest,manifest,true);
            return true;
        }
        catch
        {
            if(replaced)
            {
                File.Copy(saved,pending,true);File.Replace(pending,target,null);
                File.Copy(savedManifest,manifest,true);
            }
            throw;
        }
        finally
        {
            if(File.Exists(pending))File.Delete(pending);
            if(File.Exists(pendingManifest))File.Delete(pendingManifest);
        }
    }

    /// <summary>Retire uniquement une copie reconnue (à jour ou ancienne version suivie). Un fichier modifié à la main est refusé.</summary>
    public void Uninstall(string mods, string expectedSha)
    {
        Guard(mods);
        var state = State(mods, expectedSha);
        if (state is InstallState.Modified or InstallState.Duplicate)
            throw new InstallRefusedException("Fichier du mod modifié ou en double : utilise « Réparer » d'abord, ou retire-le à la main.");
        var targetDir = Path.Combine(Root(mods), TargetFolder);
        var target = Path.Combine(targetDir, ScriptName);
        if (File.Exists(target))
        {
            var backup = Path.Combine(backupRoot, "uninstall-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss"));
            Directory.CreateDirectory(backup);
            File.Copy(target, Path.Combine(backup, ScriptName), true);
            File.Delete(target);
        }
        var manifest = Path.Combine(targetDir, ManifestName);
        if (File.Exists(manifest)) File.Delete(manifest);
        if (Directory.Exists(targetDir) && !Directory.EnumerateFileSystemEntries(targetDir).Any()) Directory.Delete(targetDir);
    }

    /// <summary>Active contenu personnalisé et mods de script dans Options.ini après sauvegarde. Jeu fermé obligatoire (il réécrit ce fichier).</summary>
    public string EnableScriptMods(string mods)
    {
        Guard(mods);
        var options = Path.Combine(Path.GetDirectoryName(Root(mods))!, "Options.ini");
        var lines = File.ReadAllLines(options).ToList();
        var backup = Path.Combine(backupRoot, "Options-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss") + ".ini");
        Directory.CreateDirectory(backupRoot);
        File.Copy(options, backup, false);
        void Set(string key, string value)
        {
            var index = lines.FindIndex(l => l.Split('=', 2)[0].Trim().Equals(key, StringComparison.OrdinalIgnoreCase));
            if (index >= 0) { lines[index] = key + " = " + value; return; }
            var section = lines.FindIndex(l => l.Trim().Equals("[options]", StringComparison.OrdinalIgnoreCase));
            lines.Insert(section >= 0 ? section + 1 : 0, key + " = " + value);
            if (section < 0) lines.Insert(0, "[options]");
        }
        Set("modsdisabled", "0");
        Set("scriptmodsenabled", "1");
        var temporary = options + ".s4t-new";
        File.WriteAllLines(temporary, lines);
        File.Move(temporary, options, true);
        return backup;
    }
}
