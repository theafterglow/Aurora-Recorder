from __future__ import annotations
import typing as t
import shutil
import subprocess
import time
import queue
import json

from pathlib import Path
from datetime import datetime, timezone

from rich.markup import escape

from aurora_core import (
    console,
    FAILED_TXT,
    Settings,
)

import requests
from mutagen.flac import FLAC, Picture

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def start_ffmpeg(ffmpeg: str, device: str, dur_s: float, out_path: Path, fmt: str, try_24bit: bool = True) -> subprocess.Popen:
    import os, sys
    input_format = 'dshow' if os.name == 'nt' else ('avfoundation' if sys.platform == 'darwin' else 'alsa')
    cmd = [
        ffmpeg, '-y',
        '-hide_banner',
        '-fflags', '+nobuffer',
        '-flags', 'low_delay',
        '-thread_queue_size', '1024',
        '-f', input_format, '-i', device,
        '-t', str(max(0.1, dur_s)),
        '-ac', '2',
        '-ar', '44100',
    ]
    if fmt == 'flac':
        if try_24bit:
            cmd += ['-sample_fmt', 's32']
        cmd += ['-acodec', 'flac', '-vn']
    else:
        raise ValueError('Only FLAC is supported in this build')
    cmd.append(str(out_path))
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def kill_ffmpeg(p: t.Optional[subprocess.Popen]) -> None:
    if not p:
        return
    try:
        if p.poll() is None and p.stdin and not p.stdin.closed:
            try:
                p.stdin.write(b'q'); p.stdin.flush()
            except Exception:
                pass
        try:
            p.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.communicate(timeout=3)
            except Exception:
                pass
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def rewrite_headers(audio_path: Path, ffmpeg_path: str) -> bool:
    if not audio_path.exists() or audio_path.stat().st_size < 1024:
        return False
    tmp = audio_path.with_name(audio_path.stem + "_rewrite_temp" + audio_path.suffix)
    cmd = [ffmpeg_path, '-y', '-i', str(audio_path), '-acodec', 'copy', '-vn', '-map_metadata', '-1', str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=120)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size >= 1024:
            audio_path.unlink(missing_ok=True); tmp.rename(audio_path); return True
    finally:
        tmp.unlink(missing_ok=True)
    return False


def download_cover(url: t.Optional[str], dest: Path) -> bool:
    if not url:
        return False
    try:
        r = requests.get(url, timeout=10); r.raise_for_status(); dest.write_bytes(r.content); return True
    except Exception:
        return False


def embed_flac(audio_path: Path, meta: dict, cover_path: t.Optional[Path]) -> None:
    if not audio_path.exists() or audio_path.suffix.lower() != '.flac':
        return
    try:
        fl = FLAC(audio_path)
        fl['TITLE'] = meta.get('name', 'Unknown Title')
        fl['ARTIST'] = meta.get('artist_str', 'Unknown Artist')
        fl['ALBUM'] = meta.get('album', 'Unknown Album')
        if meta.get('album_artist_str'): fl['ALBUMARTIST'] = meta['album_artist_str']
        if meta.get('composer_str'): fl['COMPOSER'] = meta['composer_str']
        if meta.get('performer_str'): fl['PERFORMER'] = meta['performer_str']
        if meta.get('album_release_date'):
            y = str(meta['album_release_date']).split('-')[0]
            fl['DATE'] = y; fl['YEAR'] = y
        if meta.get('track_number'): fl['TRACKNUMBER'] = str(meta['track_number'])
        if meta.get('id'): fl['SPOTIFY_TRACK_ID'] = meta['id']
        if cover_path and cover_path.exists():
            pic = Picture(); pic.data = cover_path.read_bytes(); pic.type = 3; pic.mime = 'image/jpeg'; fl.add_picture(pic)
        fl.save()
    except Exception as e:
        console.print(f"[yellow]Embed error for {audio_path.name}: {e}[/yellow]")

