#!/usr/bin/env python3
# aurora_recorder.py
# Aurora Recorder (single-file, English)
# - Reads settings from aurora_core.read_settings()
# - Uses Spotify API client from aurora_core.get_spotify_client()
# - Skips tracks that are already recorded (FLAC file contains SPOTIFY_TRACK_ID)
# - Logs failed tracks as full web links into failed_tracks.txt
# - Minimal, robust flow for sequential playlist recording
#
# Requirements:
# pip install spotipy mutagen rich requests urllib3

from __future__ import annotations
import time
import threading
import typing as t
from pathlib import Path
from datetime import datetime, timezone

from mutagen.flac import FLAC, MutagenError
from rich.panel import Panel
from rich.text import Text
from rich.markup import escape

from spotipy import Spotify

# imports from the existing aurora project modules (assumed present)
from aurora_core import (
    console,
    Settings,
    SCRIPT_VERSION,
    read_settings,
    get_spotify_client,
    sanitize_for_filesystem,
    safe_spotify_call,
    current_track,
)
from aurora_io import (
    ensure_dir,
    start_ffmpeg,
    kill_ffmpeg,
    finalization_worker,
)
from aurora_core import (
    current_ffmpeg_process,
    current_recording_info,
    finalization_task_queue,
    stop_worker_event,
)

# ------------------------ FAILED TRACK LOG ------------------------
# File where failed track LINKS will be appended (web links)
FAILED_TRACKS_FILE = Path("failed_tracks.txt")


def log_failed_track(spotify_uri_or_id: str) -> None:
    try:
        if not spotify_uri_or_id:
            return

        spotify_uri_or_id = spotify_uri_or_id.strip()

        if spotify_uri_or_id.startswith("spotify:track:"):
            track_id = spotify_uri_or_id.split(":")[-1]
            link = f"https://open.spotify.com/track/{track_id}"
        # simple heuristic: 22-character Spotify track ID
        elif len(spotify_uri_or_id) == 22 and "/" not in spotify_uri_or_id:
            link = f"https://open.spotify.com/track/{spotify_uri_or_id}"
        else:
            # if it already looks like a link, keep as-is
            link = spotify_uri_or_id

        with open(FAILED_TRACKS_FILE, "a", encoding="utf-8") as fh:
            fh.write(link + "\n")
    except Exception:
        # Never raise from logging failures
        return


# ------------------------ EXISTING TRACK CHECK ------------------------
def is_already_recorded_by_spotify_id(final_path: str | Path, spotify_track_id: str) -> bool:
    try:
        p = Path(final_path)
        if not p.exists():
            return False

        f = FLAC(p)
        tags = f.tags or {}

        # direct uppercase tag (foobar2000 convention)
        if "SPOTIFY_TRACK_ID" in tags:
            val = tags["SPOTIFY_TRACK_ID"]
            if isinstance(val, (list, tuple)):
                val = val[0] if val else ""
            return str(val).strip() == str(spotify_track_id).strip()

        # case-insensitive / alternate names
        lower_map = {k.lower(): k for k in tags.keys()}
        for candidate in ("spotify_track_id", "spotify:id", "spotifyid", "trackid", "track_id", "spotify_track"):
            if candidate in lower_map:
                real_key = lower_map[candidate]
                val = tags.get(real_key)
                if isinstance(val, (list, tuple)):
                    val = val[0] if val else ""
                return str(val).strip() == str(spotify_track_id).strip()

        return False
    except MutagenError:
        return False
    except Exception:
        return False


# ------------------------ OUTPUT FILENAME ------------------------
def out_filename(meta: dict, fmt: str) -> str:
    try:
        tn = int(meta.get("track_number") or 0)
    except Exception:
        tn = 0
    prefix = f"{tn:02d}" if tn > 0 else "00"
    title = sanitize_for_filesystem(meta.get("name", "Unknown Title"))
    return f"{prefix} {title}.{fmt}"


