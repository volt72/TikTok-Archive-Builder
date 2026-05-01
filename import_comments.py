import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMPORT_DIR = ROOT / "import_comments"
OUT = ROOT / "archive_out"
COMMENTS_DIR = OUT / "comments"

COMMENTS_DIR.mkdir(parents=True, exist_ok=True)
IMPORT_DIR.mkdir(exist_ok=True)

MEDIA_KEYS = [
    "image", "image_url", "image_urls", "image_list",
    "sticker", "comment_media", "media",
    "downloaded_images", "downloaded_image",
    "local_images", "local_image", "image_path", "image_paths",
]

def extract_video_id_from_name(name: str):
    m = re.search(r"(\d{8,})", name)
    return m.group(1) if m else ""

def normalize_comment(item):
    out = {
        "comment_id": item.get("comment_id") or item.get("cid") or item.get("id") or "",
        "user": item.get("username") or item.get("user") or item.get("unique_id") or item.get("nickname") or "",
        "nickname": item.get("nickname") or "",
        "text": item.get("comment") or item.get("text") or "",
        "time": item.get("create_time") or item.get("time") or "",
        "likes": item.get("digg_count") or item.get("likes"),
        "replies": [normalize_comment(r) for r in item.get("replies", []) if isinstance(r, dict)],
    }

    # Preserve all TikTok media fields from the scraper instead of dropping them.
    # This allows build_archive.py to render comment pictures/stickers in the HTML.
    for key in MEDIA_KEYS:
        if key in item and item.get(key) not in (None, "", []):
            out[key] = item.get(key)

    return out

count = 0
for path in IMPORT_DIR.iterdir():
    if not path.is_file() or path.suffix.lower() != ".json":
        continue
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    vid = data.get("aweme_id") if isinstance(data, dict) else None
    if not vid and isinstance(data, dict):
        vid = data.get("id")
    if not vid:
        vid = extract_video_id_from_name(path.name)
    if not vid:
        continue
    raw_comments = data.get("comments", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    comments = [normalize_comment(c) for c in raw_comments if isinstance(c, dict)]
    out_path = COMMENTS_DIR / f"{vid}.json"
    out_path.write_text(json.dumps({"id": vid, "comments": comments}, ensure_ascii=False, indent=2), encoding="utf-8")
    count += 1
    print(f"Imported comments for {vid} -> {out_path}")

print(f"\nImported {count} comment files.")
print("")
