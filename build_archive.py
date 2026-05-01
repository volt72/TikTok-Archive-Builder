import os
import json
import html
import shutil
import subprocess
import sys
import re
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlretrieve, Request, urlopen
from urllib.parse import urlparse
import hashlib

ROOT = Path(__file__).resolve().parent
LINKS = ROOT / "links.txt"
CONFIG_PATH = ROOT / "config.json"
DELETED_VIDS = ROOT / "deletedvids.txt"
MISSING_VIDEOS = ROOT / "missing_videos.txt"
OUT = ROOT / "archive_out"
VIDEOS_OUT = OUT / "videos"
THUMBS = OUT / "thumbs"
PROFILE = OUT / "profile"
COMMENTS_DIR = OUT / "comments"
COMMENT_IMAGES_OUT = OUT / "comment_images"
SLIDESHOWS_OUT = OUT / "slideshows"
BUILD_ARCHIVE_ONLY = os.environ.get("TIKTOK_ARCHIVE_BUILD_ONLY") == "1"

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav"}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".image"}

_FILE_BYTES_CACHE = {}
_FILE_SHA1_CACHE = {}
_IMAGE_VALID_CACHE = {}
_IMAGE_PIXEL_SCORE_CACHE = {}

def _file_cache_key(path: Path):
    try:
        st = path.stat()
        return (str(path.resolve()).lower(), st.st_mtime_ns, st.st_size)
    except Exception:
        return (str(path).lower(), 0, 0)

def _read_file_bytes_cached(path: Path) -> bytes:
    key = _file_cache_key(path)
    if key not in _FILE_BYTES_CACHE:
        try:
            _FILE_BYTES_CACHE[key] = path.read_bytes()
        except Exception:
            _FILE_BYTES_CACHE[key] = b""
    return _FILE_BYTES_CACHE[key]

def _sha1_for_file(path: Path) -> str:
    key = _file_cache_key(path)
    if key not in _FILE_SHA1_CACHE:
        data = _read_file_bytes_cached(path)
        _FILE_SHA1_CACHE[key] = hashlib.sha1(data).hexdigest() if data else ""
    return _FILE_SHA1_CACHE[key]

def _looks_like_image_path(value):
    raw = str(value or "").strip()
    if not raw:
        return False
    low = raw.lower().split("?")[0].split("#")[0]
    return (
        raw.startswith(("http://", "https://", "data:image/"))
        or Path(low).suffix in IMAGE_EXTS
        or "comment_images" in low
    )

def _real_image_ext_from_bytes(data: bytes, fallback=".jpg"):
    head = data[:32]
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return ".gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if b"ftypavif" in head or b"ftypavis" in head:
        return ".avif"
    return fallback if fallback in IMAGE_EXTS and fallback != ".image" else ".jpg"

def _is_real_image_bytes(data: bytes) -> bool:
    if not data or len(data) < 16:
        return False
    head = data[:32]
    return (
        head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"GIF87a")
        or head.startswith(b"GIF89a")
        or (len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        or b"ftypavif" in head
        or b"ftypavis" in head
    )

def _is_valid_image_file(path: Path) -> bool:
    key = _file_cache_key(path)
    if key in _IMAGE_VALID_CACHE:
        return _IMAGE_VALID_CACHE[key]
    try:
        ok = path.is_file() and _is_real_image_bytes(_read_file_bytes_cached(path)[:64])
    except Exception:
        ok = False
    _IMAGE_VALID_CACHE[key] = ok
    return ok

def _comment_media_object_count(node) -> int:
    """Return how many actual media objects TikTok says a comment has.
    A single TikTok image usually contains many CDN URL variants, but it is still one media object.
    """
    if not isinstance(node, dict):
        return 0
    count = 0
    for key in ("image_list", "comment_media", "media", "images"):
        val = node.get(key)
        if isinstance(val, list):
            count += len([x for x in val if x])
        elif isinstance(val, dict):
            count += 1
        elif isinstance(val, str) and val.strip():
            count += 1
    for key in ("sticker", "image", "image_url"):
        val = node.get(key)
        if isinstance(val, (dict, str)) and val:
            count += 1
    return count

def _pick_best_comment_image_refs(refs, limit=0):
    """Final safety pass for comment images.
    Exact byte duplicates are removed by hash. If TikTok left several size/CDN variants
    for the same comment image, keep the largest local file for that comment.
    """
    cleaned = []
    seen = set()
    for rel in refs or []:
        if not rel:
            continue
        fixed = str(rel).replace("\\", "/")
        if fixed.startswith(("http://", "https://")):
            key = fixed.lower().split("?")[0].split("#")[0]
            if key not in seen:
                seen.add(key)
                cleaned.append((0, fixed))
            continue
        path = OUT / fixed
        try:
            key = _sha1_for_file(path) if path.exists() else Path(fixed).name.lower()
            if not key:
                key = Path(fixed).name.lower()
            # Use actual image resolution, not just file size, so 596x830 beats 372x518.
            score = _image_pixel_score(path) if path.exists() else 0
        except Exception:
            score = 0
            key = Path(fixed).name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append((score, fixed))

    # If the comment only has one TikTok media object, do not render every saved CDN/size variant.
    if limit <= 1 and len(cleaned) > 1:
        cleaned.sort(key=lambda x: x[0], reverse=True)
        return [cleaned[0][1]]
    if limit > 1 and len(cleaned) > limit:
        cleaned.sort(key=lambda x: x[0], reverse=True)
        return [x[1] for x in cleaned[:limit]]
    return [x[1] for x in cleaned]


