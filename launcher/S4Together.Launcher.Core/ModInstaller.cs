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
        if (File.Exists(file) && File.GetAttributes(file).HasFlag(FileAttributes.ReparsePoint))
            throw new InstallRefusedException("Fichier du mod redirigé : opération refusée.");
        for (var dir = new DirectoryInfo(Path.GetDirectoryName(file)!); dir is not null && dir.FullName.TrimEnd('\\').Length >= root.Length; dir = dir.Parent)
            if (dir.Attributes.HasFlag(FileAttributes.ReparsePoint)) throw new InstallRefusedException("Ancien mod dans un dossier redirigé : opération refusée.");
    }

    static void Writable(string file)
    {
        if (File.Exists(file)) File.SetAttributes(file, File.GetAttributes(file) & ~FileAttributes.ReadOnly);
    }

    static void DeleteTemporary(string file)
    {
        try { if (File.Exists(file)) { Writable(file); File.Delete(file); } }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    /// <summary>Prépare et vérifie toutes les copies avant remplacement. Un refus restaure les fichiers suivis.</summary>
    public string Install(string mods, ModPackage package)
    {
        Guard(mods);
        var root = Root(mods);
        Directory.CreateDirectory(root);
        var targetDir = Path.Combine(root, TargetFolder);
        if (Directory.Exists(targetDir) && new DirectoryInfo(targetDir).Attributes.HasFlag(FileAttributes.ReparsePoint))
            throw new InstallRefusedException("Dossier redirigé refusé : choisis le dossier réel.");
        // Vérifier la source AVANT de retirer la moindre ancienne copie.
        if (!string.Equals(FileHash(package.ScriptPath), package.Sha256, StringComparison.OrdinalIgnoreCase))
            throw new InstallRefusedException("Le mod fourni est altéré : ancienne version conservée. Retélécharge le launcher.");
        Directory.CreateDirectory(targetDir);
        var old = LegacyCopies(root);
        var target = Path.Combine(targetDir, ScriptName);
        var manifest = Path.Combine(targetDir, ManifestName);
        if (Directory.Exists(target) || Directory.Exists(manifest))
            throw new InstallRefusedException("Un dossier occupe le nom du fichier du mod. Installation refusée ; aucun fichier supprimé.");
        if (File.Exists(target)) old.Add(target);
        old.ForEach(f => RefuseRedirected(root, f));
        var backup = Path.Combine(backupRoot, DateTime.UtcNow.ToString("yyyyMMddHHmmss") + "-" + Guid.NewGuid().ToString("N")[..8]);
        var saved = new Dictionary<string, (string File, string Hash, FileAttributes Attributes)>(StringComparer.OrdinalIgnoreCase);
        if (File.Exists(manifest)) RefuseRedirected(root, manifest);
        var oldManifest = File.Exists(manifest) ? File.ReadAllBytes(manifest) : null;
        var oldManifestAttributes = File.Exists(manifest) ? File.GetAttributes(manifest) : FileAttributes.Normal;
        var temporary = target + ".new-" + Guid.NewGuid().ToString("N");
        var temporaryManifest = manifest + ".new-" + Guid.NewGuid().ToString("N");
        var manifestBytes = JsonSerializer.SerializeToUtf8Bytes(new { version = package.Version, sha256 = package.Sha256, file = ScriptName });
        bool targetChanged = false, manifestChanged = false, completed = false;
        var removed = new List<string>();
        try
        {
            foreach (var file in old)
            {
                Directory.CreateDirectory(backup);
                var copy = Path.Combine(backup, Guid.NewGuid().ToString("N") + ".ts4script");
                var hash = FileHash(file);
                File.Copy(file, copy);
                if (FileHash(copy) != hash) throw new InstallRefusedException("Sauvegarde du mod non vérifiée : installation annulée.");
                saved[file] = (copy, hash, File.GetAttributes(file));
            }
            if (oldManifest is not null)
            {
                var metadata = Path.Combine(backup, "metadata"); Directory.CreateDirectory(metadata);
                File.WriteAllBytes(Path.Combine(metadata, ManifestName), oldManifest);
            }
            File.Copy(package.ScriptPath, temporary);
            Writable(temporary);
            if (!string.Equals(FileHash(temporary), package.Sha256, StringComparison.OrdinalIgnoreCase))
                throw new InstallRefusedException("Copie du mod corrompue : ancienne version conservée.");
            File.WriteAllBytes(temporaryManifest, manifestBytes);
            Guard(mods);
            // Pré-vol de toutes les anciennes copies : détecter un verrou avant de remplacer quoi que ce soit.
            foreach (var file in old)
            {
                if (FileHash(file) != saved[file].Hash) throw new InstallRefusedException("Le mod a changé pendant sa mise à jour.");
                Writable(file);
                using var access = new FileStream(file, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
            }
            if (File.Exists(manifest))
            {
                if (oldManifest is null || !File.ReadAllBytes(manifest).SequenceEqual(oldManifest))
                    throw new InstallRefusedException("Le manifeste du mod a changé pendant sa mise à jour.");
                Writable(manifest);
                using var access = new FileStream(manifest, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
            }
            if (File.Exists(target)) File.Replace(temporary, target, null);
            else File.Move(temporary, target);
            targetChanged = true;
            File.Move(temporaryManifest, manifest, true); manifestChanged = true;
            foreach (var file in old.Where(f => !f.Equals(target, StringComparison.OrdinalIgnoreCase)))
            { File.Delete(file); removed.Add(file); }
            completed = true;
            return old.Count > 0 ? backup : "";
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or InstallRefusedException)
        {
            try
            {
                if (targetChanged && File.Exists(target) && FileHash(target) == package.Sha256)
                {
                    if (saved.TryGetValue(target, out var previous))
                    { File.Copy(previous.File, temporary, true); Writable(temporary); File.Replace(temporary, target, null); }
                    else File.Delete(target);
                }
                if (manifestChanged && File.Exists(manifest) && File.ReadAllBytes(manifest).SequenceEqual(manifestBytes))
                {
                    if (oldManifest is null) File.Delete(manifest);
                    else { File.WriteAllBytes(temporaryManifest, oldManifest); File.Move(temporaryManifest, manifest, true); }
                }
                foreach (var file in removed)
                    if (!File.Exists(file)) File.Copy(saved[file].File, file);
            }
            catch (Exception rollback) when (rollback is IOException or UnauthorizedAccessException)
            { throw new InstallRefusedException($"Windows bloque la restauration du mod. Les anciennes copies sont sauvegardées ici : {backup}"); }
            if (error is InstallRefusedException) throw;
            throw new InstallRefusedException("Windows refuse de remplacer le mod (fichier verrouillé ou accès protégé). " +
                "L'ancienne version est conservée. Ferme Les Sims 4 et les autres launchers, puis réessaie. " +
                $"Dossier concerné : {targetDir}");
        }
        finally
        {
            foreach (var (file, previous) in saved)
            {
                try { if (!(completed && file.Equals(target, StringComparison.OrdinalIgnoreCase)) && File.Exists(file) && FileHash(file) == previous.Hash) File.SetAttributes(file, previous.Attributes); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            if (!completed && oldManifest is not null)
            {
                try { if (File.Exists(manifest) && File.ReadAllBytes(manifest).SequenceEqual(oldManifest)) File.SetAttributes(manifest, oldManifestAttributes); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            DeleteTemporary(temporary); DeleteTemporary(temporaryManifest);
        }
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
        Install(mods, package);
        return true;
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
