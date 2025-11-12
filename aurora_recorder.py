from __future__ import annotations
import time, threading
import typing as t
from pathlib import Path
from datetime import datetime, timezone

from rich.panel import Panel
from rich.text import Text
from rich.markup import escape

from spotipy import Spotify

from aurora_core import (
    console,
    Settings,
    SCRIPT_VERSION,
    read_settings,
    get_spotify_client,
    sanitize_for_filesystem,
)
from aurora_io import (
    ensure_dir,
    start_ffmpeg,
    kill_ffmpeg,
    finalization_worker,
)
from aurora_core import current_track

from aurora_core import (
    current_ffmpeg_process,
    current_recording_info,
    finalization_task_queue,
    stop_worker_event,
)


def out_filename(meta: dict, fmt: str) -> str:
    try:
        tn = int(meta.get("track_number") or 0)
    except Exception:
        tn = 0
    nn = f"{tn:02d}" if tn > 0 else "00"
    title = sanitize_for_filesystem(meta.get("name", "Unknown Title"))
    return f"{nn} {title}.{fmt}"

def record_one_track_blocking(sp: Spotify, st: Settings) -> None:
    global current_ffmpeg_process, current_recording_info
    log_file = st['output_directory'] / 'aurora_metadata.jsonl'
    ensure_dir(st['output_directory'])
    worker = threading.Thread(target=finalization_worker, args=(st['ffmpeg_path'], log_file), daemon=True)
    worker.start()
    try:
        while True:
            meta = current_track(sp)

            if not current_ffmpeg_process:
                if meta and meta.get('is_playing'):
                    pass
            else:
                stop_reason: t.Optional[str] = None
                if not meta or not meta.get('is_playing'):
                    stop_reason = 'Playback stopped or track unavailable'
                elif meta.get('id') != current_recording_info.get('track_id'):
                    stop_reason = 'Track changed'
                else:
                    try:
                        prog = float(meta.get('progress_ms', 0) or 0)
                        dur  = float(meta.get('duration_ms', 0) or 0)
                        if dur > 0 and prog >= max(0.0, dur - 200):
                            stop_reason = 'Track finished'
                    except Exception:
                        pass
                if stop_reason:
                    kill_ffmpeg(current_ffmpeg_process)
                    snap = current_recording_info.copy(); snap['stop_reason'] = stop_reason
                    finalization_task_queue.put(snap)

                    console.print("[grey58]Post-finish 5s window: killing/renaming/arming…[/grey58]")
                    time.sleep(5)

                    current_ffmpeg_process = None; current_recording_info = {}
                    break
            time.sleep(st['polling_interval_seconds'])
    finally:
        stop_worker_event.set(); kill_ffmpeg(current_ffmpeg_process)
        worker.join(timeout=10)
        stop_worker_event.clear()
        current_ffmpeg_process = None; current_recording_info = {}

def get_spotify_uris(sp: Spotify, url_or_id: str) -> list[str]:
    try:
        if 'track/' in url_or_id or url_or_id.startswith('spotify:track:'):
            track_id = url_or_id.split('track/')[-1].split('?')[0] if 'track/' in url_or_id else url_or_id.split(':')[-1]
            return [f"spotify:track:{track_id}"]

        if 'playlist/' in url_or_id or url_or_id.startswith('spotify:playlist:'):
            pl_id = url_or_id.split('playlist/')[-1].split('?')[0] if 'playlist/' in url_or_id else url_or_id.split(':')[-1]
            uris = []
            offset = 0
            while True:
                page = sp.playlist_items(pl_id, additional_types=['track'], limit=100, offset=offset)
                items = page.get('items', [])
                for it in items:
                    tr = it.get('track')
                    if tr and tr.get('id'):
                        uris.append(f"spotify:track:{tr['id']}")
                if page.get('next'):
                    offset += 100
                else:
                    break
            return uris

        if 'album/' in url_or_id or url_or_id.startswith('spotify:album:'):
            album_id = url_or_id.split('album/')[-1].split('?')[0] if 'album/' in url_or_id else url_or_id.split(':')[-1]
            uris = []
            offset = 0
            while True:
                album = sp.album_tracks(album_id, limit=50, offset=offset)
                items = album.get('items', [])
                if not items: break
                uris += [f"spotify:track:{tr['id']}" for tr in items if tr.get('id')]
                if album.get('next'): offset += 50
                else: break
            return uris

    except Exception as e:
        console.print(f"[red]Error while parsing Spotify URL: {e}[/red]")

    return []