# ------------------------ RECORDING WORKER ------------------------
def record_one_track_blocking(sp: Spotify, st: Settings) -> None:
    global current_ffmpeg_process, current_recording_info

    log_file = st["output_directory"] / "aurora_metadata.jsonl"
    ensure_dir(st["output_directory"])

    worker = threading.Thread(
        target=finalization_worker,
        args=(st["ffmpeg_path"], log_file),
        daemon=True,
    )
    worker.start()

    try:
        while True:
            meta = current_track(sp)

            # if there's no ffmpeg process, wait until one is set externally
            if not current_ffmpeg_process:
                # if playback started elsewhere, just continue waiting
                if meta and meta.get("is_playing"):
                    pass
            else:
                stop_reason: t.Optional[str] = None

                if not meta or not meta.get("is_playing"):
                    stop_reason = "Playback stopped or track unavailable"
                elif meta.get("id") != current_recording_info.get("track_id"):
                    stop_reason = "Track changed"
                else:
                    try:
                        prog = float(meta.get("progress_ms", 0) or 0)
                        dur = float(meta.get("duration_ms", 0) or 0)
                        # consider finished if within last 200 ms
                        if dur > 0 and prog >= max(0.0, dur - 200):
                            stop_reason = "Track finished"
                    except Exception:
                        pass

                if stop_reason:
                    # stop ffmpeg and enqueue finalization
                    kill_ffmpeg(current_ffmpeg_process)
                    snap = current_recording_info.copy()
                    snap["stop_reason"] = stop_reason
                    finalization_task_queue.put(snap)

                    console.print(f"[grey58]Post-finish {st['gap_seconds']}s window: cleaning up…[/grey58]")
                    time.sleep(float(st["gap_seconds"]))

                    current_ffmpeg_process = None
                    current_recording_info = {}
                    break

            time.sleep(st["polling_interval_seconds"])
    finally:
        # ensure worker termination and ffmpeg killed
        stop_worker_event.set()
        kill_ffmpeg(current_ffmpeg_process)
        worker.join(timeout=10)
        stop_worker_event.clear()
        current_ffmpeg_process = None
        current_recording_info = {}


# ------------------------ SPOTIFY URI / PLAYLIST PARSING ------------------------
def get_spotify_uris(sp: Spotify, url_or_id: str) -> list[str]:
    try:
        url_or_id = str(url_or_id).strip()
        # track id or URI
        if "track/" in url_or_id or url_or_id.startswith("spotify:track:"):
            track_id = (
                url_or_id.split("track/")[-1].split("?")[0]
                if "track/" in url_or_id
                else url_or_id.split(":")[-1]
            )
            return [f"spotify:track:{track_id}"]

        # playlist
        if "playlist/" in url_or_id or url_or_id.startswith("spotify:playlist:"):
            pl_id = (
                url_or_id.split("playlist/")[-1].split("?")[0]
                if "playlist/" in url_or_id
                else url_or_id.split(":")[-1]
            )
            uris: list[str] = []
            offset = 0
            while True:
                page = sp.playlist_items(pl_id, additional_types=["track"], limit=100, offset=offset)
                items = page.get("items", [])
                for it in items:
                    tr = it.get("track")
                    if tr and tr.get("id"):
                        uris.append(f"spotify:track:{tr['id']}")
                if page.get("next"):
                    offset += 100
                else:
                    break
            return uris

        # album
        if "album/" in url_or_id or url_or_id.startswith("spotify:album:"):
            album_id = (
                url_or_id.split("album/")[-1].split("?")[0]
                if "album/" in url_or_id
                else url_or_id.split(":")[-1]
            )
            uris = []
            offset = 0
            while True:
                album = sp.album_tracks(album_id, limit=50, offset=offset)
                items = album.get("items", [])
                if not items:
                    break
                uris += [f"spotify:track:{tr['id']}" for tr in items if tr.get("id")]
                if album.get("next"):
                    offset += 50
                else:
                    break
            return uris
    except Exception as e:
        console.print(f"[red]Error while parsing Spotify URL: {e}[/red]")
    return []


