# Aurora Recorder – Spotify Track Recorder

**Aurora Recorder** is a powerful, CLI-based tool to record your currently playing Spotify tracks in real-time, automatically split them, embed metadata (title, artist, album, cover art), and organize them in your personal music library.

> ✅ Intended strictly for **personal archival use only**.

---

## ⚠️ Legal Disclaimer

> **This tool is for personal, non-commercial use.**
>  
> Recording copyrighted content from Spotify may violate their [Terms of Service](https://www.spotify.com/legal/end-user-agreement/) or local copyright laws.  
> You are solely responsible for your usage. The developers of this tool assume **no liability**.

---

## ✨ Features

- 🎵 Real-Time Recording (FLAC)
- 🎯 Better support for albums/playlists, now every track is treated like they're singles
- 🎼 Metadata Embedding: title, artist, album, and cover art
- ⚙️ Background Finalization for smooth capture
- 📁 File Organization: Artist/Album/Track
- 🧹 Audio Header Cleanup
- 🔧 Config File and CLI Argument Support
- 💬 Rich Terminal UI (`rich`)
- 🖥️ Cross-Platform: Windows, macOS, Linux

---

The Changes

## 💻 Installation Guides

### 🪟 Windows

1. Install **Python 3.7+** from [python.org](https://www.python.org/downloads/windows/)
2. Install **FFmpeg**:
   - Download from [gyan.dev FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/)
   - Extract it and add the `/bin` folder to your `PATH`
3. Install **VB-Audio Cable** from [vb-audio.com](https://vb-audio.com/Cable/)
4. Clone the repo and install requirements:
   ```bash
   git clone https://github.com/theafterglow/Aurora_Recorder.git
   cd Aurora_Recorder
   python3 -m venv venv
   venv\Scripts\python aurora_recorder.py (--album/playlist)
   ```
---

### 🍏 macOS

1. Install **Python 3.7+** (via [Homebrew](https://brew.sh/) or [python.org](https://www.python.org/downloads/macos/))
2. Install **FFmpeg**:
   ```bash
   brew install ffmpeg
   ```
3. Install **BlackHole (2ch)** via [BlackHole GitHub](https://github.com/ExistentialAudio/BlackHole)
4. Set Spotify output to BlackHole in System Preferences > Sound > Output
5. Clone and install dependencies:
   ```bash
   git clone https://github.com/theafterglow/Aurora_Recorder.git
   cd Aurora_Recorder
   pip install -r requirements.txt
   ```
6. Run the script:
   ```bash
   python aurora_recorder.py
   ```

---

### 🐧 Linux (PulseAudio)

1. Install **Python 3.7+**, `ffmpeg`, and `pavucontrol`:
   ```bash
   sudo apt update && sudo apt install python3 ffmpeg pavucontrol python3-pip
   pip install spotipy requests mutagen rich
   ```
2. Load PulseAudio null sink:
   ```bash
   pactl load-module module-null-sink sink_name=spytorec_sink
   ```
3. Set Spotify output to **Monitor of spytorec_sink** using `pavucontrol`
4. Clone repo and run:
   ```bash
   git clone https://github.com/YOUR_USERNAME/aurora_recorder.git
   cd aurora_recorder
   python3 aurora_recorder.py
   ```

---

## 🛠️ Usage & CLI

```bash
python aurora_recorder.py [COMMAND] [OPTIONS]
```

### ▶️ `record` (or default)
Records and saves current Spotify track with metadata.

### 🎙️ `list-devices`
Lists FFmpeg-detected audio input devices.

### 🔐 `test-auth`
Tests Spotify API credentials and shows current playback info.

---

## 🧪 Example Commands

---

## 📁 Output Features

- Tracks saved in chosen format and directory
- FLAC includes embedded album art
- Duplicate checking by track ID and filename
- Metadata includes artist, album, and title
- Rewrites headers using FFmpeg post-recording

---

## 💡 Troubleshooting

| Issue                        | Solution                                                            |
|-----------------------------|---------------------------------------------------------------------|
| No sound recorded           | Verify Spotify is routed to virtual device                         |
| "Device not found"          | Run `list-devices` and use the full audio device name              |
| Beginning cut off           | Lower `--interval` (e.g. 0.3 or 0.2)                               |
| Corrupted duration          | Ensure `ffmpeg` finalizer runs successfully                       |
| Ads in recording            | Use Spotify Premium  (eventhough Ads won't recorded in Free Subscriptions)                                              |

---

## 🤝 Contributing

Pull requests and stars ⭐ are welcome!  
Fork the repo, give it a star, and help build more useful tools for personal music archiving!

---

## 📜 License

This project is licensed under **MIT License**  
See [LICENSE](LICENSE) for details.

---

### 👤 Author

**@Darkphoenix**   
GitHub: [github.com/Danidukiyu](https://github.com/Danidukiyu)