using System.Diagnostics;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using CefSharp;
using CefSharp.WinForms;
using CefSharp.Handler;

internal static class Program
{
    internal static string Root = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", ".."));
    [STAThread]
    public static int Main(string[] args)
    {
        try { return Run(args); }
        catch (Exception ex)
        {
            Directory.CreateDirectory(Path.Combine(Root, "data", "logs"));
            File.AppendAllText(Path.Combine(Root, "data", "logs", "desktop-host.log"), DateTime.Now + " " + ex + "\n");
            MessageBox.Show("Не удалось открыть RPTS.\n\n" + ex.Message + "\n\nПодробности: data\\logs\\desktop-host.log\nКонсоль: RPTS-Console.bat", "RPTS", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
    private static int Run(string[] args)
    {
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        int subprocess = CefSharp.BrowserSubprocess.SelfHost.Main(args);
        if (subprocess >= 0) return subprocess;
        using var running = new Mutex(false, "Local\\RPTS-Running");
        string key = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Root.ToLowerInvariant()))).Substring(0,24);
        using var mutex = new Mutex(true, "Local\\RPTS-Desktop-" + key, out bool created);
        if (!created) return 0;
        string cache = Path.Combine(Root, "data", "cache", "cef");
        Directory.CreateDirectory(cache);
        Directory.CreateDirectory(Path.Combine(Root,"data","logs"));
        var settings = new CefSettings
        {
            RootCachePath = cache,
            CachePath = Path.Combine(cache,"profile"),
            BrowserSubprocessPath = Environment.ProcessPath,
            LogFile = Path.Combine(Root,"data","logs","cef.log"),
            LogSeverity = LogSeverity.Warning,
            Locale = "ru"
        };
        settings.CefCommandLineArgs.Add("disable-features", "OptimizationGuideOnDeviceModel");
        if (!Cef.Initialize(settings, performDependencyCheck:true, browserProcessHandler:null)) throw new Exception("CEF не удалось инициализировать. Проверь журнал cef.log и Visual C++ x64 Runtime.");
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        using (var window = new DesktopWindow()) Application.Run(window);
        Cef.Shutdown();
        return 0;
    }
}
internal sealed class LocalOnly : RequestHandler
{
    internal string AllowedOrigin = "";
    protected override bool OnBeforeBrowse(IWebBrowser chromiumWebBrowser, IBrowser browser, IFrame frame, IRequest request, bool userGesture, bool isRedirect)
    {
        if (request.Url == "about:blank" || request.Url.StartsWith("data:text/html")) return false;
        return !Uri.TryCreate(request.Url,UriKind.Absolute,out var url) || url.GetLeftPart(UriPartial.Authority) != AllowedOrigin;
    }
}
internal sealed class NoPopups : LifeSpanHandler
{
    protected override bool OnBeforePopup(IWebBrowser chromiumWebBrowser, IBrowser browser, IFrame frame,
        string targetUrl, string targetFrameName, WindowOpenDisposition targetDisposition, bool userGesture,
        IPopupFeatures popupFeatures, IWindowInfo windowInfo, IBrowserSettings browserSettings,
        ref bool noJavascriptAccess, out IWebBrowser newBrowser)
    { newBrowser = null!; return true; }
}
internal sealed class DesktopWindow : Form
{
    readonly ChromiumWebBrowser browser;
    readonly LocalOnly navigation = new();
    readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(5) };
    Process? backend;
    string endpoint = "";
    bool closing, allowClose;
    public DesktopWindow()
    {
        Text = "RPTS — локальный перевод игр";
        Width=1360;Height=900;MinimumSize=new Size(920,680);StartPosition=FormStartPosition.CenterScreen;
        BackColor=Color.FromArgb(17,18,24);
        Icon=Icon.ExtractAssociatedIcon(Environment.ProcessPath!);
        browser=new ChromiumWebBrowser("about:blank") { Dock=DockStyle.Fill, RequestHandler=navigation, LifeSpanHandler=new NoPopups() };
        Controls.Add(browser);
        Shown+=async (_,_)=>await StartBackend();
        FormClosing+=async (_,eventArgs)=>
        {
            if(allowClose)return;
            eventArgs.Cancel=true;
            if(closing)return;
            closing=true;Text="RPTS — остановка и сохранение…";
            await StopBackend();allowClose=true;Close();
        };
    }
    async Task StartBackend()
    {
        try
        {
            var loading=new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            browser.IsBrowserInitializedChanged+=(_,_)=>{if(browser.IsBrowserInitialized)loading.TrySetResult(true);};
            if(browser.IsBrowserInitialized)loading.TrySetResult(true);
            await loading.Task.WaitAsync(TimeSpan.FromSeconds(30));
            browser.LoadHtml("<html><meta charset='utf-8'><body style='margin:0;background:#111218;color:#d8cbff;display:grid;place-items:center;height:100vh;font:18px Segoe UI'><div>RPTS<br><small style='color:#9295ac'>Подготавливаем библиотеку…</small></div></body></html>");
            string python=Path.Combine(Program.Root,"runtime","python","python.exe");
            bool pyLauncher=false;
            if(!File.Exists(python))
            {
                python=FindOnPath("py.exe")??FindOnPath("python.exe")??throw new Exception("Python не найден. Распакуй полный архив, включая runtime\\python, или установи Python 3.10+.");
                pyLauncher=Path.GetFileName(python).Equals("py.exe",StringComparison.OrdinalIgnoreCase);
            }
            var info=new ProcessStartInfo(python) { WorkingDirectory=Program.Root,UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8 };
            if(pyLauncher)info.ArgumentList.Add("-3");
            info.ArgumentList.Add("-u");info.ArgumentList.Add(Path.Combine(Program.Root,"app","tools","desktop_server.py"));info.ArgumentList.Add("--root");info.ArgumentList.Add(Program.Root);
            info.Environment["PYTHONUTF8"]="1";info.Environment["PYTHONIOENCODING"]="utf-8";
            var ready=new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
            backend=new Process { StartInfo=info,EnableRaisingEvents=true };
            backend.OutputDataReceived+=(_,ev)=> { if(ev.Data?.StartsWith("RPTS_URL ")==true)ready.TrySetResult(ev.Data[9..]); };
            backend.ErrorDataReceived+=(_,ev)=> { if(ev.Data!=null)File.AppendAllText(Path.Combine(Program.Root,"data","logs","desktop-backend.log"),ev.Data+"\n"); };
            backend.Exited+=(_,_)=>ready.TrySetException(new Exception("Служба RPTS завершилась. Открой data\\logs\\desktop-backend.log."));
            backend.Start();backend.BeginOutputReadLine();backend.BeginErrorReadLine();
            string address=await ready.Task.WaitAsync(TimeSpan.FromSeconds(60));
            var uri=new Uri(address);endpoint=uri.GetLeftPart(UriPartial.Authority);navigation.AllowedOrigin=endpoint;
            http.DefaultRequestHeaders.Authorization=new AuthenticationHeaderValue("Bearer",uri.Fragment.TrimStart('#'));
            browser.Load(address);
        }
        catch(Exception ex)
        {
            string message=System.Net.WebUtility.HtmlEncode(ex.Message);
            browser.LoadHtml($"<html><meta charset='utf-8'><body style='background:#111218;color:#eee;font:16px Segoe UI;padding:70px'><h1>Не удалось запустить RPTS</h1><p>{message}</p><p>Консольный запуск: RPTS-Console.bat<br>Журнал: data\\logs</p></body></html>");
        }
    }
    async Task StopBackend()
    {
        if(backend==null)return;
        try
        {
            var deadline=DateTime.UtcNow.AddSeconds(120);
            while(!backend.HasExited && endpoint.Length>0 && DateTime.UtcNow<deadline)
            {
                using var response=await http.PostAsync(endpoint+"/api/shutdown",new StringContent("{}",Encoding.UTF8,"application/json"));
                using var json=JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                if(!json.RootElement.TryGetProperty("busy",out var busy)||!busy.GetBoolean())break;
                await Task.Delay(800);
            }
            if(!backend.HasExited)
            {
                try { await backend.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(3)); }
                catch(TimeoutException) { backend.Kill(entireProcessTree:true); }
            }
        }
        catch { try { if(!backend.HasExited)backend.Kill(entireProcessTree:true); } catch { } }
    }
    static string? FindOnPath(string name)
    {
        foreach(var dir in (Environment.GetEnvironmentVariable("PATH")??"").Split(Path.PathSeparator))
        { try { string p=Path.Combine(dir.Trim('"'),name);if(File.Exists(p))return p; } catch { } }
        return null;
    }
    protected override void Dispose(bool disposing)
    {
        if(disposing){browser.Dispose();http.Dispose();backend?.Dispose();}
        base.Dispose(disposing);
    }
}