# ------------------------ MAIN: play and record playlist ------------------------
def play_and_record_playlist(sp: Spotify, playlist_url: str, st: Settings, start_from: int = 1) -> None:
    global current_ffmpeg_process, current_recording_info

    uris = get_spotify_uris(sp, playlist_url)
    if not uris:
        console.print("[yellow]Playlist has no playable tracks.[/yellow]")
        return

    console.print(f"[green]Sequential mode: {len(uris)} tracks found.[/green]")

    # starting index support
    if start_from > 1:
        if start_from > len(uris):
            console.print(f"[red]Start index {start_from} is larger than playlist length ({len(uris)}).[/red]")
            return
        console.print(f"[cyan]Starting from track #{start_from}[/cyan]")
        uris = uris[start_from - 1 : ]

    # Ensure there's an active device; attempt to transfer playback to first device if none active
    try:
        devices = sp.devices().get("devices", [])
        active = next((d for d in devices if d.get("is_active")), None)
        if not active and devices:
            sp.transfer_playback(devices[0]["id"], force_play=True)
            time.sleep(0.8)
    except Exception:
        # non-fatal
        pass

    MIN_FILE_BYTES = 20 * 1024  # minimal file size to consider "recorded"

    for i, uri in enumerate(uris, 1):
        # --- fetch metadata BEFORE arming ffmpeg ---
        meta_preview = None
        try:
            if uri.startswith("spotify:track:"):
                tid = uri.split(":")[-1]
                tr = sp.track(tid)
                if tr:
                    artists = tr.get("artists", []) or []
                    names = [a.get("name", "Unknown Artist") for a in artists if isinstance(a, dict)]
                    meta_preview = {
                        "name": tr.get("name", "Unknown Title"),
                        "artists": names,
                        "artist_str": ", ".join(names),
                        "album": (tr.get("album") or {}).get("name", "Unknown Album"),
                        "track_number": tr.get("track_number"),
                        "duration_ms": tr.get("duration_ms", 0),
                        "id": tr.get("id"),
                    }
        except Exception:
            meta_preview = None

        # build target directory and final filename deterministically
        if st["organize_by_artist_album"] and meta_preview:
            artist_folder = sanitize_for_filesystem((meta_preview.get("artists") or ["Unknown Artist"])[0])
            album_folder = sanitize_for_filesystem(meta_preview.get("album", "Unknown Album"))
            target_dir = st["output_directory"] / artist_folder / album_folder
        else:
            target_dir = st["output_directory"]

        ensure_dir(target_dir)

        if meta_preview:
            final_out = target_dir / out_filename(meta_preview, st["default_format"])
        else:
            final_out = target_dir / f"{i:02d} Unknown Title.{st['default_format']}"

        # check existing recorded file by spotify id tag and skip if matches
        spotify_id_for_check = meta_preview.get("id") if meta_preview else None
        if st.get("skip_existing_file", True) and spotify_id_for_check:
            try:
                if final_out.exists() and final_out.stat().st_size >= MIN_FILE_BYTES:
                    if is_already_recorded_by_spotify_id(final_out, spotify_id_for_check):
                        console.print(f"[grey50]Skipping track #{i}: already recorded -> {final_out.name}[/grey50]")
                        continue
            except Exception:
                # any tag-read error -> ignore and continue normally
                pass

        # --- start ffmpeg (arming) ---
        arm_dir = st["output_directory"] / "__arming__"
        ensure_dir(arm_dir)
        temp_out = arm_dir / f"arming_{i:03d}.flac"

        ff_proc = start_ffmpeg(
            st["ffmpeg_path"],
            st["audio_device"],
            max(10.0, 3600.0),
            temp_out,
            st["default_format"],
        )

        armed_start_iso = datetime.now(timezone.utc).isoformat()

        # preroll
        time.sleep(max(0, st.get("preroll_ms", 0)) / 1000.0)

        # start playback on Spotify
        try:
            safe_spotify_call(sp.start_playback, uris=[uri])
        except Exception as e:
            console.print(f"[red]FAILED to start playback → {uri} ({e})[/red]")
            log_failed_track(uri)
            kill_ffmpeg(ff_proc)
            try:
                temp_out.unlink(missing_ok=True)
            except Exception:
                pass
            continue

        # small delay to allow metadata to appear in Spotify API
        time.sleep(0.25)

        meta_now = current_track(sp) or {"name": "Unknown Title", "artist_str": "Unknown Artist", "duration_ms": 0}

        # if metadata missing after start, treat as failure
        if not meta_now or not meta_now.get("id"):
            console.print(f"[red]FAILED (no metadata after start) → {uri}[/red]")
            log_failed_track(uri)
            kill_ffmpeg(ff_proc)
            try:
                temp_out.unlink(missing_ok=True)
            except Exception:
                pass
            continue

        # recompute target folder and final name from now-playing metadata (safer)
        if st["organize_by_artist_album"]:
            artist_folder = sanitize_for_filesystem((meta_now.get("artists") or ["Unknown Artist"])[0])
            album_folder = sanitize_for_filesystem(meta_now.get("album", "Unknown Album"))
            target_dir = st["output_directory"] / artist_folder / album_folder
        else:
            target_dir = st["output_directory"]

        ensure_dir(target_dir)

        final_out = target_dir / out_filename(meta_now, st["default_format"])

        # UI banner
        pretty_artist = meta_now.get("artist_str", "Unknown Artist")
        pretty_title = meta_now.get("name", "Unknown Title")
        fmt_upper = st["default_format"].upper()

        relative = str(final_out)
        if "Recordings" in relative:
            relative = "Recordings" + relative.split("Recordings", 1)[1]

        banner = Text.from_markup(
            f"""
[bold sky_blue1]Start:[/bold sky_blue1] {escape(pretty_artist)} - {escape(pretty_title)} ({fmt_upper})
[bold sky_blue1]To:[/bold sky_blue1] {escape(relative)}
[bold sky_blue1]Target duration (API±buf):[/bold sky_blue1] ~{(float(meta_now.get('duration_ms', 0)) / 1000 + float(st.get('recording_buffer_seconds', 0))):.1f}s
"""
        )

        console.print(Panel(banner, title="[white]Recording Initiated[/white]", border_style="cyan", expand=False))

        # set global ffmpeg process and recording info
        current_ffmpeg_process = ff_proc

        expected = (float(meta_now.get("duration_ms", 0) or 0) / 1000.0) + float(st.get("recording_buffer_seconds", 0))

        current_recording_info = {
            "process_obj": ff_proc,
            "track_id": meta_now.get("id"),
            "start_iso": armed_start_iso,
            "audio_path": temp_out,
            "final_path": final_out,
            "metadata": meta_now,
            "expected_duration_sec": expected,
            "stop_reason": "",
            "rewrite_enabled": st.get("rewrite_headers_enabled", False),
        }

        # blocking monitor that waits for track finish / change and finalizes
        record_one_track_blocking(sp, st)

        # after recording: check that the final file exists and is not trivially small
        try:
            if not final_out.exists() or final_out.stat().st_size < 50 * 1024:
                console.print(f"[red]FAILED (empty or too small file) → {uri}[/red]")
                log_failed_track(uri)
        except Exception:
            # ignore file-stat errors (do not crash the loop)
            pass

        # small gap before next track (if any)
        if i < len(uris):
            console.print(f"[grey58]Waiting {st.get('gap_seconds', 1.0)}s before next track…[/grey58]")
            time.sleep(float(st.get("gap_seconds", 1.0)))


