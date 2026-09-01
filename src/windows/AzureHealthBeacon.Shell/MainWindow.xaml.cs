using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using Brush = System.Windows.Media.Brush;
using Button = System.Windows.Controls.Button;
using ComboBox = System.Windows.Controls.ComboBox;
using Control = System.Windows.Controls.Control;
using CheckBox = System.Windows.Controls.CheckBox;
using FontFamily = System.Windows.Media.FontFamily;
using HAlign = System.Windows.HorizontalAlignment;
using ListBox = System.Windows.Controls.ListBox;
using OpenFileDialog = Microsoft.Win32.OpenFileDialog;
using Orientation = System.Windows.Controls.Orientation;
using Panel = System.Windows.Controls.Panel;
using RadioButton = System.Windows.Controls.RadioButton;
using SaveFileDialog = Microsoft.Win32.SaveFileDialog;
using TextBox = System.Windows.Controls.TextBox;
using VAlign = System.Windows.VerticalAlignment;

namespace AzureHealthBeacon;

public partial class MainWindow : Window
{
    private readonly CoreClient _core;
    private readonly DispatcherTimer _checkTimer = new();
    private JsonObject _snapshot = new();
    private JsonArray _activity = [];
    private string? _selectedSource;
    private JsonObject? _editingRule;
    private BeaconState _state = BeaconState.Unconnectable;
    private bool _allowClose;
    private bool _dark = true;

    public event EventHandler<BeaconState>? BeaconStateChanged;

    public MainWindow(CoreClient core)
    {
        _core = core;
        InitializeComponent();
        _checkTimer.Tick += async (_, _) => await CheckNowAsync();
    }

    public async Task InitializeAsync(bool startupLaunch)
    {
        await RefreshSnapshotAsync();
        _dark = Text(_snapshot["settings"]?["theme_mode"], "dark") != "light";
        ApplyTheme();
        UpdateConnectionSummary();
        ResetCheckTimer();
        if (!Bool(_snapshot["connection"]?["initialized"]))
            ShowOnboarding();
        else
            ShowOverview();

        var minimized = startupLaunch && Bool(_snapshot["settings"]?["start_minimized"]);
        if (!minimized) Show();
        if (Bool(_snapshot["connection"]?["initialized"]))
            await CheckNowAsync();
        await MaybeCheckForUpdatesAsync();
    }

    private async Task RefreshSnapshotAsync() => _snapshot = await _core.CallAsync("snapshot");

    private void ResetCheckTimer()
    {
        _checkTimer.Interval = TimeSpan.FromMinutes(Int(_snapshot["settings"]?["interval_minutes"], 5));
        _checkTimer.IsEnabled = Bool(_snapshot["connection"]?["initialized"]);
    }

    private void SetState(BeaconState state)
    {
        _state = state;
        BeaconStateChanged?.Invoke(this, state);
    }

    public async Task CheckNowAsync()
    {
        if (!Bool(_snapshot["connection"]?["initialized"])) return;
        SetState(BeaconState.Checking);
        try
        {
            var response = await _core.CallAsync("check_all");
            _activity = response["results"]?.AsArray() ?? [];
            SetState(Text(response["state"]) switch
            {
                "healthy" => BeaconState.Healthy,
                "failed" => BeaconState.Failed,
                _ => BeaconState.Unconnectable,
            });
            if (PageTitle.Text == "Overview") ShowOverview();
            else if (PageTitle.Text == "Activity") ShowActivity();
        }
        catch (Exception error)
        {
            SetState(BeaconState.Unconnectable);
            ShowError(error);
        }
    }

    private void ShowOnboarding()
    {
        PageTitle.Text = "Welcome to Azure Health Beacon";
        PageSubtitle.Text = "Connect securely before creating any checks";
        SetConnectionGatedNavigation(false);
        var page = PageStack();
        page.Children.Add(Heading("Let’s connect Azure", "The Beacon uses Microsoft’s interactive sign-in. Your password and MFA response are never seen or stored by this app."));
        var panel = Card();
        var content = Vertical(panel);
        content.Children.Add(IconTitle("key", "1. Sign in with Microsoft"));
        content.Children.Add(Muted("A browser-based Microsoft sign-in opens. The Beacon keeps its OAuth session only in an app-owned, Windows DPAPI-encrypted cache and hard-deletes it after 14 days."));
        var signIn = Primary("Sign in with Microsoft");
        var status = Muted("Not signed in yet.");
        var subscriptions = new ComboBox { Margin = new Thickness(0, 14, 0, 0), IsEnabled = false, DisplayMemberPath = "Name" };
        var verify = Primary("Test credentials and finish setup");
        verify.Margin = new Thickness(0, 12, 0, 0);
        verify.IsEnabled = false;
        signIn.Margin = new Thickness(0, 18, 0, 0);
        signIn.Click += async (_, _) =>
        {
            signIn.IsEnabled = false;
            status.Text = "Connecting… complete the Microsoft sign-in in your browser.";
            SetState(BeaconState.Connecting);
            try
            {
                var login = await _core.CallAsync("login");
                if (!Bool(login["success"])) throw new InvalidOperationException(Text(login["message"], "Microsoft sign-in did not complete."));
                var found = await _core.CallAsync("list_subscriptions");
                var items = found["subscriptions"]?.AsArray() ?? [];
                foreach (var item in items)
                    subscriptions.Items.Add(new SubscriptionChoice(Text(item?["name"]), Text(item?["id"]), Text(item?["tenant_id"])));
                if (subscriptions.Items.Count == 0) throw new InvalidOperationException(Text(found["error"], "No accessible subscriptions were found."));
                subscriptions.SelectedIndex = 0;
                subscriptions.IsEnabled = true;
                verify.IsEnabled = true;
                status.Text = "Signed in. Choose a subscription to verify.";
            }
            catch (Exception error) { status.Text = error.Message; signIn.IsEnabled = true; SetState(BeaconState.Unconnectable); }
        };
        verify.Click += async (_, _) =>
        {
            if (subscriptions.SelectedItem is not SubscriptionChoice choice) return;
            verify.IsEnabled = false;
            status.Text = "Testing read access…";
            try
            {
                var result = await _core.CallAsync("complete_setup", new JsonObject { ["id"] = choice.Id, ["name"] = choice.Name, ["tenant_id"] = choice.TenantId });
                if (!Bool(result["success"])) throw new InvalidOperationException(Text(result["message"], "Credential test failed."));
                await RefreshSnapshotAsync();
                UpdateConnectionSummary();
                SetConnectionGatedNavigation(true);
                ResetCheckTimer();
                ShowOverview();
                await CheckNowAsync();
            }
            catch (Exception error) { status.Text = error.Message; verify.IsEnabled = true; SetState(BeaconState.Unconnectable); }
        };
        content.Children.Add(signIn);
        content.Children.Add(status);
        content.Children.Add(subscriptions);
        content.Children.Add(verify);
        page.Children.Add(panel);
        page.Children.Add(Muted("Security boundary: no username, password, MFA response, client secret, access token, or refresh token is written to the Beacon configuration or rule files."));
        ContentHost.Content = page;
    }