def _image_dimensions_from_bytes(data: bytes):
    """Return (width, height) for common image formats without extra dependencies."""
    try:
        if not data or len(data) < 24:
            return (0, 0)
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
        if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
            return (int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"))
        if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X" and len(data) >= 30:
                return (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
            if chunk == b"VP8L" and len(data) >= 25:
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                w = 1 + (((b1 & 0x3F) << 8) | b0)
                h = 1 + ((b3 << 6) | (b2 >> 2) | ((b1 & 0xC0) << 6))
                return (w, h)
            if chunk == b"VP8 " and len(data) >= 30:
                idx = data.find(b"\x9d\x01\x2a", 20)
                if idx != -1 and len(data) >= idx + 7:
                    return (int.from_bytes(data[idx+3:idx+5], "little") & 0x3FFF, int.from_bytes(data[idx+5:idx+7], "little") & 0x3FFF)
        if data.startswith(b"\xff\xd8"):
            i = 2
            n = len(data)
            while i + 9 < n:
                while i < n and data[i] != 0xFF:
                    i += 1
                while i < n and data[i] == 0xFF:
                    i += 1
                if i >= n:
                    break
                marker = data[i]
                i += 1
                if marker in {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF}:
                    if i + 7 < n:
                        return (int.from_bytes(data[i+5:i+7], "big"), int.from_bytes(data[i+3:i+5], "big"))
                    break
                if marker in {0xD8,0xD9,0x01} or 0xD0 <= marker <= 0xD7:
                    continue
                if i + 2 > n:
                    break
                seg_len = int.from_bytes(data[i:i+2], "big")
                if seg_len < 2:
                    break
                i += seg_len
    except Exception:
        pass
    return (0, 0)


def _image_pixel_score(path: Path) -> int:
    """Prefer the highest-resolution version of a TikTok comment image."""
    key = _file_cache_key(path)
    if key in _IMAGE_PIXEL_SCORE_CACHE:
        return _IMAGE_PIXEL_SCORE_CACHE[key]
    try:
        data = _read_file_bytes_cached(path)
        w, h = _image_dimensions_from_bytes(data)
        if w and h:
            score = int(w) * int(h)
        else:
            score = path.stat().st_size
    except Exception:
        score = 0
    _IMAGE_PIXEL_SCORE_CACHE[key] = score
    return score

def _real_image_ext_from_url_or_headers(url, content_type=""):
    ext = Path(urlparse(str(url)).path).suffix.lower()
    if ext in IMAGE_EXTS and ext != ".image":
        return ".jpg" if ext == ".jpeg" else ext
    ct = (content_type or "").lower()
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "gif" in ct:
        return ".gif"
    if "avif" in ct:
        return ".avif"
    return ".jpg"

def _normal_image_ext(path_or_url, data=None, content_type=""):
    ext = Path(urlparse(str(path_or_url)).path).suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if data:
        return _real_image_ext_from_bytes(data, _real_image_ext_from_url_or_headers(str(path_or_url), content_type))
    if ext in IMAGE_EXTS and ext != ".image":
        return ext
    return _real_image_ext_from_url_or_headers(str(path_or_url), content_type)

def _comment_media_ext(value):
    raw = str(value or "").strip()
    if raw.startswith("data:image/"):
        return "data"
    low = raw.lower().split("?")[0].split("#")[0]
    ext = Path(low).suffix.lower().lstrip(".")
    if ext == "jpeg":
        return "jpg"
    if ext in {"jpg", "png", "webp", "gif", "avif"}:
        return ext
    # .image is not a real browser image extension. It is only a raw TikTok/media placeholder.
    # The image copier detects the real file type and rewrites it to .jpg/.png/.webp/etc.
    return ""

def _collect_comment_media_paths(value, paths=None):
    if paths is None:
        paths = []
    if not value:
        return paths
    if isinstance(value, str):
        fixed = value.replace("\\", "/")
        if _looks_like_image_path(fixed) and fixed not in paths:
            paths.append(fixed)
        return paths
    if isinstance(value, list):
        for item in value:
            _collect_comment_media_paths(item, paths)
        return paths
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "downloaded_images", "downloaded_image", "local_images", "local_image",
                "image", "image_url", "image_urls", "image_list", "image_path", "image_paths",
                "sticker", "comment_media", "media", "url", "url_list", "display_url",
                "download_url", "web_uri", "uri"
            } or isinstance(item, (dict, list)):
                _collect_comment_media_paths(item, paths)
        return paths
    return paths

def _add_ref_once(refs, value):
    fixed = str(value or "").replace("\\", "/").strip()
    if not fixed:
        return
    low = fixed.lower()
    if (
        fixed.startswith(("http://", "https://", "data:image/"))
        or "comment_images" in low
        or Path(low.split("?")[0].split("#")[0]).suffix in IMAGE_EXTS
    ):
        if fixed not in refs:
            refs.append(fixed)

def _best_image_url_from_list(items):
    """TikTok often stores the same comment image as many URL variants.
    Pick one URL per image object so the HTML does not show duplicates.
    """
    if not isinstance(items, list):
        return ""
    urls = []
    for item in items:
        if isinstance(item, str):
            low = item.lower()
            if item.startswith(("http://", "https://")) and not any(x in low for x in ("avatar", "music", "cover", "thumb")):
                urls.append(item)
    if not urls:
        return ""
    # Prefer TikTok's first URL. The remaining items are usually mirrors or size variants.
    return urls[0]

def _collect_potential_image_refs(value, refs=None):
    """Collect one usable image per nested TikTok comment-media object.
    Avoid collecting every url_list/display_url variant, which caused duplicate comment images.
    """
    if refs is None:
        refs = []
    if not value:
        return refs
    if isinstance(value, str):
        _add_ref_once(refs, value)
        return refs
    if isinstance(value, list):
        # A bare list of URL strings is usually one TikTok image with multiple CDN variants.
        if value and all(isinstance(x, str) for x in value):
            best = _best_image_url_from_list(value)
            if best:
                _add_ref_once(refs, best)
            return refs
        for item in value:
            _collect_potential_image_refs(item, refs)
        return refs
    if isinstance(value, dict):
        # Common TikTok shapes: {url_list:[...]} or {url:{url_list:[...]}}.
        # Use one URL from that object and do not also traverse sibling mirrors.
        if isinstance(value.get("url_list"), list):
            best = _best_image_url_from_list(value.get("url_list"))
            if best:
                _add_ref_once(refs, best)
                return refs
        if isinstance(value.get("url"), dict) and isinstance(value["url"].get("url_list"), list):
            best = _best_image_url_from_list(value["url"].get("url_list"))
            if best:
                _add_ref_once(refs, best)
                return refs
        # Local paths created by this builder are already canonical, so keep those only if present.
        if "downloaded_images" in value or "local_images" in value:
            for key in ("downloaded_images", "downloaded_image", "local_images", "local_image", "image_path", "image_paths"):
                if key in value:
                    _collect_potential_image_refs(value.get(key), refs)
            return refs
        for key, item in value.items():
            if key in {
                "image", "image_url", "image_urls", "image_list",
                "sticker", "comment_media", "media", "url", "display_url",
                "download_url", "web_uri", "uri"
            } or isinstance(item, (dict, list)):
                _collect_potential_image_refs(item, refs)
        return refs
    return refs
def comment_payload_media_exts(payload):
    exts = set()
    for path in _collect_comment_media_paths(payload):
        ext = _comment_media_ext(path)
        if ext:
            exts.add(ext)
    return sorted(exts)

def comment_payload_search_text(payload):
    parts = []
    def walk(value):
        if isinstance(value, dict):
            for key in ("user", "username", "nickname", "text", "comment"):
                v = value.get(key)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
            for reply_key in ("comments", "replies", "reply_comment"):
                rv = value.get(reply_key)
                if isinstance(rv, list):
                    walk(rv)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(payload)
    return " ".join(parts).lower()


def load_config():
    if not CONFIG_PATH.exists():
        fail("config.json was not found.")
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        fail(f"config.json is invalid: {e}")


def fail(msg: str):
    print(msg)
    print("")
    sys.exit(1)

def esc(x):
    return html.escape(str(x or ""))

def fmt_num(n):
    if n is None or n == "":
        return ""
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_date(s):
    if not s or len(s) != 8:
        return ""
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

def upload_date_from_timestamp(value):
    try:
        ts = int(value)
        if ts <= 0:
            return ""
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
    except Exception:
        return ""

def sort_key_for_video(vid, upload_date="", timestamp=None):
    date = upload_date or upload_date_from_timestamp(timestamp)
    try:
        vid_num = int(re.sub(r"\D", "", str(vid)) or "0")
    except Exception:
        vid_num = 0
    return f"{date or '00000000'}{vid_num:024d}"

def browser_can_play(path_or_name):
    return Path(str(path_or_name)).suffix.lower() in {".mp4", ".m4v", ".webm"}

def launch_chromium_browser(playwright, **kwargs):
    """Launch Chromium, preferring installed Chrome/Edge for smaller packaged builds."""
    errors = []
    for channel in ("chrome", "msedge"):
        try:
            return playwright.chromium.launch(channel=channel, **kwargs)
        except Exception as e:
            errors.append(f"{channel}: {e}")
    try:
        return playwright.chromium.launch(**kwargs)
    except Exception as e:
        errors.append(f"bundled chromium: {e}")
    raise RuntimeError("Could not launch Chrome/Edge/Chromium. " + " | ".join(errors[-3:]))

def metadata_exists():
    return any(THUMBS.glob("*.info.json"))

def truthy_file(path: Path):
    if not path.exists():
        return False
    txt = path.read_text(encoding="utf-8", errors="ignore").strip().lower()
    return txt in {"1", "true", "yes", "y", "refresh"}

def extract_id_from_name(name: str):
    m = re.search(r"\[(\d+)\]", name)
    if m:
        return m.group(1)
    m = re.search(r"(\d{8,})", Path(name).stem)
    if m:
        return m.group(1)
    return ""

def safe_video_dest(vid: str, source_name: str):
    ext = Path(source_name).suffix.lower()
    if ext not in VIDEO_EXTS:
        ext = ".mp4"
    return VIDEOS_OUT / f"{vid}{ext}"

def _copy_comment_image_to_archive(vid: str, image_path, comment_id=None):
    """Copy or download a comment image into archive_out/comment_images/<video_id>.

    If comment_id is supplied, keep that ID in the output filename so future
    HTML builds can attach the image back to the exact comment/reply.
    """
    if not image_path:
        return ""

    raw = str(image_path).strip()
    if not raw:
        return ""

    if raw.startswith("data:image/"):
        return raw

    dest_dir = COMMENT_IMAGES_OUT / vid
    dest_dir.mkdir(parents=True, exist_ok=True)
    clean_comment_id = str(comment_id or "").strip()
    if not re.fullmatch(r"\d{15,25}", clean_comment_id):
        clean_comment_id = ""

    if raw.startswith(("http://", "https://")):
        try:
            req = Request(raw, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=25) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "")
            ext = _normal_image_ext(raw, data, content_type)
            # Use the downloaded bytes for the name so duplicate TikTok CDN URLs/variants
            # collapse into one local image instead of rendering the same photo many times.
            digest = hashlib.sha1(data).hexdigest()[:16]
            name = f"{clean_comment_id}_{digest}{ext}" if clean_comment_id else f"{digest}{ext}"
            dest = dest_dir / name
            if not dest.exists():
                dest.write_bytes(data)
            return f"comment_images/{vid}/{dest.name}"
        except Exception:
            return raw.replace("\\", "/")

    candidates = []
    p = Path(raw)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.extend([
            ROOT / raw,
            OUT / raw,
            ROOT / "import_comments" / raw,
            ROOT / "data" / raw,
            ROOT / "data" / "comment_images" / vid / p.name,
            ROOT / "comment_images" / vid / p.name,
            ROOT / "import_comments" / "comment_images" / vid / p.name,
            ROOT.parent / raw,
            ROOT.parent / "import_comments" / raw,
            ROOT.parent / "data" / raw,
            ROOT.parent / "comment_images" / vid / p.name,
            ROOT.parent / "import_comments" / "comment_images" / vid / p.name,
        ])

    src = next((c for c in candidates if c.exists() and c.is_file()), None)
    if not src:
        return raw.replace("\\", "/")

    data = b""
    try:
        data = _read_file_bytes_cached(src)
    except Exception:
        pass

    ext = _normal_image_ext(src.name, data)
    if data:
        digest = (_sha1_for_file(src) or hashlib.sha1(data).hexdigest())[:16]
        stem = f"{clean_comment_id}_{digest}" if clean_comment_id else digest
    else:
        base_stem = src.stem if src.suffix.lower() != ".image" else src.with_suffix("").name
        stem = f"{clean_comment_id}_{base_stem}" if clean_comment_id and not base_stem.startswith(clean_comment_id) else base_stem
    dest = dest_dir / f"{stem}{ext}"
    if src.resolve() != dest.resolve() and not dest.exists():
        try:
            if data:
                dest.write_bytes(data)
            else:
                shutil.copy2(src, dest)
        except Exception:
            return raw.replace("\\", "/")

    return f"comment_images/{vid}/{dest.name}"


def _looks_like_slideshow_url(value):
    raw = str(value or "").strip()
    if not raw or not raw.startswith(("http://", "https://")):
        return False
    low = raw.lower()
    if any(blocked in low for blocked in ("avatar", "music", "audio", "mp3", "m4a", "video", "playwm", "play_addr")):
        return False
    return (
        Path(urlparse(raw).path).suffix.lower() in IMAGE_EXTS
        or any(token in low for token in ("tos-maliva-p", "tos-useast", "image", "photo", "img", "p16", "p19", "p21", "tiktokcdn"))
    )


SLIDESHOW_BLOCKED_KEY_TOKENS = (
    "avatar", "author", "user", "music", "audio", "sound", "cover",
    "thumbnail", "thumb", "animatedcover", "dynamiccover", "video", "playaddr", "play_addr"
)

SLIDESHOW_CONTEXT_KEYS = {
    "images", "image", "imagelist", "image_list", "imagepost", "imagepostinfo",
    "image_post", "image_post_info", "photoimages", "photo_images", "photos",
    "slideshow", "slides", "displayimage", "display_image", "originimage",
    "origin_image", "downloadimage", "download_image", "url_list", "urllist",
    "url", "uri", "web_uri", "weburi", "download_url", "downloadurl",
    "display_url", "displayurl"
}

def _is_blocked_slideshow_key(key) -> bool:
    key_l = str(key or "").lower()
    return any(token in key_l for token in SLIDESHOW_BLOCKED_KEY_TOKENS)

def _collect_slideshow_image_refs(node, refs=None, in_image_context=False):
    """Collect only real photo-mode/slideshow image URLs.

    This intentionally skips avatar/profile images, music cover art, thumbnails, and video cover fields.
    Those fields often appear in the same TikTok JSON blob and were causing fake slideshow images.
    """
    if refs is None:
        refs = []
    if not node:
        return refs
    if isinstance(node, str):
        fixed = node.replace("\\", "/").strip()
        if in_image_context and _looks_like_slideshow_url(fixed) and fixed not in refs:
            refs.append(fixed)
        return refs
    if isinstance(node, list):
        for item in node:
            _collect_slideshow_image_refs(item, refs, in_image_context)
        return refs
    if isinstance(node, dict):
        for key, item in node.items():
            key_l = str(key).lower()
            if _is_blocked_slideshow_key(key_l):
                continue
            image_context = in_image_context or key_l in SLIDESHOW_CONTEXT_KEYS
            _collect_slideshow_image_refs(item, refs, image_context)
        return refs
    return refs

def _node_mentions_vid(node, vid: str) -> bool:
    if not vid:
        return False
    if isinstance(node, str):
        return node == vid or f"/{vid}" in node
    if isinstance(node, list):
        return any(_node_mentions_vid(x, vid) for x in node[:30])
    if isinstance(node, dict):
        for key in ("id", "aweme_id", "awemeId", "item_id", "itemId", "video_id", "videoId"):
            if str(node.get(key, "")) == str(vid):
                return True
        return any(_node_mentions_vid(v, vid) for v in list(node.values())[:80])
    return False


def _collect_slideshow_refs_for_vid(node, vid: str, refs=None):
    if refs is None:
        refs = []
    if not node:
        return refs
    if isinstance(node, dict):
        key_blob = " ".join(str(k).lower() for k in node.keys())
        has_photo_keys = any(k in key_blob for k in ("imagepost", "image_post", "photo", "slideshow", "images"))
        if has_photo_keys and _node_mentions_vid(node, vid):
            _collect_slideshow_image_refs(node, refs, True)
        for item in node.values():
            _collect_slideshow_refs_for_vid(item, vid, refs)
    elif isinstance(node, list):
        for item in node:
            _collect_slideshow_refs_for_vid(item, vid, refs)
    return refs


