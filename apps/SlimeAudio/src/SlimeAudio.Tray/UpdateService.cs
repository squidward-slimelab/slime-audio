using System.Diagnostics;
using System.Net.Http.Headers;
using System.Text.Json;

namespace SlimeAudio.Tray;

internal static class UpdateService
{
    private const string LatestReleaseUrl = "https://api.github.com/repos/squidward-slimelab/slime-audio/releases/latest";

    public static async Task<string> OpenLatestInstallerAsync()
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
            if (!string.Equals(name, "SlimeAudioSetup.exe", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var url = asset.GetProperty("browser_download_url").GetString();
            if (string.IsNullOrWhiteSpace(url))
            {
                break;
            }

            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            return $"Opened update installer: {url}";
        }

        throw new InvalidOperationException("Latest release does not include SlimeAudioSetup.exe");
    }
}