# ------------------------ simple manual follow mode (optional) ------------------------
def manual_follow_current(sp: Spotify, st: Settings) -> None:
    global current_ffmpeg_process, current_recording_info

    log_file = st["output_directory"] / "aurora_metadata.jsonl"
    ensure_dir(st["output_directory"])

    worker = threading.Thread(
        target=finalization_worker, args=(st["ffmpeg_path"], log_file), daemon=True
    )
    worker.start()

    from aurora_io import ensure_standby, drop_standby, standby_ffmpeg_process, standby_file

    ensure_standby(st)

    try:
        with console.status("[italic grey50]Monitoring… (global pre-arm)[/italic grey50]", spinner="line", speed=1.2):
            while True:
                meta = current_track(sp)

                if not current_ffmpeg_process:
                    if meta and meta.get("is_playing"):
                        ensure_dir(st["output_directory"])
                        meta_now = meta

                        if st["organize_by_artist_album"]:
                            artist_folder = sanitize_for_filesystem((meta_now.get("artists") or ["Unknown Artist"])[0])
                            album_folder = sanitize_for_filesystem(meta_now.get("album", "Unknown Album"))
                            target_dir = st["output_directory"] / artist_folder / album_folder
                        else:
                            target_dir = st["output_directory"]

                        ensure_dir(target_dir)
                        final_out = target_dir / out_filename(meta_now, st["default_format"])

                        # use standby ffmpeg process created by aurora_io
                        current_ffmpeg_process = standby_ffmpeg_process
                        current_recording_info = {
                            "process_obj": current_ffmpeg_process,
                            "track_id": meta_now.get("id"),
                            "start_iso": datetime.now(timezone.utc).isoformat(),
                            "audio_path": standby_file,
                            "final_path": final_out,
                            "metadata": meta_now,
                            "expected_duration_sec": (float(meta_now.get("duration_ms", 0) or 0) / 1000.0) + float(st.get("recording_buffer_seconds", 0)),
                            "stop_reason": "",
                            "rewrite_enabled": st.get("rewrite_headers_enabled", False),
                        }

                        from aurora_io import ensure_standby as _ensure
                        standby_ffmpeg_process = None
                        standby_file = None
                        _ensure(st)
                    else:
                        ensure_standby(st)
                else:
                    stop_reason: t.Optional[str] = None
                    if not meta or not meta.get("is_playing"):
                        stop_reason = "Playback stopped or track unavailable"
                    elif meta.get("id") != current_recording_info.get("track_id"):
                        stop_reason = "Track changed"
                    else:
                        try:
                            prog = float(meta.get("progress_ms", 0) or 0)
                            dur = float(meta.get("duration_ms", 0) or 0)
                            if dur > 0 and prog >= max(0.0, dur - 200):
                                stop_reason = "Track finished"
                        except Exception:
                            pass

                    if stop_reason:
                        kill_ffmpeg(current_ffmpeg_process)
                        snap = current_recording_info.copy()
                        snap["stop_reason"] = stop_reason
                        finalization_task_queue.put(snap)
                        current_ffmpeg_process = None
                        current_recording_info = {}

                time.sleep(st["polling_interval_seconds"])
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted[/yellow]")
    finally:
        stop_worker_event.set()
        kill_ffmpeg(current_ffmpeg_process)
        if current_recording_info:
            snap = current_recording_info.copy()
            snap["stop_reason"] = "Shutdown"
            finalization_task_queue.put(snap)
            current_recording_info = {}
        from aurora_io import drop_standby
        drop_standby()
        worker.join(timeout=10)