def _extract_json_from_tiktok_html(page_html: str):
    blobs = []
    # TikTok commonly stores page state in JSON script tags. The ids have changed over time,
    # so collect any valid JSON scripts and let the photo-post extractor find the matching item id.
    for m in re.finditer(r'<script[^>]*>(.*?)</script>', page_html, flags=re.S | re.I):
        body = html.unescape(m.group(1).strip())
        if not body or not (body.startswith("{") or body.startswith("[")):
            continue
        if "imagePost" not in body and "image_post" not in body and "aweme" not in body and "ItemModule" not in body:
            continue
        try:
            blobs.append(json.loads(body))
        except Exception:
            continue
    return blobs


def _fetch_slideshow_refs_from_webpage(url: str, vid: str):
    refs = []
    if not url:
        return refs

    # Fast path: try a normal HTTP fetch first.
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urlopen(req, timeout=20) as resp:
            page_html = resp.read().decode("utf-8", "ignore")
        for blob in _extract_json_from_tiktok_html(page_html):
            _collect_slideshow_refs_for_vid(blob, vid, refs)
    except Exception as e:
        print(f"Could not fetch slideshow page with urllib for {vid}: {e}")

    if refs:
        return refs

    # Fallback: use Playwright if it is installed. This helps when TikTok only hydrates the photo data in-browser.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = launch_chromium_browser(p, headless=True)
            page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_timeout(3500)
            except Exception:
                pass
            page_html = page.content()
            browser.close()
        for blob in _extract_json_from_tiktok_html(page_html):
            _collect_slideshow_refs_for_vid(blob, vid, refs)
    except Exception as e:
        print(f"Could not fetch slideshow page with Playwright for {vid}: {e}")
    return refs


def _slideshow_ref_key(raw: str) -> str:
    parsed = urlparse(raw)
    path = parsed.path or raw
    # TikTok CDN variants often differ only by signed query params and tplv transforms.
    path = re.sub(r"~tplv-[^/]+", "", path)
    path = re.sub(r"_(?:720|1080|1440|origin|large|medium|small)(?=\.|$)", "", path, flags=re.I)
    return path or raw

def _dedupe_slideshow_refs(refs):
    cleaned = []
    seen = set()
    for ref in refs or []:
        raw = str(ref or "").strip()
        if not raw or not _looks_like_slideshow_url(raw):
            continue
        key = _slideshow_ref_key(raw)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(raw)
    return cleaned

def _dedupe_slideshow_files(vid: str):
    """Remove exact duplicate slideshow files after download and return archive paths."""
    dest_dir = SLIDESHOWS_OUT / vid
    if not dest_dir.exists():
        return []
    seen_hashes = set()
    kept = []
    for p in sorted(dest_dir.iterdir(), key=lambda x: x.name):
        if not p.is_file() or p.suffix.lower() == ".image" or not _is_valid_image_file(p):
            try:
                p.unlink()
            except Exception:
                pass
            continue
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        except Exception:
            continue
        if digest in seen_hashes:
            try:
                p.unlink()
            except Exception:
                pass
            continue
        seen_hashes.add(digest)
        kept.append(f"slideshows/{vid}/{p.name}")
    return kept

def _download_or_copy_slideshow_image(vid: str, image_ref, index: int):
    raw = str(image_ref or "").strip()
    if not raw:
        return ""

    dest_dir = SLIDESHOWS_OUT / vid
    dest_dir.mkdir(parents=True, exist_ok=True)

    if raw.startswith(("http://", "https://")):
        try:
            req = Request(raw, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.tiktok.com/"})
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "")
            if not _is_real_image_bytes(data):
                print(f"Skipped slideshow image {index} for {vid}: downloaded data was not an image")
                return ""
            ext = _normal_image_ext(raw, data, content_type)
            dest = dest_dir / f"{index:02d}{ext}"
            if not dest.exists() or not _is_valid_image_file(dest):
                dest.write_bytes(data)
            return f"slideshows/{vid}/{dest.name}"
        except Exception as e:
            print(f"Could not download slideshow image {index} for {vid}: {e}")
            return ""

    src = Path(raw)
    candidates = [src] if src.is_absolute() else [ROOT / raw, OUT / raw, ROOT.parent / raw]
    src = next((c for c in candidates if c.exists() and c.is_file()), None)
    if not src:
        return ""
    try:
        data = src.read_bytes()
    except Exception:
        data = b""
    if data and not _is_real_image_bytes(data):
        print(f"Skipped slideshow image {index} for {vid}: local file was not a real image ({src.name})")
        return ""
    ext = _normal_image_ext(src.name, data)
    dest = dest_dir / f"{index:02d}{ext}"
    try:
        if data:
            dest.write_bytes(data)
        else:
            shutil.copy2(src, dest)
        return f"slideshows/{vid}/{dest.name}"
    except Exception:
        return ""


def materialize_slideshow_images(vid: str, metadata: dict):
    """Save TikTok photo-mode images locally and return archive-relative paths.

    Important: do not re-download/rebuild slideshow folders every archive build.
    Older versions rebuilt these folders every time, which made the app look
    stuck after "Refreshing root and archive video lists..." while it silently
    re-downloaded hundreds of slideshow images that were already archived.
    """
    existing_dir = SLIDESHOWS_OUT / vid

    # Fast path: if this slideshow already has valid images in archive_out,
    # reuse them. This prevents long hangs/re-downloads on every Build Archive.
    if existing_dir.exists():
        existing_rels = _dedupe_slideshow_files(vid)
        if existing_rels:
            return existing_rels

    # Build Archive Only should never fetch TikTok pages or re-download slideshow images.
    # It should only rebuild index.html from files that already exist locally.
    if BUILD_ARCHIVE_ONLY:
        return []

    refs = _collect_slideshow_image_refs(metadata)
    refs = _dedupe_slideshow_refs(refs)

    # If yt-dlp only gave one poster/thumb image, go back to the TikTok page and try to extract the full photo list.
    webpage_url = metadata.get("webpage_url") or metadata.get("original_url") or metadata.get("url")
    if len(refs) <= 1 and webpage_url:
        page_refs = _fetch_slideshow_refs_from_webpage(webpage_url, vid)
        refs = _dedupe_slideshow_refs(page_refs + refs)

    if not refs:
        return []

    print(f"Slideshow/photo post detected for {vid}. Downloading {len(refs)} candidate image(s)...")
    for idx, ref in enumerate(refs, start=1):
        _download_or_copy_slideshow_image(vid, ref, idx)

    rels = _dedupe_slideshow_files(vid)
    if len(rels) != len(refs):
        print(f"Kept {len(rels)} real slideshow image(s) for {vid} after filtering avatars/music covers/duplicates.")
    return rels

def build_audio_map(folder: Path):
    result = {}
    if not folder.exists():
        return result
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            vid = extract_id_from_name(p.name)
            if vid and vid not in result:
                result[vid] = p
    return result

def build_slideshow_map(folder: Path):
    result = {}
    if not folder.exists():
        return result
    for p in folder.iterdir():
        if p.is_dir() and re.fullmatch(r"\d{8,}", p.name):
            if any(child.is_file() and _is_valid_image_file(child) for child in p.iterdir()):
                result[p.name] = p
    return result



def _comment_image_source_dirs(vid: str):
    """Every place comment scraper images may exist before HTML build.

    Older versions saved comment images in different layouts. Some files are
    under comment_images/<video_id>/, some are flat in comment_images/, and some
    are under import_comments/. We scan both the exact video folder and those
    broad parent folders, then attach by comment_id.
    """
    candidates = [
        COMMENT_IMAGES_OUT / vid,
        COMMENT_IMAGES_OUT,
        ROOT / "comment_images" / vid,
        ROOT / "comment_images",
        ROOT / "data" / "comment_images" / vid,
        ROOT / "data" / "comment_images",
        ROOT / "import_comments" / "comment_images" / vid,
        ROOT / "import_comments" / "comment_images",
        ROOT / "import_comments" / vid,
        ROOT / "import_comments",
        ROOT.parent / "comment_images" / vid,
        ROOT.parent / "comment_images",
        ROOT.parent / "data" / "comment_images" / vid,
        ROOT.parent / "data" / "comment_images",
        ROOT.parent / "import_comments" / "comment_images" / vid,
        ROOT.parent / "import_comments" / "comment_images",
        ROOT.parent / "import_comments" / vid,
        ROOT.parent / "import_comments",
    ]
    out = []
    seen = set()
    for c in candidates:
        try:
            key = str(c.resolve()).lower()
        except Exception:
            key = str(c).lower()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out

_COMMENT_IMAGE_INDEX_CACHE = {}
_COMMENT_IMAGE_GLOBAL_INDEX_CACHE = None

def _comment_image_global_source_roots():
    """Broad roots to scan once for all downloaded comment images."""
    candidates = [
        COMMENT_IMAGES_OUT,
        ROOT / "comment_images",
        ROOT / "data" / "comment_images",
        ROOT / "import_comments" / "comment_images",
        ROOT / "import_comments",
        ROOT.parent / "comment_images",
        ROOT.parent / "data" / "comment_images",
        ROOT.parent / "import_comments" / "comment_images",
        ROOT.parent / "import_comments",
    ]
    out = []
    seen = set()
    for c in candidates:
        try:
            key = str(c.resolve()).lower()
        except Exception:
            key = str(c).lower()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out

def _infer_vid_from_comment_image_path(path: Path):
    """Infer video id from folder layout, e.g. comment_images/<video_id>/<comment_id>_1.jpg."""
    for part in reversed(path.parts[:-1]):
        if re.fullmatch(r"\d{15,25}", str(part)):
            return str(part)
    return None

def _build_global_comment_image_index():
    """Build a one-time map of downloaded comment images.

    Returns {"by_vid": {video_id: {comment_id: [Path]}}, "by_cid": {comment_id: [Path]}}
    """
    global _COMMENT_IMAGE_GLOBAL_INDEX_CACHE
    if _COMMENT_IMAGE_GLOBAL_INDEX_CACHE is not None:
        return _COMMENT_IMAGE_GLOBAL_INDEX_CACHE

    print("Indexing downloaded comment images once...")
    by_vid = {}
    by_cid = {}
    seen_paths = set()

    for base in _comment_image_global_source_roots():
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in IMAGE_EXTS and path.suffix.lower() != ".image":
                continue
            try:
                key_path = str(path.resolve()).lower()
            except Exception:
                key_path = str(path).lower()
            if key_path in seen_paths:
                continue
            seen_paths.add(key_path)

            m = re.match(r"^(\d{15,25})(?:[_\-.].*)?$", path.stem)
            if not m:
                m = re.match(r"^(\d{15,25})$", path.parent.name)
            if not m:
                continue
            cid = m.group(1)

            try:
                data = _read_file_bytes_cached(path)
                if not _is_real_image_bytes(data):
                    continue
            except Exception:
                continue

            by_cid.setdefault(cid, []).append(path)
            vid = _infer_vid_from_comment_image_path(path)
            if vid and vid != cid:
                by_vid.setdefault(vid, {}).setdefault(cid, []).append(path)

    _COMMENT_IMAGE_GLOBAL_INDEX_CACHE = {"by_vid": by_vid, "by_cid": by_cid}
    total_groups = len(by_cid)
    total_files = sum(len(v) for v in by_cid.values())
    print(f"Indexed {total_files} downloaded comment image file(s) across {total_groups} comment id group(s).")
    return _COMMENT_IMAGE_GLOBAL_INDEX_CACHE

