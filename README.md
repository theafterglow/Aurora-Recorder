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

- Real-Time Recording (FLAC)
- Better support for albums/playlists, now every track is treated like they're singles
- Metadata Embedding: title, artist, album, and cover art
- Background Finalization for smooth capture
- File Organization: Artist/Album/Track
- Audio Header Cleanup
- Config File and CLI Argument Support
- Rich Terminal UI (`rich`)
- Cross-Platform: Windows, macOS, Linux

---

The Changes

## 💻 Installation Guides

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
   venv\Sripts\python -m pip install -r requirements.txt
   ```
`venv\Sripts\python aurora_recorder.py text_file.txt` will play & record the tracks, albums and playlists
`venv\Sripts\python aurora_recorder.py --playlist/album` will play & record the album/playlist
`venv\Sripts\python aurora_recorder.py track_link` will play & record the given track only

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