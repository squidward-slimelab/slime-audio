using System.Diagnostics;
using System.Net.Http.Headers;
using System.Text.Json;

namespace SlimeAudio.Tray;

internal static class UpdateService
{
    private const string LatestReleaseUrl = "https://api.github.com/repos/squidward-slimelab/slime-audio/releases/latest";
    private const string InstallerName = "SlimeAudioSetup.exe";

    public static async Task<string> DownloadAndRunLatestInstallerAsync()
    {
        using var http = new HttpClient();
        http.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("SlimeAudio", Application.ProductVersion));
        using var response = await http.GetAsync(LatestReleaseUrl).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();

        await using var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false);
        using var json = await JsonDocument.ParseAsync(stream).ConfigureAwait(false);
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
            await DownloadFileAsync(http, url, installerPath).ConfigureAwait(false);
            Process.Start(new ProcessStartInfo(installerPath) { UseShellExecute = true });
            return $"Started update installer: {installerPath}";
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
