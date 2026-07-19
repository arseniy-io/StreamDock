using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace StreamDock.NativeHost
{
    internal static class Program
    {
        private const int MaxMessageBytes = 64 * 1024;
        private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer();

        private static int Main()
        {
            AppController controller = null;
            try
            {
                controller = new AppController(ProjectRoot());
                Stream input = Console.OpenStandardInput();
                Stream output = Console.OpenStandardOutput();

                while (true)
                {
                    Dictionary<string, object> request = ReadMessage(input);
                    if (request == null)
                    {
                        break;
                    }

                    Dictionary<string, object> response = HandleRequest(controller, request);
                    WriteMessage(output, response);

                    object state;
                    if (response.TryGetValue("state", out state) && String.Equals(Convert.ToString(state), "stopped", StringComparison.Ordinal))
                    {
                        break;
                    }
                }

                return 0;
            }
            catch (Exception exception)
            {
                SafeLog("Native host завершился с ошибкой: " + exception);
                return 1;
            }
            finally
            {
                if (controller != null)
                {
                    controller.Dispose();
                }
            }
        }

        private static Dictionary<string, object> HandleRequest(AppController controller, Dictionary<string, object> request)
        {
            string requestId = ReadString(request, "requestId");
            string command = ReadString(request, "command").ToLowerInvariant();

            Dictionary<string, object> response;
            try
            {
                switch (command)
                {
                    case "status":
                        response = controller.Status();
                        break;
                    case "start":
                        response = controller.Start();
                        break;
                    case "stop":
                        response = controller.Stop();
                        break;
                    default:
                        response = Result(false, "error", false, "Неизвестная команда локального помощника.");
                        break;
                }
            }
            catch (Exception exception)
            {
                SafeLog("Ошибка команды " + command + ": " + exception);
                response = Result(false, "error", false, "Локальный помощник не смог выполнить команду.");
            }

            response["requestId"] = requestId;
            return response;
        }

        internal static Dictionary<string, object> Result(bool ok, string state, bool managed, string message)
        {
            return new Dictionary<string, object>
            {
                { "ok", ok },
                { "state", state },
                { "managed", managed },
                { "message", message }
            };
        }

        private static string ReadString(Dictionary<string, object> value, string key)
        {
            object raw;
            return value.TryGetValue(key, out raw) && raw != null ? Convert.ToString(raw) : String.Empty;
        }

        private static Dictionary<string, object> ReadMessage(Stream input)
        {
            byte[] lengthBytes = ReadExactly(input, 4, true);
            if (lengthBytes == null)
            {
                return null;
            }

            int length = BitConverter.ToInt32(lengthBytes, 0);
            if (length <= 0 || length > MaxMessageBytes)
            {
                throw new InvalidDataException("Недопустимый размер Native Messaging сообщения.");
            }

            byte[] body = ReadExactly(input, length, false);
            string json = Encoding.UTF8.GetString(body);
            Dictionary<string, object> parsed = Serializer.Deserialize<Dictionary<string, object>>(json);
            if (parsed == null)
            {
                throw new InvalidDataException("Получено пустое Native Messaging сообщение.");
            }
            return parsed;
        }

        private static byte[] ReadExactly(Stream input, int count, bool eofAllowed)
        {
            byte[] buffer = new byte[count];
            int offset = 0;
            while (offset < count)
            {
                int read = input.Read(buffer, offset, count - offset);
                if (read == 0)
                {
                    if (offset == 0 && eofAllowed)
                    {
                        return null;
                    }
                    throw new EndOfStreamException("Native Messaging сообщение оборвалось.");
                }
                offset += read;
            }
            return buffer;
        }

        private static void WriteMessage(Stream output, Dictionary<string, object> response)
        {
            byte[] body = Encoding.UTF8.GetBytes(Serializer.Serialize(response));
            byte[] length = BitConverter.GetBytes(body.Length);
            output.Write(length, 0, length.Length);
            output.Write(body, 0, body.Length);
            output.Flush();
        }

        private static string ProjectRoot()
        {
            string executableDirectory = Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName);
            return Path.GetFullPath(Path.Combine(executableDirectory, "..", "..", ".."));
        }

        internal static void SafeLog(string message)
        {
            try
            {
                string logDirectory = Path.Combine(ProjectRoot(), "logs");
                Directory.CreateDirectory(logDirectory);
                File.AppendAllText(
                    Path.Combine(logDirectory, "native-host.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " | " + message + Environment.NewLine,
                    Encoding.UTF8
                );
            }
            catch
            {
                // stdout занят протоколом Chrome, поэтому ошибки логирования игнорируются.
            }
        }
    }

    internal sealed class AppController : IDisposable
    {
        private const string HealthUrl = "http://127.0.0.1:8765/api/health";
        private const string ShutdownUrl = "http://127.0.0.1:8765/api/control/shutdown";
        private const string ControlTokenResponseHeader = "X-StreamDock-Control-Token";
        private const int StillActive = 259;
        private const uint CreateSuspended = 0x00000004;
        private const uint CreateNoWindow = 0x08000000;
        private const uint StartfUseStdHandles = 0x00000100;
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint HandleFlagInherit = 0x00000001;
        private const uint GenericRead = 0x80000000;
        private const uint FileShareRead = 0x00000001;
        private const uint FileShareWrite = 0x00000002;
        private const uint OpenExisting = 3;

        private readonly string projectRoot;
        private readonly string pythonPath;
        private IntPtr jobHandle = IntPtr.Zero;
        private IntPtr processHandle = IntPtr.Zero;
        private string controlToken = String.Empty;

        internal AppController(string projectRootPath)
        {
            projectRoot = projectRootPath;
            pythonPath = Path.Combine(projectRoot, ".venv", "Scripts", "python.exe");
        }

        internal Dictionary<string, object> Status()
        {
            if (TrackedProcessAlive())
            {
                return Program.Result(true, "running", true, "StreamDock запущен и готов к работе.");
            }

            ReleaseHandles();
            if (IsStreamDockOnline())
            {
                return Program.Result(true, "running", false, "StreamDock запущен. Его можно остановить из расширения.");
            }

            return Program.Result(true, "stopped", false, "StreamDock остановлен.");
        }

        internal Dictionary<string, object> Start()
        {
            if (TrackedProcessAlive())
            {
                return Program.Result(true, "running", true, "StreamDock уже запущен.");
            }

            ReleaseHandles();
            if (IsStreamDockOnline())
            {
                return Program.Result(true, "running", false, "StreamDock уже запущен и готов к работе.");
            }
            if (IsPortOpen())
            {
                return Program.Result(false, "error", false, "Порт 8765 занят другой программой. Закройте её и повторите запуск.");
            }
            if (!File.Exists(pythonPath) || !File.Exists(Path.Combine(projectRoot, "app", "main.py")))
            {
                return Program.Result(false, "error", false, "Файлы локального приложения не найдены. Запустите install.bat ещё раз.");
            }

            try
            {
                StartSuspendedProcess();
            }
            catch (Exception exception)
            {
                Program.SafeLog("Не удалось запустить сервер: " + exception);
                ForceStopTrackedProcess();
                return Program.Result(false, "error", false, "Не удалось запустить StreamDock. Подробности сохранены в локальном логе.");
            }

            DateTime deadline = DateTime.UtcNow.AddSeconds(20);
            while (DateTime.UtcNow < deadline)
            {
                if (!TrackedProcessAlive())
                {
                    ForceStopTrackedProcess();
                    return Program.Result(false, "error", false, "StreamDock завершился во время запуска. Проверьте локальный лог приложения.");
                }
                if (IsStreamDockOnline())
                {
                    return Program.Result(true, "running", true, "StreamDock запущен и готов к работе.");
                }
                Thread.Sleep(200);
            }

            ForceStopTrackedProcess();
            return Program.Result(false, "error", false, "StreamDock не успел запуститься. Попробуйте ещё раз.");
        }

        internal Dictionary<string, object> Stop()
        {
            if (!TrackedProcessAlive())
            {
                ReleaseHandles();
                if (IsStreamDockOnline())
                {
                    if (!RequestGracefulShutdown())
                    {
                        return Program.Result(false, "running", false, "StreamDock отвечает, но безопасная остановка недоступна. Перезапустите приложение и повторите попытку.");
                    }

                    DateTime externalDeadline = DateTime.UtcNow.AddSeconds(5);
                    while (DateTime.UtcNow < externalDeadline && IsPortOpen())
                    {
                        Thread.Sleep(100);
                    }
                    if (IsPortOpen())
                    {
                        return Program.Result(false, "running", false, "StreamDock не успел остановиться. Повторите попытку через несколько секунд.");
                    }
                    return Program.Result(true, "stopped", false, "StreamDock остановлен.");
                }
                return Program.Result(true, "stopped", false, "StreamDock уже остановлен.");
            }

            RequestGracefulShutdown();
            DateTime deadline = DateTime.UtcNow.AddSeconds(5);
            while (DateTime.UtcNow < deadline && TrackedProcessAlive())
            {
                Thread.Sleep(100);
            }

            ForceStopTrackedProcess();
            DateTime portDeadline = DateTime.UtcNow.AddSeconds(3);
            while (DateTime.UtcNow < portDeadline && IsPortOpen())
            {
                Thread.Sleep(100);
            }
            return Program.Result(true, "stopped", false, "StreamDock остановлен. Фоновые процессы завершены.");
        }

        private void StartSuspendedProcess()
        {
            Directory.CreateDirectory(Path.Combine(projectRoot, "logs"));
            string logPath = Path.Combine(projectRoot, "logs", "server-console.log");
            string commandLine = Quote(pythonPath) + " -m uvicorn app.main:app --host 127.0.0.1 --port 8765";
            controlToken = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");

            IntPtr createdJob = NativeMethods.CreateJobObject(IntPtr.Zero, null);
            if (createdJob == IntPtr.Zero)
            {
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
            ConfigureJob(createdJob);

            FileStream log = null;
            IntPtr inputHandle = IntPtr.Zero;
            ProcessInformation processInfo = new ProcessInformation();
            try
            {
                log = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                MarkInheritable(log.SafeFileHandle.DangerousGetHandle());
                SecurityAttributes securityAttributes = new SecurityAttributes();
                securityAttributes.nLength = Marshal.SizeOf(typeof(SecurityAttributes));
                securityAttributes.bInheritHandle = true;
                inputHandle = NativeMethods.CreateFile(
                    "NUL",
                    GenericRead,
                    FileShareRead | FileShareWrite,
                    ref securityAttributes,
                    OpenExisting,
                    0,
                    IntPtr.Zero
                );
                if (inputHandle == new IntPtr(-1))
                {
                    inputHandle = IntPtr.Zero;
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }
                ClearStandardHandleInheritance();

                StartupInfo startup = new StartupInfo();
                startup.cb = Marshal.SizeOf(typeof(StartupInfo));
                startup.dwFlags = StartfUseStdHandles;
                startup.hStdInput = inputHandle;
                startup.hStdOutput = log.SafeFileHandle.DangerousGetHandle();
                startup.hStdError = log.SafeFileHandle.DangerousGetHandle();

                string oldToken = Environment.GetEnvironmentVariable("STREAMDOCK_CONTROL_TOKEN");
                string oldInstance = Environment.GetEnvironmentVariable("STREAMDOCK_INSTANCE_ID");
                try
                {
                    Environment.SetEnvironmentVariable("STREAMDOCK_CONTROL_TOKEN", controlToken);
                    Environment.SetEnvironmentVariable("STREAMDOCK_INSTANCE_ID", Guid.NewGuid().ToString("N"));
                    StringBuilder mutableCommand = new StringBuilder(commandLine);
                    bool created = NativeMethods.CreateProcess(
                        pythonPath,
                        mutableCommand,
                        IntPtr.Zero,
                        IntPtr.Zero,
                        true,
                        CreateSuspended | CreateNoWindow,
                        IntPtr.Zero,
                        projectRoot,
                        ref startup,
                        out processInfo
                    );
                    if (!created)
                    {
                        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                    }
                }
                finally
                {
                    Environment.SetEnvironmentVariable("STREAMDOCK_CONTROL_TOKEN", oldToken);
                    Environment.SetEnvironmentVariable("STREAMDOCK_INSTANCE_ID", oldInstance);
                }

                if (!NativeMethods.AssignProcessToJobObject(createdJob, processInfo.hProcess))
                {
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }
                if (NativeMethods.ResumeThread(processInfo.hThread) == UInt32.MaxValue)
                {
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }

                jobHandle = createdJob;
                processHandle = processInfo.hProcess;
                createdJob = IntPtr.Zero;
                processInfo.hProcess = IntPtr.Zero;
            }
            finally
            {
                if (processInfo.hThread != IntPtr.Zero)
                {
                    NativeMethods.CloseHandle(processInfo.hThread);
                }
                if (processInfo.hProcess != IntPtr.Zero)
                {
                    NativeMethods.TerminateProcess(processInfo.hProcess, 1);
                    NativeMethods.CloseHandle(processInfo.hProcess);
                }
                if (createdJob != IntPtr.Zero)
                {
                    NativeMethods.TerminateJobObject(createdJob, 1);
                    NativeMethods.CloseHandle(createdJob);
                }
                if (inputHandle != IntPtr.Zero)
                {
                    NativeMethods.CloseHandle(inputHandle);
                }
                if (log != null)
                {
                    log.Dispose();
                }
            }
        }

        private static void ConfigureJob(IntPtr job)
        {
            JobObjectExtendedLimitInformation info = new JobObjectExtendedLimitInformation();
            info.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            int length = Marshal.SizeOf(typeof(JobObjectExtendedLimitInformation));
            IntPtr pointer = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(info, pointer, false);
                if (!NativeMethods.SetInformationJobObject(job, JobObjectExtendedLimitInformation, pointer, (uint)length))
                {
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }
            }
            finally
            {
                Marshal.FreeHGlobal(pointer);
            }
        }

        private static void MarkInheritable(IntPtr handle)
        {
            if (!NativeMethods.SetHandleInformation(handle, HandleFlagInherit, HandleFlagInherit))
            {
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
        }

        private static void ClearStandardHandleInheritance()
        {
            int[] standardHandles = { -10, -11, -12 };
            foreach (int value in standardHandles)
            {
                IntPtr handle = NativeMethods.GetStdHandle(value);
                if (handle != IntPtr.Zero && handle != new IntPtr(-1))
                {
                    NativeMethods.SetHandleInformation(handle, HandleFlagInherit, 0);
                }
            }
        }

        private bool TrackedProcessAlive()
        {
            if (processHandle == IntPtr.Zero)
            {
                return false;
            }
            uint exitCode;
            return NativeMethods.GetExitCodeProcess(processHandle, out exitCode) && exitCode == StillActive;
        }

        private bool IsStreamDockOnline()
        {
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(HealthUrl);
                request.Method = "GET";
                request.Proxy = null;
                request.Timeout = 700;
                request.ReadWriteTimeout = 700;
                request.CachePolicy = new System.Net.Cache.RequestCachePolicy(System.Net.Cache.RequestCacheLevel.NoCacheNoStore);
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                {
                    bool isStreamDock = response.StatusCode == HttpStatusCode.OK &&
                        String.Equals(response.Headers["X-StreamDock-App"], "1", StringComparison.Ordinal);
                    if (isStreamDock)
                    {
                        string discoveredToken = response.Headers[ControlTokenResponseHeader];
                        if (!String.IsNullOrWhiteSpace(discoveredToken))
                        {
                            controlToken = discoveredToken;
                        }
                    }
                    return isStreamDock;
                }
            }
            catch
            {
                return false;
            }
        }

        private static bool IsPortOpen()
        {
            try
            {
                using (TcpClient client = new TcpClient())
                {
                    IAsyncResult pending = client.BeginConnect(IPAddress.Loopback, 8765, null, null);
                    if (!pending.AsyncWaitHandle.WaitOne(350))
                    {
                        return false;
                    }
                    client.EndConnect(pending);
                    return true;
                }
            }
            catch
            {
                return false;
            }
        }

        private bool RequestGracefulShutdown()
        {
            if (String.IsNullOrWhiteSpace(controlToken))
            {
                return false;
            }
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(ShutdownUrl);
                request.Method = "POST";
                request.Proxy = null;
                request.Timeout = 2000;
                request.ReadWriteTimeout = 2000;
                request.ContentLength = 0;
                request.Headers["X-StreamDock-Token"] = controlToken;
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                {
                    return response.StatusCode == HttpStatusCode.Accepted;
                }
            }
            catch (WebException exception)
            {
                HttpWebResponse response = exception.Response as HttpWebResponse;
                if (response != null)
                {
                    using (response)
                    {
                        return response.StatusCode == HttpStatusCode.Accepted;
                    }
                }
                // При быстром завершении backend может закрыть соединение раньше чтения ответа.
                return true;
            }
        }

        private void ForceStopTrackedProcess()
        {
            if (jobHandle != IntPtr.Zero)
            {
                NativeMethods.TerminateJobObject(jobHandle, 1);
            }
            ReleaseHandles();
        }

        private void ReleaseHandles()
        {
            if (processHandle != IntPtr.Zero)
            {
                NativeMethods.CloseHandle(processHandle);
                processHandle = IntPtr.Zero;
            }
            if (jobHandle != IntPtr.Zero)
            {
                NativeMethods.CloseHandle(jobHandle);
                jobHandle = IntPtr.Zero;
            }
            controlToken = String.Empty;
        }

        private static string Quote(string value)
        {
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        public void Dispose()
        {
            ForceStopTrackedProcess();
        }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct StartupInfo
    {
        internal int cb;
        internal string lpReserved;
        internal string lpDesktop;
        internal string lpTitle;
        internal int dwX;
        internal int dwY;
        internal int dwXSize;
        internal int dwYSize;
        internal int dwXCountChars;
        internal int dwYCountChars;
        internal int dwFillAttribute;
        internal uint dwFlags;
        internal short wShowWindow;
        internal short cbReserved2;
        internal IntPtr lpReserved2;
        internal IntPtr hStdInput;
        internal IntPtr hStdOutput;
        internal IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct ProcessInformation
    {
        internal IntPtr hProcess;
        internal IntPtr hThread;
        internal uint dwProcessId;
        internal uint dwThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct SecurityAttributes
    {
        internal int nLength;
        internal IntPtr lpSecurityDescriptor;
        [MarshalAs(UnmanagedType.Bool)]
        internal bool bInheritHandle;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobObjectBasicLimitInformation
    {
        internal long PerProcessUserTimeLimit;
        internal long PerJobUserTimeLimit;
        internal uint LimitFlags;
        internal UIntPtr MinimumWorkingSetSize;
        internal UIntPtr MaximumWorkingSetSize;
        internal uint ActiveProcessLimit;
        internal IntPtr Affinity;
        internal uint PriorityClass;
        internal uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct IoCounters
    {
        internal ulong ReadOperationCount;
        internal ulong WriteOperationCount;
        internal ulong OtherOperationCount;
        internal ulong ReadTransferCount;
        internal ulong WriteTransferCount;
        internal ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobObjectExtendedLimitInformation
    {
        internal JobObjectBasicLimitInformation BasicLimitInformation;
        internal IoCounters IoInfo;
        internal UIntPtr ProcessMemoryLimit;
        internal UIntPtr JobMemoryLimit;
        internal UIntPtr PeakProcessMemoryUsed;
        internal UIntPtr PeakJobMemoryUsed;
    }

    internal static class NativeMethods
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool CreateProcess(
            string applicationName,
            StringBuilder commandLine,
            IntPtr processAttributes,
            IntPtr threadAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool inheritHandles,
            uint creationFlags,
            IntPtr environment,
            string currentDirectory,
            ref StartupInfo startupInfo,
            out ProcessInformation processInformation
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern IntPtr CreateJobObject(IntPtr jobAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool TerminateJobObject(IntPtr job, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool TerminateProcess(IntPtr process, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern uint ResumeThread(IntPtr thread);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern IntPtr GetStdHandle(int standardHandle);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern IntPtr CreateFile(
            string fileName,
            uint desiredAccess,
            uint shareMode,
            ref SecurityAttributes securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile
        );
    }
}
