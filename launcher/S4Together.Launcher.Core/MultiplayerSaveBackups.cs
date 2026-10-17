using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace S4Together.Launcher.Core;

public sealed record SaveBackupResult(string Status,string? BackupPath=null);
public sealed class MultiplayerSaveBackups(string root)
{
    sealed record Entry(string Sha256,long Bytes,DateTime CreatedAtUtc,string SourceName);
    const long MaxSaveBytes=256L*1024*1024,MaxTotalBytes=2L*1024*1024*1024;
    static string Hash(string file){using var stream=File.OpenRead(file);return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();}
    static void NoLinks(string path)
    {
        for(var item=new DirectoryInfo(Path.GetFullPath(path));item is not null;item=item.Parent)
            if(item.Exists&&(item.Attributes&FileAttributes.ReparsePoint)!=0)throw new IOException("Dossier de sauvegarde redirige refuse.");
    }
    static bool Matches(CharacterInfo character,string file,string? slot=null)
    {var sims=LocalSimCatalog.ReadSave(file);return MultiplayerSave.Find(character,sims) is not null&&(slot is null||sims.Any(s=>s.SaveId==slot));}
    public SaveBackupResult Capture(string profile,string server,CharacterInfo character,string? requiredSlotId=null)
    {
        if(!character.Bound||!CharacterRoster.IsUuid(character.Id)||character.GameSimIds.Count==0||string.IsNullOrWhiteSpace(server))return new("unbound");
        var saves=Path.Combine(Path.GetFullPath(profile),"saves");NoLinks(saves);
        if(Path.GetFullPath(root).StartsWith(saves+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase)||Path.GetFullPath(root).Equals(saves,StringComparison.OrdinalIgnoreCase))throw new IOException("Les copies doivent rester hors du dossier de jeu.");
        if(!Directory.Exists(saves))return new("not_found");
        string? source=null;
        foreach(var file in Directory.EnumerateFiles(saves,"Slot_*.save").OrderByDescending(File.GetLastWriteTimeUtc).Take(256))
        {
            var info=new FileInfo(file);
            if((info.Attributes&FileAttributes.ReparsePoint)!=0||info.Length<=0||info.Length>MaxSaveBytes||DateTime.UtcNow-info.LastWriteTimeUtc<TimeSpan.FromSeconds(5))continue;
            try {if(Matches(character,file,requiredSlotId)){source=file;break;}}
            catch(Exception e)when(e is IOException or InvalidDataException or UnauthorizedAccessException or OverflowException or DecoderFallbackException){continue;}
        }
        if(source is null)return new("not_found");
        var scope=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(server))).ToLowerInvariant();
        var folder=Path.Combine(Path.GetFullPath(root),scope,character.Id);NoLinks(folder);Directory.CreateDirectory(folder);
        var lockPath=Path.Combine(folder,"backup.lock");
        if(File.Exists(lockPath)&&(new FileInfo(lockPath).Attributes&FileAttributes.ReparsePoint)!=0)throw new IOException("Verrou redirige refuse.");
        using var processLock=new FileStream(lockPath,FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);
        var manifest=Path.Combine(folder,"history.json");
        if(File.Exists(manifest)&&(new FileInfo(manifest).Attributes&FileAttributes.ReparsePoint)!=0)throw new IOException("Historique redirige refuse.");
        if(File.Exists(manifest)&&new FileInfo(manifest).Length>65536)throw new InvalidDataException("Historique trop grand.");
        var history=File.Exists(manifest)?JsonSerializer.Deserialize<Entry[]>(File.ReadAllText(manifest))??throw new InvalidDataException("Historique invalide."):[];
        if(history.Length>10||history.Any(e=>e is null||!Regex.IsMatch(e.Sha256,"^[0-9a-f]{64}$")||e.Bytes<=0||e.Bytes>MaxSaveBytes))throw new InvalidDataException("Historique invalide.");
        var infoBefore=new FileInfo(source);var length=infoBefore.Length;var stamp=infoBefore.LastWriteTimeUtc;
        var options=new EnumerationOptions{RecurseSubdirectories=true,MaxRecursionDepth=3,AttributesToSkip=FileAttributes.ReparsePoint};
        if(Directory.EnumerateFiles(root,"*.save",options).Take(4097).Sum(p=>new FileInfo(p).Length)+length>MaxTotalBytes)return new("quota");
        var temporary=Path.Combine(folder,Guid.NewGuid().ToString("N")+".tmp");
        try
        {
            using(var input=new FileStream(source,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
            using(var output=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.None))
            {input.CopyTo(output);output.Flush(true);}
            var after=new FileInfo(source);
            if(after.Length!=length||after.LastWriteTimeUtc!=stamp||!Matches(character,temporary,requiredSlotId))return new("unstable");
            var digest=Hash(temporary);
            if(Hash(source)!=digest)return new("unstable");
            var destination=Path.Combine(folder,digest+".save");
            if(File.Exists(destination))
            {
                if((new FileInfo(destination).Attributes&FileAttributes.ReparsePoint)!=0||Hash(destination)!=digest)throw new InvalidDataException("Copie de sauvegarde alteree.");
                if(history.Any(e=>e.Sha256==digest))return new("unchanged",destination);
            }
            else File.Move(temporary,destination);
            var next=history.Append(new Entry(digest,length,DateTime.UtcNow,Path.GetFileName(source))).TakeLast(10).ToArray();
            var indexTemp=Path.Combine(folder,Guid.NewGuid().ToString("N")+".tmp");
            try
            {
                using(var output=new FileStream(indexTemp,FileMode.CreateNew,FileAccess.Write,FileShare.None))
                {JsonSerializer.Serialize(output,next);output.Flush(true);}
                File.Move(indexTemp,manifest,true);
            }
            finally {if(File.Exists(indexTemp))File.Delete(indexTemp);}
            foreach(var expired in history.Where(e=>!next.Any(n=>n.Sha256==e.Sha256)))
            {
                var file=Path.Combine(folder,expired.Sha256+".save");
                if(File.Exists(file)&&(new FileInfo(file).Attributes&FileAttributes.ReparsePoint)==0&&Hash(file)==expired.Sha256)File.Delete(file);
            }
            return new("created",destination);
        }
        finally {if(File.Exists(temporary))File.Delete(temporary);}
    }
}
