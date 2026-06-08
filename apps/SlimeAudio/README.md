# Slime Audio

Windows tray receiver and sender tooling for LAN audio broadcast.

This is the first pass for house TTS and shared audio broadcasts to Windows machines with speakers. Each receiver sits in the Windows tray and listens for UDP audio packets. The sender sends the same audio stream to one or more receivers with a future start timestamp so devices can begin together.

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

GitHub Actions publishes `SlimeAudioSetup.exe`. The installer bundles `ffplay.exe` beside the tray app for shared-stream playback.

## Run Receiver

On each Windows speaker device:

```powershell
SlimeAudio.Tray.exe --port 47777
```

The app stays in the system tray. The installer also adds a Start Menu shortcut and can optionally start the tray app when Windows signs in.

The tray menu includes:

- `Slime Audio <version>`
- `Status`
- `Receive stream here`
- `Volume`
- `Output device`
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

From the repo root, `scripts/slime_audio_stream.py` decodes a local audio file and sends it through shared-stream backends. It can target any combo of connected receivers by discovered machine name, explicit `host:port`, or `all`.

```bash
python3 scripts/slime_audio_stream.py ./song.flac --target SPATULA --target SPONGEBOT --mode snapcast
python3 scripts/slime_audio_stream.py ./mix.mp3 --target all --mode snapcast
```

The streamer uses FFmpeg for decoding and shared-stream transport. Packet audio mode has been removed from the Python tools; use Snapcast for room playback and multicast only for debugging the shared-stream listener path.

Receivers muted from the tray are excluded from `--target all` streams. Use `--include-muted` only for diagnostics or intentional override.

```bash
python3 scripts/slime_audio_stream.py ./mix.flac --target all --mode multicast
python3 scripts/slime_audio_stream.py --target all --start-listeners
python3 scripts/slime_audio_stream.py --target all --stop-listeners
```

Receiver discovery includes Snapcast client telemetry for skip diagnosis: server host, snapclient PID, start time, uptime, exit count, reconnect attempts, last stderr time, last status, last exit status, last stderr line, start command, and the local telemetry file path. The Windows tray writes JSONL events to `%LOCALAPPDATA%\SlimeAudio\telemetry.jsonl` whenever snapclient starts, exits, emits stderr, changes volume, changes output device, or is stopped. After a skip, run discovery and compare `shared_stream_exits`, `last_exit_status`, `last_stderr`, `start_command`, `shared_stream_last_stderr_ms`, and `telemetry_path` against the sender/session logs. Last-exit fields are preserved separately from current status so later controls do not bury the crash reason.

If snapclient exits without an explicit stop/mute/reset request, the tray treats it as a shared-stream disconnect and attempts a bounded reconnect to the last sender. Discovery keeps reporting the exit count and last status so planned sender handoffs can be separated from real client crashes.

The tray `Output device` menu lists devices reported by `snapclient.exe --list`. Picking one saves it to `%LOCALAPPDATA%\SlimeAudio\settings.json` and restarts the active Snapcast listener with `--soundcard`. You can also set it remotely:

```bash
python3 scripts/slime_audio_stream.py --target SPATULA --output-device "Speakers"
python3 scripts/slime_audio_stream.py --target SPATULA --default-output-device
```

## Linux Headless Receiver

`SlimeAudio.Headless` is a cross-platform receiver for Linux debugging and CI. It speaks the same discovery, reset, and shared-stream control protocol as the Windows tray app, but runs as a console process.

```bash
dotnet run --project apps/SlimeAudio/src/SlimeAudio.Headless/SlimeAudio.Headless.csproj -c Release -- --port 47777
dotnet run --project apps/SlimeAudio/src/SlimeAudio.Headless/SlimeAudio.Headless.csproj -c Release -- --port 47777 --no-audio
```

Use `--no-audio` for protocol/debug smoke tests without opening an audio device.

## Timed Spotify Drops

The old packet-audio Spotify drop runner is disabled. For agent DJ/sample-drop mode, plan mic lean-ins as timestamped mix-session events, then render and stream the session through Snapcast.

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
python3 scripts/slime_audio_lean_ins.py --session runtime/mix-session.json --create --start 01:12.000 --text "ride this bit" --volume 1.7 --duck-volume 0.45
python3 scripts/slime_audio_commentary_planner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --count 3
python3 scripts/slime_audio_session_runner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --target all
```

## Design Notes

- No remote volume control. Room volume is human-side until we have microphones or real SPL sensing.
- Audio should use Snapcast or multicast shared streams. UDP remains for receiver discovery/control messages, not music transport.
- Time sync is coarse wall-clock sync right now. Next step is sender-side ping/offset estimation per receiver.