def play_and_record_playlist(sp: Spotify, playlist_url: str, st: Settings):
    global current_ffmpeg_process, current_recording_info
    uris = get_spotify_uris(sp, playlist_url)
    if not uris:
        console.print('[yellow]Playlist has no playable tracks.[/yellow]')
        return
    console.print(f"[green]Sequential mode: {len(uris)} tracks found.[/green]")

    try:
        devices = sp.devices().get('devices', [])
        active = next((d for d in devices if d.get('is_active')), None)
        if not active and devices:
            sp.transfer_playback(devices[0]['id'], force_play=True)
            time.sleep(0.8)
    except Exception:
        pass

    for i, uri in enumerate(uris, 1):

        arm_dir = st['output_directory'] / "__arming__"
        ensure_dir(arm_dir)
        temp_out = arm_dir / f"arming_{i:03d}.flac"

        ff_proc = start_ffmpeg(
            st['ffmpeg_path'], st['audio_device'], max(10.0, 3600.0),
            temp_out, st['default_format']
        )
        armed_start_iso = datetime.now(timezone.utc).isoformat()

        time.sleep(max(0, st['preroll_ms']) / 1000.0)
        try:
            sp.start_playback(uris=[uri])
        except Exception as e:
            console.print(f"[yellow]Could not start track: {e}[/yellow]")
            kill_ffmpeg(ff_proc)
            temp_out.unlink(missing_ok=True)
            continue

        time.sleep(0.25)
        meta_now = current_track(sp) or {
            "name": "Unknown Title",
            "artist_str": "Unknown Artist",
            "duration_ms": 0
        }

        if st['organize_by_artist_album']:
            artist_folder = sanitize_for_filesystem(
                (meta_now.get('artists') or ['Unknown Artist'])[0])
            album_folder = sanitize_for_filesystem(
                meta_now.get('album', 'Unknown Album'))
            target_dir = st['output_directory'] / artist_folder / album_folder
        else:
            target_dir = st['output_directory']
        ensure_dir(target_dir)
        final_out = target_dir / out_filename(meta_now, st['default_format'])

        pretty_artist = meta_now.get("artist_str", "Unknown Artist")
        pretty_title = meta_now.get("name", "Unknown Title")
        fmt_upper = st['default_format'].upper()
        relative = str(final_out)
        if "Recordings" in relative:
            relative = "Recordings" + relative.split("Recordings", 1)[1]
        banner = Text.from_markup(f"""
[bold sky_blue1]Start:[/bold sky_blue1] {escape(pretty_artist)} - {escape(pretty_title)} ({fmt_upper})
[bold sky_blue1]To:[/bold sky_blue1] {escape(relative)}
[bold sky_blue1]Target duration (API±buf):[/bold sky_blue1] ~{(float(meta_now.get('duration_ms', 0))/1000 + float(st['recording_buffer_seconds'])):.1f}s
""")
        console.print(Panel(banner, title="[white]Recording Initiated[/white]",
                            border_style="cyan", expand=False))

        current_ffmpeg_process = ff_proc
        expected = (float(meta_now.get('duration_ms', 0) or 0) / 1000.0) + \
                   float(st['recording_buffer_seconds'])
        current_recording_info = {
            'process_obj': ff_proc,
            'track_id': meta_now.get('id'),
            'start_iso': armed_start_iso,
            'audio_path': temp_out,
            'final_path': final_out,
            'metadata': meta_now,
            'expected_duration_sec': expected,
            'stop_reason': '',
            'rewrite_enabled': st['rewrite_headers_enabled'],
        }

        record_one_track_blocking(sp, st)

        if i < len(uris):
            console.print(f"[grey58]Waiting {st['gap_seconds']}s before next track…[/grey58]")
            time.sleep(float(st['gap_seconds']))

def manual_follow_current(sp: Spotify, st: Settings):
    global current_ffmpeg_process, current_recording_info
    log_file = st['output_directory'] / 'aurora_metadata.jsonl'
    ensure_dir(st['output_directory'])
    worker = threading.Thread(target=finalization_worker, args=(st['ffmpeg_path'], log_file), daemon=True)
    worker.start()

    from aurora_io import ensure_standby, drop_standby, standby_ffmpeg_process, standby_file
    ensure_standby(st)

    try:
        with console.status('[italic grey50]Monitoring… (global pre-arm)[/italic grey50]', spinner='line', speed=1.2):
            while True:
                meta = current_track(sp)
                if not current_ffmpeg_process:
                    if meta and meta.get('is_playing'):
                        ensure_dir(st['output_directory'])
                        meta_now = meta
                        if st['organize_by_artist_album']:
                            artist_folder = sanitize_for_filesystem((meta_now.get('artists') or ['Unknown Artist'])[0])
                            album_folder  = sanitize_for_filesystem(meta_now.get('album', 'Unknown Album'))
                            target_dir = st['output_directory'] / artist_folder / album_folder
                        else:
                            target_dir = st['output_directory']
                        ensure_dir(target_dir)
                        final_out = target_dir / out_filename(meta_now, st['default_format'])

                        current_ffmpeg_process = standby_ffmpeg_process
                        current_recording_info = {
                            'process_obj': current_ffmpeg_process,
                            'track_id': meta_now.get('id'),
                            'start_iso': datetime.now(timezone.utc).isoformat(),
                            'audio_path': standby_file,
                            'final_path': final_out,
                            'metadata': meta_now,
                            'expected_duration_sec': (float(meta_now.get('duration_ms',0) or 0)/1000.0) + float(st['recording_buffer_seconds']),
                            'stop_reason': '',
                            'rewrite_enabled': st['rewrite_headers_enabled'],
                        }

                        from aurora_io import ensure_standby as _ensure
                        standby_ffmpeg_process = None
                        standby_file = None
                        _ensure(st)
                    else:
                        ensure_standby(st)
                else:
                    stop_reason: t.Optional[str] = None
                    if not meta or not meta.get('is_playing'):
                        stop_reason = 'Playback stopped or track unavailable'
                    elif meta.get('id') != current_recording_info.get('track_id'):
                        stop_reason = 'Track changed'
                    else:
                        try:
                            prog = float(meta.get('progress_ms', 0) or 0)
                            dur  = float(meta.get('duration_ms', 0) or 0)
                            if dur > 0 and prog >= max(0.0, dur - 200):
                                stop_reason = 'Track finished'
                        except Exception:
                            pass

                    if stop_reason:
                        kill_ffmpeg(current_ffmpeg_process)
                        snap = current_recording_info.copy(); snap['stop_reason'] = stop_reason
                        finalization_task_queue.put(snap)
                        current_ffmpeg_process = None; current_recording_info = {}

                time.sleep(st['polling_interval_seconds'])

    except KeyboardInterrupt:
        console.print('[yellow]Interrupted[/yellow]')
    finally:
        stop_worker_event.set(); kill_ffmpeg(current_ffmpeg_process)
        if current_recording_info:
            snap = current_recording_info.copy(); snap['stop_reason'] = 'Shutdown'
            finalization_task_queue.put(snap)
            current_recording_info = {}
        from aurora_io import drop_standby
        drop_standby()
        worker.join(timeout=10)

