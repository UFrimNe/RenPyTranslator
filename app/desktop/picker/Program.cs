using System;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Windows.Forms;

internal static class FolderPicker
{
    sealed class Owner : IWin32Window
    {
        public IntPtr Handle { get; }
        public Owner(long handle) { Handle=new IntPtr(handle); }
    }
    [STAThread]
    public static int Main()
    {
        Console.OutputEncoding=new UTF8Encoding(false);
        try
        {
            using var request=JsonDocument.Parse(Console.In.ReadToEnd());
            var value=request.RootElement;
            string kind=value.GetProperty("kind").GetString()??"";
            if(kind!="game" && kind!="translation")throw new ArgumentException("Неверный тип выбора папки.");
            string selected=value.GetProperty("initial").GetString()??"";
            if(selected.Length>2048)throw new ArgumentException("Слишком длинный путь.");
            long owner=value.GetProperty("owner").GetInt64();
            Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            selected=selected.Trim().Trim('"');
            if(File.Exists(selected))selected=Path.GetDirectoryName(Path.GetFullPath(selected))??"";
            if(!Directory.Exists(selected))selected=Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
            using var dialog=new FolderBrowserDialog {
                AutoUpgradeEnabled=true,
                UseDescriptionForTitle=true,
                Description=kind=="game"?"Выбери папку Ren’Py-игры":"Выбери папку готового RPTS-перевода",
                ShowNewFolderButton=true,
                InitialDirectory=selected,
                SelectedPath=selected
            };
            DialogResult result=owner==0?dialog.ShowDialog():dialog.ShowDialog(new Owner(owner));
            Console.WriteLine(JsonSerializer.Serialize(new { path=result==DialogResult.OK?dialog.SelectedPath:null }));
            return 0;
        }
        catch(Exception ex)
        {
            Console.Error.WriteLine(ex.ToString());
            Console.WriteLine(JsonSerializer.Serialize(new { error="Не удалось открыть системный диалог. Можно вставить путь вручную." }));
            return 1;
        }
    }
}
