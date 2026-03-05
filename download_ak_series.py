#!/usr/bin/env python3
import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
MEDIA_EXTENSIONS = (".mp4", ".mkv", ".avi", ".m3u8", ".mov")

# Optional hardcoded Telegram settings for VPS usage.
# Fill these two values if you want Telegram sending without env vars/CLI args.
TELEGRAM_BOT_TOKEN = "8651393081:AAFbmGSevj-7ESUN7MVYusR-Bt03CZq7Na0"
TELEGRAM_CHAT_ID = "1052952229"


def configure_stdio_utf8() -> None:
    # On Windows GUI-launched processes may default to cp1252 and fail on Arabic text.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def fetch_html(session: requests.Session, url: str, *, referer: Optional[str] = None, timeout: int = 45) -> str:
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    resp = session.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def extract_episode_number(url: str, text: str = "") -> Optional[int]:
    decoded_url = unquote(url)
    for pattern in [r"الحلقة-(\d+)", r"episode/(?:\d+)/[^\s]*?(\d+)(?:/|$)"]:
        m = re.search(pattern, decoded_url)
        if m:
            return int(m.group(1))

    decoded_text = unquote(text)
    m = re.search(r"(?:حلقة|الحلقة)\s*(\d+)", decoded_text)
    if m:
        return int(m.group(1))
    return None


def get_episode_pages(session: requests.Session, series_url: str) -> List[Tuple[int, str]]:
    html = fetch_html(session, series_url)
    soup = BeautifulSoup(html, "html.parser")

    episodes: Dict[int, str] = {}
    for a in soup.select('a[href*="/episode/"]'):
        href = a.get("href")
        if not href:
            continue
        full_url = urljoin(series_url, href)
        text = " ".join(a.get_text(" ", strip=True).split())
        ep_num = extract_episode_number(full_url, text)
        if ep_num is None:
            continue
        episodes[ep_num] = full_url

    if not episodes:
        raise RuntimeError("Could not find episode links on the series page.")

    return sorted(episodes.items(), key=lambda x: x[0])


def quality_value_from_text(text: str) -> Optional[int]:
    m = re.search(r"(\d{3,4})\s*p?", text.lower())
    if not m:
        return None
    return int(m.group(1))


def normalize_quality_choice(raw_quality: str) -> str:
    q = (raw_quality or "best").strip().lower()
    if q.endswith("p") and q[:-1].isdigit():
        q = q[:-1]
    return q or "best"


def get_download_options(session: requests.Session, episode_url: str) -> List[Tuple[str, str, Optional[int]]]:
    html = fetch_html(session, episode_url)
    soup = BeautifulSoup(html, "html.parser")
    options: List[Tuple[str, str, Optional[int]]] = []
    seen: Set[str] = set()

    tab_quality_map: Dict[str, str] = {}
    for tab_anchor in soup.select(".header-tabs a[href]"):
        href = (tab_anchor.get("href") or "").strip()
        if not href.startswith("#"):
            continue
        tab_id = href[1:]
        label = " ".join(tab_anchor.get_text(" ", strip=True).split())
        if tab_id and label:
            tab_quality_map[tab_id] = label

    for a in soup.select("a.link-download[href]"):
        href = a.get("href")
        if href:
            go_link = urljoin(episode_url, href).strip()
            if not go_link or go_link in seen:
                continue
            seen.add(go_link)

            quality_label = "unknown"
            tab_container = a.find_parent(
                lambda tag: tag.name == "div" and "tab-content" in (tag.get("class") or [])
            )
            if tab_container:
                tab_id = tab_container.get("id")
                if tab_id and tab_id in tab_quality_map:
                    quality_label = tab_quality_map[tab_id]

            if quality_label == "unknown":
                quality_source = a.find_parent(attrs={"data-quality": True})
                if quality_source:
                    quality_label = str(quality_source.get("data-quality") or "unknown")

            options.append((go_link, quality_label, quality_value_from_text(quality_label)))

    if not options:
        # Fallback: find any go.ak.sv link in raw HTML
        fallback_links = re.findall(r"https?://go\.ak\.sv/link/\d+", html, flags=re.IGNORECASE)
        for link in fallback_links:
            normalized = link.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                options.append((normalized, "unknown", None))

    return options


