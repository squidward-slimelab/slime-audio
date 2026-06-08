# SlimeAudio Windows App

The Windows app lives in `apps/SlimeAudio/`. It provides LAN audio receiver and sender pieces for Slime Lab machines such as `SPATULA` and `SPONGEBOT`.

## Projects

- `SlimeAudio.Tray` is the Windows tray receiver. It listens on UDP `47777`, handles playback/control messages, and exposes update/check behavior.
- `SlimeAudio.Send` is the sender CLI. It sends PCM WAV/audio to one or more devices with a shared future start timestamp and supports discovery/update commands.
- `SlimeAudio.Protocol` contains shared protocol types such as audio packets and control messages.
- `SlimeAudio.Headless` is a Linux/CI-friendly receiver target for debugging and tests.
- `SlimeAudio.Protocol.Tests` covers protocol behavior.

## Installer And Assets

- Installer script: `apps/SlimeAudio/installer/SlimeAudio.iss`
- Icons/images: `apps/SlimeAudio/assets/`
- Solution: `apps/SlimeAudio/SlimeAudio.sln`

The installer should create a usable Windows install with Start Menu shortcut and optional startup launch.

## Common Commands

Build/test from the app directory:

```bash
dotnet test apps/SlimeAudio/SlimeAudio.sln
```

Discover receivers:

```bash
SlimeAudio.Send.exe discover
```

Run a Linux debugging receiver:

```bash
dotnet run --project apps/SlimeAudio/src/SlimeAudio.Headless/SlimeAudio.Headless.csproj -c Release -- --port 47777 --no-audio
```

## Releases

GitHub Actions builds win-x64 artifacts from `.github/workflows/slime-audio.yml`.

## Shared Stream Diagnostics

Receiver discovery includes Snapcast client telemetry for skip diagnosis: server host, snapclient PID, start time, uptime, exit count, reconnect attempts, last stderr time, selected output device, last status, last exit status, last stderr line, start command, and the local telemetry file path. The Windows tray writes JSONL events to `%LOCALAPPDATA%\SlimeAudio\telemetry.jsonl` whenever snapclient starts, exits, emits stderr, changes volume, changes output device, or is stopped. Last-exit fields are preserved separately from current status so later controls do not bury the crash reason.

If snapclient exits without an explicit stop/mute/reset request, the tray app treats it as a shared-stream disconnect and attempts a bounded reconnect to the last sender. Discovery still reports the exit count and last status so planned sender handoffs can be distinguished from real client crashes.
