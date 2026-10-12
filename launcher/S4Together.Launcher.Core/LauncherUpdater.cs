using System.Text.Json;
using System.Text.RegularExpressions;
using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;

namespace S4Together.Launcher.Core;

public sealed record LauncherRelease(Version Version, string Tag, Uri ZipUrl, Uri HashUrl, long Size);
public sealed record PreparedLauncherUpdate(string SourceDirectory, string TargetDirectory, string PlanPath, Version Version, string Sha256);

public sealed class LauncherUpdater : IDisposable
{
    readonly HttpClient client;
    readonly string stagingRoot;
    public LauncherUpdater(HttpMessageHandler? handler = null, string? stagingRoot = null)
    {
        client = new HttpClient(handler ?? new SocketsHttpHandler { AllowAutoRedirect = false, UseCookies = false, UseProxy = false });
        client.Timeout = Timeout.InfiniteTimeSpan;
        client.DefaultRequestHeaders.UserAgent.ParseAdd("S4Together-Launcher/1.0");
        this.stagingRoot = Path.GetFullPath(stagingRoot ?? Path.Combine(LauncherSettings.DefaultFolder, "updates"));
    }
    public async Task<LauncherRelease?> CheckAsync(Version currentVersion, CancellationToken cancel = default)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancel); deadline.CancelAfter(TimeSpan.FromSeconds(10));
        using var response = await GetAsync(new Uri($"https://api.github.com/repos/{Repository}/releases/latest"), false, deadline.Token);
        if (response.StatusCode == HttpStatusCode.NotFound) return null;
        Require(response.IsSuccessStatusCode, "Vérification GitHub indisponible.");
        using var bytes = new MemoryStream(); await CopyBounded(response, bytes, 524288, deadline.Token);
        using var metadata = JsonDocument.Parse(bytes.ToArray());
        var release = ReadRelease(metadata.RootElement, currentVersion);
        if (release is not null && File.Exists(Path.Combine(stagingRoot, "failure.json")))
        {
            try
            {
                using var failure = JsonDocument.Parse(File.ReadAllText(Path.Combine(stagingRoot, "failure.json")));
                if (failure.RootElement.GetProperty("version").GetString() == release.Version.ToString(3)
                    && failure.RootElement.GetProperty("timeUtc").GetDateTimeOffset() > DateTimeOffset.UtcNow.AddHours(-24)) return null;
            }
            catch (Exception e) when (e is JsonException or KeyNotFoundException or InvalidOperationException or FormatException) { }
        }
        return release;
    }
    public async Task<PreparedLauncherUpdate> PrepareAsync(LauncherRelease release, string targetDirectory, CancellationToken cancel = default)
    {
        Require(release.Tag == "v" + release.Version.ToString(3) && release.Size is > 0 and <= MaxArchiveBytes, "Release de mise à jour invalide.");
        ValidateAsset(release.ZipUrl, release.Tag, ArchiveName); ValidateAsset(release.HashUrl, release.Tag, HashName);
        Require(Path.IsPathFullyQualified(targetDirectory), "Le dossier du launcher doit être absolu.");
        var target = Path.GetFullPath(targetDirectory).TrimEnd(Path.DirectorySeparatorChar);
        Require(Directory.Exists(target) && File.Exists(Path.Combine(target, "S4Together.exe")) && target != Path.GetPathRoot(target), "Dossier du launcher invalide.");
        EnsureNoReparse(target); EnsureNoReparse(stagingRoot);
        var work = Path.Combine(stagingRoot, Guid.NewGuid().ToString("N")); Directory.CreateDirectory(work);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancel); deadline.CancelAfter(TimeSpan.FromMinutes(3));
        try
        {
            using var sumResponse = await GetAsync(release.HashUrl, true, deadline.Token);
            Require(sumResponse.IsSuccessStatusCode, "Empreinte de mise à jour indisponible.");
            using var sumBytes = new MemoryStream(); await CopyBounded(sumResponse, sumBytes, 4096, deadline.Token);
            var sum = Encoding.UTF8.GetString(sumBytes.ToArray()).Trim().TrimStart('\ufeff');
            var match = Regex.Match(sum, "^([a-fA-F0-9]{64})(?:[ \\t]+\\*?" + Regex.Escape(ArchiveName) + ")?$", RegexOptions.CultureInvariant);
            Require(match.Success, "Format SHA256 de mise à jour invalide.");
            var expected = match.Groups[1].Value.ToLowerInvariant();
            var zipPath = Path.Combine(work, "client.zip");
            using (var response = await GetAsync(release.ZipUrl, true, deadline.Token))
            {
                Require(response.IsSuccessStatusCode, "Téléchargement de mise à jour indisponible.");
                await using var output = new FileStream(zipPath, FileMode.CreateNew, FileAccess.Write, FileShare.None, 81920, true);
                await CopyBounded(response, output, release.Size, deadline.Token);
            }
            Require(new FileInfo(zipPath).Length == release.Size && ModInstaller.FileHash(zipPath) == expected, "Empreinte ou taille ZIP de mise à jour différente.");
            var source = Path.Combine(work, "package"); ExtractPackage(zipPath, source, release.Version);
            var files = Directory.GetFiles(source, "*", SearchOption.AllDirectories).Select(path => new { relativePath = Path.GetRelativePath(source, path).Replace('\\', '/'), sha256 = ModInstaller.FileHash(path) }).ToArray();
            var nonce = Guid.NewGuid().ToString("N");
            var planPath = Path.Combine(work, "plan.json");
            File.WriteAllText(planPath, JsonSerializer.Serialize(new { source, target, version = release.Version.ToString(3), zipSha256 = expected,
                nonce, marker = Path.Combine(work, "startup-ready.json"), failure = Path.Combine(stagingRoot, "failure.json"), result = Path.Combine(work, "result.json"), files }));
            return new(source, target, planPath, release.Version, expected);
        }
        catch { Directory.Delete(work, true); throw; }
    }
    async Task<HttpResponseMessage> GetAsync(Uri first, bool asset, CancellationToken cancel)
    {
        var uri = first;
        for (var count = 0; count <= 3; count++)
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, uri);
            var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancel);
            if ((int)response.StatusCode is not (301 or 302 or 303 or 307 or 308)) return response;
            var location = response.Headers.Location; response.Dispose();
            Require(asset && count < 3 && location is not null, "Redirection de mise à jour refusée.");
            uri = location!.IsAbsoluteUri ? location : new Uri(uri, location);
            Require(uri.Scheme == "https" && uri.IsDefaultPort && uri.UserInfo.Length == 0 && uri.Fragment.Length == 0 && uri.OriginalString.Length <= 16384
                && (uri.Host is "release-assets.githubusercontent.com" or "objects.githubusercontent.com" || uri.Host == "github.com" && uri.AbsolutePath == first.AbsolutePath && uri.Query.Length == 0), "Redirection hors GitHub refusée.");
        }
        throw new InvalidDataException("Trop de redirections GitHub.");
    }
    static async Task CopyBounded(HttpResponseMessage response, Stream target, long max, CancellationToken cancel)
    {
        Require(response.Content.Headers.ContentLength is not { } size || size <= max, "Réponse de mise à jour trop grande.");
        await using var input = await response.Content.ReadAsStreamAsync(cancel); var buffer = new byte[81920]; long length = 0; int read;
        while ((read = await input.ReadAsync(buffer, cancel)) > 0) { length += read; Require(length <= max, "Réponse de mise à jour trop grande."); await target.WriteAsync(buffer.AsMemory(0, read), cancel); }
    }
    public void Dispose() => client.Dispose();
    public Process LaunchAfterExit(PreparedLauncherUpdate update, int parentProcessId)
    {
        Require(!GameLocator.IsGameRunning(), "Ferme Les Sims 4 avant de mettre à jour le launcher.");
        var plan = Path.GetFullPath(update.PlanPath); var directory = Path.GetDirectoryName(plan)!;
        Require(Path.GetDirectoryName(directory) == stagingRoot && Regex.IsMatch(Path.GetFileName(directory), "^[a-f0-9]{32}$") && Path.GetFileName(plan) == "plan.json", "Plan temporaire de mise à jour invalide.");
        EnsureNoReparse(directory); EnsureNoReparse(update.TargetDirectory);
        using var parent = Process.GetProcessById(parentProcessId);
        Require(string.Equals(parent.MainModule?.FileName, Path.Combine(update.TargetDirectory, "S4Together.exe"), StringComparison.OrdinalIgnoreCase), "Le processus parent n'est pas ce launcher.");
        var script = Path.Combine(directory, "replace.ps1"); File.WriteAllText(script, ReplacementScript, new UTF8Encoding(false));
        var powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
        var start = new ProcessStartInfo(powershell) { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden, RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var arg in new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script, "-PlanPath", plan,
            "-ParentId", parentProcessId.ToString(), "-ParentStartTicks", parent.StartTime.ToUniversalTime().Ticks.ToString() }) start.ArgumentList.Add(arg);
        return Process.Start(start) ?? throw new IOException("Le programme de remplacement n'a pas démarré.");
    }
    const string ReplacementScript = """
param([string]$PlanPath,[int]$ParentId,[long]$ParentStartTicks)
$ErrorActionPreference='Stop'
$swapped=New-Object 'System.Collections.Generic.List[object]'
$newProcess=$null
$plan=$null
$backup=$null
function Require($ok,[string]$message) { if(-not $ok){ throw $message } }
function NoLinks([string]$path) {
  $current=[System.IO.Path]::GetFullPath($path)
  while($current){
    if([System.IO.File]::Exists($current) -or [System.IO.Directory]::Exists($current)){
      Require (([System.IO.File]::GetAttributes($current) -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) 'Chemin redirigé refusé.'
    }
    $next=[System.IO.Path]::GetDirectoryName($current)
    if($next -eq $current){break}; $current=$next
  }
}
function Hash([string]$path) { return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
function GameOpen { return @(Get-Process -Name TS4_x64,TS4,TS4_DX9_x64 -ErrorAction SilentlyContinue).Count -gt 0 }
function StartLauncher([string]$exe,[string]$arguments) {
  $start=New-Object System.Diagnostics.ProcessStartInfo
  $start.FileName=$exe; $start.Arguments=$arguments; $start.WorkingDirectory=[System.IO.Path]::GetDirectoryName($exe)
  $start.UseShellExecute=$false
  return [System.Diagnostics.Process]::Start($start)
}
try {
  Require ((Get-Item -LiteralPath $PlanPath).Length -le 16384) 'Plan trop grand.'
  $plan=Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $work=[System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($PlanPath))
  Require ([System.IO.Path]::GetFileName($work) -cmatch '^[a-f0-9]{32}$') 'Dossier temporaire invalide.'
  Require ($plan.nonce -cmatch '^[a-f0-9]{32}$') 'Nonce invalide.'
  Require ($plan.version -cmatch '^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$') 'Version invalide.'
  Require ($plan.source -eq [System.IO.Path]::Combine($work,'package')) 'Source inattendue.'
  Require ($plan.marker -eq [System.IO.Path]::Combine($work,'startup-ready.json')) 'Marqueur inattendu.'
  Require ($plan.result -eq [System.IO.Path]::Combine($work,'result.json')) 'Résultat inattendu.'
  Require ($plan.failure -eq [System.IO.Path]::Combine([System.IO.Path]::GetDirectoryName($work),'failure.json')) 'Journal inattendu.'
  $target=[System.IO.Path]::GetFullPath($plan.target).TrimEnd('\')
  Require ($target -ne [System.IO.Path]::GetPathRoot($target).TrimEnd('\')) 'Installation racine refusée.'
  NoLinks $target; NoLinks $work
  $exe=[System.IO.Path]::Combine($target,'S4Together.exe')
  Require ([System.IO.File]::Exists($exe)) 'Ancien launcher absent.'
  $allowed=@('S4Together.exe','mod/S4Together-GameClient.ts4script','mod/mod-readiness.json','VERSION.txt','LIRE-MOI.txt','SHA256SUMS.txt','README.md')
  $seen=New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
  Require (@($plan.files).Count -ge 4 -and @($plan.files).Count -le 7) 'Liste de fichiers invalide.'
  foreach($item in $plan.files){
    Require ($allowed -ccontains $item.relativePath) 'Fichier refusé.'
    Require ($seen.Add($item.relativePath)) 'Fichier en doublon.'
    Require ($item.sha256 -cmatch '^[a-f0-9]{64}$') 'Empreinte invalide.'
    $source=[System.IO.Path]::Combine($plan.source,$item.relativePath.Replace('/','\'))
    $destination=[System.IO.Path]::Combine($target,$item.relativePath.Replace('/','\'))
    NoLinks $source; NoLinks $destination
    Require ((Hash $source) -eq $item.sha256) 'Fichier temporaire altéré.'
  }
  foreach($required in @('S4Together.exe','mod/S4Together-GameClient.ts4script','mod/mod-readiness.json','VERSION.txt')){ Require ($seen.Contains($required)) 'Fichier obligatoire absent.' }
  Require ($plan.zipSha256 -cmatch '^[a-f0-9]{64}$' -and (Hash ([System.IO.Path]::Combine($work,'client.zip'))) -eq $plan.zipSha256) 'ZIP temporaire altéré.'
  $parent=Get-Process -Id $ParentId -ErrorAction SilentlyContinue
  if($parent){
    Require ($parent.StartTime.ToUniversalTime().Ticks -eq $ParentStartTicks -and $parent.Path -eq $exe) 'Processus parent différent.'
    Require ($parent.WaitForExit(300000)) 'Le launcher reste ouvert.'
  }
  Require (-not (GameOpen)) 'Le jeu est ouvert.'
  $backup=[System.IO.Path]::Combine($target,'.s4t-update-'+$plan.nonce)
  NoLinks $backup; Require (-not [System.IO.Directory]::Exists($backup)) 'Sauvegarde déjà présente.'
  [void][System.IO.Directory]::CreateDirectory($backup)
  $oldHash=Hash $exe
  foreach($item in ($plan.files | Sort-Object @{Expression={$_.relativePath -eq 'S4Together.exe'}},relativePath)){
    Require (-not (GameOpen)) 'Le jeu a démarré.'
    $relative=$item.relativePath.Replace('/','\')
    $source=[System.IO.Path]::Combine($plan.source,$relative)
    $destination=[System.IO.Path]::Combine($target,$relative)
    $pending=[System.IO.Path]::Combine($backup,'pending',$relative)
    $saved=[System.IO.Path]::Combine($backup,'old',$relative)
    [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($pending))
    [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($saved))
    [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($destination))
    [System.IO.File]::Copy($source,$pending,$false)
    Require ((Hash $pending) -eq $item.sha256) 'Copie temporaire altérée.'
    $hadOld=[System.IO.File]::Exists($destination)
    if($hadOld){ [System.IO.File]::Replace($pending,$destination,$saved,$true) } else { [System.IO.File]::Move($pending,$destination) }
    $swapped.Add(@{destination=$destination;saved=$saved;hadOld=$hadOld})
  }
  Require (-not [System.IO.File]::Exists($plan.marker)) 'Ancien marqueur refusé.'
  $arguments='--update-verify "'+$plan.marker+'" --update-nonce '+$plan.nonce
  $newProcess=StartLauncher $exe $arguments
  $until=[DateTime]::UtcNow.AddSeconds(60)
  $rendered=$false
  while([DateTime]::UtcNow -lt $until -and -not $newProcess.HasExited){
    if([System.IO.File]::Exists($plan.marker) -and (Get-Item -LiteralPath $plan.marker).Length -le 128){
      $rendered=([System.IO.File]::ReadAllText($plan.marker).Trim() -ceq $plan.nonce)
      if($rendered){break}
    }
    Start-Sleep -Milliseconds 100
  }
  Require ($rendered -and -not $newProcess.HasExited) 'La nouvelle version ne démarre pas.'
  [System.IO.Directory]::Delete($backup,$true)
  @{status='updated';version=$plan.version} | ConvertTo-Json | Set-Content -LiteralPath $plan.result -Encoding UTF8
  exit 0
} catch {
  if($work){[System.IO.File]::WriteAllText([System.IO.Path]::Combine($work,'error-debug.txt'),$_.Exception.Message)}
  if($newProcess -and -not $newProcess.HasExited){$newProcess.Kill();$newProcess.WaitForExit()}
  $rollback=$true
  for($i=$swapped.Count-1;$i -ge 0;$i--){
    $item=$swapped[$i]
    try {
      if($item.hadOld){
        if([System.IO.File]::Exists($item.destination)){[System.IO.File]::Replace($item.saved,$item.destination,[System.Management.Automation.Language.NullString]::Value,$true)}
        else{[System.IO.File]::Move($item.saved,$item.destination)}
      } elseif([System.IO.File]::Exists($item.destination)){[System.IO.File]::Delete($item.destination)}
    } catch {$rollback=$false}
  }
  if($plan -and $backup -and [System.IO.Directory]::Exists($backup)){
    if($rollback){[System.IO.Directory]::Delete($backup,$true)}
    @{status=$(if($rollback){'rolled_back'}else{'rollback_failed'});version=$plan.version} | ConvertTo-Json | Set-Content -LiteralPath $plan.result -Encoding UTF8
    @{version=$plan.version;sha256=$plan.zipSha256;timeUtc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $plan.failure -Encoding UTF8
    if($rollback -and (Hash $exe) -eq $oldHash){[void](StartLauncher $exe '')}
  }
  exit 1
}
""";
    public const string Repository = "SuguruFPS/S4Together-client";
    public const string ArchiveName = "S4Together-Client-win-x64.zip";
    public const string HashName = ArchiveName + ".sha256";
    public const long MaxArchiveBytes = 256L * 1024 * 1024;
    static Version Normalize(Version value) => new(value.Major, value.Minor, Math.Max(0, value.Build));
    static void Require(bool yes, string message) { if (!yes) throw new InvalidDataException(message); }
    internal static LauncherRelease? ReadRelease(JsonElement metadata, Version current)
    {
        try
        {
            Require(metadata.ValueKind == JsonValueKind.Object, "Release GitHub invalide.");
            if (metadata.GetProperty("draft").GetBoolean() || metadata.GetProperty("prerelease").GetBoolean()) return null;
            var tag = metadata.GetProperty("tag_name").GetString() ?? "";
            Require(Regex.IsMatch(tag, @"^v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$") && Version.TryParse(tag[1..], out _), "Version de release invalide.");
            var version = Version.Parse(tag[1..]);
            if (version <= Normalize(current)) return null;
            var assets = metadata.GetProperty("assets").EnumerateArray().ToArray();
            Require(assets.Length <= 32, "Trop de fichiers dans la release.");
            (Uri Uri, long Size) Asset(string name, long max)
            {
                var matches = assets.Where(a => a.GetProperty("name").GetString() == name).ToArray();
                Require(matches.Length == 1, "Fichier de mise à jour absent ou dupliqué.");
                var size = matches[0].GetProperty("size").GetInt64();
                Require(size is > 0 && size <= max, "Fichier de mise à jour trop grand ou vide.");
                var uri = new Uri(matches[0].GetProperty("browser_download_url").GetString()!);
                ValidateAsset(uri, tag, name);
                return (uri, size);
            }
            var zip = Asset(ArchiveName, MaxArchiveBytes); var hash = Asset(HashName, 4096);
            return new(version, tag, zip.Uri, hash.Uri, zip.Size);
        }
        catch (Exception e) when (e is JsonException or KeyNotFoundException or InvalidOperationException or FormatException or OverflowException or ArgumentException)
        { throw new InvalidDataException("Métadonnées de mise à jour GitHub invalides."); }
    }
    static void ValidateAsset(Uri uri, string tag, string name) => Require(uri.Scheme == "https" && uri.Host == "github.com" && uri.IsDefaultPort
        && uri.UserInfo.Length == 0 && uri.Query.Length == 0 && uri.Fragment.Length == 0
        && uri.AbsolutePath == $"/{Repository}/releases/download/{tag}/{name}", "Adresse de fichier GitHub refusée.");
    static readonly HashSet<string> PackageFiles = new(StringComparer.Ordinal)
    {
        "S4Together.exe", "mod/S4Together-GameClient.ts4script", "mod/mod-readiness.json", "VERSION.txt", "LIRE-MOI.txt", "SHA256SUMS.txt", "README.md"
    };
    internal static void ExtractPackage(string zip, string stage, Version version)
    {
        using var archive = ZipFile.OpenRead(zip);
        Require(archive.Entries.Count is > 0 and <= 16, "Archive de mise à jour invalide.");
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase); long total = 0;
        foreach (var entry in archive.Entries)
        {
            var name = entry.FullName;
            var directory = name.EndsWith('/');
            Require(directory ? name == "mod/" : PackageFiles.Contains(name), "L'archive contient un chemin ou fichier non autorisé.");
            Require(seen.Add(name), "L'archive contient des doublons de chemins Windows.");
            var unixType = (entry.ExternalAttributes >> 16) & 0xf000;
            Require(unixType is 0 or 0x8000 or 0x4000 && (entry.ExternalAttributes & 0x400) == 0, "Les liens et fichiers spéciaux ne sont pas autorisés.");
            Require(entry.Length is >= 0 and <= 268435456 && (total += entry.Length) <= 536870912, "Archive décompressée trop grande.");
        }
        foreach (var required in new[] { "S4Together.exe", "mod/S4Together-GameClient.ts4script", "mod/mod-readiness.json", "VERSION.txt" })
            Require(seen.Contains(required), "Fichier obligatoire absent de la mise à jour.");
        Require(!Directory.Exists(stage) || Directory.GetFileSystemEntries(stage).Length == 0, "Le dossier temporaire de mise à jour n'est pas vide.");
        EnsureNoReparse(stage);
        Directory.CreateDirectory(stage);
        foreach (var entry in archive.Entries.Where(e => !e.FullName.EndsWith('/')))
        {
            var path = Path.Combine(stage, entry.FullName.Replace('/', Path.DirectorySeparatorChar));
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            using var input = entry.Open(); using var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write);
            var buffer = new byte[81920]; long copied = 0;
            int read;
            while ((read = input.Read(buffer)) > 0) { copied += read; Require(copied <= entry.Length, "Taille ZIP incohérente."); output.Write(buffer, 0, read); }
            Require(copied == entry.Length, "Fichier ZIP tronqué.");
        }
        var executable = Path.Combine(stage, "S4Together.exe");
        using (var pe = File.OpenRead(executable))
        using (var reader = new BinaryReader(pe))
        {
            Require(pe.Length >= 64 && reader.ReadUInt16() == 0x5a4d, "Le fichier launcher n'est pas un exécutable Windows.");
            pe.Position = 0x3c; var header = reader.ReadInt32();
            Require(header >= 64 && header <= 1048576 && header + 6L < pe.Length, "En-tête exécutable invalide.");
            pe.Position = header; Require(reader.ReadUInt32() == 0x00004550 && reader.ReadUInt16() == 0x8664, "La mise à jour doit être Windows x64.");
        }
        var info = FileVersionInfo.GetVersionInfo(executable);
        Require(new Version(info.FileMajorPart, info.FileMinorPart, info.FileBuildPart) == Normalize(version), "Version de l'exécutable différente de la release.");
        var versionFile = Path.Combine(stage, "VERSION.txt");
        Require(new FileInfo(versionFile).Length <= 4096 && File.ReadAllText(versionFile).Split('\n')[0].Trim() is { } text
            && (text == version.ToString(3) || text == version.ToString(3) + "-game-test"), "Version du paquet différente de la release.");
        var package = ModInstaller.LoadPackage(Path.Combine(stage, "mod"));
        Require(package.Version == version.ToString(3) || package.Version == version.ToString(3) + "-game-test", "Version du mod différente de la release.");
    }
    static void EnsureNoReparse(string path)
    {
        for (var current = new DirectoryInfo(Path.GetFullPath(path)); current is not null; current = current.Parent)
            Require(!current.Exists || !current.Attributes.HasFlag(FileAttributes.ReparsePoint), "Dossier de mise à jour lié ou redirigé refusé.");
    }
}