def main():
    from argparse import ArgumentParser

    banner = Text.from_markup(f"""
[bold sky_blue1]Aurora Recorder[/bold sky_blue1]
Version: {SCRIPT_VERSION}
""")
    console.print(Panel(banner, title='[white]Welcome[/white]', border_style='magenta', expand=False, padding=(1,2)))

    disclaimer = Text.from_markup("""
[bold red]Disclaimer:[/bold red] Recording streams may violate Terms of Service.
Use only for personal/private purposes and comply with local laws.
""")
    console.print(Panel(disclaimer, title='[bold yellow]Important Notice[/bold yellow]', border_style='yellow', expand=False))

    ap = ArgumentParser(description='Record Spotify playback to FLAC (VB-CABLE, Windows friendly)')
    ap.add_argument('track', nargs='?', help='Single Spotify track URL/URI or text file with multiple links')
    ap.add_argument('--album', help='Spotify album or playlist URL/URI for sequential recording')
    ap.add_argument('--playlist', help='Spotify playlist or album URL/URI for sequential recording')
    ap.add_argument('--device', help='Override FFmpeg input device (default from config.ini)')
    ap.add_argument('--ffmpeg', help='Override FFmpeg path (default from config.ini)')
    ap.add_argument('--out', help='Override output base directory (default from config.ini)')
    ap.add_argument('--no-rewrite', action='store_true', help='Disable header rewrite step')
    args = ap.parse_args()

    st = read_settings()
    if args.device: st['audio_device'] = args.device
    if args.ffmpeg: st['ffmpeg_path'] = args.ffmpeg
    if args.out:    st['output_directory'] = Path(args.out)
    if args.no_rewrite: st['rewrite_headers_enabled'] = False

    sp = get_spotify_client()

    source = args.album or args.playlist
    if source:
        uris = get_spotify_uris(sp, source)
        if not uris:
            console.print("[red]No tracks found in album or playlist.[/red]")
            return
        console.print(f"[green]Sequential recording: {len(uris)} tracks detected.[/green]")
        play_and_record_playlist(sp, source, st)
        return

    if args.track:
        p = Path(args.track)

        if p.exists() and p.is_file() and p.suffix.lower() == ".txt":
            blocked_names = {"failed_tracks.txt", "requirements.txt"}
            if p.name.lower() in blocked_names:
                console.print(f"[red]'{p.name}' is a reserved internal file and cannot be used as input.[/red]")
                return

            console.print(f"[cyan]Reading links from {p.name}[/cyan]")
            with open(p, "r", encoding="utf-8") as f:
                links = [line.strip() for line in f if line.strip()]
            console.print(f"[green]{len(links)} link(s) loaded from {p.name}[/green]")

            for i, link in enumerate(links, 1):
                console.print(f"[yellow]({i}/{len(links)}) Playing {link}[/yellow]")
                uris = get_spotify_uris(sp, link)
                if not uris:
                    console.print(f"[red]Invalid or unsupported link: {link}[/red]")
                    continue
                play_and_record_playlist(sp, link, st)
            return

        uris = get_spotify_uris(sp, args.track)
        if not uris:
            console.print("[red]Invalid or unsupported track link.[/red]")
            return
        console.print("[green]Single track mode: starting Spotify playback...[/green]")
        play_and_record_playlist(sp, args.track, st)
        return



if __name__ == '__main__':
    try:
        import signal as _s
        _s.signal(_s.SIGINT, _s.default_int_handler)
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
        pass