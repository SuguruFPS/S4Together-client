namespace S4Together.Launcher.Core;

/// <summary>Détourage d'affichage : seul le blanc relié au bord devient transparent.</summary>
public static class PortraitBackground
{
    public static void ClearBorderWhite(byte[] bgra, int width, int height)
    {
        if (width < 1 || height < 1 || (long)width * height > 4_000_000 || (long)width * height * 4 != bgra.Length)
            throw new ArgumentException("Dimensions du portrait invalides.");
        var visited = new bool[width * height];
        var queue = new int[visited.Length];
        int read = 0, end = 0;
        void Visit(int pixel)
        {
            if (visited[pixel]) return;
            visited[pixel] = true;
            int p = pixel * 4;
            int min = Math.Min(bgra[p], Math.Min(bgra[p + 1], bgra[p + 2]));
            int max = Math.Max(bgra[p], Math.Max(bgra[p + 1], bgra[p + 2]));
            // Les pixels déjà transparents permettent de rejoindre un éventuel halo blanc.
            if (bgra[p + 3] != 0 && (min < 240 || max - min > 15)) return;
            bgra[p + 3] = 0;
            queue[end++] = pixel;
        }
        for (int x = 0; x < width; x++) { Visit(x); Visit((height - 1) * width + x); }
        for (int y = 0; y < height; y++) { Visit(y * width); Visit(y * width + width - 1); }
        while (read < end)
        {
            int p = queue[read++], x = p % width;
            if (x > 0) Visit(p - 1);
            if (x + 1 < width) Visit(p + 1);
            if (p >= width) Visit(p - width);
            if (p + width < visited.Length) Visit(p + width);
        }
    }
}