    private void ShowOverview()
    {
        PageTitle.Text = "Overview";
        PageSubtitle.Text = "Your Azure signals at a glance";
        var page = PageStack();
        var hero = Card();
        var heroContent = Vertical(hero);
        var top = new DockPanel();
        var check = Primary("Check now");
        check.Click += async (_, _) => await CheckNowAsync();
        DockPanel.SetDock(check, Dock.Right);
        top.Children.Add(check);
        top.Children.Add(Heading(StateTitle(), StateDescription()));
        heroContent.Children.Add(top);
        page.Children.Add(hero);
        var rules = _snapshot["rules"]?.AsArray() ?? [];
        page.Children.Add(SectionTitle($"Configured checks  ·  {rules.Count}"));
        if (rules.Count == 0)
        {
            var empty = Card();
            var c = Vertical(empty);
            c.Children.Add(Heading("No checks yet", "Choose from six Azure signal sources and test the exact condition before enabling it."));
            var add = Primary("Add your first check"); add.Margin = new Thickness(0, 15, 0, 0); add.Click += (_, _) => StartNewRule(); c.Children.Add(add);
            page.Children.Add(empty);
        }
        else
        {
            foreach (var rule in rules)
            {
                var id = Text(rule?["id"]);
                var latest = _activity.FirstOrDefault(item => Text(item?["check_id"]) == id);
                page.Children.Add(CheckCard(rule?.AsObject(), latest?.AsObject()));
            }
        }
        ContentHost.Content = page;
    }

    private Border CheckCard(JsonObject? rule, JsonObject? result)
    {
        var card = Card();
        var content = Vertical(card);
        var row = new DockPanel();
        var badge = Badge(result is null ? "WAITING" : Text(result["state"]).ToUpperInvariant(), result is null ? "muted" : Text(result["state"]));
        DockPanel.SetDock(badge, Dock.Right); row.Children.Add(badge);
        row.Children.Add(Heading(Text(rule?["name"], "Unnamed check"), SourceName(Text(rule?["kind"]))));
        content.Children.Add(row);
        if (result is not null)
        {
            content.Children.Add(new TextBlock { Text = Text(result["summary"]), Margin = new Thickness(0, 10, 0, 0) });
            content.Children.Add(Muted($"Last checked {FriendlyTime(Text(result["checked_at"]))}"));
        }
        return card;
    }

    private void ShowChecks()
    {
        PageTitle.Text = "Checks";
        PageSubtitle.Text = "Create, test, edit, share, and remove monitoring rules";
        var page = PageStack();
        var toolbar = new WrapPanel { Margin = new Thickness(0, 0, 0, 18) };
        var add = Primary("＋  Add a check"); add.Click += (_, _) => StartNewRule(); toolbar.Children.Add(add);
        var import = Secondary("Import rule pack…"); import.Margin = new Thickness(8, 0, 0, 0); import.Click += async (_, _) => await ImportRulesAsync(); toolbar.Children.Add(import);
        var export = Secondary("Export all rules…"); export.Margin = new Thickness(8, 0, 0, 0); export.Click += async (_, _) => await ExportRulesAsync(); toolbar.Children.Add(export);
        page.Children.Add(toolbar);
        var rules = _snapshot["rules"]?.AsArray() ?? [];
        if (rules.Count == 0) page.Children.Add(Heading("No checks configured", "Add a check to choose a signal source."));
        foreach (var node in rules)
        {
            var rule = node!.AsObject();
            var card = Card();
            var content = Vertical(card);
            var row = new DockPanel();
            var actions = new StackPanel { Orientation = Orientation.Horizontal };
            var edit = Secondary("Edit"); edit.Click += (_, _) => EditRule(rule); actions.Children.Add(edit);
            var delete = Secondary("Delete"); delete.Margin = new Thickness(7, 0, 0, 0); delete.Click += async (_, _) => await DeleteRuleAsync(rule); actions.Children.Add(delete);
            DockPanel.SetDock(actions, Dock.Right); row.Children.Add(actions);
            row.Children.Add(IconTitle(IconFor(Text(rule["kind"])), Text(rule["name"], "Unnamed check")));
            content.Children.Add(row);
            content.Children.Add(Muted($"{SourceName(Text(rule["kind"]))}  ·  {(Bool(rule["enabled"], true) ? "Enabled" : "Disabled")}"));
            page.Children.Add(card);
        }
        ContentHost.Content = page;
    }

    private void StartNewRule()
    {
        _editingRule = null;
        _selectedSource = null;
        ShowSourceChooser();
    }

    private void EditRule(JsonObject rule)
    {
        _editingRule = rule.DeepClone().AsObject();
        _selectedSource = Text(rule["kind"]);
        ShowRuleConfiguration();
    }

