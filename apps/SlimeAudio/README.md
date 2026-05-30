# Slime Audio

Windows tray receiver and sender tooling for LAN audio broadcast.

This is the first pass for house TTS and VLC-style broadcasts to Windows machines with speakers. Each receiver sits in the Windows tray and listens for UDP audio packets. The sender sends the same audio stream to one or more receivers with a future start timestamp so devices can begin together.

## Devices

- `SPATULA`: work laptop. Bedroom or office.
- `SPONGEBOT`: living room laptop on surround sound.

## Build

Requires .NET 8 SDK.

```powershell
dotnet build apps/SlimeAudio/SlimeAudio.sln
dotnet test apps/SlimeAudio/SlimeAudio.sln
```

Publish the tray app on Windows:

```powershell
dotnet publish apps/SlimeAudio/src/SlimeAudio.Tray/SlimeAudio.Tray.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true
```

Build the Windows installer:

```powershell
choco install innosetup -y
iscc apps/SlimeAudio/installer/SlimeAudio.iss
```

GitHub Actions publishes `SlimeAudioSetup.exe`.

## Run Receiver

On each Windows speaker device:

```powershell
SlimeAudio.Tray.exe --port 47777
```

The app stays in the system tray. The installer also adds a Start Menu shortcut and can optionally start the tray app when Windows signs in.

The tray menu includes:

- `Slime Audio <version>`
- `Status`
- `Mute stream here` / `Unmute stream here`
- `Start shared stream listener`
- `Stop shared stream listener`
- `Check for updates`
- `Quit`

`Check for updates` compares the running tray version against the latest GitHub release. If an update is available, it downloads the installer and runs it silently. The installer stops any running `SlimeAudio.Tray.exe` before replacing files, then launches the updated tray app.

`Mute stream here` is a server-side subscription toggle. Muted clients advertise `StreamMuted` in discovery and the Python streamer skips them when resolving `--target all`; the local tray also resets active audio immediately.

## Discover Receivers

```powershell
SlimeAudio.Send.exe discover
```

Remote update prompt:

```powershell
SlimeAudio.Send.exe update --target SPATULA:47777
```

Remote shared stream controls:

```powershell
SlimeAudio.Send.exe shared-start --target SPATULA:47777 --target SPONGEBOT:47777
SlimeAudio.Send.exe shared-stop --target SPATULA:47777 --target SPONGEBOT:47777
```

## Send WAV Audio

First pass supports PCM 16-bit WAV files.

```powershell
SlimeAudio.Send.exe --file .\tts.wav --target SPATULA:47777 --target SPONGEBOT:47777 --delay-ms 2000
```

Both receivers buffer the stream and start at the same UTC timestamp. Real sync quality depends on the laptops having sane clocks, so keep Windows time sync enabled.

## Stream Local Files

From the repo root, `scripts/slime_audio_stream.py` decodes a local audio file and sends it through the same UDP protocol as voice. It can target any combo of connected receivers by discovered machine name, explicit `host:port`, or `all`.

```bash
python3 scripts/slime_audio_stream.py ./song.flac --target SPATULA --target SPONGEBOT
python3 scripts/slime_audio_stream.py ./mix.mp3 --target all --delay-ms 3000
```

The streamer prefers VLC/cvlc when installed and falls back to GStreamer. Packet mode is fine for TTS and short samples; for multi-room music, use multicast mode so every receiver listens to one live RTP source. Multicast mode starts the selected receivers' shared stream listeners before playback.

Receivers muted from the tray are excluded from `--target all` streams. Packet streams refresh discovered subscribers while playing, so muting a tray stops future packets within a few seconds instead of only taking effect on the next track. Use `--include-muted` only for diagnostics or intentional override.

```bash
python3 scripts/slime_audio_stream.py ./mix.flac --target all --mode multicast
python3 scripts/slime_audio_stream.py --target all --start-listeners
python3 scripts/slime_audio_stream.py --target all --stop-listeners
```

## Timed Spotify Drops

For agent DJ/sample-drop mode, use the Python drop runner from the repo root. It pre-renders phrases, polls `spogo status`, checks the current Spotify track and progress, and sends SlimeAudio packets only while Spotify is playing. The runner defaults to 5 second status polling and backs off up to 30 seconds on failures; local probing showed 5 seconds was clean and 1.5 seconds was too aggressive.

If Spotify returns stale `progress_ms: 0`, timed drops do not fire by default until the runner has a reliable song clock from either non-zero Spotify progress or an observed track change.

```json
{
  "target": "SPATULA:47777",
  "volume": 1.7,
  "poll_ms": 5000,
  "require_known_progress": true,
  "drops": [
    {
      "track_uri": "spotify:track:3hmCHZFkgE4tkJKSqpOUhz",
      "at": "1:12",
      "text": "ride this bit. drums are doing expensive furniture things."
    }
  ]
}
```

```bash
python3 scripts/slime_audio_drops.py --plan drops.json --max-minutes 20
```

## Design Notes

- No remote volume control. Room volume is human-side until we have microphones or real SPL sensing.
- UDP is fine for first-pass LAN TTS and bumpers. For long VLC relays, add packet loss recovery or TCP/WebRTC later.
- Time sync is coarse wall-clock sync right now. Next step is sender-side ping/offset estimation per receiver.