def select_download_options(
    options: List[Tuple[str, str, Optional[int]]], quality_choice: str
) -> List[Tuple[str, str, Optional[int]]]:
    if not options:
        return []

    choice = normalize_quality_choice(quality_choice)
    if choice in {"any", "all"}:
        return options

    if choice == "best":
        values = [quality for _, _, quality in options if quality is not None]
        if not values:
            return options
        best = max(values)
        return [opt for opt in options if opt[2] == best]

    if choice.isdigit():
        target = int(choice)
        exact = [opt for opt in options if opt[2] == target]
        if exact:
            return exact
        return [opt for opt in options if re.search(rf"\b{target}p?\b", opt[1].lower())]

    return [opt for opt in options if choice in opt[1].lower()]


def format_available_qualities(options: List[Tuple[str, str, Optional[int]]]) -> str:
    labels: List[str] = []
    for _, quality_label, quality_value in options:
        if quality_value is not None:
            label = f"{quality_value}p"
        else:
            label = quality_label or "unknown"
        if label not in labels:
            labels.append(label)
    return ", ".join(labels) if labels else "unknown"


def find_download_page_url(html: str) -> Optional[str]:
    # Primary: direct download page URLs embedded in the go page.
    candidates = re.findall(r"https?://[^\"'\s>]+/download/[^\"'\s<]+", html, flags=re.IGNORECASE)

    # Keep only site-related download URLs
    filtered = [c for c in candidates if any(domain in c for domain in ["ak.sv/download/", "akw.cam/download/", "akw-cdn"])]
    if filtered:
        return filtered[0]

    return candidates[0] if candidates else None


def resolve_download_page(session: requests.Session, go_link: str) -> Optional[str]:
    html = fetch_html(session, go_link)
    return find_download_page_url(html)


def extract_media_links(session: requests.Session, download_page_url: str) -> List[str]:
    html = fetch_html(session, download_page_url, referer=download_page_url)
    soup = BeautifulSoup(html, "html.parser")

    links: List[str] = []

    # Anchors first
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if href.lower().startswith("javascript:"):
            continue
        full = urljoin(download_page_url, href)
        if any(ext in full.lower() for ext in MEDIA_EXTENSIONS):
            links.append(full)

    # Regex fallback for direct links inside scripts
    regex_links = re.findall(r"https?://[^\"'\s>]+", html, flags=re.IGNORECASE)
    for link in regex_links:
        if any(ext in link.lower() for ext in MEDIA_EXTENSIONS):
            links.append(link)

    deduped: List[str] = []
    seen: Set[str] = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            deduped.append(link)

    return deduped


def safe_filename_from_url(url: str, episode_num: int) -> str:
    parsed = urlparse(url)
    base = unquote(os.path.basename(parsed.path))
    if not base or "." not in base:
        return f"episode_{episode_num:02d}.mp4"

    safe = re.sub(r"[\\/:*?\"<>|]", "_", base)
    if not safe.lower().startswith("ein") and not safe.lower().startswith("episode"):
        safe = f"episode_{episode_num:02d}_{safe}"
    return safe


def extract_series_name(series_url: str) -> str:
    path = unquote(urlparse(series_url).path).strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0].lower() == "series":
        name = parts[2]
    elif parts:
        name = parts[-1]
    else:
        name = "Series"
    name = name.replace("-", " ").strip()
    return name or "Series"


def build_telegram_caption(series_name: str, episode_num: int) -> str:
    return f"{series_name} - الحلقة {episode_num}"


def send_file_to_telegram(
    session: requests.Session,
    bot_token: str,
    chat_id: str,
    file_path: Path,
    caption: str,
) -> None:
    api_url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    with file_path.open("rb") as f:
        resp = session.post(
            api_url,
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (file_path.name, f, "application/octet-stream")},
            timeout=1800,
        )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def parse_episode_selection(selection: str, available_numbers: Set[int]) -> Tuple[List[int], List[str], List[int]]:
    raw = selection.strip().lower()
    if not raw or raw in {"all", "*"}:
        ordered = sorted(available_numbers)
        return ordered, [], []

    selected: Set[int] = set()
    invalid_tokens: List[str] = []

    tokens = [t for t in re.split(r"[,\s]+", raw) if t]
    for token in tokens:
        range_match = re.fullmatch(r"(\d+)-(\d+)", token)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start <= end:
                selected.update(range(start, end + 1))
            else:
                selected.update(range(end, start + 1))
            continue

        if token.isdigit():
            selected.add(int(token))
            continue

        invalid_tokens.append(token)

    not_available = sorted(n for n in selected if n not in available_numbers)
    filtered = sorted(n for n in selected if n in available_numbers)
    return filtered, invalid_tokens, not_available