    private void ShowSourceChooser()
    {
        PageTitle.Text = "Add a check";
        PageSubtitle.Text = "Step 1 of 3 — choose a signal source";
        var page = PageStack();
        page.Children.Add(Heading("What should the Beacon watch?", "Nothing is preselected. Choose the Azure signal that best expresses the condition you care about."));
        var wrap = new WrapPanel { Margin = new Thickness(-6, 12, -6, 12) };
        foreach (var source in _snapshot["sources"]?.AsArray() ?? [])
        {
            var key = Text(source?["key"]);
            var button = new Button
            {
                Width = 285, MinHeight = 150, Margin = new Thickness(6), Padding = new Thickness(18),
                HorizontalContentAlignment = HAlign.Stretch,
                Background = key == _selectedSource ? Brush("AccentSoftBrush") : Brush("CardBrush"),
                BorderBrush = key == _selectedSource ? Brush("AccentBrush") : Brush("BorderBrush"),
                Tag = key,
            };
            System.Windows.Automation.AutomationProperties.SetName(button, Text(source?["label"]));
            var body = new StackPanel();
            body.Children.Add(IconTitle(IconFor(key), Text(source?["label"])));
            body.Children.Add(Muted(Text(source?["description"])));
            body.Children.Add(Badge(string.IsNullOrEmpty(Text(source?["query_language"])) ? "GUIDED" : "ADVANCED", "muted"));
            button.Content = body;
            button.Click += (_, _) => { _selectedSource = key; ShowSourceChooser(); };
            wrap.Children.Add(button);
        }
        page.Children.Add(wrap);
        var buttons = new DockPanel();
        var cancel = Secondary("Cancel"); cancel.Click += (_, _) => ShowChecks(); buttons.Children.Add(cancel);
        var next = Primary("Continue  →"); next.IsEnabled = _selectedSource is not null; next.Click += (_, _) => ShowRuleConfiguration(); DockPanel.SetDock(next, Dock.Right); buttons.Children.Add(next);
        page.Children.Add(buttons);
        ContentHost.Content = page;
    }

