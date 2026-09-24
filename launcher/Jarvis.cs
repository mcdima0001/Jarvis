// Jarvis.exe — запускатель без окна консоли.
//
// Сам ассистент он не содержит: находит папку проекта рядом с собой и зовёт
// `pythonw -m jarvis --tray`. Поэтому правки кода подхватываются без
// пересборки, а весит он десяток килобайт. Собирается `python launcher/build.py`
// компилятором, который есть в любой Windows, — ставить ничего не нужно.
//
// Язык — C# 5: другого компилятор из .NET Framework не знает. Отсюда ни
// интерполяции строк, ни `?.`.

using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class Launcher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr owner, string text, string caption, uint type);

    private const uint MB_ICONERROR = 0x10;

    // Интерпретатор, на котором Jarvis живёт у владельца. Переопределяется
    // переменной JARVIS_PYTHON.
    private const string OwnerPython = @"C:\Python314\pythonw.exe";

    [STAThread]
    private static int Main(string[] args)
    {
        string root = FindRoot(AppDomain.CurrentDomain.BaseDirectory);
        if (root == null)
        {
            Fail("Не нашёл папку Jarvis. Положи Jarvis.exe в корень проекта — рядом с папкой jarvis.");
            return 1;
        }

        string python = FindPython();
        if (python == null)
        {
            Fail("Не нашёл pythonw.exe. Укажи путь к нему в переменной окружения JARVIS_PYTHON.");
            return 2;
        }

        StringBuilder arguments = new StringBuilder("-m jarvis --tray");
        foreach (string arg in args)
        {
            arguments.Append(' ').Append(Quote(arg));
        }

        // В диспетчере задач процесс подписан «Python», и владелец просил
        // называть его Джарвисом (23.09.2026). Рядом с проектом лежит копия
        // интерпретатора с переписанным описанием и значком — `JarvisApp.exe`,
        // её и запускаем. Стандартную библиотеку она ищет рядом с собой, поэтому
        // ей нужен PYTHONHOME: папку мы и так знаем — нашли настоящий pythonw.
        // Имя сменилось 24.09.2026 с `JarvisHost.exe`: диспетчер продолжал
        // рисовать питоновский значок, хотя сама система для того же пути
        // отдавала наш. Новый путь не кеширован нигде.
        string host = Path.Combine(root, "JarvisApp.exe");
        bool renamed = File.Exists(host);

        ProcessStartInfo info = new ProcessStartInfo(renamed ? host : python, arguments.ToString());
        info.WorkingDirectory = root;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        if (renamed)
        {
            info.EnvironmentVariables["PYTHONHOME"] = Path.GetDirectoryName(python);
        }
        try
        {
            Process.Start(info);
        }
        catch (Exception error)
        {
            Fail("Не удалось запустить " + python + ":\n" + error.Message);
            return 3;
        }
        return 0;
    }

    // Вверх от папки программы до той, где лежит jarvis\__main__.py.
    private static string FindRoot(string start)
    {
        DirectoryInfo folder = new DirectoryInfo(start);
        while (folder != null)
        {
            if (File.Exists(Path.Combine(folder.FullName, "jarvis", "__main__.py")))
            {
                return folder.FullName;
            }
            folder = folder.Parent;
        }
        return null;
    }

    private static string FindPython()
    {
        string given = Environment.GetEnvironmentVariable("JARVIS_PYTHON");
        if (!string.IsNullOrEmpty(given) && File.Exists(given))
        {
            return given;
        }
        if (File.Exists(OwnerPython))
        {
            return OwnerPython;
        }
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string folder in path.Split(Path.PathSeparator))
        {
            try
            {
                string candidate = Path.Combine(folder.Trim('"'), "pythonw.exe");
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch (ArgumentException)
            {
                // Кривой элемент PATH — просто пропускаем.
            }
        }
        return null;
    }

    private static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
        {
            return arg;
        }
        return "\"" + arg.Replace("\"", "\\\"") + "\"";
    }

    private static void Fail(string text)
    {
        MessageBoxW(IntPtr.Zero, text, "Jarvis", MB_ICONERROR);
    }
}
