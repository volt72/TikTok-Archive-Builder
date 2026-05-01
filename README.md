![Banner](banner.svg)

# TikTok Archive Builder

A Windows GUI tool for saving TikTok profiles into a local offline archive you can browse in your browser.

## What It Does

TikTok Archive Builder downloads profile posts, saves metadata and thumbnails, imports comments, handles slideshow posts, and builds a searchable local HTML archive.

## Features

- Download TikTok videos with `yt-dlp`
- Save thumbnails and metadata
- Import comments, replies, stickers, and comment images
- Support slideshow/photo posts
- Avoid re-downloading files you already have
- Track deleted or unavailable videos
- Build a local offline `index.html` archive

## HTML Archive

The generated archive lets you:

- Play downloaded videos
- View slideshow posts as image galleries
- Read comments and replies
- View comment images and stickers
- Filter posts by video, slideshow, or image posts
- Sort posts by newest or oldest
- Toggle comments on and off

## Screenshots

### GUI

![GUI](screenshot_gui.png)

## Project Structure

```text
output/
  username/
    links.txt
    deletedvids.txt
    archive_out/
      index.html
      videos/
      thumbs/
      comments/
      comment_images/
      slideshows/
```
---

## How to Use

Install dependencies:

```
pip install -r requirements.txt
```

Run:

```
python gui_integrated_output.py
```

---

## Build EXE

```
build_integrated_output_gui_spec_playwright_bundled.bat
```

---

## Credits

* yt-dlp
  https://github.com/yt-dlp/yt-dlp

* TikTok Comment Scraper
  https://github.com/RomySaputraSihananda/tiktok-comment-scrapper

---

## Disclaimer

For personal use only. Respect content ownership.
