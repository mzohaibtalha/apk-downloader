"""
APK Downloader — core logic

Sources (tried in order):
  1. APKCombo  — direct URL construction from package name, no search needed
  2. APKPure   — direct download API endpoint (d.apkpure.com)

XAPK files are automatically unpacked → base.apk is returned to the caller.
"""

import os
import re
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

ProgressCallback = Optional[Callable[[int, int], None]]


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def extract_package_from_play_url(text: str) -> str:
    """If text is a Google Play Store URL, return the package ID. Otherwise return ''."""
    m = re.search(r"play\.google\.com/store/apps/details.*[?&]id=([A-Za-z0-9_.]+)", text)
    return m.group(1) if m else ""


def detect_input_type(text: str) -> tuple:
    """
    Returns (type, value) where type is 'url' or 'app_id'.
    Google Play Store URLs are parsed to extract the package ID.
    """
    t = text.strip()
    pkg = extract_package_from_play_url(t)
    if pkg:
        return "app_id", pkg
    if t.startswith(("http://", "https://")):
        return "url", t
    return "app_id", t


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def stream_download(url: str, dest_path: Path, progress_cb: ProgressCallback = None) -> Path:
    """Stream-download url → dest_path; calls progress_cb(bytes_done, total) each chunk."""
    session = make_session()
    with session.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
    return dest_path


def _find_main_apk(namelist: list) -> str:
    """
    Pick the main APK from an XAPK's namelist.
    Priority: 'base.apk' > largest non-config APK > first APK found.
    Config splits are named like 'config.*.apk' and are skipped.
    """
    if "base.apk" in namelist:
        return "base.apk"
    apks = [n for n in namelist if n.endswith(".apk") and not n.startswith("config.")]
    if apks:
        return apks[0]
    # fallback: any apk
    apks_all = [n for n in namelist if n.endswith(".apk")]
    return apks_all[0] if apks_all else ""


def extract_base_apk(path: Path) -> Path:
    """
    If path is an XAPK / ZIP bundle, extract the main APK and delete the bundle.
    Returns the pure .apk path (original path if it was already a plain APK).
    """
    try:
        with zipfile.ZipFile(path) as z:
            main = _find_main_apk(z.namelist())
            if not main:
                return path
            out = path.parent / "base.apk"
            data = z.read(main)
        # file is closed — safe to write and delete
        out.write_bytes(data)
        try:
            path.unlink()
        except OSError:
            pass
        return out
    except zipfile.BadZipFile:
        pass
    return path


# ---------------------------------------------------------------------------
# Source 1 — APKCombo
#
# Key discovery: APKCombo uses the package name as the real identifier.
# The slug (first path segment) can be *anything* — even the pkg name with
# dots replaced by hyphens. No search endpoint is needed.
# ---------------------------------------------------------------------------

def _apkcombo_url(package_name: str) -> str:
    slug = package_name.replace(".", "-")
    return f"https://apkcombo.com/{slug}/{package_name}"


