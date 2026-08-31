using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Media;
using Brush = System.Windows.Media.Brush;

namespace AzureHealthBeacon;

public sealed class TablerIcon : FrameworkElement
{
    public static readonly DependencyProperty IconProperty = DependencyProperty.Register(
        nameof(Icon), typeof(string), typeof(TablerIcon), new FrameworkPropertyMetadata("activity", FrameworkPropertyMetadataOptions.AffectsRender));
    public static readonly DependencyProperty StrokeProperty = DependencyProperty.Register(
        nameof(Stroke), typeof(Brush), typeof(TablerIcon), new FrameworkPropertyMetadata(System.Windows.Media.Brushes.White, FrameworkPropertyMetadataOptions.AffectsRender));

    public string Icon { get => (string)GetValue(IconProperty); set => SetValue(IconProperty, value); }
    public Brush Stroke { get => (Brush)GetValue(StrokeProperty); set => SetValue(StrokeProperty, value); }

    private static readonly Dictionary<string, string[]> Paths = new()
    {
        ["activity"] = ["M3 12h4l3 8l4 -16l3 8h4"],
        ["plus"] = ["M12 5l0 14", "M5 12l14 0"],
        ["server-2"] = ["M3 7a3 3 0 0 1 3 -3h12a3 3 0 0 1 3 3v2a3 3 0 0 1 -3 3h-12a3 3 0 0 1 -3 -3v-2", "M3 15a3 3 0 0 1 3 -3h12a3 3 0 0 1 3 3v2a3 3 0 0 1 -3 3h-12a3 3 0 0 1 -3 -3l0 -2", "M7 8l0 .01", "M7 16l0 .01", "M11 8h6", "M11 16h6"],
        ["braces"] = ["M7 4a2 2 0 0 0 -2 2v3a2 3 0 0 1 -2 3a2 3 0 0 1 2 3v3a2 2 0 0 0 2 2", "M17 4a2 2 0 0 1 2 2v3a2 3 0 0 0 2 3a2 3 0 0 0 -2 3v3a2 2 0 0 1 -2 2"],
        ["hierarchy-2"] = ["M10 3h4v4h-4l0 -4", "M3 17h4v4h-4l0 -4", "M17 17h4v4h-4l0 -4", "M7 17l5 -4l5 4", "M12 7l0 6"],
        ["file-analytics"] = ["M14 3v4a1 1 0 0 0 1 1h4", "M17 21h-10a2 2 0 0 1 -2 -2v-14a2 2 0 0 1 2 -2h7l5 5v11a2 2 0 0 1 -2 2", "M9 17l0 -5", "M12 17l0 -1", "M15 17l0 -3"],
        ["progress-check"] = ["M10 20.777a8.942 8.942 0 0 1 -2.48 -.969", "M14 3.223a9.003 9.003 0 0 1 0 17.554", "M4.579 17.093a8.961 8.961 0 0 1 -1.227 -2.592", "M3.124 10.5c.16 -.95 .468 -1.85 .9 -2.675l.169 -.305", "M6.907 4.579a8.954 8.954 0 0 1 3.093 -1.356", "M9 12l2 2l4 -4"],
        ["settings"] = ["M10.325 4.317c.426 -1.756 2.924 -1.756 3.35 0a1.724 1.724 0 0 0 2.573 1.066c1.543 -.94 3.31 .826 2.37 2.37a1.724 1.724 0 0 0 1.065 2.572c1.756 .426 1.756 2.924 0 3.35a1.724 1.724 0 0 0 -1.066 2.573c.94 1.543 -.826 3.31 -2.37 2.37a1.724 1.724 0 0 0 -2.572 1.065c-.426 1.756 -2.924 1.756 -3.35 0a1.724 1.724 0 0 0 -2.573 -1.066c-1.543 .94 -3.31 -.826 -2.37 -2.37a1.724 1.724 0 0 0 -1.065 -2.572c-1.756 -.426 -1.756 -2.924 0 -3.35a1.724 1.724 0 0 0 1.066 -2.573c-.94 -1.543 .826 -3.31 2.37 -2.37c1 .608 2.296 .07 2.572 -1.065", "M9 12a3 3 0 1 0 6 0a3 3 0 0 0 -6 0"],
        ["info-circle"] = ["M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0", "M12 9h.01", "M11 12h1v4h1"],
        ["key"] = ["M16.555 3.843l3.602 3.602a2.877 2.877 0 0 1 0 4.069l-2.643 2.643a2.877 2.877 0 0 1 -4.069 0l-.301 -.301l-6.558 6.558a2 2 0 0 1 -1.239 .578l-.175 .008h-1.172a1 1 0 0 1 -.993 -.883l-.007 -.117v-1.172a2 2 0 0 1 .467 -1.284l.119 -.13l.414 -.414h2v-2h2v-2l2.144 -2.144l-.301 -.301a2.877 2.877 0 0 1 0 -4.069l2.643 -2.643a2.877 2.877 0 0 1 4.069 0", "M15 9h.01"],
    };

    protected override void OnRender(DrawingContext drawingContext)
    {
        base.OnRender(drawingContext);
        if (!Paths.TryGetValue(Icon, out var paths)) paths = Paths["activity"];
        var scale = Math.Min(ActualWidth, ActualHeight) / 24.0;
        drawingContext.PushTransform(new TranslateTransform((ActualWidth - 24 * scale) / 2, (ActualHeight - 24 * scale) / 2));
        drawingContext.PushTransform(new ScaleTransform(scale, scale));
        var pen = new System.Windows.Media.Pen(Stroke, 2) { StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round, LineJoin = PenLineJoin.Round };
        foreach (var path in paths) drawingContext.DrawGeometry(null, pen, Geometry.Parse(path));
        drawingContext.Pop();
        drawingContext.Pop();
    }
}
