import re
import queue
import time
import typing as t
import threading
import traceback
import subprocess
import configparser
from pathlib import Path

from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

SCRIPT_VERSION = "2.1"
SPOTIPY_REDIRECT_URI = "http://127.0.0.1:8888/callback"
SPOTIPY_SCOPE = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "user-modify-playback-state "
    "playlist-read-private playlist-read-collaborative"
)

console = Console()
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.ini"
FAILED_TXT = BASE_DIR / "failed_tracks.txt"

current_ffmpeg_process: t.Optional[subprocess.Popen] = None
current_recording_info: dict = {}
standby_ffmpeg_process: t.Optional[subprocess.Popen] = None
standby_file: t.Optional[Path] = None

finalization_task_queue: "queue.Queue[dict]" = queue.Queue()
stop_worker_event = threading.Event()

class Settings(t.TypedDict):
    output_directory: Path
    default_format: str
    polling_interval_seconds: float
    audio_device: str
    ffmpeg_path: str
    min_duration_seconds: int
    recording_buffer_seconds: float
    skip_existing_file: bool
    organize_by_artist_album: bool
    rewrite_headers_enabled: bool
    preroll_ms: int
    gap_seconds: float
    standby_seconds: float

def sanitize_for_filesystem(text: str, max_len: int = 70) -> str:
    text = "".join(c if c.isalnum() or c in " ._-" else "_" for c in str(text)).strip()
    text = re.sub(r"[_ ]{2,}", "_", text)
    return text[:max_len].strip("_")

def fmt_time(sec: float) -> str:
    if sec is None or sec < 0:
        return "--:--"
    m = int(sec // 60)
    s = int(sec % 60)
    return f"{m:02d}:{s:02d}"

def ensure_default_config(path: Path) -> None:
    if path.exists():
        return
    cfg = configparser.ConfigParser(allow_no_value=True)
    cfg["SpotifyAPI"] = {
        "SPOTIPY_CLIENT_ID": "",
        "SPOTIPY_CLIENT_SECRET": "",
    }
    cfg["GeneralSettings"] = {
        "output_directory": "Recordings",
        "default_format": "flac",
        "polling_interval_seconds": "0.35",
        "audio_device": "audio=CABLE Output (VB-Audio Virtual Cable)",
        "ffmpeg_path": "ffmpeg",
        "min_duration_seconds": "30",
        "recording_buffer_seconds": "-0.20",
        "skip_existing_file": "true",
        "organize_by_artist_album": "true",
        "rewrite_headers_enabled": "true",
        "preroll_ms": "180",
        "gap_seconds": "5",
        "standby_seconds": "900",
    }
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)

def safe_spotify_call(func, *args, retries=5, delay=12, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)

        except Exception as e:
            console.print(f"[red]Spotify API error:[/red] {e}")

def read_settings() -> Settings:
    ensure_default_config(CONFIG_FILE)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return {
        "output_directory": BASE_DIR / cfg.get("GeneralSettings", "output_directory", fallback="Recordings"),
        "default_format": cfg.get("GeneralSettings", "default_format", fallback="flac").lower(),
        "polling_interval_seconds": cfg.getfloat("GeneralSettings", "polling_interval_seconds", fallback=0.35),
        "audio_device": cfg.get("GeneralSettings", "audio_device", fallback="audio=CABLE Output (VB-Audio Virtual Cable)"),
        "ffmpeg_path": cfg.get("GeneralSettings", "ffmpeg_path", fallback="ffmpeg"),
        "min_duration_seconds": cfg.getint("GeneralSettings", "min_duration_seconds", fallback=25),
        "recording_buffer_seconds": cfg.getfloat("GeneralSettings", "recording_buffer_seconds", fallback=-0.20),
        "skip_existing_file": cfg.getboolean("GeneralSettings", "skip_existing_file", fallback=True),
        "organize_by_artist_album": cfg.getboolean("GeneralSettings", "organize_by_artist_album", fallback=True),
        "rewrite_headers_enabled": cfg.getboolean("GeneralSettings", "rewrite_headers_enabled", fallback=True),
        "preroll_ms": cfg.getint("GeneralSettings", "preroll_ms", fallback=180),
        "gap_seconds": cfg.getfloat("GeneralSettings", "gap_seconds", fallback=5.0),
        "standby_seconds": cfg.getfloat("GeneralSettings", "standby_seconds", fallback=900.0),
    }

def get_spotify_client() -> Spotify:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    cid = cfg.get("SpotifyAPI", "SPOTIPY_CLIENT_ID", fallback="").strip()
    csec = cfg.get("SpotifyAPI", "SPOTIPY_CLIENT_SECRET", fallback="").strip()

    if not cid or not csec:
        console.print("[bold red]Spotify credentials missing[/bold red]")
        cid = Prompt.ask("Enter your Spotify Client ID").strip()
        csec = Prompt.ask("Enter your Spotify Client Secret", password=True).strip()
        cfg.set("SpotifyAPI", "SPOTIPY_CLIENT_ID", cid)
        cfg.set("SpotifyAPI", "SPOTIPY_CLIENT_SECRET", csec)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            cfg.write(f)
        console.print("[green]Credentials saved[/green]")

    return Spotify(
        auth_manager=SpotifyOAuth(
            client_id=cid,
            client_secret=csec,
            redirect_uri=SPOTIPY_REDIRECT_URI,
            scope=SPOTIPY_SCOPE,
            open_browser=True,
            requests_timeout=15,
            cache_path=str(BASE_DIR / ".cache-aurora")
        )
    )

def current_track(sp: Spotify) -> t.Optional[dict]:
    try:
        pb = safe_spotify_call(sp.current_playback)
        if not pb:
            return None

        item = pb.get("item")
        if not item or item.get("type") != "track":
            return None

        artists = item.get("artists", []) or []
        names = [a.get("name", "Unknown Artist") for a in artists if isinstance(a, dict)] or ["Unknown Artist"]

        album = item.get("album", {}) or {}
        images = album.get("images", []) or []
        cover_url = images[0].get("url") if images else None

        album_artists = album.get("artists", []) or []
        aa_names = [a.get("name", "Unknown Artist") for a in album_artists if isinstance(a, dict)]
        album_artist_str = ", ".join(aa_names) if aa_names else ", ".join(names)

        return {
            "id": item.get("id"),
            "name": item.get("name", "Unknown Title"),
            "artists": names,
            "artist_str": ", ".join(names),
            "album": album.get("name", "Unknown Album"),
            "album_release_date": album.get("release_date"),
            "track_number": item.get("track_number"),
            "duration_ms": item.get("duration_ms", 0),
            "is_playing": pb.get("is_playing", False),
            "progress_ms": pb.get("progress_ms", 0),
            "album_artist_str": album_artist_str,
            "composer_str": album_artist_str,
            "performer_str": album_artist_str,
            "cover_url": cover_url,
        }

    except Exception as e:
        console.print("[red]current_track() error: " + str(e) + "" + escape(traceback.format_exc()))
        return None