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
