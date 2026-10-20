using System.Windows;

namespace S4Together.Launcher;

/// <summary>Glyphe Segoe Fluent affiché devant un élément du menu (le Tag reste le nom de page).</summary>
public static class NavIcon
{
    public static readonly DependencyProperty GlyphProperty =
        DependencyProperty.RegisterAttached("Glyph", typeof(string), typeof(NavIcon), new PropertyMetadata(""));

    public static string GetGlyph(DependencyObject element) => (string)element.GetValue(GlyphProperty);
    public static void SetGlyph(DependencyObject element, string value) => element.SetValue(GlyphProperty, value);
}