def robust_move(src: Path, dst: Path) -> Path:
    """Move/rename even if src is on Windows and was just closed by ffmpeg."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.replace(dst)
        return dst
    except Exception:
        try:
            shutil.move(str(src), str(dst))
            return dst
        except Exception:
            time.sleep(0.5)
            shutil.move(str(src), str(dst))
            return dst


def finalization_worker(ffmpeg_path: str, log_file: Path):
    console.print("[cyan]Finalization worker started.[/cyan]")
    from aurora_core import finalization_task_queue, stop_worker_event
    while not stop_worker_event.is_set() or not finalization_task_queue.empty():
        try:
            task = finalization_task_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            proc: subprocess.Popen = task['process_obj']
            temp_path: Path = task['audio_path']
            final_path: Path = task.get('final_path', temp_path)
            meta: dict = task['metadata']
            start_iso: str = task['start_iso']
            expected_duration_sec: float = task['expected_duration_sec']
            stop_reason: str = task['stop_reason']
            rewrite_enabled: bool = task.get('rewrite_enabled', True)

            if proc and proc.poll() is None:
                try:
                    proc.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill(); proc.communicate(timeout=3)
                    except Exception:
                        pass
                time.sleep(5)

            # If still in __arming__, move now to final
            audio_path = temp_path
            if str(temp_path).find("__arming__") != -1:
                try:
                    audio_path = robust_move(temp_path, final_path)
                except Exception as e:
                    console.print(f"[yellow]Move failed: {escape(str(e))}[/yellow]")
                    audio_path = temp_path

            if not (audio_path.exists() and audio_path.stat().st_size > 1024):
                finalization_task_queue.task_done(); continue

            rewrite_ok = False
            if rewrite_enabled:
                rewrite_ok = rewrite_headers(audio_path, ffmpeg_path)

            try:
                st_dt = datetime.fromisoformat(start_iso)
                if st_dt.tzinfo is None:
                    st_dt = st_dt.replace(tzinfo=timezone.utc)
                rec_sec = (datetime.now(timezone.utc) - st_dt).total_seconds()
            except Exception:
                rec_sec = -1

            sp_dur = (float(meta.get('duration_ms', 0) or 0) / 1000.0)
            min_required = max(sp_dur - 3.0, 0.0)
            if rec_sec > 0 and rec_sec < min_required:
                try: audio_path.unlink(missing_ok=True)
                except Exception: pass
                try:
                    with open(FAILED_TXT, 'a', encoding='utf-8') as f:
                        f.write(f"{meta.get('artist_str','Unknown Artist')} - {meta.get('name','Unknown Title')}\n")
                except Exception as e:
                    console.print(f"[yellow]failed_tracks.txt write error: {e}[/yellow]")
                finalization_task_queue.task_done(); continue

            cover = audio_path.with_name(audio_path.stem + '_cover.jpg')
            cover_ok = download_cover(meta.get('cover_url'), cover)
            try:
                embed_flac(audio_path, meta, cover if cover_ok else None)
            finally:
                if cover.exists():
                    try: cover.unlink()
                    except Exception: pass

            try:
                entry = {
                    'track_id': meta.get('id'),
                    'title': meta.get('name'),
                    'artist_str': meta.get('artist_str'),
                    'album': meta.get('album'),
                    'start_time': start_iso,
                    'end_time': datetime.now(timezone.utc).isoformat(),
                    'original_duration_sec': sp_dur,
                    'ffmpeg_target_duration_sec': expected_duration_sec,
                    'recorded_duration_seconds': round(rec_sec, 2) if rec_sec > 0 else 'N/A',
                    'header_rewrite_successful': rewrite_ok,
                    'stop_reason': stop_reason,
                    'filename': str(audio_path),
                    'format': audio_path.suffix.lstrip('.')
                }
                with open(log_file, 'a', encoding='utf-8') as f: f.write(json.dumps(entry) + '')  # type: ignore[name-defined]
            except Exception as e:
                console.print(f"[yellow]Log write error: {e}[/yellow]")

            finalization_task_queue.task_done()
        except Exception as e:
            console.print("[bold red][Worker] Finalization error: "+str(e))
            try: finalization_task_queue.task_done()
            except Exception: pass
    console.print("[cyan]Finalization worker stopped.[/cyan]")

standby_ffmpeg_process = None
standby_file = None

def ensure_standby(st: Settings):
    global standby_ffmpeg_process, standby_file
    if standby_ffmpeg_process is None or standby_ffmpeg_process.poll() is not None:
        arm_dir = st['output_directory'] / "__standby__"
        ensure_dir(arm_dir)
        standby_file = arm_dir / f"standby_{int(time.time())}.flac"
        standby_ffmpeg_process = start_ffmpeg(
            st['ffmpeg_path'], st['audio_device'], max(10.0, float(st['standby_seconds'])), standby_file, st['default_format']
        )
        console.print("[grey58]Standby capture armed.[/grey58]")


def drop_standby():
    global standby_ffmpeg_process, standby_file
    try:
        kill_ffmpeg(standby_ffmpeg_process)
    finally:
        standby_ffmpeg_process = None
    if standby_file and Path(standby_file).exists():
        try: Path(standby_file).unlink()
        except Exception: pass
    standby_file = None