using System.Diagnostics;
using System.Net.Http.Headers;
using System.Reflection;
using System.Text.Json;

namespace SlimeAudio.Tray;

internal static class UpdateService
{
    private const string LatestReleaseUrl = "https://api.github.com/repos/squidward-slimelab/slime-audio/releases/latest";
    private const string InstallerName = "SlimeAudioSetup.exe";

    public static async Task<string> DownloadAndRunLatestInstallerAsync()
    {
        using var http = new HttpClient();
        http.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("SlimeAudio", VersionInfo.DisplayVersion));
        using var response = await http.GetAsync(LatestReleaseUrl).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();

        await using var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false);
        using var json = await JsonDocument.ParseAsync(stream).ConfigureAwait(false);
        var latestTag = json.RootElement.GetProperty("tag_name").GetString();
        if (VersionInfo.IsCurrentRelease(latestTag))
        {
            return $"Slime Audio {VersionInfo.DisplayVersion} is already current";
        }

        foreach (var asset in json.RootElement.GetProperty("assets").EnumerateArray())
        {
            var name = asset.GetProperty("name").GetString();
            if (!string.Equals(name, InstallerName, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var url = asset.GetProperty("browser_download_url").GetString();
            if (string.IsNullOrWhiteSpace(url))
            {
                break;
            }

            var installerPath = Path.Combine(Path.GetTempPath(), $"SlimeAudioSetup-{DateTimeOffset.UtcNow:yyyyMMddHHmmss}.exe");
            var logPath = Path.ChangeExtension(installerPath, ".log");
            await DownloadFileAsync(http, url, installerPath).ConfigureAwait(false);
            Process.Start(new ProcessStartInfo
            {
                FileName = installerPath,
                Arguments = $"/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL /LOG=\"{logPath}\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
            });
            return $"Started silent update installer: {installerPath}";
        }

        throw new InvalidOperationException($"Latest release does not include {InstallerName}");
    }

    private static async Task DownloadFileAsync(HttpClient http, string url, string outputPath)
    {
        using var response = await http.GetAsync(url).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        await using var input = await response.Content.ReadAsStreamAsync().ConfigureAwait(false);
        await using var output = File.Create(outputPath);
        await input.CopyToAsync(output).ConfigureAwait(false);
    }
}

internal static class VersionInfo
{
    public static string DisplayVersion { get; } = GetDisplayVersion();

    public static bool IsCurrentRelease(string? releaseTag)
    {
        if (string.IsNullOrWhiteSpace(releaseTag))
        {
            return false;
        }

        return string.Equals(Normalize(DisplayVersion), Normalize(releaseTag), StringComparison.OrdinalIgnoreCase);
    }

    private static string GetDisplayVersion()
    {
        var version = typeof(VersionInfo).Assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?
            .InformationalVersion;
        return Normalize(string.IsNullOrWhiteSpace(version) ? Application.ProductVersion : version);
    }

    private static string Normalize(string value)
    {
        var normalized = value.Trim();
        if (normalized.StartsWith("v", StringComparison.OrdinalIgnoreCase))
        {
            normalized = normalized[1..];
        }

        var metadata = normalized.IndexOf('+');
        return metadata >= 0 ? normalized[..metadata] : normalized;
    }
}
