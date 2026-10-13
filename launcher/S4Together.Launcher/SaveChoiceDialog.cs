using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using S4Together.Launcher.Core;

namespace S4Together.Launcher;

/// <summary>Personnage pas encore lié : nouvelle partie (recommandé) ou sauvegarde existante choisie explicitement.</summary>
sealed class SaveChoiceDialog : Window
{
    /// <summary>Choix fait : Save null = nouvelle partie.</summary>
    public sealed record Answer(SaveChoice? Save);

    Answer? answer;

    SaveChoiceDialog(string characterName, IReadOnlyList<SaveChoice> saves, string? current)
    {
        Title = "Sauvegarde de " + characterName;
        Width = 560; SizeToContent = SizeToContent.Height; ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        Background = new SolidColorBrush(Color.FromRgb(0x1E, 0x22, 0x2B));
        var text = new SolidColorBrush(Color.FromRgb(0xEE, 0xF1, 0xF6));
        var panel = new StackPanel { Margin = new Thickness(18) };
        panel.Children.Add(new TextBlock
        {
            Foreground = text, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 10),
            Text = $"« {characterName} » n'est pas encore lié à un foyer.\n\n" +
                   "Nouvelle partie (recommandé) : dans le jeu, « Nouvelle partie », puis crée ton foyer.\n\n" +
                   "Ou choisis une sauvegarde déjà commencée pour ce personnage. Seule cette sauvegarde sera autorisée. " +
                   "À la liaison, les relations du foyer chargé avec les PNJ sont effacées ; la famille et les autres foyers joués restent. " +
                   "Ne choisis pas une vraie partie solo.",
        });
        var list = new ListBox { Height = 180, Margin = new Thickness(0, 0, 0, 12) };
        foreach (var save in saves) list.Items.Add(new ListBoxItem { Content = save.Label, Tag = save, IsSelected = save.SlotId == current });
        panel.Children.Add(list);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        var fresh = new Button { Content = "Nouvelle partie (recommandé)", Padding = new Thickness(12, 6, 12, 6), Margin = new Thickness(0, 0, 8, 0), IsDefault = true };
        var use = new Button { Content = "Utiliser cette sauvegarde", Padding = new Thickness(12, 6, 12, 6), Margin = new Thickness(0, 0, 8, 0), IsEnabled = list.SelectedItem is not null };
        var cancel = new Button { Content = "Annuler", Padding = new Thickness(12, 6, 12, 6), IsCancel = true };
        list.SelectionChanged += (_, _) => use.IsEnabled = list.SelectedItem is not null;
        fresh.Click += (_, _) => { answer = new(null); DialogResult = true; };
        use.Click += (_, _) =>
        {
            if (list.SelectedItem is not ListBoxItem { Tag: SaveChoice save }) return;
            var confirm = MessageBox.Show(this, $"Utiliser « {save.Name} » pour « {characterName} » ?\nLes relations de ce foyer avec les PNJ seront effacées à la première connexion.",
                "Confirmer la sauvegarde", MessageBoxButton.YesNo, MessageBoxImage.Warning);
            if (confirm != MessageBoxResult.Yes) return;
            answer = new(save); DialogResult = true;
        };
        buttons.Children.Add(fresh); buttons.Children.Add(use); buttons.Children.Add(cancel);
        panel.Children.Add(buttons);
        Content = panel;
    }

    /// <summary>null : annulé, rien ne change.</summary>
    public static Answer? Ask(Window owner, string characterName, IReadOnlyList<SaveChoice> saves, string? current)
    {
        var dialog = new SaveChoiceDialog(characterName, saves, current) { Owner = owner };
        return dialog.ShowDialog() == true ? dialog.answer : null;
    }
}