    private void ShowRuleConfiguration()
    {
        if (_selectedSource is null) { ShowSourceChooser(); return; }
        PageTitle.Text = _editingRule is null ? "Add a check" : "Edit check";
        PageSubtitle.Text = $"Step 2 of 3 — configure {SourceName(_selectedSource)}";
        var page = PageStack();
        var form = Card();
        var body = Vertical(form);
        body.Children.Add(IconTitle(IconFor(_selectedSource), SourceName(_selectedSource)));
        var name = Field(body, "Rule name", Text(_editingRule?["name"]));
        var enabled = new CheckBox { Content = "Enabled", IsChecked = Bool(_editingRule?["enabled"], true) }; body.Children.Add(enabled);
        var fields = new Dictionary<string, Control>();

        if (_selectedSource is "azure_resource_provisioning" or "azure_vm_power_state" or "azure_resource_property" or "azure_monitor_metric")
        {
            var resource = Field(body, "Azure resource ID", Text(_editingRule?["resource_id"])); fields["resource_id"] = resource;
            var discover = Secondary("Discover Azure resources…"); discover.Margin = new Thickness(0, 6, 0, 12); body.Children.Add(discover);
            discover.Click += async (_, _) => await DiscoverResourceAsync(resource);
        }
        if (_selectedSource == "azure_resource_provisioning")
            fields["expected_values"] = Field(body, "Healthy provisioning states (comma separated)", TextArray(_editingRule?["expected_values"], "Succeeded"));
        if (_selectedSource == "azure_vm_power_state")
            fields["expected_values"] = Field(body, "Healthy power states (comma separated)", TextArray(_editingRule?["expected_values"], "PowerState/running"));
        if (_selectedSource == "azure_resource_property")
        {
            fields["property_path"] = Field(body, "Property path", Text(_editingRule?["property_path"], "properties.provisioningState"));
            fields["property_operator"] = Choice(body, "Comparison", ["equals_any", "not_equals_any", "contains", "not_contains", "greater_than", "less_than", "exists", "missing"], Text(_editingRule?["property_operator"], "equals_any"));
            fields["expected_values"] = Field(body, "Comparison values (comma separated)", TextArray(_editingRule?["expected_values"], "Succeeded"));
        }
        if (_selectedSource is "azure_resource_graph" or "azure_log_analytics")
        {
            if (_selectedSource == "azure_log_analytics")
            {
                var workspace = Field(body, "Log Analytics workspace ID", Text(_editingRule?["workspace_id"])); fields["workspace_id"] = workspace;
                var discover = Secondary("Discover workspaces…"); discover.Margin = new Thickness(0, 6, 0, 12); body.Children.Add(discover); discover.Click += async (_, _) => await DiscoverWorkspaceAsync(workspace);
                fields["lookback_minutes"] = Field(body, "Lookback in minutes", Text(_editingRule?["lookback_minutes"], "5"));
            }
            var query = Field(body, "KQL query — returned rows are confirmed findings", Text(_editingRule?["query"]), true); fields["query"] = query;
            body.Children.Add(Muted(_selectedSource == "azure_resource_graph" ? "Runs read-only across every subscription accessible to the signed-in account." : "Runs read-only in the selected Log Analytics / Application Insights workspace."));
        }
        if (_selectedSource == "azure_monitor_metric")
        {
            fields["metric_name"] = Field(body, "Metric name", Text(_editingRule?["metric_name"]));
            fields["metric_namespace"] = Field(body, "Metric namespace (optional)", Text(_editingRule?["metric_namespace"]));
            fields["metric_aggregation"] = Choice(body, "Azure aggregation", ["Average", "Count", "Maximum", "Minimum", "Total"], Text(_editingRule?["metric_aggregation"], "Average"));
            fields["metric_reducer"] = Choice(body, "Reduce time series using", ["latest", "maximum", "minimum", "average", "total"], Text(_editingRule?["metric_reducer"], "latest"));
            fields["metric_operator"] = Choice(body, "Finding when value is", ["gt", "gte", "lt", "lte", "eq", "ne"], Text(_editingRule?["metric_operator"], "gt"));
            fields["metric_threshold"] = Field(body, "Threshold", Text(_editingRule?["metric_threshold"], "0"));
            fields["lookback_minutes"] = Field(body, "Lookback in minutes", Text(_editingRule?["lookback_minutes"], "5"));
            fields["metric_filter"] = Field(body, "Dimension filter (optional)", Text(_editingRule?["metric_filter"]));
            var discover = Secondary("Discover metrics for this resource…"); discover.Margin = new Thickness(0, 6, 0, 8); body.Children.Add(discover);
            discover.Click += async (_, _) => await DiscoverMetricAsync((TextBox)fields["resource_id"], (TextBox)fields["metric_name"], (TextBox)fields["metric_namespace"]);
        }
        page.Children.Add(form);
        var resultPanel = Card(); resultPanel.Visibility = Visibility.Collapsed; var resultBody = Vertical(resultPanel); page.Children.Add(resultPanel);
        var bar = new DockPanel();
        var back = Secondary(_editingRule is null ? "←  Signal sources" : "Cancel"); back.Click += (_, _) => { if (_editingRule is null) ShowSourceChooser(); else ShowChecks(); }; bar.Children.Add(back);
        var save = Primary("Save and enable"); save.IsEnabled = false; DockPanel.SetDock(save, Dock.Right); bar.Children.Add(save);
        var test = Secondary("Test without saving"); test.Margin = new Thickness(0, 0, 8, 0); DockPanel.SetDock(test, Dock.Right); bar.Children.Add(test);
        page.Children.Add(bar);
        JsonObject? testedDraft = null;
        test.Click += async (_, _) =>
        {
            test.IsEnabled = false; save.IsEnabled = false; resultPanel.Visibility = Visibility.Visible; resultBody.Children.Clear(); resultBody.Children.Add(Heading("Testing live…", "The exact unsaved draft is being evaluated."));
            try
            {
                var draft = BuildDraft(name.Text, enabled.IsChecked == true, fields);
                var result = await _core.CallAsync("test_rule", new JsonObject { ["rule"] = draft.DeepClone() });
                resultBody.Children.Clear();
                var reachable = Text(result["state"]) != "unconnectable";
                resultBody.Children.Add(Heading(reachable ? (Text(result["state"]) == "healthy" ? "Healthy" : "Confirmed finding") : "Could not determine", Text(result["summary"])));
                if (!string.IsNullOrWhiteSpace(Text(result["observed_value"]))) resultBody.Children.Add(Muted($"Observed: {Text(result["observed_value"])}"));
                testedDraft = draft;
                save.IsEnabled = reachable;
            }
            catch (Exception error) { resultBody.Children.Clear(); resultBody.Children.Add(Heading("Could not test this rule", error.Message)); }
            finally { test.IsEnabled = true; }
        };
        save.Click += async (_, _) =>
        {
            if (testedDraft is null) return;
            save.IsEnabled = false;
            try
            {
                var currentDraft = BuildDraft(name.Text, enabled.IsChecked == true, fields);
                await _core.CallAsync("save_rule", new JsonObject { ["rule"] = currentDraft });
                await RefreshSnapshotAsync(); ShowChecks();
            }
            catch (Exception error) { ShowError(error); save.IsEnabled = true; }
        };
        ContentHost.Content = page;

        JsonObject BuildDraft(string ruleName, bool isEnabled, Dictionary<string, Control> controls)
        {
            var draft = _editingRule?.DeepClone().AsObject() ?? new JsonObject();
            draft["id"] = Text(draft["id"], Guid.NewGuid().ToString()); draft["name"] = ruleName.Trim(); draft["kind"] = _selectedSource; draft["enabled"] = isEnabled;
            draft["resource_id"] = ControlText(controls, "resource_id"); draft["portal_url"] = Text(draft["portal_url"]); draft["tenant_id"] = Text(draft["tenant_id"]);
            var querySource = _selectedSource is "azure_resource_graph" or "azure_log_analytics";
            draft["expected_values"] = new JsonArray(ControlText(controls, "expected_values", querySource ? "" : "Succeeded").Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).Select(value => JsonValue.Create(value)).ToArray());
            draft["query"] = ControlText(controls, "query"); draft["scope"] = _selectedSource == "azure_resource_graph" ? "all_accessible" : _selectedSource == "azure_log_analytics" ? "workspace" : "resource";
            draft["workspace_id"] = ControlText(controls, "workspace_id"); draft["lookback_minutes"] = ParseInt(ControlText(controls, "lookback_minutes", "5"), 5);
            draft["metric_name"] = ControlText(controls, "metric_name"); draft["metric_namespace"] = ControlText(controls, "metric_namespace"); draft["metric_aggregation"] = ControlText(controls, "metric_aggregation", "Average");
            draft["metric_reducer"] = ControlText(controls, "metric_reducer", "latest"); draft["metric_operator"] = ControlText(controls, "metric_operator", "gt"); draft["metric_threshold"] = ParseDouble(ControlText(controls, "metric_threshold", "0")); draft["metric_filter"] = ControlText(controls, "metric_filter");
            draft["property_path"] = ControlText(controls, "property_path"); draft["property_operator"] = ControlText(controls, "property_operator", "equals_any");
            return draft;
        }
    }

    private async Task DiscoverResourceAsync(TextBox target)
    {
        try
        {
            var response = await _core.CallAsync("discover_resources");
            var picker = new ResourcePicker(response["resources"]?.AsArray() ?? []);
            if (picker.ShowDialog() == true) target.Text = picker.SelectedValue;
        }
        catch (Exception error) { ShowError(error); }
    }

    private async Task DiscoverWorkspaceAsync(TextBox target)
    {
        try
        {
            var response = await _core.CallAsync("discover_workspaces");
            var picker = new ResourcePicker(response["workspaces"]?.AsArray() ?? [], "customer_id");
            if (picker.ShowDialog() == true) target.Text = picker.SelectedValue;
        }
        catch (Exception error) { ShowError(error); }
    }

    private async Task DiscoverMetricAsync(TextBox resource, TextBox name, TextBox metricNamespace)
    {
        try
        {
            var response = await _core.CallAsync("discover_metrics", new JsonObject { ["resource_id"] = resource.Text.Trim() });
            var picker = new ResourcePicker(response["metrics"]?.AsArray() ?? [], "name", "display_name");
            if (picker.ShowDialog() == true)
            {
                name.Text = picker.SelectedValue;
                metricNamespace.Text = picker.SelectedNode is null ? "" : Text(picker.SelectedNode["namespace"]);
            }
        }
        catch (Exception error) { ShowError(error); }
    }

    private async Task DeleteRuleAsync(JsonObject rule)
    {
        if (System.Windows.MessageBox.Show($"Delete ‘{Text(rule["name"])}’?", "Delete check", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        try { await _core.CallAsync("delete_rule", new JsonObject { ["id"] = Text(rule["id"]) }); await RefreshSnapshotAsync(); ShowChecks(); }
        catch (Exception error) { ShowError(error); }
    }

    private async Task ImportRulesAsync()
    {
        var dialog = new OpenFileDialog { Filter = "Azure Health Beacon rule packs (*.json)|*.json", CheckFileExists = true };
        if (dialog.ShowDialog() != true) return;
        if (System.Windows.MessageBox.Show("Imported rules are always disabled. Review their resource IDs and KQL before testing and enabling them. Continue?", "Review imported rules", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        try { await _core.CallAsync("import_rules", new JsonObject { ["path"] = dialog.FileName }); await RefreshSnapshotAsync(); ShowChecks(); }
        catch (Exception error) { ShowError(error); }
    }

    private async Task ExportRulesAsync()
    {
        var dialog = new SaveFileDialog { Filter = "Azure Health Beacon rule packs (*.json)|*.json", FileName = "azure-health-beacon-rules.json" };
        if (dialog.ShowDialog() != true) return;
        try { await _core.CallAsync("export_rules", new JsonObject { ["path"] = dialog.FileName }); }
        catch (Exception error) { ShowError(error); }
    }

    private void ShowActivity()
    {
        PageTitle.Text = "Activity"; PageSubtitle.Text = "Results from this running session only";
        var page = PageStack(); page.Children.Add(Muted("Activity is kept in memory and is cleared when the app exits. Raw Azure responses and credentials are never retained here."));
        if (_activity.Count == 0) page.Children.Add(Heading("No activity yet", "Run Check now to populate this view."));
        foreach (var node in _activity) page.Children.Add(CheckCard(new JsonObject { ["name"] = Text(node?["name"]), ["kind"] = "" }, node?.AsObject()));
        ContentHost.Content = page;
    }

    private void ShowSettings()
    {
        PageTitle.Text = "Settings"; PageSubtitle.Text = "Windows, monitoring, appearance, and updates";
        var settings = _snapshot["settings"]?.AsObject() ?? new JsonObject();
        var page = new StackPanel { MaxWidth = 1100, HorizontalAlignment = HAlign.Stretch };
        var columns = new Grid();
        columns.ColumnDefinitions.Add(new ColumnDefinition());
        columns.ColumnDefinitions.Add(new ColumnDefinition());

        var windowsCard = Card(); windowsCard.Margin = new Thickness(0, 0, 7, 14);
        var windowsBody = Vertical(windowsCard); windowsBody.Children.Add(CompactSectionTitle("Windows"));
        var startup = new CheckBox { Content = "Start with Windows", IsChecked = Bool(settings["start_with_windows"]), Margin = new Thickness(0, 2, 0, 5) }; windowsBody.Children.Add(startup);
        var minimized = new CheckBox { Content = "Start minimized in the notification area", IsChecked = Bool(settings["start_minimized"]), Margin = new Thickness(0, 2, 0, 0) }; windowsBody.Children.Add(minimized);
        windowsBody.Children.Add(Muted("Both options are explicitly opt-in."));
        windowsBody.Children.Add(SectionTitle("Monitoring"));
        var interval = CompactField(windowsBody, "Check interval", "minutes", Text(settings["interval_minutes"], "5"));
        var timeout = CompactField(windowsBody, "Attempt timeout", "seconds", Text(settings["timeout_seconds"], "30"));
        var retries = CompactField(windowsBody, "Retry count", "", Text(settings["retry_count"], "2"));
        Grid.SetColumn(windowsCard, 0); columns.Children.Add(windowsCard);

        var serviceCard = Card(); serviceCard.Margin = new Thickness(7, 0, 0, 14);
        var serviceBody = Vertical(serviceCard); serviceBody.Children.Add(CompactSectionTitle("Azure connection"));
        serviceBody.Children.Add(Muted("Remove this app's encrypted OAuth cache and subscription binding. Rules are retained."));
        var deleteConnection = Secondary("Delete Azure connection…");
        deleteConnection.HorizontalAlignment = HAlign.Left; deleteConnection.MinWidth = 190; deleteConnection.Margin = new Thickness(0, 10, 0, 0);
        deleteConnection.Click += async (_, _) =>
        {
            if (System.Windows.MessageBox.Show("Delete all authorization state owned by Azure Health Beacon? Your rules will be retained.", "Delete Azure connection", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
            try
            {
                await _core.CallAsync("delete_connection");
                await RefreshSnapshotAsync();
                UpdateConnectionSummary();
                ResetCheckTimer();
                SetState(BeaconState.Unconnectable);
                ShowOnboarding();
            }
            catch (Exception error) { ShowError(error); }
        };
        serviceBody.Children.Add(deleteConnection);
        serviceBody.Children.Add(SectionTitle("Updates"));
        var manual = new RadioButton { Content = "Manual only", GroupName = "Updates", IsChecked = Text(settings["update_mode"], "manual") == "manual", Margin = new Thickness(0, 5, 0, 3) };
        var notify = new RadioButton { Content = "Notify me when an update is available", GroupName = "Updates", IsChecked = Text(settings["update_mode"]) == "notify", Margin = new Thickness(0, 5, 0, 3) };
        var automatic = new RadioButton { Content = "Install verified updates automatically", GroupName = "Updates", IsChecked = Text(settings["update_mode"]) == "automatic", Margin = new Thickness(0, 5, 0, 3) };
        serviceBody.Children.Add(manual); serviceBody.Children.Add(notify); serviceBody.Children.Add(automatic); serviceBody.Children.Add(Muted("Update checks and automatic installation are opt-in. Every download must match its published SHA-256 checksum."));
        var updateStatus = new TextBlock { Foreground = Brush("SuccessBrush"), Margin = new Thickness(0, 10, 0, 0), TextWrapping = TextWrapping.Wrap };
        var checkUpdate = Secondary("Check for updates"); checkUpdate.HorizontalAlignment = HAlign.Left; checkUpdate.MinWidth = 160; checkUpdate.Margin = new Thickness(0, 8, 0, 0); checkUpdate.Click += async (_, _) => await CheckForUpdatesInteractiveAsync(updateStatus); serviceBody.Children.Add(updateStatus); serviceBody.Children.Add(checkUpdate);
        Grid.SetColumn(serviceCard, 1); columns.Children.Add(serviceCard);
        page.Children.Add(columns);

        var actions = new DockPanel(); var save = Primary("Save settings"); save.Width = 160; DockPanel.SetDock(save, Dock.Right); actions.Children.Add(save); page.Children.Add(actions);
        save.Click += async (_, _) =>
        {
            try
            {
                if (automatic.IsChecked == true && Text(settings["update_mode"], "manual") != "automatic"
                    && System.Windows.MessageBox.Show("Install future verified releases automatically and restart the Beacon when needed?", "Enable automatic updates", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes)
                    return;
                StartupRegistry.SetEnabled(startup.IsChecked == true);
                var response = await _core.CallAsync("update_settings", new JsonObject
                {
                    ["start_with_windows"] = startup.IsChecked == true, ["start_minimized"] = minimized.IsChecked == true,
                    ["interval_minutes"] = ParseInt(interval.Text, 5), ["timeout_seconds"] = ParseInt(timeout.Text, 30), ["retry_count"] = ParseInt(retries.Text, 2),
                    ["update_mode"] = automatic.IsChecked == true ? "automatic" : notify.IsChecked == true ? "notify" : "manual", ["theme_mode"] = _dark ? "dark" : "light",
                });
                _snapshot["settings"] = response; ResetCheckTimer(); ShowSettings();
            }
            catch (Exception error) { ShowError(error); }
        };
        ContentHost.Content = page;
    }

    private void ShowAbout()
    {
        PageTitle.Text = "About"; PageSubtitle.Text = "Product, privacy, and open-source acknowledgements";
        var page = PageStack(); var card = Card(); var body = Vertical(card);
        body.Children.Add(Heading("Azure Health Beacon", $"Version {Text(_snapshot["version"], "0.7.1")} · Windows 11"));
        body.Children.Add(Muted("An extensible, local-first Azure signal monitor. Confirmed Azure findings are red; authentication, network, timeout, access, and indeterminate results remain grey."));
        var actions = new WrapPanel { Margin = new Thickness(0, 16, 0, 0) };
        var github = Primary("Open GitHub repository"); github.Click += (_, _) => Process.Start(new ProcessStartInfo("https://github.com/Anders0lesen/azure-extensible-systray-monitor") { UseShellExecute = true }); actions.Children.Add(github);
        var checkUpdate = Secondary("Check for updates"); checkUpdate.Margin = new Thickness(8, 0, 0, 0); actions.Children.Add(checkUpdate);
        var updateStatus = new TextBlock { Foreground = Brush("SuccessBrush"), Margin = new Thickness(0, 10, 0, 0), TextWrapping = TextWrapping.Wrap };
        checkUpdate.Click += async (_, _) => await CheckForUpdatesInteractiveAsync(updateStatus);
        body.Children.Add(actions); body.Children.Add(updateStatus);
        body.Children.Add(SectionTitle("Bundled icons")); body.Children.Add(Muted("Tabler Icons v3.46.0 · MIT License · baked into the application for offline use."));
        page.Children.Add(card); ContentHost.Content = page;
    }

    private async Task CheckForUpdatesInteractiveAsync(TextBlock status)
    {
        status.Text = "Checking GitHub releases…";
        try
        {
            var update = await _core.CallAsync("check_update");
            if (!Bool(update["is_newer"])) { status.Text = "✅ Full up-to-date - No new updates available"; return; }
            status.Text = $"Version {Text(update["version"])} is available.";
            if (System.Windows.MessageBox.Show($"Update Azure Health Beacon to {Text(update["version"])} now?", "Update available", MessageBoxButton.YesNo, MessageBoxImage.Information) == MessageBoxResult.Yes)
                await InstallUpdateAsync();
        }
        catch (Exception error) { status.Text = error.Message; }
    }

    private async Task MaybeCheckForUpdatesAsync()
    {
        var mode = Text(_snapshot["settings"]?["update_mode"], "manual");
        if (mode == "manual") return;
        if (DateTimeOffset.TryParse(Text(_snapshot["settings"]?["last_update_check_utc"]), out var lastCheck)
            && lastCheck > DateTimeOffset.UtcNow.AddDays(-1)) return;
        try
        {
            var update = await _core.CallAsync("check_update");
            if (!Bool(update["is_newer"])) return;
            if (mode == "automatic") await InstallUpdateAsync();
            else (System.Windows.Application.Current as App)?.Tray?.Notify("Azure Health Beacon update", $"Version {Text(update["version"])} is ready. Open Settings to update.");
        }
        catch { /* Background update checks must not disturb monitoring. */ }
    }

    private async Task InstallUpdateAsync()
    {
        var prepared = await _core.CallAsync("prepare_update");
        var path = Text(prepared["path"]);
        await _core.ShutdownAsync();
        var start = new ProcessStartInfo(path)
        {
            UseShellExecute = false,
            WorkingDirectory = Path.GetDirectoryName(path)!,
        };
        start.ArgumentList.Add("/VERYSILENT"); start.ArgumentList.Add("/SUPPRESSMSGBOXES"); start.ArgumentList.Add("/NORESTART"); start.ArgumentList.Add("/CLOSEAPPLICATIONS"); start.ArgumentList.Add("/RESTARTAPPLICATIONS");
        start.Environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1";
        Process.Start(start);
        ExitApplication();
    }

    private void ApplyTheme()
    {
        var colors = _dark
            ? new Dictionary<string, string> { ["WindowBrush"]="#090E15",["PanelBrush"]="#101823",["CardBrush"]="#151F2B",["BorderBrush"]="#273547",["TextBrush"]="#F3F7FB",["MutedBrush"]="#9BAABC",["AccentSoftBrush"]="#173B57" }
            : new Dictionary<string, string> { ["WindowBrush"]="#F4F7FA",["PanelBrush"]="#FFFFFF",["CardBrush"]="#FFFFFF",["BorderBrush"]="#D4DDE6",["TextBrush"]="#17212C",["MutedBrush"]="#5D6D7E",["AccentSoftBrush"]="#DCEFFE" };
        foreach (var item in colors) System.Windows.Application.Current.Resources[item.Key] = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(item.Value));
        ThemeButton.Content = _dark ? "☀  Light" : "☾  Dark";
    }

    private void UpdateConnectionSummary()
    {
        var connected = Bool(_snapshot["connection"]?["initialized"]);
        ConnectionLabel.Text = connected ? "Connected" : "Not connected";
        ConnectionDetail.Text = connected ? Text(_snapshot["connection"]?["subscription_name"], "Azure") : "Azure connection required";
    }

    private string StateTitle() => _state switch { BeaconState.Healthy => "All configured checks are healthy", BeaconState.Failed => "Azure needs your attention", BeaconState.Checking => "Checking Azure…", _ => "Status could not be determined" };
    private string StateDescription() => _state switch { BeaconState.Healthy => "No configured rule currently reports a finding.", BeaconState.Failed => "At least one check confirmed the condition you asked it to find.", BeaconState.Checking => "The Beacon is refreshing every enabled rule.", _ => "Authentication, access, network, timeout, or missing telemetry prevented a definitive result." };

    private static StackPanel PageStack() => new() { MaxWidth = 900, HorizontalAlignment = HAlign.Stretch };
    private static Border Card() => new() { Background = Brush("CardBrush"), BorderBrush = Brush("BorderBrush"), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(12), Padding = new Thickness(20), Margin = new Thickness(0,0,0,14) };
    private static StackPanel Vertical(Border card) { var body = new StackPanel(); card.Child = body; return body; }
    private static StackPanel Heading(string title, string subtitle) { var p = new StackPanel(); p.Children.Add(new TextBlock { Text=title, FontSize=19, FontWeight=FontWeights.SemiBold }); if (!string.IsNullOrWhiteSpace(subtitle)) p.Children.Add(new TextBlock { Text=subtitle, Foreground=Brush("MutedBrush"), Margin=new Thickness(0,4,0,0) }); return p; }
    private static TextBlock SectionTitle(string text) => new() { Text=text, FontWeight=FontWeights.SemiBold, FontSize=15, Margin=new Thickness(0,18,0,9) };
    private static TextBlock CompactSectionTitle(string text) => new() { Text=text, FontWeight=FontWeights.SemiBold, FontSize=15, Margin=new Thickness(0,0,0,9) };
    private static TextBlock Muted(string text) => new() { Text=text, Foreground=Brush("MutedBrush"), FontSize=12, Margin=new Thickness(0,5,0,0) };
    private static Button Primary(string text) => new() { Content=text, Style=(Style)System.Windows.Application.Current.FindResource("PrimaryButton") };
    private static Button Secondary(string text) => new() { Content=text };
    private static StackPanel IconTitle(string icon, string title) { var row = new StackPanel { Orientation=Orientation.Horizontal, Margin=new Thickness(0,0,0,5) }; row.Children.Add(new TablerIcon { Icon=icon, Width=24, Height=24, Stroke=Brush("AccentBrush"), Margin=new Thickness(0,0,10,0) }); row.Children.Add(new TextBlock { Text=title, FontWeight=FontWeights.SemiBold, FontSize=16, VerticalAlignment=VAlign.Center }); return row; }
    private static Border Badge(string text, string state) { var color = state switch { "healthy" => Brush("SuccessBrush"), "failed" => Brush("DangerBrush"), "unconnectable" => Brush("MutedBrush"), _ => Brush("MutedBrush") }; return new Border { BorderBrush=color, BorderThickness=new Thickness(1), CornerRadius=new CornerRadius(10), Padding=new Thickness(8,3,8,3), Margin=new Thickness(8,0,0,0), Child=new TextBlock { Text=text, Foreground=color, FontSize=10, FontWeight=FontWeights.SemiBold } }; }
    private static TextBox Field(Panel parent, string label, string value, bool multiline=false) { parent.Children.Add(new TextBlock { Text=label, FontWeight=FontWeights.SemiBold, Margin=new Thickness(0,12,0,5) }); var box = new TextBox { Text=value, AcceptsReturn=multiline, TextWrapping=multiline ? TextWrapping.NoWrap : TextWrapping.Wrap, MinHeight=multiline ? 170 : 0, VerticalScrollBarVisibility=multiline ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled, HorizontalScrollBarVisibility=multiline ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled, FontFamily=multiline ? new FontFamily("Cascadia Mono, Consolas") : System.Windows.Application.Current.MainWindow?.FontFamily }; parent.Children.Add(box); return box; }
    private static TextBox CompactField(Panel parent, string label, string unit, string value)
    {
        var row = new Grid { Margin = new Thickness(0, 9, 0, 0) };
        row.ColumnDefinitions.Add(new ColumnDefinition());
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        row.Children.Add(new TextBlock { Text=label, FontWeight=FontWeights.SemiBold, VerticalAlignment=VAlign.Center });
        var box = new TextBox { Text=value, Width=72, Margin=new Thickness(12,0,0,0), HorizontalContentAlignment=HAlign.Right };
        Grid.SetColumn(box, 1); row.Children.Add(box);
        if (!string.IsNullOrWhiteSpace(unit))
        {
            var suffix = new TextBlock { Text=unit, Foreground=Brush("MutedBrush"), Margin=new Thickness(8,0,0,0), VerticalAlignment=VAlign.Center, Width=54 };
            Grid.SetColumn(suffix, 2); row.Children.Add(suffix);
        }
        parent.Children.Add(row);
        return box;
    }
    private static ComboBox Choice(Panel parent, string label, string[] values, string selected) { parent.Children.Add(new TextBlock { Text=label, FontWeight=FontWeights.SemiBold, Margin=new Thickness(0,12,0,5) }); var combo = new ComboBox { ItemsSource=values, SelectedItem=selected }; if (combo.SelectedIndex < 0) combo.SelectedIndex=0; parent.Children.Add(combo); return combo; }
    private static Brush Brush(string key) => (Brush)System.Windows.Application.Current.FindResource(key);
    private static string Text(JsonNode? node, string fallback="") { if (node is null) return fallback; try { return node.GetValue<string>(); } catch { return node.ToJsonString().Trim('"'); } }
    private static bool Bool(JsonNode? node, bool fallback=false) { try { return node?.GetValue<bool>() ?? fallback; } catch { return fallback; } }
    private static int Int(JsonNode? node, int fallback) => int.TryParse(Text(node), out var value) ? value : fallback;
    private static int ParseInt(string value, int fallback) => int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed) ? parsed : fallback;
    private static double ParseDouble(string value) => double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed) ? parsed : double.NaN;
    private static string TextArray(JsonNode? node, string fallback) => node is JsonArray a ? string.Join(", ", a.Select(item => Text(item))) : fallback;
    private static string ControlText(Dictionary<string, Control> fields, string key, string fallback="") => fields.TryGetValue(key, out var control) ? control switch { TextBox box => box.Text.Trim(), ComboBox combo => combo.SelectedItem?.ToString() ?? fallback, _ => fallback } : fallback;
    private static string FriendlyTime(string value) => DateTimeOffset.TryParse(value, out var time) ? time.ToLocalTime().ToString("g") : value;
    private static string SourceName(string key) => key switch { "azure_resource_provisioning"=>"Provisioning state", "azure_vm_power_state"=>"VM power state", "azure_resource_property"=>"Resource property", "azure_resource_graph"=>"Resource Graph", "azure_log_analytics"=>"Logs / Application Insights", "azure_monitor_metric"=>"Azure Monitor metric", _=>key };
    private static string IconFor(string key) => key switch { "azure_resource_provisioning"=>"progress-check", "azure_vm_power_state"=>"server-2", "azure_resource_property"=>"braces", "azure_resource_graph"=>"hierarchy-2", "azure_log_analytics"=>"file-analytics", "azure_monitor_metric"=>"activity", _=>"activity" };
    private static void ShowError(Exception error) => System.Windows.MessageBox.Show(error.Message, "Azure Health Beacon", MessageBoxButton.OK, MessageBoxImage.Error);
    private void SetConnectionGatedNavigation(bool connected)
    {
        OverviewNav.IsEnabled = true;
        ChecksNav.IsEnabled = connected;
        ActivityNav.IsEnabled = connected;
        SettingsNav.IsEnabled = true;
        AboutNav.IsEnabled = true;
    }

    private void Overview_Click(object sender, RoutedEventArgs e)
    {
        if (Bool(_snapshot["connection"]?["initialized"])) ShowOverview();
        else ShowOnboarding();
    }
    private void Checks_Click(object sender, RoutedEventArgs e) => ShowChecks();
    private void Activity_Click(object sender, RoutedEventArgs e) => ShowActivity();
    private void Settings_Click(object sender, RoutedEventArgs e) => ShowSettings();
    private void About_Click(object sender, RoutedEventArgs e) => ShowAbout();
    private async void Theme_Click(object sender, RoutedEventArgs e) { _dark=!_dark; ApplyTheme(); try { var s=_snapshot["settings"]?.AsObject() ?? new JsonObject(); s["theme_mode"]=_dark?"dark":"light"; _snapshot["settings"]=await _core.CallAsync("update_settings", s.DeepClone().AsObject()); } catch { } }
    private void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e) { if (!_allowClose) { e.Cancel=true; Hide(); } }
    public void ExitApplication() { _allowClose=true; System.Windows.Application.Current.Shutdown(); }
    public void PrepareForSystemShutdown() => _allowClose = true;

    private sealed record SubscriptionChoice(string Name, string Id, string TenantId);
}

internal sealed class ResourcePicker : Window
{
    public string SelectedValue { get; private set; } = "";
    public JsonObject? SelectedNode { get; private set; }
    public ResourcePicker(JsonArray items, string valueKey="resource_id", string labelKey="name")
    {
        Title="Choose an Azure item"; Width=760; Height=560; WindowStartupLocation=WindowStartupLocation.CenterOwner; Background=(Brush)System.Windows.Application.Current.FindResource("WindowBrush"); Foreground=(Brush)System.Windows.Application.Current.FindResource("TextBrush");
        var root=new DockPanel { Margin=new Thickness(18) }; var search=new TextBox { Margin=new Thickness(0,0,0,10) }; DockPanel.SetDock(search,Dock.Top); root.Children.Add(search);
        var buttons=new StackPanel { Orientation=Orientation.Horizontal, HorizontalAlignment=HAlign.Right, Margin=new Thickness(0,10,0,0) }; DockPanel.SetDock(buttons,Dock.Bottom); var cancel=new Button { Content="Cancel", MinWidth=90 }; cancel.Click+=(_,_)=>Close(); var choose=new Button { Content="Choose", MinWidth=90, Margin=new Thickness(8,0,0,0), Style=(Style)System.Windows.Application.Current.FindResource("PrimaryButton"), IsEnabled=false }; buttons.Children.Add(cancel); buttons.Children.Add(choose); root.Children.Add(buttons);
        var list=new ListBox(); root.Children.Add(list); var all=items.Where(x=>x is not null).Select(x=>x!.AsObject()).ToList();
        void Fill(string filter) { list.Items.Clear(); foreach(var item in all.Where(i => (Text(i[labelKey])+" "+Text(i[valueKey])+" "+Text(i["resource_type"])).Contains(filter,StringComparison.OrdinalIgnoreCase))) list.Items.Add(new PickerItem(Text(item[labelKey],Text(item[valueKey])),Text(item[valueKey]),item)); }
        Fill(""); search.TextChanged+=(_,_)=>Fill(search.Text); list.SelectionChanged+=(_,_)=>choose.IsEnabled=list.SelectedItem is PickerItem; list.MouseDoubleClick+=(_,_)=>Accept(); choose.Click+=(_,_)=>Accept();
        void Accept() { if(list.SelectedItem is not PickerItem item)return; SelectedValue=item.Value; SelectedNode=item.Node; DialogResult=true; }
        Content=root;
    }
    private sealed record PickerItem(string Label,string Value,JsonObject Node) { public override string ToString()=> $"{Label}\n{Value}"; }
    private static string Text(JsonNode? node,string fallback="") { try{return node?.GetValue<string>()??fallback;}catch{return fallback;} }
}