def find_existing_episode_file(out_dir: Path, episode_num: int) -> Optional[Path]:
    if not out_dir.exists():
        return None

    ep_re = re.compile(
        rf"(?i)(?:\be0*{episode_num}\b|\bs\d{{1,3}}e0*{episode_num}\b|\bepisode[_\-\s]*0*{episode_num}\b|\bep[_\-\s]*0*{episode_num}\b)"
    )

    for file_path in out_dir.iterdir():
        if not file_path.is_file():
            continue
        if file_path.stat().st_size <= 0:
            continue
        if file_path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        if ep_re.search(file_path.stem):
            return file_path

    return None


def _download_file_once(
    session: requests.Session,
    file_url: str,
    out_path: Path,
    *,
    headers: Dict[str, str],
    verify: Optional[bool] = None,
) -> None:
    request_kwargs = {"headers": headers, "stream": True, "timeout": 90}
    if verify is not None:
        request_kwargs["verify"] = verify

    with session.get(file_url, **request_kwargs) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", "0"))
        downloaded = 0
        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        with tmp_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded * 100 // total
                    print(f"    {downloaded / (1024 * 1024):.1f} MB / {total / (1024 * 1024):.1f} MB ({pct}%)", end="\r")
                else:
                    print(f"    {downloaded / (1024 * 1024):.1f} MB", end="\r")
        print(" " * 80, end="\r")
        tmp_path.replace(out_path)


def download_file(
    session: requests.Session,
    file_url: str,
    out_path: Path,
    referer: Optional[str] = None,
    *,
    insecure_fallback: bool = True,
) -> None:
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer

    try:
        _download_file_once(session, file_url, out_path, headers=headers)
    except requests.exceptions.SSLError:
        if not insecure_fallback:
            raise
        print("  - SSL certificate issue. Retrying without certificate verification...")
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _download_file_once(session, file_url, out_path, headers=headers, verify=False)


