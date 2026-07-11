"""YouTube URL helpers for remote single-video grid and playback."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from PIL import Image

_SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"

_VIDEO_ID_RE = re.compile(
    r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})"
)

_DEFAULT_YT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class YouTubeStream:
    url: str
    http_headers: dict[str, str] = field(default_factory=dict)
    title: str | None = None


def is_remote_media_url(path: str) -> bool:
    if not isinstance(path, str):
        return False
    p = path.strip().lower()
    return p.startswith("http://") or p.startswith("https://")


def is_youtube_url(path: str) -> bool:
    if not is_remote_media_url(path):
        return False
    p = path.strip().lower()
    return "youtube.com" in p or "youtu.be" in p


def is_youtube_watch_url(path: str) -> bool:
    return is_youtube_url(path) and extract_youtube_video_id(path) is not None


def extract_youtube_playlist_id(url: str) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url.strip())
    list_ids = urllib.parse.parse_qs(parsed.query).get("list")
    if not list_ids or not list_ids[0]:
        return None
    list_id = list_ids[0].strip()
    return list_id or None


def normalize_youtube_playlist_url(url: str) -> str:
    """Canonical playlist URL for yt-dlp and current_directory."""
    list_id = extract_youtube_playlist_id(url)
    if list_id:
        return f"https://www.youtube.com/playlist?list={list_id}"
    return url.strip()


def is_youtube_playlist_url(path: str) -> bool:
    """True for playlist pages and watch URLs that include a list= parameter."""
    if not is_youtube_url(path):
        return False
    if "/playlist" in path.strip().lower():
        return True
    return extract_youtube_playlist_id(path) is not None


def extract_youtube_video_id(url: str) -> str | None:
    if not url:
        return None
    match = _VIDEO_ID_RE.search(url.strip())
    return match.group(1) if match else None


def normalize_youtube_watch_url(url: str) -> str | None:
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_thumbnail_url(video_id: str, quality: str = "hqdefault") -> str:
    return f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"


def fetch_youtube_oembed(watch_url: str) -> dict | None:
    query = urllib.parse.urlencode({"url": watch_url, "format": "json"})
    request_url = f"https://www.youtube.com/oembed?{query}"
    try:
        with urllib.request.urlopen(request_url, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        logging.warning("YouTube oEmbed fetch failed for %s: %s", watch_url, exc)
        return None


def _read_youtube_settings() -> dict:
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def save_youtube_auth_settings(cookies_file: str = "", cookies_browser: str = "") -> None:
    """Persist YouTube cookie settings into settings.json."""
    settings: dict = {}
    if _SETTINGS_PATH.is_file():
        try:
            with open(_SETTINGS_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                settings = data
        except (OSError, json.JSONDecodeError, TypeError):
            settings = {}
    settings["youtube_cookies_file"] = (cookies_file or "").strip()
    settings["youtube_cookies_browser"] = (cookies_browser or "").strip()
    try:
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=4)
    except OSError as exc:
        logging.warning("Could not save YouTube auth settings: %s", exc)


def get_youtube_cookies_file() -> str | None:
    """Optional Netscape cookies.txt path from settings.json (youtube_cookies_file)."""
    path = (_read_youtube_settings().get("youtube_cookies_file") or "").strip()
    if path and os.path.isfile(path):
        return path
    return None


def get_youtube_cookies_browser() -> str | None:
    """Optional browser name for yt-dlp cookiesfrombrowser (youtube_cookies_browser)."""
    browser = (_read_youtube_settings().get("youtube_cookies_browser") or "").strip()
    return browser or None


def _iter_yt_dlp_auth_variants() -> list[tuple[dict, str]]:
    """Auth option sets to try (user-configured only — no slow auto browser sweep)."""
    cookiefile = get_youtube_cookies_file()
    if cookiefile:
        return [({"cookiefile": cookiefile}, "cookie file")]

    browser = get_youtube_cookies_browser()
    if browser:
        return [({"cookiesfrombrowser": (browser,)}, f"browser:{browser}")]

    return [({}, "anonymous")]


def _is_browser_cookie_access_error(exc: Exception | None) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "dpapi",
            "could not copy",
            "cookie database",
            "failed to decrypt",
        )
    )


def _apply_youtube_auth_to_ydl_opts(ydl_opts: dict, auth_opts: dict | None = None) -> dict:
    merged = dict(ydl_opts)
    if auth_opts:
        merged.update(auth_opts)
        return merged

    cookiefile = get_youtube_cookies_file()
    if cookiefile:
        merged["cookiefile"] = cookiefile
        return merged

    browser = get_youtube_cookies_browser()
    if browser:
        merged["cookiesfrombrowser"] = (browser,)
    return merged


def is_youtube_auth_error(exc: Exception | None) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "sign in to confirm",
            "not a bot",
            "use --cookies",
            "pass cookies",
            "cookies-from-browser",
            "cookiesfrombrowser",
            "login required",
            "please sign in",
        )
    )


def youtube_cookies_setup_hint() -> str:
    return (
        "YouTube blocked the request (bot check / login required).\n\n"
        "Open Preferences → General → YouTube and either:\n"
        "• set a cookies.txt file exported while logged into YouTube, or\n"
        "• pick Firefox / Edge / Chrome (close that browser first).\n\n"
        "Export guide:\n"
        "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies"
    )


def format_youtube_error_for_user(exc: Exception | None, *, context: str = "playback") -> str:
    if is_youtube_auth_error(exc):
        return youtube_cookies_setup_hint()
    if exc is not None:
        if context == "playlist":
            return f"Could not load YouTube playlist:\n{exc}"
        return f"Could not load YouTube stream:\n{exc}"
    if context == "playlist":
        return "Could not load that YouTube playlist."
    return "Could not load YouTube stream."


def _yt_dlp_extract_info(url: str, base_opts: dict, client_sets: list[list[str]]) -> tuple[dict | None, Exception | None]:
    """Run yt-dlp extract_info with auth + client fallbacks (fail fast on bot-check)."""
    import yt_dlp

    last_exc: Exception | None = None

    for auth_opts, auth_label in _iter_yt_dlp_auth_variants():
        auth_failed = False
        for clients in client_sets:
            attempt_opts = _apply_youtube_auth_to_ydl_opts(dict(base_opts), auth_opts)
            attempt_opts["extractor_args"] = {"youtube": {"player_client": clients}}
            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if isinstance(info, dict):
                    logging.info(
                        "YouTube extract OK (%s, clients=%s): %s",
                        auth_label,
                        clients,
                        url[:80],
                    )
                    return info, None
            except Exception as exc:
                last_exc = exc
                logging.warning(
                    "YouTube extract failed (%s, %s, %s): %s",
                    url[:60],
                    auth_label,
                    clients,
                    exc,
                )
                if _is_browser_cookie_access_error(exc):
                    auth_failed = True
                    break
                if auth_label == "anonymous" and is_youtube_auth_error(exc):
                    auth_failed = True
                    break
        if auth_failed:
            break

    return None, last_exc


def _playlist_permission_error(exc: Exception | None, info: dict | None) -> bool:
    text = str(exc or "").lower()
    if "does not have permission" in text or "private" in text:
        return True
    if "403" in text and "forbidden" in text:
        return True
    if isinstance(info, dict):
        title = (info.get("title") or "").strip().lower()
        if title in ("private video", "private playlist"):
            return True
    return False


def _playlist_fetch_error_message(
    exc: Exception | None,
    *,
    permission_denied: bool,
) -> str:
    if is_youtube_auth_error(exc):
        return format_youtube_error_for_user(exc, context="playlist")
    cookie_hint = (
        "Private or restricted playlists need YouTube login cookies.\n"
        "Set them in Preferences → General → YouTube."
    )
    if permission_denied:
        return (
            "Could not load that YouTube playlist.\n"
            "It appears to be private or restricted.\n\n"
            f"{cookie_hint}"
        )
    if exc is not None:
        return f"{format_youtube_error_for_user(exc, context='playlist')}\n\n{cookie_hint}"
    return (
        "Could not load that YouTube playlist.\n"
        "It may be private, unavailable, or blocked.\n\n"
        f"{cookie_hint}"
    )


def _entry_from_yt_dlp_video_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None

    title = (item.get("title") or "").strip()
    if title in ("[Deleted video]", "[Private video]", "[Unavailable video]"):
        return None

    watch_url = item.get("webpage_url") or item.get("original_url") or item.get("url")
    if watch_url and not is_youtube_watch_url(watch_url):
        watch_url = None

    video_id = item.get("id") if isinstance(item.get("id"), str) else None
    if video_id and len(video_id) != 11:
        video_id = extract_youtube_video_id(str(video_id)) or extract_youtube_video_id(str(watch_url or ""))
    elif not video_id:
        video_id = extract_youtube_video_id(str(watch_url or ""))

    if not video_id and not watch_url:
        return None

    if not watch_url and video_id:
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
    watch_url = normalize_youtube_watch_url(watch_url or "")
    if not watch_url:
        return None

    if not title:
        title = f"YouTube: {video_id or watch_url}"

    thumb_url = (item.get("thumbnail") or "").strip()
    if not thumb_url and video_id:
        thumb_url = youtube_thumbnail_url(video_id)

    entry = {
        "path": watch_url,
        "name": title,
        "is_folder": False,
        "is_remote": True,
        "remote_source": "youtube",
        "thumb_url": thumb_url or youtube_thumbnail_url(video_id or ""),
    }
    duration = item.get("duration")
    if duration is not None:
        try:
            entry["duration"] = float(duration)
        except (TypeError, ValueError):
            pass
    return entry


def fetch_youtube_playlist_entries(
    playlist_url: str,
    *,
    source_url: str | None = None,
) -> tuple[list[dict], str | None, str | None, str | None]:
    """Load playlist metadata for the virtual grid (no stream URLs yet).

    Returns (entries, error_message, warning_message, playlist_title).
    """
    original_url = (playlist_url or "").strip()
    normalized_url = normalize_youtube_playlist_url(original_url)
    if not is_youtube_playlist_url(normalized_url):
        return [], "Not a YouTube playlist URL.", None, None

    try:
        import yt_dlp  # noqa: F401 — availability check
    except ImportError:
        logging.error("yt-dlp is not installed in this Python environment.")
        return [], "yt-dlp is not installed.\nRun: pip install yt-dlp", None, None

    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "socket_timeout": 15,
        "retries": 2,
        "ignoreerrors": True,
    }
    client_sets = [
        ["android", "web"],
        ["web"],
    ]

    urls_to_try = [normalized_url]
    if original_url != normalized_url:
        urls_to_try.append(original_url)
    fallback_source = (source_url or original_url or "").strip()

    info = None
    last_exc: Exception | None = None
    resolved_url = normalized_url

    for try_url in urls_to_try:
        info, last_exc = _yt_dlp_extract_info(try_url, base_opts, client_sets)
        if isinstance(info, dict) and info.get("entries"):
            resolved_url = try_url
            break

    permission_denied = _playlist_permission_error(last_exc, info if isinstance(info, dict) else None)

    if not isinstance(info, dict) or not info.get("entries"):
        fallback_entry = None
        if fallback_source and extract_youtube_video_id(fallback_source):
            fallback_entry = build_youtube_single_video_entry(fallback_source)
        if fallback_entry:
            logging.warning(
                "YouTube playlist unavailable; falling back to single video from %s",
                fallback_source,
            )
            return [fallback_entry], None, (
                "Playlist could not be loaded (private or restricted).\n"
                "Showing only the video from your link.\n"
                "Set YouTube cookies in Preferences → General → YouTube."
            ), None
        return [], _playlist_fetch_error_message(last_exc, permission_denied=permission_denied), None, None

    playlist_title = (info.get("title") or "YouTube Playlist").strip()
    raw_entries = info.get("entries") or []
    entries: list[dict] = []
    for item in raw_entries:
        entry = _entry_from_yt_dlp_video_item(item or {})
        if entry:
            entries.append(entry)

    if not entries:
        fallback_entry = None
        if fallback_source and extract_youtube_video_id(fallback_source):
            fallback_entry = build_youtube_single_video_entry(fallback_source)
        if fallback_entry:
            logging.warning(
                "YouTube playlist had no public entries; using video from %s",
                fallback_source,
            )
            return [fallback_entry], None, (
                "No public videos in that playlist.\n"
                "Showing only the video from your link."
            ), playlist_title
        return [], "No playable videos found in that playlist.", None, playlist_title

    logging.info(
        "YouTube playlist loaded: %s (%d videos) from %s",
        playlist_title,
        len(entries),
        resolved_url,
    )
    return entries, None, None, playlist_title


def build_youtube_single_video_entry(url: str) -> dict | None:
    """Build one virtual-grid item dict for a single YouTube video URL."""
    watch_url = normalize_youtube_watch_url(url.strip())
    if not watch_url:
        return None

    video_id = extract_youtube_video_id(watch_url)
    if not video_id:
        return None

    title = f"YouTube: {video_id}"
    thumb_url = youtube_thumbnail_url(video_id)

    oembed = fetch_youtube_oembed(watch_url)
    if oembed:
        title = (oembed.get("title") or title).strip() or title
        thumb_url = (oembed.get("thumbnail_url") or thumb_url).strip() or thumb_url

    return {
        "path": watch_url,
        "name": title,
        "is_folder": False,
        "is_remote": True,
        "remote_source": "youtube",
        "thumb_url": thumb_url,
    }


def resolve_youtube_playback(watch_url: str) -> tuple[YouTubeStream | None, str | None]:
    """
    Resolve a YouTube watch page to a direct stream URL VLC can play.

    Returns (YouTubeStream, error_message).
    """
    watch_url = normalize_youtube_watch_url((watch_url or "").strip())
    if not watch_url:
        return None, "Invalid YouTube URL."

    try:
        import yt_dlp  # noqa: F401 — availability check
    except ImportError:
        logging.error("yt-dlp is not installed in this Python environment.")
        return None, "yt-dlp is not installed.\nRun: pip install yt-dlp"

    base_opts = {
        "format": (
            "18/b[ext=mp4][acodec!=none][vcodec!=none][height<=1080]/"
            "best[ext=mp4][acodec!=none][vcodec!=none]/best"
        ),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 12,
        "retries": 2,
    }
    client_sets = [["android", "web"], ["web"]]

    info, last_exc = _yt_dlp_extract_info(watch_url, base_opts, client_sets)

    if not isinstance(info, dict):
        exc = last_exc or RuntimeError("YouTube returned no video information.")
        logging.warning("YouTube stream resolve failed for %s: %s", watch_url, exc)
        return None, format_youtube_error_for_user(exc, context="playback")

    playback_url = info.get("url")
    if not playback_url:
        return None, "YouTube stream URL could not be resolved."

    headers = dict(info.get("http_headers") or {})
    headers.setdefault("Referer", "https://www.youtube.com/")
    headers.setdefault("User-Agent", _DEFAULT_YT_USER_AGENT)

    stream = YouTubeStream(
        url=playback_url,
        http_headers=headers,
        title=(info.get("title") or None),
    )
    logging.info(
        "YouTube stream resolved: %s -> %s (headers=%s)",
        watch_url,
        playback_url[:80] + ("..." if len(playback_url) > 80 else ""),
        ", ".join(sorted(headers.keys())),
    )
    return stream, None


def resolve_youtube_playback_url(watch_url: str) -> tuple[str | None, str | None]:
    """Backward-compatible wrapper returning only the playback URL."""
    stream, err = resolve_youtube_playback(watch_url)
    if not stream:
        return None, err
    return stream.url, None


def youtube_thumbnail_cache_paths(
    watch_url: str,
    thumbnail_size: tuple[int, int],
    cache_root: str,
    thumbnail_format: str = "jpg",
) -> tuple[str, str]:
    """Disk cache path for a YouTube grid thumbnail."""
    video_id = extract_youtube_video_id(watch_url)
    if not video_id:
        video_id = hashlib.sha1(watch_url.encode("utf-8")).hexdigest()[:16]
    fmt = (thumbnail_format or "jpg").lower().lstrip(".")
    w, h = int(thumbnail_size[0]), int(thumbnail_size[1])
    cache_dir_path = os.path.join(cache_root, "youtube")
    filename = f"{video_id}_{w}x{h}.{fmt}"
    return cache_dir_path, os.path.join(cache_dir_path, filename)


def load_cached_youtube_thumbnail_pil(
    watch_url: str,
    thumbnail_size: tuple[int, int],
    cache_root: str,
    thumbnail_format: str = "jpg",
) -> Image.Image | None:
    _cache_dir, full_path = youtube_thumbnail_cache_paths(
        watch_url, thumbnail_size, cache_root, thumbnail_format
    )
    if not os.path.isfile(full_path):
        return None
    try:
        with Image.open(full_path) as image:
            return image.copy()
    except Exception as exc:
        logging.debug("YouTube thumbnail cache read failed (%s): %s", full_path, exc)
        return None


def save_youtube_thumbnail_pil(
    watch_url: str,
    image: Image.Image,
    thumbnail_size: tuple[int, int],
    cache_root: str,
    thumbnail_format: str = "jpg",
) -> str | None:
    cache_dir_path, full_path = youtube_thumbnail_cache_paths(
        watch_url, thumbnail_size, cache_root, thumbnail_format
    )
    fmt = (thumbnail_format or "jpg").lower().lstrip(".")
    try:
        os.makedirs(cache_dir_path, exist_ok=True)
        image.convert("RGB").save(full_path, format=fmt.upper())
        return full_path
    except Exception as exc:
        logging.warning("YouTube thumbnail cache write failed (%s): %s", full_path, exc)
        return None


def fetch_youtube_thumbnail_pil(
    watch_url: str,
    thumb_url: str | None,
    thumbnail_size: tuple[int, int],
    cache_root: str,
    *,
    cache_enabled: bool = True,
    overwrite: bool = False,
    thumbnail_format: str = "jpg",
) -> Image.Image | None:
    """Load a YouTube thumbnail from disk cache or download it once."""
    if cache_enabled and not overwrite:
        cached = load_cached_youtube_thumbnail_pil(
            watch_url, thumbnail_size, cache_root, thumbnail_format
        )
        if cached is not None:
            return cached

    video_id = extract_youtube_video_id(watch_url)
    remote_url = (thumb_url or "").strip() or (
        youtube_thumbnail_url(video_id) if video_id else ""
    )
    if not remote_url:
        return None

    pil = download_image_as_pil(remote_url)
    if pil is not None and cache_enabled:
        save_youtube_thumbnail_pil(
            watch_url, pil, thumbnail_size, cache_root, thumbnail_format
        )
    return pil


def download_image_as_pil(url: str, timeout: float = 8.0) -> Image.Image | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = response.read()
        return Image.open(BytesIO(data))
    except Exception as exc:
        logging.warning("Remote thumbnail download failed (%s): %s", url, exc)
        return None