def search_apkcombo(package_name: str) -> dict:
    """Fetch app metadata from APKCombo. Raises ValueError if not found."""
    session = make_session()
    r = session.get(_apkcombo_url(package_name) + "/", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else package_name

    # Version — look for a meta or span near "Version"
    version = ""
    ver_label = soup.find(string=re.compile(r"^Version$", re.I))
    if ver_label:
        sib = ver_label.find_parent()
        if sib:
            nxt = sib.find_next_sibling()
            version = nxt.get_text(strip=True) if nxt else ""

    # Size — find the first "XX MB" occurrence
    size = ""
    size_match = re.search(r"(\d+(?:\.\d+)?\s*MB)", r.text)
    if size_match:
        size = size_match.group(1)

    return {
        "name": name,
        "version": version,
        "size": size,
        "source": "APKCombo",
        "_package": package_name,
    }


def download_apkcombo(
    package_name: str,
    save_dir: Path,
    progress_cb: ProgressCallback = None,
) -> Path:
    session = make_session()
    dl_page_url = _apkcombo_url(package_name) + "/download/apk"
    r = session.get(dl_page_url, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    link = soup.find("a", href=lambda h: h and "/d?u=" in str(h))
    if not link:
        raise ValueError(f"APKCombo: no download link on page for '{package_name}'")

    href = link["href"]
    if href.startswith("/"):
        href = "https://apkcombo.com" + href

    # Follow redirect to actual CDN URL
    r2 = session.get(href, allow_redirects=True, timeout=30)
    r2.raise_for_status()
    cdn_url = r2.url

    filename = cdn_url.split("?")[0].split("/")[-1] or f"{package_name}.xapk"
    dest = save_dir / _safe_filename(filename)
    return stream_download(cdn_url, dest, progress_cb)


# ---------------------------------------------------------------------------
# Source 2 — APKPure direct download API
#
# endpoint: https://d.apkpure.com/b/APK/{package_name}?version=latest
# Works for popular apps; less popular apps return 404.
# ---------------------------------------------------------------------------

def _apkpure_cdn_url(package_name: str, fmt: str = "APK") -> str:
    return f"https://d.apkpure.com/b/{fmt}/{package_name}?version=latest"


def search_apkpure(package_name: str) -> dict:
    """Check if APKPure has the app (HEAD request). Raises ValueError if not."""
    session = make_session()
    for fmt in ("APK", "XAPK"):
        url = _apkpure_cdn_url(package_name, fmt)
        r = session.head(url, allow_redirects=True, timeout=15)
        if r.status_code == 200:
            content_length = r.headers.get("Content-Length", "")
            size = f"{int(content_length) // 1_048_576} MB" if content_length else "Unknown"
            return {
                "name": package_name,
                "version": "latest",
                "size": size,
                "source": "APKPure",
                "_fmt": fmt,
                "_package": package_name,
            }
    raise ValueError(f"APKPure: '{package_name}' returned 404 for both APK and XAPK")


def download_apkpure(
    package_name: str,
    save_dir: Path,
    progress_cb: ProgressCallback = None,
    fmt: str = "APK",
) -> Path:
    session = make_session()
    url = _apkpure_cdn_url(package_name, fmt)
    r = session.head(url, allow_redirects=True, timeout=15)
    if r.status_code != 200:
        if fmt == "APK":
            return download_apkpure(package_name, save_dir, progress_cb, fmt="XAPK")
        raise ValueError(f"APKPure: '{package_name}' not available (404)")

    cdn_url = r.url
    filename = cdn_url.split("?")[0].split("/")[-1] or f"{package_name}.apk"
    dest = save_dir / _safe_filename(filename)
    return stream_download(cdn_url, dest, progress_cb)


# ---------------------------------------------------------------------------
# Source 3 — Aptoide public API
# ---------------------------------------------------------------------------

def search_aptoide(package_name: str) -> dict:
    """Query Aptoide's public REST API. Raises ValueError if not found."""
    session = make_session()
    url = f"https://ws75.aptoide.com/api/7/app/getMeta/package_name/{package_name}"
    r = session.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("info", {}).get("status") != "OK":
        raise ValueError(f"Aptoide: '{package_name}' not found")
    app = data.get("data", {})
    name = app.get("name", package_name)
    version = app.get("file", {}).get("vername", "")
    size_bytes = app.get("file", {}).get("filesize", 0)
    size = f"{size_bytes // 1_048_576} MB" if size_bytes else "Unknown"
    download_url = app.get("file", {}).get("path", "")
    if not download_url:
        raise ValueError(f"Aptoide: no download path for '{package_name}'")
    return {
        "name": name,
        "version": version,
        "size": size,
        "source": "Aptoide",
        "_package": package_name,
        "_direct_url": download_url,
    }


def download_aptoide(
    package_name: str,
    save_dir: Path,
    progress_cb: ProgressCallback = None,
) -> Path:
    meta = search_aptoide(package_name)
    url = meta["_direct_url"]
    filename = url.split("?")[0].split("/")[-1] or f"{package_name}.apk"
    dest = save_dir / _safe_filename(filename)
    return stream_download(url, dest, progress_cb)


# ---------------------------------------------------------------------------
# Source 4 — APKPure scraping (search page fallback for less-known apps)
# ---------------------------------------------------------------------------

def search_apkpure_scrape(package_name: str) -> dict:
    """Scrape APKPure search page to find the app download page."""
    session = make_session()
    r = session.get(f"https://apkpure.com/search?q={package_name}", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    # Look for an app page link whose href contains the package name
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if package_name in href and href.startswith("http") and "/search" not in href:
            # fetch that page to confirm and get metadata
            r2 = session.get(href, timeout=15)
            r2.raise_for_status()
            soup2 = BeautifulSoup(r2.text, "lxml")
            h1 = soup2.find("h1")
            name = h1.get_text(strip=True) if h1 else package_name
            size_m = re.search(r"(\d+(?:\.\d+)?\s*MB)", r2.text)
            size = size_m.group(1) if size_m else "Unknown"
            return {
                "name": name,
                "version": "",
                "size": size,
                "source": "APKPure",
                "_package": package_name,
                "_app_page": href,
            }
    raise ValueError(f"APKPure scrape: '{package_name}' not found in search results")


def download_apkpure_scrape(
    package_name: str,
    save_dir: Path,
    progress_cb: ProgressCallback = None,
) -> Path:
    meta = search_apkpure_scrape(package_name)
    session = make_session()
    app_page = meta["_app_page"].rstrip("/") + "/download"
    r = session.get(app_page, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    # Find direct download anchor
    link = soup.find("a", href=re.compile(r"https://.*\.(apk|xapk)"))
    if not link:
        # Try meta refresh or data-url patterns
        tag = soup.find(attrs={"data-dt-url": True})
        if tag:
            link_href = tag["data-dt-url"]
        else:
            raise ValueError(f"APKPure scrape: no download link found for '{package_name}'")
    else:
        link_href = link["href"]
    filename = link_href.split("?")[0].split("/")[-1] or f"{package_name}.apk"
    dest = save_dir / _safe_filename(filename)
    return stream_download(link_href, dest, progress_cb)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SOURCES = [
    ("APKCombo",       search_apkcombo,        download_apkcombo),
    ("APKPure",        search_apkpure,          download_apkpure),
    ("Aptoide",        search_aptoide,          download_aptoide),
    ("APKPure (scan)", search_apkpure_scrape,   download_apkpure_scrape),
]


def search_app(package_name: str) -> dict:
    """Try each source in order; return first hit or raise AppNotFoundError."""
    for _, search_fn, _ in SOURCES:
        try:
            return search_fn(package_name)
        except Exception:
            continue
    raise RuntimeError("App not found.")


def download_by_app_id(
    package_name: str,
    save_dir: Path = DOWNLOAD_DIR,
    progress_cb: ProgressCallback = None,
) -> tuple:
    """Download APK by package name. Returns (Path, source_label)."""
    for label, _, dl_fn in SOURCES:
        try:
            path = dl_fn(package_name, save_dir, progress_cb)
            path = extract_base_apk(path)
            return path, label
        except Exception:
            continue
    raise RuntimeError("App not found. Please check the package ID and try again.")


def download_from_url(
    url: str,
    save_dir: Path = DOWNLOAD_DIR,
    progress_cb: ProgressCallback = None,
) -> Path:
    """Download APK/XAPK from a direct URL; extracts base.apk if it's an XAPK."""
    filename = url.split("?")[0].split("/")[-1] or "download.apk"
    dest = save_dir / _safe_filename(filename)
    path = stream_download(url, dest, progress_cb)
    return extract_base_apk(path)


# ---------------------------------------------------------------------------
# Quick CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    pkg = sys.argv[1] if len(sys.argv) > 1 else "pdf.scanner.scannerapp.free.pdfscanner"
    print(f"Searching: {pkg}")
    try:
        meta = search_app(pkg)
        print(f"Found on {meta['source']}: {meta['name']}  |  size: {meta['size']}")
    except RuntimeError as e:
        print(f"Not found: {e}")
        sys.exit(1)

    print("Downloading...")

    def _progress(done: int, total: int) -> None:
        if total:
            pct = done / total * 100
            bar = "#" * int(pct / 5)
            print(f"\r[{bar:<20}] {pct:.0f}%  {done//1024} KB / {total//1024} KB", end="", flush=True)

    path, source = download_by_app_id(pkg, progress_cb=_progress)
    print(f"\nSaved: {path}  (via {source})")