def _build_comment_image_disk_index(vid: str):
    """Map comment_id -> local image file paths. Uses the one-time global disk index."""
    if vid in _COMMENT_IMAGE_INDEX_CACHE:
        return _COMMENT_IMAGE_INDEX_CACHE[vid]

    global_index = _build_global_comment_image_index()
    index = {}

    # Best match: images inside this video's folder.
    for cid, paths in global_index.get("by_vid", {}).get(str(vid), {}).items():
        index.setdefault(cid, []).extend(paths)

    # Safety fallback: older builds sometimes stored images flat. Reuse cid-only map.
    for cid, paths in global_index.get("by_cid", {}).items():
        dest = index.setdefault(cid, [])
        for path in paths:
            if path not in dest:
                dest.append(path)

    _COMMENT_IMAGE_INDEX_CACHE[vid] = index
    return index

def _existing_comment_images_for_node(vid: str, node):
    """Find already-downloaded local images for one comment/reply by comment_id.

    Fast path: uses the one-time global comment image index. No per-comment rglob.
    """
    if not isinstance(node, dict):
        return []
    comment_id = str(node.get("comment_id") or node.get("cid") or node.get("id") or "").strip()
    if not comment_id:
        return []

    refs = []
    seen_hashes = set()

    for path in _build_comment_image_disk_index(vid).get(comment_id, []):
        try:
            data = _read_file_bytes_cached(path)
            if not _is_real_image_bytes(data):
                continue
            h = _sha1_for_file(path) or hashlib.sha1(data).hexdigest()
        except Exception:
            h = str(path).lower()

        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        rel = _copy_comment_image_to_archive(vid, str(path), comment_id)
        if rel and not rel.startswith(("http://", "https://")) and rel not in refs:
            refs.append(rel)

    return refs

def _attach_comment_images_from_anywhere(vid: str, node):
    """Compatibility wrapper.

    The previous version recursively scanned the filesystem for every comment,
    which made Build Archive Only take forever. The global index now handles
    this, so this function simply returns the indexed local refs.
    """
    return _existing_comment_images_for_node(vid, node)

def _prepare_comment_media_node(vid: str, node):
    if isinstance(node, dict):
        is_comment_like = any(k in node for k in ("comment_id", "cid", "id", "text", "comment", "username", "user"))

        if is_comment_like:
            comment_id = str(node.get("comment_id") or node.get("cid") or node.get("id") or "").strip()
            # Prefer local downloaded files that match THIS exact comment_id. This keeps
            # comment images attached to the right comment/reply and works offline even
            # when TikTok CDN URLs expire.
            local_refs = _existing_comment_images_for_node(vid, node)

            raw_refs = []
            primary_keys = ("image_list", "sticker", "comment_media", "media", "media_url", "media_urls", "image", "image_url", "image_urls")
            fallback_keys = ("downloaded_images", "downloaded_image", "local_image", "local_images", "image_path", "image_paths", "comment_images", "comment_image", "images")

            for key in primary_keys:
                if key in node:
                    _collect_potential_image_refs(node.get(key), raw_refs)

            if not raw_refs:
                for key in fallback_keys:
                    if key in node:
                        _collect_potential_image_refs(node.get(key), raw_refs)

            # Use every trustworthy source, then dedupe at the very end.
            # This keeps the local comment_id fallback that fixed missing images,
            # while still preventing duplicate CDN variants from rendering.
            candidate_refs = []
            for item in raw_refs:
                if item not in candidate_refs:
                    candidate_refs.append(item)
            for item in local_refs:
                if item not in candidate_refs:
                    candidate_refs.append(item)
            if not candidate_refs:
                for key in fallback_keys:
                    if key in node:
                        _collect_potential_image_refs(node.get(key), candidate_refs)

            expected_count = _comment_media_object_count(node)
            if expected_count <= 0 and candidate_refs:
                expected_count = 1
            fixed = []
            seen = set()
            for item in candidate_refs:
                rel = _copy_comment_image_to_archive(vid, item, comment_id)
                if rel and not rel.startswith(("http://", "https://")):
                    try:
                        data_key = _sha1_for_file(OUT / rel)
                        if not data_key:
                            data_key = Path(str(rel).split("?")[0].split("#")[0]).stem.lower()
                    except Exception:
                        data_key = Path(str(rel).split("?")[0].split("#")[0]).stem.lower()
                    if data_key not in seen:
                        fixed.append(rel)
                        seen.add(data_key)

            # If CDN refs failed or were expired, fall back to already-downloaded local files.
            if not fixed:
                for item in local_refs or _existing_comment_images_for_node(vid, node):
                    rel = _copy_comment_image_to_archive(vid, item, comment_id)
                    if rel and not rel.startswith(("http://", "https://")):
                        try:
                            data_key = _sha1_for_file(OUT / rel)
                            if not data_key:
                                data_key = Path(str(rel).split("?")[0].split("#")[0]).stem.lower()
                        except Exception:
                            data_key = Path(str(rel).split("?")[0].split("#")[0]).stem.lower()
                        if data_key not in seen:
                            fixed.append(rel)
                            seen.add(data_key)

            fixed = _pick_best_comment_image_refs(fixed, expected_count)
            if fixed:
                node["downloaded_images"] = fixed

        for value in list(node.values()):
            _prepare_comment_media_node(vid, value)
    elif isinstance(node, list):
        for item in node:
            _prepare_comment_media_node(vid, item)

def load_comments_json(vid: str):
    path = COMMENTS_DIR / f"{vid}.json"
    if not path.exists():
        return {"id": vid, "comments": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            payload = {"id": vid, "comments": data}
        elif isinstance(data, dict):
            payload = {"id": data.get("id", vid), "comments": data.get("comments", [])}
        else:
            payload = {"id": vid, "comments": []}
        _prepare_comment_media_node(vid, payload)
        return payload
    except Exception:
        pass
    return {"id": vid, "comments": []}

def links_by_video_id(lines):
    result = {}
    for line in lines:
        m = re.search(r"/video/(\d+)", line)
        if m:
            result[m.group(1)] = line.strip()
    return result



def run_yt_dlp_url_file(urls, extra_args, check=False):
    """Run yt-dlp with a temporary URL batch file.

    This avoids Windows [WinError 206] when many TikTok URLs make the
    command line too long.
    """
    if not urls:
        return subprocess.CompletedProcess(["yt-dlp"], 0)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix="_yt_dlp_urls.txt",
            delete=False,
            dir=str(ROOT),
        ) as tmp:
            tmp_path = Path(tmp.name)
            for url in urls:
                if str(url).strip():
                    tmp.write(str(url).strip() + "\n")

        return subprocess.run(["yt-dlp", "--batch-file", str(tmp_path), *extra_args], check=check)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

def fetch_metadata_for_urls(urls):
    if not urls:
        return
    print(f"Fetching metadata/thumbnails for {len(urls)} new or missing videos...")
    try:
        result = run_yt_dlp_url_file(urls, [
            "--cookies-from-browser", COOKIES_BROWSER,
            "--ignore-errors",
            "--skip-download",
            "--write-info-json",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "-o", str(THUMBS / "%(id)s.%(ext)s"),
        ], check=False)
        if result.returncode != 0:
            print("Some metadata fetches failed, but the build will continue.")
            print("This can happen when TikTok blocks an individual video URL or yt-dlp cannot see formats for that post.")
    except FileNotFoundError:
        fail("yt-dlp was not found. Install yt-dlp first and make sure it is in PATH.")

def extract_video_ids_from_lines(lines):
    ids = []
    seen = set()
    for line in lines:
        m = re.search(r"/video/(\d+)", line)
        if m:
            vid = m.group(1)
            if vid not in seen:
                ids.append(vid)
                seen.add(vid)
    return ids

def extract_video_id(value):
    """Return a TikTok video/photo ID from a URL or filename-like string."""
    text = str(value or "")
    m = re.search(r"/(?:video|photo)/(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"\[(\d{15,25})\]", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|[^0-9])(\d{15,25})(?:[^0-9]|$)", text)
    return m.group(1) if m else None

