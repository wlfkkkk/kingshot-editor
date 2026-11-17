# Kingshot Editor

A simple, fast video editor for Windows, made for YouTube Shorts and long videos. Cut parts out, add music, blur areas, and export with your graphics card.

## Download

**[Download KingshotEditorSetup.exe (version 1.0)](https://github.com/wlfkkkk/kingshot-editor/raw/main/KingshotEditorSetup.exe)**

## Install

1. Download and double-click `KingshotEditorSetup.exe`.
2. **Windows SmartScreen** may say "Windows protected your PC" because the installer isn't code-signed. Click **More info**, then **Run anyway**.
3. Click **Install**. The first install downloads about 250 MB (Python, Qt, FFmpeg) and takes around 3 minutes.
4. Click **Open Kingshot Editor**. There is a shortcut on your Desktop and in the Start menu.

It installs into `%LOCALAPPDATA%\KingshotEditor` and changes nothing else. Remove it from Settings > Apps.

## Features

- **Cut** parts out of a video, from seconds to many hours long.
- **Music**: add your own songs, or paste a YouTube link (Ctrl+V) to add its song. A copyright hint is shown before downloading.
- **Blur** boxes on the video, with timing and strength (switch on "Blur tools").
- **Formats**: YouTube Short 9:16, vertical video 9:16, wide video 16:9, or "same size as the video".
- **Speed** controls for the video and the music.
- **Input**: H.264, HEVC (H.265, including iPhone HDR) and AV1.
- **Fast export on your GPU**: AV1, HEVC or H.264 on NVIDIA, with AMD and Intel fallbacks, and a CPU fallback if no GPU encoder works. "Recommended" picks the best one for you. Quality and file size stay close to the original.
- Exports go to `Videos\YT Ready`.

## Requirements

- Windows 10 or 11, 64-bit.
- An internet connection for the first install.
- An NVIDIA graphics card is recommended for the fastest exports (AV1 needs an RTX 40 series or newer). Without one, exports run on the processor and are slower.

## Updates

The editor checks this repo for a newer version a few seconds after it starts. You can also check by hand: click **?**, then **Check for updates**. Choose **Update now** and it downloads the new version, keeps a backup of the old one, and restarts. You don't need to download the installer again, unless an update says it needs the full installer.

## Troubleshooting

Logs are in `%LOCALAPPDATA%\KingshotEditor\logs`. Click **?** then **Open log folder** to open it. `editor-log.txt` records edits and exports, and `yt-download-log.txt` records song downloads.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
