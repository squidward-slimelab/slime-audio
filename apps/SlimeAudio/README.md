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

- `Status`
- `Check for updates`
- `Quit`

## Discover Receivers

```powershell
SlimeAudio.Send.exe discover
```

Remote update prompt:

```powershell
SlimeAudio.Send.exe update --target SPATULA:47777
```

## Send WAV Audio

First pass supports PCM 16-bit WAV files.

```powershell
SlimeAudio.Send.exe --file .\tts.wav --target SPATULA:47777 --target SPONGEBOT:47777 --delay-ms 2000
```

Both receivers buffer the stream and start at the same UTC timestamp. Real sync quality depends on the laptops having sane clocks, so keep Windows time sync enabled.

## Design Notes

- No remote volume control. Room volume is human-side until we have microphones or real SPL sensing.
- UDP is fine for first-pass LAN TTS and bumpers. For long VLC relays, add packet loss recovery or TCP/WebRTC later.
- Time sync is coarse wall-clock sync right now. Next step is sender-side ping/offset estimation per receiver.
