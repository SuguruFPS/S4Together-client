using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace S4Together.Launcher.Core;

public sealed record SaveUploadContext(string CharacterId,string HouseholdId,string SlotId,IReadOnlyList<string> SimIds,DateTimeOffset ObservedAt)
{
    public static SaveUploadContext? Parse(JsonObject payload)
    {
        static string Text(JsonNode? n)=>n is JsonValue v&&v.TryGetValue<string>(out var s)?s:"";
        static bool Native(string s)=>s.Length is >0 and <=20&&s.All(char.IsAsciiDigit)&&s!="0";
        var id=Text(payload["characterId"]);var home=Text(payload["householdId"]);var slot=Text(payload["slotId"]);
        if(payload.Count!=4||!CharacterRoster.IsUuid(id)||!Native(home)||!Native(slot)||payload["simIds"] is not JsonArray list||list.Count is <1 or >8)return null;
        var sims=list.Select(Text).ToArray();
        return sims.Any(s=>!Native(s))||sims.Distinct().Count()!=sims.Length?null:new(id,home,slot,sims,DateTimeOffset.UtcNow);
    }
    public bool Matches(CharacterInfo character)=>character.Bound&&CharacterId==character.Id&&HouseholdId==character.GameHouseholdId&&
        DateTimeOffset.UtcNow-ObservedAt is var age&&age>=TimeSpan.Zero&&age<TimeSpan.FromSeconds(60)&&character.GameSimIds.Count>0&&character.GameSimIds.All(SimIds.Contains);
}

public sealed class PrivateSaveUploader:IDisposable
{
    readonly HttpClient http;readonly bool owns;
    public PrivateSaveUploader(){http=new(new SocketsHttpHandler{AllowAutoRedirect=false,UseProxy=false,ConnectTimeout=TimeSpan.FromSeconds(10)}){Timeout=TimeSpan.FromMinutes(5),MaxResponseContentBufferSize=8192};owns=true;}
    public PrivateSaveUploader(HttpClient client){http=client;}
    public void Dispose(){if(owns)http.Dispose();}
    public async Task<string> UploadAsync(ServerAddress address,HelloResult hello,CharacterInfo character,SaveUploadContext context,string copy,CancellationToken cancel)
    {
        address.RequireSafeTransport();
        if(!context.Matches(character))return "context_mismatch";
        if((File.GetAttributes(copy)&FileAttributes.ReparsePoint)!=0)throw new IOException("Copie redirigee refusee.");
        var sims=LocalSimCatalog.ReadSave(copy);
        if(MultiplayerSave.Find(character,sims) is null||!sims.Any(s=>s.SaveId==context.SlotId))return "context_mismatch";
        using var input=new FileStream(copy,FileMode.Open,FileAccess.Read,FileShare.Read,65536,true);
        if(input.Length<96||input.Length>256L*1024*1024)throw new InvalidDataException("Copie trop grande.");
        var sha=Convert.ToHexString(await SHA256.HashDataAsync(input,cancel)).ToLowerInvariant();input.Position=0;
        var uri=new Uri(address.HealthUri,"private-saves/"+character.Id+"/"+sha);
        HttpRequestMessage Request(HttpMethod method)
        {
            var request=new HttpRequestMessage(method,uri);
            request.Headers.Authorization=new AuthenticationHeaderValue("Bearer",hello.Token);
            request.Headers.Add("X-S4T-Player",hello.PlayerId);request.Headers.Add("X-S4T-Session",hello.SessionId);return request;
        }
        using(var head=Request(HttpMethod.Head))
        using(var response=await http.SendAsync(head,cancel))
        {
            if(response.StatusCode==HttpStatusCode.OK)return "unchanged";
            if(response.StatusCode!=HttpStatusCode.NotFound)response.EnsureSuccessStatusCode();
        }
        if(!context.Matches(character))return "context_mismatch";
        using var upload=Request(HttpMethod.Put);upload.Content=new StreamContent(input);
        upload.Content.Headers.ContentType=new MediaTypeHeaderValue("application/octet-stream");
        using var reply=await http.SendAsync(upload,cancel);
        if(reply.StatusCode==HttpStatusCode.NotFound)return "unsupported";
        reply.EnsureSuccessStatusCode();
        var json=await reply.Content.ReadAsStringAsync(cancel);
        if(json.Length>8192)throw new InvalidDataException("Reponse sauvegarde trop grande.");
        using var document=JsonDocument.Parse(json);var root=document.RootElement;
        if(root.GetProperty("sha256").GetString()!=sha||root.GetProperty("bytes").GetInt64()!=input.Length||root.GetProperty("validation").GetString()!="quarantined")throw new InvalidDataException("Confirmation sauvegarde invalide.");
        return "stored";
    }
}