def main() -> int:
    configure_stdio_utf8()

    parser = argparse.ArgumentParser(description="Download episodes from an ak.sv series page.")
    parser.add_argument("--series-url", help="Series page URL, e.g. https://ak.sv/series/...")
    parser.add_argument(
        "--episodes",
        help='Episode selection. Examples: "10-14" or "10,12,14" or "10-14,20". Use "all" for all episodes.',
    )
    parser.add_argument(
        "--start-episode",
        type=int,
        default=None,
        help="Compatibility option: download from this episode to the end (ignored if --episodes is set).",
    )
    parser.add_argument(
        "--quality",
        default="best",
        help='Quality filter: "best" (default), "all", "720", "1080", etc.',
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification for all requests (use only if needed).",
    )
    parser.add_argument(
        "--telegram-token",
        default=os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        help="Telegram bot token (CLI > env var > hardcoded value in file).",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        help="Telegram chat ID (CLI > env var > hardcoded value in file).",
    )
    parser.add_argument(
        "--telegram-send-existing",
        action="store_true",
        help="If episode file already exists, send it to Telegram instead of skipping silently.",
    )
    parser.add_argument("--output-dir", default="downloads", help="Output folder for files")
    parser.add_argument("--dry-run", action="store_true", help="Only print discovered links without downloading")
    args = parser.parse_args()

    args.telegram_token = (args.telegram_token or "").strip()
    args.telegram_chat_id = (args.telegram_chat_id or "").strip()

    series_url = (args.series_url or "").strip()
    if not series_url:
        if sys.stdin.isatty():
            try:
                series_url = input("Enter series URL: ").strip()
            except EOFError:
                series_url = ""
        if not series_url:
            print("Error: series URL is required. Pass --series-url or enter it interactively.")
            return 1

    if bool(args.telegram_token) ^ bool(args.telegram_chat_id):
        print("Error: both --telegram-token and --telegram-chat-id are required together.")
        return 1

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if args.insecure:
        session.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        print("SSL verification: disabled (--insecure)")

    telegram_enabled = bool(args.telegram_token and args.telegram_chat_id)
    series_name = extract_series_name(series_url)
    if telegram_enabled:
        print(f"Telegram upload: enabled for chat {args.telegram_chat_id}")

    print(f"[1/4] Reading series page: {series_url}")
    episodes = get_episode_pages(session, series_url)
    available_numbers = [n for n, _ in episodes]
    available_set = set(available_numbers)
    episode_map = {n: u for n, u in episodes}

    print(
        f"Found {len(available_numbers)} episode(s) on series: "
        f"{available_numbers[0]} -> {available_numbers[-1]}"
    )

    selected_numbers: List[int]
    if args.episodes:
        selected_numbers, invalid_tokens, not_available = parse_episode_selection(args.episodes, available_set)
        if invalid_tokens:
            print(f"Invalid tokens in --episodes: {invalid_tokens}")
            return 1
        if not_available:
            print(f"These episode numbers are not available on the page and will be skipped: {not_available}")
    elif args.start_episode is not None:
        selected_numbers = [n for n in available_numbers if n >= args.start_episode]
    else:
        if sys.stdin.isatty():
            prompt = 'Enter episodes ("all", "10-14", "10,12,14", "10-14,20"): '
            try:
                user_input = input(prompt).strip()
            except EOFError:
                user_input = "all"
        else:
            user_input = "all"
        selected_numbers, invalid_tokens, not_available = parse_episode_selection(user_input, available_set)
        if invalid_tokens:
            print(f"Invalid input tokens: {invalid_tokens}")
            return 1
        if not_available:
            print(f"These episode numbers are not available and will be skipped: {not_available}")

    target_episodes = [(n, episode_map[n]) for n in selected_numbers if n in episode_map]

    if not target_episodes:
        print("No episodes selected for processing.")
        return 1

    print(f"Found {len(target_episodes)} episode(s) to process: {[n for n, _ in target_episodes]}")
    quality_choice = normalize_quality_choice(args.quality)
    print(f"Quality mode: {quality_choice}")

    for index, (ep_num, ep_url) in enumerate(target_episodes, start=1):
        print(f"\n[{index}/{len(target_episodes)}] Episode {ep_num}: {ep_url}")
        try:
            existing_file = find_existing_episode_file(out_dir, ep_num)
            if existing_file:
                print(f"  - Skipped (already exists): {existing_file.name}")
                if telegram_enabled and args.telegram_send_existing and not args.dry_run:
                    try:
                        caption = build_telegram_caption(series_name, ep_num)
                        print("  - Sending existing file to Telegram...")
                        send_file_to_telegram(session, args.telegram_token, args.telegram_chat_id, existing_file, caption)
                        print("  - Sent to Telegram.")
                    except Exception as e:
                        print(f"  - Telegram send failed: {e}")
                continue

            download_options = get_download_options(session, ep_url)
            if not download_options:
                print("  - No go download links found on episode page.")
                continue

            selected_options = select_download_options(download_options, quality_choice)
            if not selected_options:
                available = format_available_qualities(download_options)
                print(f"  - Requested quality '{args.quality}' is unavailable. Available: {available}")
                continue

            print(f"  - Available qualities: {format_available_qualities(download_options)}")

            media_link = None
            selected_download_page = None

            for go, _, _ in selected_options:
                download_page = resolve_download_page(session, go)
                if not download_page:
                    continue
                media_links = extract_media_links(session, download_page)
                if not media_links:
                    continue
                media_link = media_links[0]
                selected_download_page = download_page
                break

            if not media_link:
                print("  - Could not resolve a direct media link.")
                continue

            filename = safe_filename_from_url(media_link, ep_num)
            out_path = out_dir / filename
            print(f"  - Media: {media_link}")
            print(f"  - File:  {out_path}")

            if args.dry_run:
                continue

            if out_path.exists() and out_path.stat().st_size > 0:
                print("  - Skipped (already exists).")
                if telegram_enabled and args.telegram_send_existing and not args.dry_run:
                    try:
                        caption = build_telegram_caption(series_name, ep_num)
                        print("  - Sending existing file to Telegram...")
                        send_file_to_telegram(session, args.telegram_token, args.telegram_chat_id, out_path, caption)
                        print("  - Sent to Telegram.")
                    except Exception as e:
                        print(f"  - Telegram send failed: {e}")
                continue

            print("  - Downloading...")
            download_file(session, media_link, out_path, referer=selected_download_page, insecure_fallback=not args.insecure)
            print(f"  - Saved: {out_path}")

            if telegram_enabled:
                try:
                    caption = build_telegram_caption(series_name, ep_num)
                    print("  - Sending to Telegram...")
                    send_file_to_telegram(session, args.telegram_token, args.telegram_chat_id, out_path, caption)
                    print("  - Sent to Telegram.")
                except Exception as e:
                    print(f"  - Telegram send failed: {e}")

        except requests.exceptions.SSLError as e:
            print(f"  - SSL error: {e}")
            print("  - Tip: rerun with --insecure if this host has an invalid certificate.")
        except requests.HTTPError as e:
            print(f"  - HTTP error: {e}")
        except requests.RequestException as e:
            print(f"  - Network error: {e}")
        except Exception as e:
            print(f"  - Error: {e}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