def refresh_links_file(profile_url):
    print(f"Checking links from: {profile_url}")
    try:
        result = subprocess.run([
            "yt-dlp",
            profile_url,
            "--cookies-from-browser", COOKIES_BROWSER,
            "--flat-playlist",
            "--print", "%(webpage_url)s",
        ], capture_output=True, text=True, check=True)
    except FileNotFoundError:
        fail("yt-dlp was not found. Install yt-dlp first and make sure it is in PATH.")
    except subprocess.CalledProcessError as e:
        print(e.stdout or "")
        print(e.stderr or "")
        fail("yt-dlp failed while checking links.")

    new_links = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    new_ids = extract_video_ids_from_lines(new_links)

    if not new_links or not new_ids:
        fail("yt-dlp did not return any valid video links.")

    old_links = []
    if LINKS.exists():
        old_links = [line.strip() for line in LINKS.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
    old_ids = extract_video_ids_from_lines(old_links)

    if old_ids == new_ids:
        print(f"No link changes found. Reusing existing links.txt with {len(old_ids)} videos.")
        return False

    old_link_map = links_by_video_id(old_links)
    added = [vid for vid in new_ids if vid not in set(old_ids)]
    removed = [vid for vid in old_ids if vid not in set(new_ids)]

    if removed:
        removed_links = [old_link_map.get(vid, "") for vid in removed]
        removed_links = [link for link in removed_links if link]

        existing_deleted = []
        if DELETED_VIDS.exists():
            existing_deleted = [
                line.strip()
                for line in DELETED_VIDS.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip()
            ]

        combined = []
        seen_deleted = set()
        for link in existing_deleted + removed_links:
            if link not in seen_deleted:
                combined.append(link)
                seen_deleted.add(link)

        DELETED_VIDS.write_text("\n".join(combined) + "\n", encoding="utf-8")
        print(f"Videos no longer listed: {len(removed)}")
        print(f"Wrote removed links to: {DELETED_VIDS}")

    LINKS.write_text("\n".join(new_links) + "\n", encoding="utf-8")
    print(f"Updated links.txt with {len(new_ids)} videos.")
    if added:
        print(f"New videos found: {len(added)}")
    return True

def profile_url_from_config(config):
    raw = str(config.get("profile_url", "")).strip()
    if not raw:
        return "", ""
    m = re.search(r"https?://(?:www\.)?tiktok\.com/@([^/?#]+)", raw)
    if m:
        handle = m.group(1)
        return f"https://www.tiktok.com/@{handle}", handle
    raw = raw.lstrip("@").strip()
    if raw:
        return f"https://www.tiktok.com/@{raw}", raw
    return "", ""

def profile_url_from_links(links):
    for line in links:
        m = re.search(r"https?://(?:www\.)?tiktok\.com/@([^/?#]+)", line)
        if m:
            return f"https://www.tiktok.com/@{m.group(1)}", m.group(1)
    return "", ""

def clean_count(text):
    if not text:
        return ""
    text = str(text).strip().replace(",", "")
    m = re.search(r"([\d.]+)\s*([KMB]?)", text, re.I)
    if not m:
        return text
    num = float(m.group(1))
    suffix = m.group(2).upper()
    if suffix == "K":
        return int(num * 1_000)
    if suffix == "M":
        return int(num * 1_000_000)
    if suffix == "B":
        return int(num * 1_000_000_000)
    return int(num)

def first_text(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                txt = loc.first.inner_text(timeout=2000).strip()
                if txt:
                    return txt
        except Exception:
            pass
    return ""

def first_attr(page, selectors, attr):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                val = loc.first.get_attribute(attr, timeout=2000)
                if val:
                    return val
        except Exception:
            pass
    return ""

def walk_for_profile(obj, found, target_handle):
    if isinstance(obj, dict):
        user = obj.get("user")
        stats = obj.get("stats") or obj.get("statsV2") or obj.get("authorStats")
        if isinstance(user, dict):
            uid = user.get("uniqueId") or user.get("unique_id")
            if uid and str(uid).lower() == target_handle.lower():
                found["id"] = user.get("id") or user.get("uid") or found.get("id", "")
                found["handle"] = uid
                found["display_name"] = user.get("nickname") or found.get("display_name", "")
                found["bio"] = user.get("signature") or found.get("bio", "")
                found["avatar_url"] = (
                    user.get("avatarLarger")
                    or user.get("avatarMedium")
                    or user.get("avatarThumb")
                    or user.get("avatar")
                    or found.get("avatar_url", "")
                )
                if isinstance(stats, dict):
                    found["followers"] = stats.get("followerCount") or stats.get("follower_count") or found.get("followers", "")
                    found["likes_total"] = stats.get("heartCount") or stats.get("heart") or found.get("likes_total", "")
                    found["video_count"] = stats.get("videoCount") or found.get("video_count", "")

        if str(obj.get("uniqueId", "")).lower() == target_handle.lower():
            found["id"] = obj.get("id") or obj.get("uid") or found.get("id", "")
            found["handle"] = obj.get("uniqueId") or found.get("handle", "")
            found["display_name"] = obj.get("nickname") or found.get("display_name", "")
            found["bio"] = obj.get("signature") or found.get("bio", "")
            found["avatar_url"] = obj.get("avatarLarger") or obj.get("avatarMedium") or obj.get("avatarThumb") or found.get("avatar_url", "")
            found["followers"] = obj.get("followerCount") or found.get("followers", "")
            found["likes_total"] = obj.get("heartCount") or found.get("likes_total", "")
            found["video_count"] = obj.get("videoCount") or found.get("video_count", "")

        for v in obj.values():
            walk_for_profile(v, found, target_handle)
    elif isinstance(obj, list):
        for item in obj:
            walk_for_profile(item, found, target_handle)

def extract_page_json(page, target_handle):
    found = {}

    for selector in ["script#__NEXT_DATA__", "script#SIGI_STATE"]:
        try:
            raw = page.locator(selector).first.text_content(timeout=3000)
            if raw:
                data = json.loads(raw)
                walk_for_profile(data, found, target_handle)
        except Exception:
            pass

    try:
        scripts = page.locator("script").all_text_contents()
        for raw in scripts:
            if target_handle.lower() not in raw.lower():
                continue
            try:
                start = raw.find("{")
                end = raw.rfind("}")
                if start >= 0 and end > start:
                    data = json.loads(raw[start:end+1])
                    walk_for_profile(data, found, target_handle)
            except Exception:
                continue
    except Exception:
        pass

    return found

def scrape_profile_with_playwright(profile_url, target_handle):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("Playwright is not installed. Skipping profile scrape.")
        print("Install with: python -m pip install playwright && python -m playwright install chromium")
        return None

    print(f"Scraping profile info from: {profile_url}")
    try:
        with sync_playwright() as p:
            browser = launch_chromium_browser(p, headless=True)
            context = browser.new_context(
                viewport={"width": 1400, "height": 1000},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(7000)

            for sel in ['button:has-text("Not now")', 'button:has-text("Close")', '[aria-label="Close"]', 'button:has-text("Decline")']:
                try:
                    loc = page.locator(sel)
                    if loc.count():
                        loc.first.click(timeout=1500)
                        page.wait_for_timeout(800)
                except Exception:
                    pass

            profile = extract_page_json(page, target_handle)

            if not profile.get("handle"):
                profile["handle"] = target_handle

            if not profile.get("display_name"):
                profile["display_name"] = first_text(page, [
                    '[data-e2e="user-subtitle"]',
                    'h2[data-e2e="user-subtitle"]',
                    'h2',
                ])

            if not profile.get("bio"):
                profile["bio"] = first_text(page, [
                    '[data-e2e="user-bio"]',
                    'div[data-e2e="user-bio"]',
                ])

            if not profile.get("followers"):
                profile["followers"] = clean_count(first_text(page, [
                    '[data-e2e="followers-count"]',
                    'strong[data-e2e="followers-count"]',
                ]))

            if not profile.get("likes_total"):
                profile["likes_total"] = clean_count(first_text(page, [
                    '[data-e2e="likes-count"]',
                    'strong[data-e2e="likes-count"]',
                ]))

            if not profile.get("avatar_url"):
                profile["avatar_url"] = first_attr(page, [
                    'img[data-e2e="user-avatar"]',
                    'span[data-e2e="user-avatar"] img',
                    'img[src*="avatar"]',
                    'img',
                ], "src")

            browser.close()

            avatar_local = ""
            avatar_url = profile.get("avatar_url", "")
            if avatar_url:
                try:
                    avatar_path = PROFILE / "avatar.jpg"
                    urlretrieve(avatar_url, avatar_path)
                    avatar_local = "profile/avatar.jpg"
                except Exception as e:
                    print(f"Could not download avatar: {e}")

            return {
                "handle": profile.get("handle") or target_handle,
                "display_name": profile.get("display_name") or "",
                "bio": profile.get("bio") or "",
                "followers": profile.get("followers") or "",
                "likes_total": profile.get("likes_total") or "",
                "video_count": profile.get("video_count") or "",
                "downloaded": "",
                "avatar_local": avatar_local,
                "avatar_url": avatar_url,
                "last_checked": time.strftime("%Y-%m-%d"),
                "source": "playwright_profile_from_links",
            }
    except Exception as e:
        print(f"Profile scrape failed: {e}")
        return None

def infer_profile_from_local_metadata(info_files, target_handle):
    profile = {
        "handle": target_handle,
        "display_name": "",
        "bio": "",
        "followers": "",
        "likes_total": "",
        "video_count": "",
        "downloaded": 0,
        "avatar_local": "",
        "last_checked": "today",
        "source": "local_metadata_fallback",
    }
    for info_file in info_files:
        try:
            data = json.loads(info_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        vid = str(data.get("id") or info_file.stem.replace(".info", ""))
        if not profile["display_name"]:
            profile["display_name"] = data.get("channel") or data.get("uploader") or data.get("creator") or ""
        if profile["followers"] in ("", None):
            maybe_followers = data.get("channel_follower_count") or data.get("follower_count")
            if maybe_followers is not None:
                profile["followers"] = maybe_followers
        if not profile["avatar_local"]:
            for ext in ("jpg", "jpeg", "png", "webp", "image"):
                candidate = THUMBS / f"{vid}.{ext}"
                if candidate.exists():
                    suffix = candidate.suffix if candidate.suffix else ".jpg"
                    if suffix.lower() == ".image":
                        suffix = ".jpg"
                    dest = PROFILE / f"avatar_fallback{suffix}"
                    try:
                        if not dest.exists():
                            shutil.copy2(candidate, dest)
                        profile["avatar_local"] = f"profile/{dest.name}"
                    except Exception:
                        pass
                    break
        if profile["display_name"] and profile["avatar_local"]:
            break
    return profile

OUT.mkdir(exist_ok=True)
VIDEOS_OUT.mkdir(exist_ok=True)
THUMBS.mkdir(exist_ok=True)
PROFILE.mkdir(exist_ok=True)
COMMENTS_DIR.mkdir(exist_ok=True)
COMMENT_IMAGES_OUT.mkdir(exist_ok=True)
SLIDESHOWS_OUT.mkdir(exist_ok=True)

config = load_config()
refresh_cfg = config.get("refresh", {})
download_cfg = config.get("download", {})

refresh_requested = bool(refresh_cfg.get("metadata", False))
refresh_profile = bool(refresh_cfg.get("profile", True))
refresh_links = bool(refresh_cfg.get("links", True))

if BUILD_ARCHIVE_ONLY:
    print("Build Archive Only mode: using existing local links, metadata, videos, slideshows, and comments.")
    refresh_requested = False
    refresh_profile = False
    refresh_links = False

COOKIES_BROWSER = str(download_cfg.get("use_cookies", "firefox")).strip() or "firefox"

profile_url, target_handle = profile_url_from_config(config)

existing_links = []
if LINKS.exists():
    existing_links = [line.strip() for line in LINKS.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
    if not profile_url:
        profile_url, target_handle = profile_url_from_links(existing_links)

links_changed = False
if refresh_links:
    if not profile_url:
        fail("config.json is missing a valid profile_url.")
    links_changed = refresh_links_file(profile_url)

if links_changed:
    print("Links changed. Only missing/new video metadata will be fetched.")

if not LINKS.exists():
    fail("links.txt was not found. Either set refresh.links to true with profile_url filled in, or add links.txt manually.")

links = [line.strip() for line in LINKS.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
if not links:
    fail("links.txt is empty.")

if not profile_url:
    profile_url, target_handle = profile_url_from_links(links)

if not target_handle:
    target_handle = "unknown"

if refresh_requested or not metadata_exists():
    print("Fetching metadata and thumbnails for all links...")
    try:
        result = subprocess.run([
            "yt-dlp",
            "-a", str(LINKS),
            "--cookies-from-browser", COOKIES_BROWSER,
            "--ignore-errors",
            "--skip-download",
            "--write-info-json",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "-o", str(THUMBS / "%(id)s.%(ext)s"),
        ], check=False)
        if result.returncode != 0:
            print("Some metadata fetches failed, but the build will continue.")
    except FileNotFoundError:
        fail("yt-dlp was not found. Install yt-dlp first and make sure it is in PATH.")
    except subprocess.CalledProcessError:
        fail("yt-dlp failed while fetching metadata.")
else:
    print("Reusing existing metadata and thumbnails in archive_out/thumbs")
    current_link_map = links_by_video_id(links)
    existing_meta_ids = {
        p.name.replace(".info.json", "")
        for p in THUMBS.glob("*.info.json")
    }
    missing_metadata_urls = [
        url for vid, url in current_link_map.items()
        if vid not in existing_meta_ids
    ]
    if BUILD_ARCHIVE_ONLY:
        if missing_metadata_urls:
            print(f"Build Archive Only mode: skipping metadata fetch for {len(missing_metadata_urls)} missing item(s).")
    else:
        fetch_metadata_for_urls(missing_metadata_urls)

info_files = sorted(THUMBS.glob("*.info.json"))
if not info_files:
    fail("No metadata files were found in archive_out/thumbs.")

def load_info_items_from_links(info_files, links):
    """Return normal metadata items plus minimal items for links yt-dlp did not write.

    This lets the builder still try slideshow/photo extraction from the TikTok page.
    Some slideshow posts do not download as videos and may have no audio, but their
    images can still be saved into archive_out/slideshows/<id>/.
    """
    items = []
    seen = set()
    for info_file in info_files:
        try:
            data = json.loads(info_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        vid = str(data.get("id") or info_file.stem.replace(".info", ""))
        if not vid:
            continue
        seen.add(vid)
        items.append((info_file, data))

    link_map = links_by_video_id(links)
    for vid, url in link_map.items():
        if vid in seen:
            continue
        print(f"No yt-dlp metadata found for {vid}. Trying slideshow/photo fallback from page URL.")
        data = {
            "id": vid,
            "webpage_url": url,
            "original_url": url,
            "title": f"TikTok post #{vid}",
            "description": "",
            "upload_date": "",
        }
        items.append((None, data))
        seen.add(vid)
    return items

info_items = load_info_items_from_links(info_files, links)

profile_json = PROFILE / "profile.json"

profile_cfg = config.get("profile", {})
auto_refresh_profile = bool(profile_cfg.get("auto_refresh", False))
max_age_hours = float(profile_cfg.get("max_age_hours", 24))
force_refresh_profile = bool(profile_cfg.get("force_refresh", False))

def is_profile_expired(path, max_hours):
    if not path.exists():
        return True
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds > (max_hours * 3600)

profile_data = None

if force_refresh_profile:
    print("Profile force_refresh is enabled. Scraping profile.")
elif profile_json.exists():
    if auto_refresh_profile:
        if is_profile_expired(profile_json, max_age_hours):
            print(f"Cached profile.json is older than {max_age_hours} hours. Scraping profile.")
        else:
            try:
                profile_data = json.loads(profile_json.read_text(encoding="utf-8"))
                print("Using cached profile.json. Profile is still fresh.")
            except Exception:
                profile_data = None
    else:
        try:
            profile_data = json.loads(profile_json.read_text(encoding="utf-8"))
            print("Using cached profile.json. Profile auto_refresh is disabled.")
        except Exception:
            profile_data = None
else:
    print("profile.json was not found. Scraping profile.")

if profile_data is None:
    if not profile_url:
        fail("Cannot scrape profile because config.json is missing a valid profile_url.")

    profile_data = scrape_profile_with_playwright(profile_url, target_handle)

    if profile_data is None:
        fail("Profile scrape failed. Cannot continue without valid profile data.")

    profile_json.write_text(json.dumps(profile_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved fresh profile.json.")

# Make sure the handle from links wins over accidental numeric IDs.
profile_data["handle"] = target_handle or profile_data.get("handle") or ""
profile_json.write_text(json.dumps(profile_data, ensure_ascii=False, indent=2), encoding="utf-8")

def build_video_map(folder: Path):
    priority = {".mp4": 0, ".m4v": 1, ".webm": 2, ".mov": 3, ".mkv": 4}
    found = {}
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            vid = extract_id_from_name(p.name)
            if not vid:
                continue
            old = found.get(vid)
            if old is None or priority.get(p.suffix.lower(), 99) < priority.get(old.suffix.lower(), 99):
                found[vid] = p
    return found

print("Checking root folder for existing videos...")
root_video_map = build_video_map(ROOT)
archive_video_map = build_video_map(VIDEOS_OUT)
root_audio_map = build_audio_map(ROOT)
archive_audio_map = build_audio_map(OUT)
archive_slideshow_map = build_slideshow_map(SLIDESHOWS_OUT)

missing_urls = []
for _info_file, data in info_items:
    vid = str(data.get("id") or (getattr(_info_file, "stem", "").replace(".info", "") if _info_file else ""))
    url = data.get("webpage_url") or data.get("original_url") or ""
    existing_video = root_video_map.get(vid) or archive_video_map.get(vid)
    existing_audio = root_audio_map.get(vid) or archive_audio_map.get(vid)
    existing_slideshow = archive_slideshow_map.get(vid)
    has_local_media = (
        (existing_video and browser_can_play(existing_video))
        or bool(existing_audio)
        or bool(existing_slideshow)
    )
    if not has_local_media and url:
        missing_urls.append(url)

if missing_urls:
    if BUILD_ARCHIVE_ONLY:
        print(f"Build Archive Only mode: skipping download for {len(missing_urls)} missing media item(s).")
    else:
        print(f"Downloading {len(missing_urls)} missing videos to the root folder...")
        for dl_i, dl_url in enumerate(missing_urls, start=1):
            dl_id = extract_video_id(dl_url) or dl_url
            print(f"Downloading video {dl_i}/{len(missing_urls)}: {dl_id}")
            try:
                run_yt_dlp_url_file([dl_url], [
                    "--cookies-from-browser", COOKIES_BROWSER,
                    "--ignore-errors",
                    "-f", "mp4/b",
                    "-S", "codec:h264,ext:mp4",
                    "--merge-output-format", "mp4",
                    "-o", str(ROOT / "%(title)s [%(id)s].%(ext)s"),
                    "--no-overwrites",
                ], check=False)
            except subprocess.CalledProcessError:
                print(f"Video download {dl_i}/{len(missing_urls)} failed or was skipped: {dl_id}")

print("Refreshing root and archive video lists...", flush=True)
root_video_map = build_video_map(ROOT)
archive_video_map = build_video_map(VIDEOS_OUT)
root_audio_map = build_audio_map(ROOT)
archive_audio_map = build_audio_map(OUT)
print("Video/audio lists refreshed.", flush=True)

entries = []
embedded_comment_scripts = []

total_info_items = len(info_items)
print(f"Preparing HTML entries for {total_info_items} post(s)...")

for entry_i, (_info_file, data) in enumerate(info_items, start=1):
    if entry_i == 1 or entry_i % 25 == 0 or entry_i == total_info_items:
        print(f"Preparing HTML entry {entry_i}/{total_info_items}...")
    vid = str(data.get("id") or (getattr(_info_file, "stem", "").replace(".info", "") if _info_file else ""))
    if not vid:
        continue
    title = data.get("title") or f"TikTok video #{vid}"
    uploader = profile_data.get("handle") or target_handle or data.get("uploader") or data.get("channel") or data.get("creator") or "Unknown"
    webpage_url = data.get("webpage_url") or data.get("original_url") or ""
    duration = data.get("duration")
    view_count = data.get("view_count")
    like_count = data.get("like_count")
    comment_count = data.get("comment_count")
    upload_date = data.get("upload_date") or upload_date_from_timestamp(data.get("timestamp")) or ""
    description = data.get("description") or ""

    thumb_src = None
    for ext in ("jpg", "jpeg", "png", "webp", "image"):
        candidate = THUMBS / f"{vid}.{ext}"
        if candidate.exists():
            thumb_src = candidate
            break

    thumb_rel = ""
    if thumb_src and thumb_src.exists():
        suffix = thumb_src.suffix if thumb_src.suffix else ".jpg"
        if suffix.lower() == ".image":
            suffix = ".jpg"
        dest = THUMBS / f"{thumb_src.stem}{suffix}"
        if thumb_src != dest and not dest.exists():
            shutil.copy2(thumb_src, dest)
        thumb_rel = f"thumbs/{dest.name}"

    video_rel = ""
    video_path = ""
    local_video = root_video_map.get(vid)
    archive_video = archive_video_map.get(vid)

    if local_video and local_video.exists():
        dest = safe_video_dest(vid, local_video.name)
        if local_video.resolve() != dest.resolve():
            if not dest.exists():
                shutil.move(str(local_video), str(dest))
            else:
                try:
                    local_video.unlink()
                except Exception:
                    pass
        archive_video_map[vid] = dest
        video_rel = f"videos/{dest.name}"
        video_path = str(dest.resolve())
    elif archive_video and archive_video.exists():
        dest = safe_video_dest(vid, archive_video.name)
        if archive_video.resolve() != dest.resolve():
            if not dest.exists():
                shutil.move(str(archive_video), str(dest))
            else:
                try:
                    archive_video.unlink()
                except Exception:
                    pass
            archive_video_map[vid] = dest
            archive_video = dest
        video_rel = f"videos/{archive_video.name}"
        video_path = str(archive_video.resolve())

    slideshow_images = []
    audio_rel = ""
    if not video_rel:
        slideshow_images = materialize_slideshow_images(vid, data)
        audio_file = root_audio_map.get(vid) or archive_audio_map.get(vid)
        if audio_file and audio_file.exists():
            audio_dest = OUT / f"audio_{vid}{audio_file.suffix.lower()}"
            try:
                if audio_file.resolve() != audio_dest.resolve() and not audio_dest.exists():
                    shutil.copy2(audio_file, audio_dest)
                audio_rel = audio_dest.name
            except Exception:
                audio_rel = ""

    comments_payload = load_comments_json(vid)
    comment_media_exts = comment_payload_media_exts(comments_payload)
    comments_json_text = json.dumps(comments_payload, ensure_ascii=False).replace("</script>", "<\\/script>")
    embedded_comment_scripts.append(f'<script type="application/json" id="comments-data-{esc(vid)}">{comments_json_text}</script>')

    entries.append({
        "id": vid,
        "title": title,
        "uploader": uploader,
        "url": webpage_url,
        "duration": duration,
        "views": view_count,
        "likes": like_count,
        "comments": comment_count,
        "upload_date": upload_date,
        "description": description,
        "thumb": thumb_rel,
        "video": video_rel,
        "video_path": video_path,
        "slideshow_images": slideshow_images,
        "audio": audio_rel,
        "sort_date": upload_date if upload_date else "00000000",
        "sort_key": sort_key_for_video(vid, upload_date, data.get("timestamp")),
        "comment_media_exts": comment_media_exts,
        "has_comment_media": bool(comment_media_exts),
        "comment_search": comment_payload_search_text(comments_payload),
    })

entries.sort(key=lambda e: e.get("sort_key", ""), reverse=True)
profile_data["downloaded"] = sum(1 for e in entries if e["video"] or e.get("slideshow_images"))

try:
    link_lines = [line.strip() for line in LINKS.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
    link_ids = extract_video_ids_from_lines(link_lines)
    link_map = links_by_video_id(link_lines)
    downloaded_ids = {str(e.get("id")) for e in entries if e.get("video") or e.get("slideshow_images")}
    missing_ids = [vid for vid in link_ids if vid not in downloaded_ids]
    missing_links = [link_map.get(vid) or f"https://www.tiktok.com/@{target_handle}/video/{vid}" for vid in missing_ids]
    MISSING_VIDEOS.write_text(("\n".join(missing_links) + "\n") if missing_links else "", encoding="utf-8")
    if missing_links:
        print(f"Wrote {len(missing_links)} missing video link(s) to: {MISSING_VIDEOS}")
    else:
        print(f"No missing video links found. Cleared: {MISSING_VIDEOS}")
except Exception as e:
    print(f"Could not write missing_videos.txt: {e}")

profile_json.write_text(json.dumps(profile_data, ensure_ascii=False, indent=2), encoding="utf-8")

def build_card(e):
    title = esc(e["title"])
    uploader = esc(e["uploader"])
    url = esc(e["url"])
    desc = esc(e["description"][:220])
    video_rel = esc(e["video"])
    video_path = esc(e["video_path"])
    slideshow_images = e.get("slideshow_images") or []
    audio_rel = esc(e.get("audio") or "")
    meta_bits = []
    if e["duration"]:
        meta_bits.append(f'{e["duration"]}s')
    if e["views"] is not None:
        meta_bits.append(f'{fmt_num(e["views"])} views')
    if e["likes"] is not None:
        meta_bits.append(f'{fmt_num(e["likes"])} likes')
    if e["comments"] is not None:
        meta_bits.append(f'{fmt_num(e["comments"])} comments')
    if e["upload_date"]:
        meta_bits.append(fmt_date(e["upload_date"]))
    meta = " • ".join(meta_bits)
    thumb_src = e["thumb"] or (slideshow_images[0] if slideshow_images else "")
    thumb_html = f'<img src="{esc(thumb_src)}" alt="{title}" loading="lazy">' if thumb_src else '<div class="no-thumb">No thumbnail</div>'
    play_attr = f'data-video="{video_rel}"' if e["video"] else ''
    playable_class = " playable" if e["video"] else ""
    if e["video"]:
        saved_line = f'<div class="file-path">Saved: {video_path}</div>'
    elif slideshow_images:
        saved_line = f'<div class="file-path">Saved slideshow images: {len(slideshow_images)}</div>'
    else:
        saved_line = '<div class="file-path">Saved: not downloaded</div>'
    comments_toggle = f'<button class="comments-toggle" data-comments-id="{esc(e["id"])}" data-target="comments-{esc(e["id"])}">Show offline comments</button><div id="comments-{esc(e["id"])}" class="comments-wrap hidden"></div>'
    media_exts = ",".join(e.get("comment_media_exts") or [])
    media_badge = f'<span class="media-badge">Comment pictures: {esc(media_exts)}</span>' if media_exts else ''
    if slideshow_images:
        slide_imgs = "".join(f'<img class="slideshow-img" src="{esc(src)}" alt="slideshow image" loading="lazy">' for src in slideshow_images)
        audio_html = f'<audio class="slideshow-audio" src="{audio_rel}" controls preload="metadata"></audio>' if audio_rel else ''
        slideshow_html = f'<div class="slideshow-block"><div class="media-badge">Slideshow/photo post: {len(slideshow_images)} image(s)</div><div class="slideshow-grid">{slide_imgs}</div>{audio_html}</div>'
    else:
        slideshow_html = ''
    return f"""
    <article class="card" data-vid="{esc(e['id'])}" data-uploader="{uploader.lower()}" data-title="{title.lower()}" data-comment-text="{esc(e.get('comment_search', ''))}" data-date="{esc(e.get('sort_key') or e['sort_date'])}" data-media-type="{'video' if e['video'] else ('slideshow' if slideshow_images else 'missing')}" data-comment-media="{esc(media_exts)}" data-has-comment-media="{'1' if e.get('has_comment_media') else '0'}">
      <button class="thumb-btn{playable_class}" {play_attr} aria-label="Open video player">
        <div class="thumb">
          {thumb_html}
          {'<div class="play-overlay">▶</div>' if e["video"] else ""}
        </div>
      </button>
      <div class="content">
        <div class="topline">
          <div class="author">@{uploader}</div>
          <button class="copy-btn" data-url="{url}">Copy Link</button>
        </div>
        <h2><a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a></h2>
        <div class="meta">{esc(meta)}</div>
        {media_badge}
        <p class="desc">{desc}</p>
        {slideshow_html}
        {comments_toggle}
        <div class="footer">
          <div class="file-meta">
            <span class="id">ID: {esc(e["id"])}</span>
            {saved_line}
          </div>
          <div class="footer-actions">
            {'<button class="play-btn" data-video="' + video_rel + '">Play Local</button>' if e["video"] else ""}
            <a class="open-btn" href="{url}" target="_blank" rel="noopener noreferrer">Open on TikTok</a>
          </div>
        </div>
      </div>
    </article>
    """

cards_html = "".join(build_card(e) for e in entries)
embedded_scripts_html = "\n".join(embedded_comment_scripts)
avatar_html = f'<img src="{esc(profile_data.get("avatar_local"))}" alt="{esc(profile_data.get("handle"))}">' if profile_data.get("avatar_local") else '<div class="avatar-placeholder"></div>'
bio_html = esc(profile_data.get("bio")) if profile_data.get("bio") else "-"

html_template = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Local Archive</title>
<style>
:root{--bg:#333;--panel:#202020;--panel-2:#2a2a2a;--text:#ddd;--muted:#a7a7a7;--accent:#fe2c55;--border:#444}*{box-sizing:border-box}body{margin:0;color:var(--text);background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Oxygen,Ubuntu,Cantarell,"Helvetica Neue",sans-serif}.wrap{max-width:1100px;margin:0 auto;padding:18px}.profile-card{display:grid;grid-template-columns:200px 1fr;gap:28px;padding:24px;margin-bottom:18px}.avatar-box{width:200px;height:200px;border-radius:999px;overflow:hidden;background:#444}.avatar-box img,.avatar-placeholder{width:100%;height:100%;object-fit:cover;display:block;background:#555}.profile-main h1{margin:0 0 10px;font-size:22px;font-weight:500}.stats-grid{display:grid;grid-template-columns:repeat(3,minmax(120px,1fr));gap:14px 36px;margin-top:8px}.stat .label{color:var(--muted);margin-bottom:4px}.stat .value{font-size:16px}.bio-dots{color:#74b7ff;font-weight:700;text-decoration:none}.bio-value{white-space:pre-wrap;line-height:1.35}.header{position:sticky;top:0;z-index:2;background:rgba(51,51,51,.95);backdrop-filter:blur(8px);padding-bottom:12px}.toolbar{display:flex;gap:10px;flex-wrap:wrap}.toolbar input,.toolbar select{flex:1 1 220px;min-width:0;padding:12px 14px;border-radius:10px;border:1px solid var(--border);background:var(--panel);color:var(--text)}.sort-box{flex:0 0 220px}.count{padding:12px 14px;border-radius:10px;background:var(--panel);border:1px solid var(--border);color:var(--muted)}.list{display:grid;gap:16px;margin-top:18px}.card{display:grid;grid-template-columns:280px 1fr;background:var(--panel);border:1px solid var(--border);border-radius:16px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.25)}.thumb-btn{border:0;padding:0;background:transparent;cursor:default;position:relative}.thumb-btn.playable{cursor:pointer}.thumb{display:block;background:#111;min-height:320px;height:100%;position:relative}.thumb img{width:100%;height:100%;object-fit:cover;display:block}.play-overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:54px;color:rgba(255,255,255,.92);background:rgba(0,0,0,.15);opacity:0;transition:opacity .15s ease}.thumb-btn.playable:hover .play-overlay{opacity:1}.no-thumb{height:100%;min-height:320px;display:flex;align-items:center;justify-content:center;color:var(--muted);background:#1a1a1a}.content{padding:16px}.topline{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}.author{color:#fff;font-weight:600}h2{margin:0 0 10px;font-size:20px;line-height:1.35}h2 a{color:var(--text);text-decoration:none}.meta{color:var(--muted);font-size:14px;margin-bottom:10px}.media-badge{display:inline-block;margin:0 0 10px;padding:5px 8px;border-radius:999px;background:#333;color:#ffd0da;font-size:12px;border:1px solid #555}.slideshow-block{margin:0 0 14px;padding:12px;border:1px solid var(--border);border-radius:12px;background:#1b1b1b}.slideshow-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}.slideshow-img{width:100%;max-height:260px;object-fit:contain;background:#111;border-radius:10px;cursor:zoom-in}.slideshow-audio{width:100%;margin-top:10px}.desc{margin:0 0 14px;color:#cfcfcf;white-space:pre-wrap}.comments-toggle{margin:0 0 12px;border:0;cursor:pointer;padding:10px 12px;border-radius:10px;background:var(--panel-2);color:var(--text);font:inherit}.comments-wrap.hidden{display:none}.comments-wrap{margin:0 0 14px;border:1px solid var(--border);border-radius:12px;background:#1b1b1b;padding:12px}.comments-list{display:grid;gap:10px}.comment-card{background:#252525;border:1px solid #3a3a3a;border-radius:12px;padding:10px 12px}.comment-user{font-weight:600;margin-bottom:6px}.comment-text{white-space:pre-wrap;margin-bottom:6px}.comment-meta{color:var(--muted);font-size:12px;margin-bottom:8px}.comment-images{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.comment-image{max-width:220px;max-height:220px;width:auto;border-radius:10px;display:block;object-fit:cover;background:#111;cursor:zoom-in}.image-modal-img{max-width:92vw;max-height:90vh;border-radius:14px;background:#111}.reply-toggle{margin-top:8px;border:0;cursor:pointer;padding:8px 10px;border-radius:8px;background:#343434;color:var(--text);font:inherit}.replies.hidden{display:none}.replies{margin-top:8px;display:grid;gap:8px;padding-left:12px;border-left:2px solid #444}.footer{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap}.footer-actions{display:flex;gap:10px;flex-wrap:wrap}.file-meta{display:flex;flex-direction:column;gap:6px;min-width:0}.id{color:var(--muted);font-size:13px}.file-path{color:var(--muted);font-size:12px;word-break:break-all;max-width:560px}.open-btn,.copy-btn,.play-btn{border:0;cursor:pointer;text-decoration:none;padding:10px 12px;border-radius:10px;background:var(--panel-2);color:var(--text);font:inherit}.open-btn{background:var(--accent);color:#fff}.hidden{display:none!important}.modal{position:fixed;inset:0;background:rgba(0,0,0,.86);display:none;align-items:center;justify-content:center;padding:20px;z-index:20}.modal.show{display:flex}.modal-inner{width:min(92vw,560px);position:relative}.modal video{width:100%;max-height:88vh;background:#000;border-radius:16px;outline:none}.close-btn{position:absolute;top:-44px;right:0;border:0;background:#444;color:white;padding:8px 12px;border-radius:10px;cursor:pointer}.player-error{display:none;margin-top:10px;color:#ffb4c1;background:#301820;border:1px solid #6a2c3a;padding:10px;border-radius:10px}.player-error.show{display:block}
@media(max-width:760px){.profile-card{grid-template-columns:1fr}.avatar-box{width:140px;height:140px}.card{grid-template-columns:1fr}.thumb{height:auto;min-height:0}.thumb img{height:auto;max-height:70vh;object-fit:contain}.modal-inner{width:96vw}}
</style></head><body>
<div class="wrap"><section class="profile-card"><div class="avatar-box">__AVATAR__</div><div class="profile-main"><h1>@__HANDLE__</h1><div class="stats-grid"><div class="stat"><div class="label">name:</div><div class="value">__NAME__</div></div><div class="stat"><div class="label">last checked:</div><div class="value">__LASTCHECKED__</div></div><div class="stat bio-stat"><div class="label">bio:</div><div class="value bio-value">__BIO__</div></div><div class="stat"><div class="label">followers:</div><div class="value">__FOLLOWERS__</div></div><div class="stat"><div class="label">downloaded:</div><div class="value">__DOWNLOADED__</div></div><div class="stat"><div class="label">profile videos:</div><div class="value">__PROFILEVIDEOS__</div></div></div></div></section><div class="header"><div class="toolbar"><input id="search" type="text" placeholder="Search title, creator, or comments"><select id="sort" class="sort-box"><option value="newest">Sort: Newest to oldest</option><option value="oldest">Sort: Oldest to newest</option></select><select id="mediaFilter" class="sort-box"><option value="all">Comments: All videos</option><option value="has">Comments with pictures</option><option value="jpg">Pictures: JPG/JPEG</option><option value="png">Pictures: PNG</option><option value="webp">Pictures: WEBP</option><option value="gif">Pictures: GIF</option><option value="none">No comment pictures</option></select><select id="typeFilter" class="sort-box"><option value="all">Post type: All</option><option value="video">Post type: Videos only</option><option value="slideshow">Post type: Slideshows only</option><option value="missing">Post type: Missing local media</option></select><div class="count"><span id="visibleCount">__COUNT__</span> / __COUNT__ videos</div></div></div><div id="list" class="list">__CARDS__</div></div>
__EMBEDDED_COMMENT_SCRIPTS__
<div id="imageModal" class="modal"><div class="modal-inner"><button id="closeImageModal" class="close-btn">Close</button><img id="imageModalImg" class="image-modal-img" alt="comment image"></div></div>
<div id="playerModal" class="modal"><div class="modal-inner"><button id="closeModal" class="close-btn">Close</button><video id="player" controls playsinline preload="metadata"></video><div id="playerError" class="player-error">This browser could not play the local file. Try opening the video file directly from the Saved path.</div></div></div>
<script>
const search=document.getElementById('search'),sort=document.getElementById('sort'),mediaFilter=document.getElementById('mediaFilter'),typeFilter=document.getElementById('typeFilter'),list=document.getElementById('list'),cards=[...document.querySelectorAll('.card')],visibleCount=document.getElementById('visibleCount'),modal=document.getElementById('playerModal'),player=document.getElementById('player'),closeModal=document.getElementById('closeModal'),playerError=document.getElementById('playerError'),imageModal=document.getElementById('imageModal'),imageModalImg=document.getElementById('imageModalImg'),closeImageModal=document.getElementById('closeImageModal');
function sortCards(){const mode=sort.value;const sorted=[...cards].sort((a,b)=>{const da=a.dataset.date||"00000000",db=b.dataset.date||"00000000";return mode==='oldest'?da.localeCompare(db):db.localeCompare(da)});for(const card of sorted)list.appendChild(card)}
function applyFilter(){const q=search.value.trim().toLowerCase();const mediaMode=mediaFilter.value;let shown=0;for(const card of cards){const hay=(card.dataset.title+' '+card.dataset.uploader+' '+(card.dataset.commentText||'')).toLowerCase();const media=(card.dataset.commentMedia||'').split(',').filter(Boolean);const hasMedia=card.dataset.hasCommentMedia==='1';const textOk=!q||hay.includes(q);const typeMode=typeFilter.value;const typeOk=typeMode==='all'||card.dataset.mediaType===typeMode;let mediaOk=true;if(mediaMode==='has')mediaOk=hasMedia;else if(mediaMode==='none')mediaOk=!hasMedia;else if(mediaMode!=='all')mediaOk=media.includes(mediaMode);const ok=textOk&&mediaOk&&typeOk;card.classList.toggle('hidden',!ok);if(ok)shown++}visibleCount.textContent=shown}
function openPlayer(src){if(!src)return;playerError.classList.remove('show');player.pause();player.removeAttribute('src');player.load();player.src=new URL(src, document.baseURI).href;modal.classList.add('show');player.play().catch(()=>{})}
function closePlayer(){player.pause();player.removeAttribute('src');player.load();playerError.classList.remove('show');modal.classList.remove('show')}
function openImage(src){if(!src)return;imageModalImg.src=src;imageModal.classList.add('show')}
function closeImage(){imageModalImg.removeAttribute('src');imageModal.classList.remove('show')}
player.addEventListener('error',()=>{playerError.classList.add('show')});
function looksLikeImagePath(s){return /\.(jpg|jpeg|png|webp|gif|avif)(\?|#|$)/i.test(s)||s.startsWith('data:image/')||s.includes('comment_images/')}
function collectImageUrls(value,urls=new Set()){if(!value)return urls;if(typeof value==='string'){const fixed=value.split('\\\\').join('/');if(looksLikeImagePath(fixed))urls.add(fixed);return urls}if(Array.isArray(value)){for(const item of value)collectImageUrls(item,urls);return urls}if(typeof value==='object'){for(const [key,item] of Object.entries(value)){if(['url','url_list','uri','display_url','download_url','web_uri','downloaded_images','downloaded_image','local_images','local_image','image','images','image_url','image_urls','image_path','image_paths','comment_images','comment_image','comment_media','sticker','media','media_url','media_urls'].includes(key)||typeof item==='object')collectImageUrls(item,urls)}return urls}return urls}
function mediaKey(src){const clean=String(src||'').split('\\\\').join('/');const low=clean.toLowerCase().split('?')[0].split('#')[0];if(low.endsWith('.image'))return '';let key=low;if(low.includes('/comment_images/')){key=low.split('/').pop().replace(/_[0-9]+(?=\.(jpg|jpeg|png|webp|gif|avif)$)/i,'')}const m=low.match(/\/([^/?#]+)~tplv-[^/?#]+-(?:image-)?(?:medium|origin)\.(?:image|jpe?g|png|webp|gif|avif)/i);if(m)key=m[1];return key}
function dedupeImageList(items){const byKey=new Map();for(const src of items){if(!src)continue;const clean=String(src).split('\\\\').join('/');const key=mediaKey(clean);if(!key)continue;const score=(clean.includes('/comment_images/')?1000:0)+(clean.match(/\.(jpg|jpeg|png|webp|gif|avif)(\?|#|$)/i)?100:0)+(clean.includes('origin')?10:0);const prev=byKey.get(key);if(!prev||score>prev.score)byKey.set(key,{src:clean,score})}return [...byKey.values()].map(x=>x.src)}
function commentImageUrls(comment){let local=new Set();collectImageUrls(comment.downloaded_images,local);collectImageUrls(comment.downloaded_image,local);collectImageUrls(comment.local_images,local);collectImageUrls(comment.local_image,local);collectImageUrls(comment.image_path,local);collectImageUrls(comment.image_paths,local);let primary=dedupeImageList([...local]);if(primary.length)return primary;local=new Set();collectImageUrls(comment.comment_images,local);collectImageUrls(comment.comment_image,local);collectImageUrls(comment.images,local);collectImageUrls(comment.image_list,local);collectImageUrls(comment.comment_media,local);collectImageUrls(comment.sticker,local);collectImageUrls(comment.media,local);collectImageUrls(comment.image,local);collectImageUrls(comment.image_url,local);collectImageUrls(comment.image_urls,local);return dedupeImageList([...local])}
function renderCommentNode(comment){const wrap=document.createElement('div');wrap.className='comment-card';const user=document.createElement('div');user.className='comment-user';user.textContent='@'+(comment.user||comment.username||'unknown');wrap.appendChild(user);const text=document.createElement('div');text.className='comment-text';text.textContent=comment.text||comment.comment||'';wrap.appendChild(text);const metaBits=[];if(comment.time||comment.create_time)metaBits.push(comment.time||comment.create_time);if(comment.likes!=null)metaBits.push(String(comment.likes)+' likes');if(metaBits.length){const meta=document.createElement('div');meta.className='comment-meta';meta.textContent=metaBits.join(' • ');wrap.appendChild(meta)}const imageUrls=commentImageUrls(comment);if(imageUrls.length){const imgs=document.createElement('div');imgs.className='comment-images';for(const imageSrc of imageUrls){const img=document.createElement('img');img.className='comment-image';img.src=imageSrc;img.alt='';img.loading='lazy';img.onerror=()=>img.remove();imgs.appendChild(img)}wrap.appendChild(imgs)}const replies=comment.replies||[];if(Array.isArray(replies)&&replies.length){const btn=document.createElement('button');btn.className='reply-toggle';btn.textContent=`Show replies (${replies.length})`;const repliesWrap=document.createElement('div');repliesWrap.className='replies hidden';btn.addEventListener('click',()=>{const hidden=repliesWrap.classList.toggle('hidden');btn.textContent=hidden?`Show replies (${replies.length})`:`Hide replies (${replies.length})`});wrap.appendChild(btn);for(const reply of replies){repliesWrap.appendChild(renderCommentNode(reply))}wrap.appendChild(repliesWrap)}return wrap}
function toggleComments(btn){const targetId=btn.dataset.target,commentsId=btn.dataset.commentsId,wrap=document.getElementById(targetId);if(!wrap)return;if(wrap.dataset.loaded==='true'){const hidden=wrap.classList.toggle('hidden');btn.textContent=hidden?'Show offline comments':'Hide offline comments';return}btn.disabled=true;btn.textContent='Loading comments...';try{const dataTag=document.getElementById('comments-data-'+commentsId);if(!dataTag)throw new Error('Embedded comment data not found');const data=JSON.parse(dataTag.textContent),comments=Array.isArray(data)?data:(data.comments||[]),listNode=document.createElement('div');listNode.className='comments-list';if(!comments.length){const empty=document.createElement('div');empty.className='comment-card';empty.textContent='No offline comments found.';listNode.appendChild(empty)}else{for(const comment of comments)listNode.appendChild(renderCommentNode(comment))}wrap.innerHTML='';wrap.appendChild(listNode);wrap.dataset.loaded='true';wrap.classList.remove('hidden');btn.textContent=`Hide offline comments (${comments.length})`}catch(err){wrap.innerHTML='<div class="comment-card">Could not load offline comments.</div>';wrap.dataset.loaded='true';wrap.classList.remove('hidden');btn.textContent='Hide offline comments'}finally{btn.disabled=false}}
search.addEventListener('input',applyFilter);sort.addEventListener('change',sortCards);mediaFilter.addEventListener('change',applyFilter);typeFilter.addEventListener('change',applyFilter);closeModal.addEventListener('click',closePlayer);closeImageModal.addEventListener('click',closeImage);modal.addEventListener('click',e=>{if(e.target===modal)closePlayer()});imageModal.addEventListener('click',e=>{if(e.target===imageModal)closeImage()});document.addEventListener('keydown',e=>{if(e.key==='Escape'){closePlayer();closeImage()}});document.addEventListener('click',async e=>{const anyImg=e.target.closest('.comment-image,.slideshow-img');if(anyImg){openImage(anyImg.src);return}const copyBtn=e.target.closest('.copy-btn');if(copyBtn){const url=copyBtn.dataset.url||'';try{await navigator.clipboard.writeText(url);const old=copyBtn.textContent;copyBtn.textContent='Copied';setTimeout(()=>copyBtn.textContent=old,1200)}catch{alert(url)}return}const playBtn=e.target.closest('.play-btn');if(playBtn){openPlayer(playBtn.dataset.video||'');return}const thumbBtn=e.target.closest('.thumb-btn.playable');if(thumbBtn){openPlayer(thumbBtn.dataset.video||'');return}const commentsBtn=e.target.closest('.comments-toggle');if(commentsBtn)toggleComments(commentsBtn)});sortCards();applyFilter();
</script></body></html>
"""

html_text = (html_template
    .replace("__AVATAR__", avatar_html)
    .replace("__HANDLE__", esc(profile_data.get("handle")))
    .replace("__NAME__", esc(profile_data.get("display_name")) or "-")
    .replace("__LASTCHECKED__", esc(profile_data.get("last_checked")))
    .replace("__BIO__", bio_html)
    .replace("__FOLLOWERS__", esc(fmt_num(profile_data.get("followers"))) or "-")
    .replace("__DOWNLOADED__", str(profile_data["downloaded"]))
    .replace("__PROFILEVIDEOS__", esc(fmt_num(profile_data.get("video_count"))) or "-")
    .replace("__COUNT__", str(len(entries)))
    .replace("__CARDS__", cards_html)
    .replace("__EMBEDDED_COMMENT_SCRIPTS__", embedded_scripts_html)
)

(OUT / "index.html").write_text(html_text, encoding="utf-8")
print(f"\nBuilt {OUT / 'index.html'} with {len(entries)} entries")
print(f"Open this file in your browser:\n{OUT / 'index.html'}")
print("")