# ------------------------ CLI entrypoint ------------------------
def main() -> None:
    from argparse import ArgumentParser

    banner = Text.from_markup(f"""
[bold sky_blue1]Aurora Recorder[/bold sky_blue1]
Version: {SCRIPT_VERSION}
""")
    console.print(Panel(banner, title="[white]Welcome[/white]", border_style="magenta", expand=False, padding=(1, 2)))

    disclaimer = Text.from_markup(
        """
[bold red]Disclaimer:[/bold red] Recording streams may violate Terms of Service.
Use only for personal/private purposes and comply with local laws.
"""
    )
    console.print(Panel(disclaimer, title="[bold yellow]Important Notice[/bold yellow]", border_style="yellow", expand=False))

    ap = ArgumentParser(description="Record Spotify playback to FLAC (VB-CABLE, Windows friendly)")
    ap.add_argument("track", nargs="?", help="Single Spotify track URL/URI or text file with multiple links")
    ap.add_argument("--album", help="Spotify album or playlist URL/URI for sequential recording")
    ap.add_argument("--track-no", type=int, default=1, help="Start recording from this track index (1-based)")
    ap.add_argument("--playlist", help="Spotify playlist or album URL/URI for sequential recording")
    ap.add_argument("--device", help="Override FFmpeg input device (default from config.ini)")
    ap.add_argument("--ffmpeg", help="Override FFmpeg path (default from config.ini)")
    ap.add_argument("--out", help="Override output base directory (default from config.ini)")
    ap.add_argument("--no-rewrite", action="store_true", help="Disable header rewrite step")

    args = ap.parse_args()

    st = read_settings()
    if args.device:
        st["audio_device"] = args.device
    if args.ffmpeg:
        st["ffmpeg_path"] = args.ffmpeg
    if args.out:
        st["output_directory"] = Path(args.out)
    if args.no_rewrite:
        st["rewrite_headers_enabled"] = False

    sp = get_spotify_client()

    # Playlist or album mode
    source = args.album or args.playlist
    if source:
        uris = get_spotify_uris(sp, source)
        if not uris:
            console.print("[red]No tracks found in album or playlist.[/red]")
            return
        console.print(f"[green]Sequential recording: {len(uris)} tracks detected.[/green]")
        play_and_record_playlist(sp, source, st, start_from=args.track_no)
        return

    # Single track or text file with links
    if args.track:
        p = Path(args.track)
        if p.exists() and p.is_file() and p.suffix.lower() == ".txt":
            blocked_names = {"failed_tracks.txt", "requirements.txt"}
            if p.name.lower() in blocked_names:
                console.print(f"[red]'{p.name}' is a reserved internal file and cannot be used as input.[/red]")
                return
            console.print(f"[cyan]Reading links from {p.name}[/cyan]")
            with open(p, "r", encoding="utf-8") as fh:
                links = [line.strip() for line in fh if line.strip()]
            console.print(f"[green]{len(links)} link(s) loaded from {p.name}[/green]")
            for i, link in enumerate(links, 1):
                console.print(f"[yellow]({i}/{len(links)}) Playing {link}[/yellow]")
                uris = get_spotify_uris(sp, link)
                if not uris:
                    console.print(f"[red]Invalid or unsupported link: {link}[/red]")
                    continue
                play_and_record_playlist(sp, link, st, start_from=args.track_no)
            return

        # Single track
        uris = get_spotify_uris(sp, args.track)
        if not uris:
            console.print("[red]Invalid or unsupported track link.[/red]")
            return
        console.print("[green]Single track mode: starting Spotify playback...[/green]")
        play_and_record_playlist(sp, args.track, st)
        return


if __name__ == "__main__":
    try:
        import signal as _s
        _s.signal(_s.SIGINT, _s.default_int_handler)
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
        pass