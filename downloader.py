"""
APK Downloader — core logic

Sources tried in order (all verified working):
  1. APKCombo         — direct URL from package name, encoded CDN redirect
  2. APKPure API      — direct d.apkpure.com download endpoint
  3. mi9.com          — downloadapks.androidcontents.com CDN
  4. Aptoide API      — public REST API with direct file URL
  5. APKPure scraper  — search-page scrape fallback

XAPK / ZIP bundles are automatically unpacked → pure base.apk returned.
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
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

ProgressCallback = Optional[Callable[[int, int], None]]


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def detect_input_type(text: str) -> tuple:
    """
    Returns (type, value):
      - Google Play URLs → ('app_id', package_name)
      - http/https URLs  → ('url', url)
      - anything else    → ('app_id', text)
    """
    t = text.strip()
    m = re.search(r"play\.google\.com/store/apps/details.*[?&]id=([A-Za-z0-9_.]+)", t)
    if m:
        return "app_id", m.group(1)
    if t.startswith(("http://", "https://")):
        return "url", t
    return "app_id", t


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def stream_download(url: str, dest_path: Path, progress_cb: ProgressCallback = None) -> Path:
    """Stream-download url → dest_path; calls progress_cb(bytes_done, total) per chunk."""
    session = make_session()
    with session.get(url, stream=True, timeout=90) as r:
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
    """Pick the main APK inside an XAPK bundle."""
    if "base.apk" in namelist:
        return "base.apk"
    non_config = [n for n in namelist if n.endswith(".apk") and not n.startswith("config.")]
    if non_config:
        return non_config[0]
    apks = [n for n in namelist if n.endswith(".apk")]
    return apks[0] if apks else ""


def extract_base_apk(path: Path) -> Path:
    """
    If path is an XAPK / ZIP bundle, extract the main APK and delete the bundle.
    Returns the pure .apk path (original path returned unchanged if already a plain APK).
    """
    try:
        with zipfile.ZipFile(path) as z:
            main = _find_main_apk(z.namelist())
            if not main:
                return path
            data = z.read(main)
        out = path.parent / "base.apk"
        out.write_bytes(data)
        try:
            path.unlink()
        except OSError:
            pass
        return out
    except zipfile.BadZipFile:
        return path


# ---------------------------------------------------------------------------
# Source 1 — APKCombo
# Any slug works; package name is the real key. No search needed.
# ---------------------------------------------------------------------------

def _apkcombo_base(pkg: str) -> str:
    return f"https://apkcombo.com/{pkg.replace('.', '-')}/{pkg}"


def search_apkcombo(pkg: str) -> dict:
    s = make_session()
    r = s.get(_apkcombo_base(pkg) + "/", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    # App name: h1 is most reliable on APKCombo, og:title as fallback
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True) and h1.get_text(strip=True) != pkg:
        name = h1.get_text(strip=True)
    else:
        og = soup.find("meta", attrs={"property": "og:title"})
        raw = og.get("content", "") if og else ""
        name = re.sub(r"\s+APK\b.*$", "", raw, flags=re.IGNORECASE).strip() or pkg
    # Version from JSON-LD softwareVersion
    ver_m = re.search(r'"softwareVersion"\s*:\s*"([^"]+)"', r.text)
    version = ver_m.group(1) if ver_m else ""
    # Size
    size_m = re.search(r"(\d+(?:\.\d+)?\s*MB)", r.text)
    size = size_m.group(1) if size_m else ""
    # Min Android version (e.g. "5.0+")
    req_m = re.search(r"Android\s+(\d+\.\d+\+?)", r.text)
    min_android = f"Android {req_m.group(1)}" if req_m else ""
    return {"name": name, "version": version, "size": size, "min_android": min_android, "_package": pkg}


def download_apkcombo(pkg: str, save_dir: Path, progress_cb: ProgressCallback = None) -> Path:
    s = make_session()
    r = s.get(_apkcombo_base(pkg) + "/download/apk", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    link = soup.find("a", href=lambda h: h and "/d?u=" in str(h))
    if not link:
        raise ValueError("no download link on page")
    href = link["href"]
    if href.startswith("/"):
        href = "https://apkcombo.com" + href
    r2 = s.get(href, allow_redirects=True, timeout=30)
    r2.raise_for_status()
    cdn = r2.url
    fname = _safe_filename(cdn.split("?")[0].split("/")[-1] or f"{pkg}.xapk")
    return stream_download(cdn, save_dir / fname, progress_cb)


# ---------------------------------------------------------------------------
# Source 2 — APKPure direct API  (d.apkpure.com/b/APK or XAPK)
# Works for popular apps; 404 for less-known ones.
# ---------------------------------------------------------------------------

def search_apkpure(pkg: str) -> dict:
    s = make_session()
    for fmt in ("APK", "XAPK"):
        r = s.head(f"https://d.apkpure.com/b/{fmt}/{pkg}?version=latest", allow_redirects=True, timeout=15)
        if r.status_code == 200:
            cl = r.headers.get("Content-Length", "")
            size = f"{int(cl) // 1_048_576} MB" if cl else ""
            return {"name": pkg, "version": "latest", "size": size, "_fmt": fmt, "_package": pkg}
    raise ValueError("not found via direct API")


def download_apkpure(pkg: str, save_dir: Path, progress_cb: ProgressCallback = None, fmt: str = "APK") -> Path:
    s = make_session()
    r = s.head(f"https://d.apkpure.com/b/{fmt}/{pkg}?version=latest", allow_redirects=True, timeout=15)
    if r.status_code != 200:
        if fmt == "APK":
            return download_apkpure(pkg, save_dir, progress_cb, "XAPK")
        raise ValueError("not available")
    cdn = r.url
    fname = _safe_filename(cdn.split("?")[0].split("/")[-1] or f"{pkg}.apk")
    return stream_download(cdn, save_dir / fname, progress_cb)


# ---------------------------------------------------------------------------
# Source 3 — mi9.com  (downloadapks.androidcontents.com CDN)
# Verified: returns real APK files with signed token URLs.
# ---------------------------------------------------------------------------

def _mi9_cdn_link(pkg: str, session: requests.Session) -> str:
    """Visit app page first (sets cookies), then fetch download page with Referer."""
    app_url = f"https://mi9.com/package/{pkg}/"
    session.get(app_url, timeout=15)  # warms up cookies
    session.headers.update({"Referer": app_url})
    r = session.get(f"https://mi9.com/package/{pkg}/download/", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for a in soup.find_all("a", href=True):
        if "androidcontents.com" in a["href"]:
            return a["href"]
    raise ValueError("no CDN link found on mi9 download page")


def search_mi9(pkg: str) -> dict:
    s = make_session()
    r = s.get(f"https://mi9.com/package/{pkg}/", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else pkg
    if not name or name.lower() in ("404", "not found"):
        raise ValueError("app page not found on mi9")
    # verify download page also has a CDN link
    _mi9_cdn_link(pkg, s)
    size_m = re.search(r"(\d+(?:\.\d+)?\s*MB)", r.text)
    ver_m = re.search(r"Version[:\s]+([0-9.]+)", r.text, re.I)
    return {
        "name": name,
        "version": ver_m.group(1) if ver_m else "",
        "size": size_m.group(1) if size_m else "",
        "_package": pkg,
    }


def download_mi9(pkg: str, save_dir: Path, progress_cb: ProgressCallback = None) -> Path:
    s = make_session()
    cdn = _mi9_cdn_link(pkg, s)
    rh = s.head(cdn, allow_redirects=True, timeout=15)
    rh.raise_for_status()
    fname = _safe_filename(f"{pkg}.apk")
    return stream_download(cdn, save_dir / fname, progress_cb)


# ---------------------------------------------------------------------------
# Source 4 — Aptoide public REST API
# ---------------------------------------------------------------------------

def search_aptoide(pkg: str) -> dict:
    s = make_session()
    r = s.get(f"https://ws75.aptoide.com/api/7/app/getMeta/package_name/{pkg}", timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("info", {}).get("status") != "OK":
        raise ValueError("not found on Aptoide")
    app = data.get("data", {})
    dl_url = app.get("file", {}).get("path", "")
    if not dl_url:
        raise ValueError("no download path in Aptoide response")
    size_b = app.get("file", {}).get("filesize", 0)
    return {
        "name": app.get("name", pkg),
        "version": app.get("file", {}).get("vername", ""),
        "size": f"{size_b // 1_048_576} MB" if size_b else "",
        "_direct_url": dl_url,
        "_package": pkg,
    }


def download_aptoide(pkg: str, save_dir: Path, progress_cb: ProgressCallback = None) -> Path:
    meta = search_aptoide(pkg)
    url = meta["_direct_url"]
    fname = _safe_filename(url.split("?")[0].split("/")[-1] or f"{pkg}.apk")
    return stream_download(url, save_dir / fname, progress_cb)


# ---------------------------------------------------------------------------
# Source 5 — APKPure scraper (search-page fallback for obscure apps)
# ---------------------------------------------------------------------------

def search_apkpure_scrape(pkg: str) -> dict:
    s = make_session()
    r = s.get(f"https://apkpure.com/search?q={pkg}", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if pkg in href and href.startswith("http") and "/search" not in href:
            r2 = s.get(href, timeout=15)
            r2.raise_for_status()
            soup2 = BeautifulSoup(r2.text, "lxml")
            h1 = soup2.find("h1")
            name = h1.get_text(strip=True) if h1 else pkg
            size_m = re.search(r"(\d+(?:\.\d+)?\s*MB)", r2.text)
            return {
                "name": name,
                "version": "",
                "size": size_m.group(1) if size_m else "",
                "_app_page": href,
                "_package": pkg,
            }
    raise ValueError("not found in APKPure search")


def download_apkpure_scrape(pkg: str, save_dir: Path, progress_cb: ProgressCallback = None) -> Path:
    meta = search_apkpure_scrape(pkg)
    s = make_session()
    r = s.get(meta["_app_page"].rstrip("/") + "/download", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    link = soup.find("a", href=re.compile(r"https://.*\.(apk|xapk)"))
    if not link:
        tag = soup.find(attrs={"data-dt-url": True})
        if not tag:
            raise ValueError("no download link found on APKPure app page")
        href = tag["data-dt-url"]
    else:
        href = link["href"]
    fname = _safe_filename(href.split("?")[0].split("/")[-1] or f"{pkg}.apk")
    return stream_download(href, save_dir / fname, progress_cb)


# ---------------------------------------------------------------------------
# Source 6 — Uptodown  (slug = dots→hyphens, e.g. com-whatsapp.en.uptodown.com)
# Works when the subdomain exists. Download via /download page link.
# ---------------------------------------------------------------------------

def _uptodown_base(pkg: str) -> str:
    return f"https://{pkg.replace('.', '-')}.en.uptodown.com/android"


def search_uptodown(pkg: str) -> dict:
    s = make_session()
    r = s.get(_uptodown_base(pkg), timeout=15)
    if r.status_code != 200:
        raise ValueError(f"Uptodown: no page for {pkg} (HTTP {r.status_code})")
    soup = BeautifulSoup(r.text, "lxml")
    h1 = soup.find("h1", {"id": "detail-app-name"}) or soup.find("h1")
    name = h1.get_text(strip=True) if h1 else pkg
    size_m = re.search(r"(\d+(?:\.\d+)?\s*MB)", r.text)
    ver_tag = soup.find("div", {"id": "detail-version"}) or soup.find(attrs={"itemprop": "softwareVersion"})
    version = ver_tag.get_text(strip=True) if ver_tag else ""
    return {
        "name": name,
        "version": version,
        "size": size_m.group(1) if size_m else "",
        "_package": pkg,
    }


def download_uptodown(pkg: str, save_dir: Path, progress_cb: ProgressCallback = None) -> Path:
    s = make_session()
    base = _uptodown_base(pkg)
    r = s.get(base, timeout=15)
    if r.status_code != 200:
        raise ValueError(f"Uptodown: no page for {pkg}")
    soup = BeautifulSoup(r.text, "lxml")
    # Extract file_id for the download button
    tag = soup.find(attrs={"data-file-id": True})
    if not tag:
        raise ValueError("Uptodown: no file-id found on app page")
    file_id = tag["data-file-id"]
    # The actual download link is on the download page
    r2 = s.get(f"{base}/download", timeout=15)
    r2.raise_for_status()
    soup2 = BeautifulSoup(r2.text, "lxml")
    # Find direct CDN link (usually a button with data-url or an <a> pointing to utdstc/cdn)
    cdn_tag = soup2.find("a", href=re.compile(r"dw\.uptodown\.com|\.apk|\.xapk"))
    if cdn_tag:
        cdn_url = cdn_tag["href"]
    else:
        # Try the version endpoint which provides a redirect to the file
        rv = s.get(f"{base}/download/{file_id}", timeout=15, allow_redirects=True)
        if rv.headers.get("Content-Type", "").startswith("application"):
            fname = _safe_filename(f"{pkg}.apk")
            return stream_download(rv.url, save_dir / fname, progress_cb)
        raise ValueError("Uptodown: could not resolve CDN download URL")
    fname = _safe_filename(cdn_url.split("?")[0].split("/")[-1] or f"{pkg}.apk")
    return stream_download(cdn_url, save_dir / fname, progress_cb)


# ---------------------------------------------------------------------------
# Version history helpers
# ---------------------------------------------------------------------------

def _list_versions_apkcombo(pkg: str, limit: int) -> list:
    s = make_session()
    r = s.get(_apkcombo_base(pkg) + "/old-versions/", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    results = []
    for a in soup.find_all("a", href=re.compile(r"versionCode=\d+")):
        href = a["href"]
        if href.startswith("/"):
            href = "https://apkcombo.com" + href
        ver_m = re.search(r"(\d+[\d.]+)", a.get_text(strip=True))
        version = ver_m.group(1) if ver_m else ""
        parent = a.parent or a
        size_m = re.search(r"\d+(?:\.\d+)?\s*MB", parent.get_text())
        # Convert /download/phone or /download/apk?versionCode=X
        dl_page = re.sub(r"/download/[^?]+", "/download/apk", href)
        results.append({
            "version": version,
            "size": size_m.group(0) if size_m else "",
            "date": "",
            "_dl_page": dl_page,
            "_source": "apkcombo",
        })
        if len(results) >= limit:
            break
    if not results:
        raise ValueError("no old versions found on APKCombo")
    return results


def _list_versions_uptodown(pkg: str, limit: int) -> list:
    s = make_session()
    base = _uptodown_base(pkg)
    r = s.get(base + "/versions", timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    results = []
    for item in soup.select("[data-url], div.version-item, li.version"):
        ver_tag = item.find(class_=re.compile(r"version|ver", re.I))
        version = ver_tag.get_text(strip=True) if ver_tag else item.get_text(strip=True)[:20]
        size_m = re.search(r"\d+(?:\.\d+)?\s*MB", item.get_text())
        date_tag = item.find("time") or item.find(class_=re.compile(r"date", re.I))
        date = date_tag.get_text(strip=True) if date_tag else ""
        dl_url = item.get("data-url", "")
        if not dl_url:
            a = item.find("a", href=True)
            dl_url = a["href"] if a else ""
        if dl_url and not dl_url.startswith("http"):
            dl_url = base.rstrip("/") + "/" + dl_url.lstrip("/")
        if not dl_url:
            continue
        results.append({
            "version": version,
            "size": size_m.group(0) if size_m else "",
            "date": date,
            "_dl_page": dl_url,
            "_source": "uptodown",
        })
        if len(results) >= limit:
            break
    if not results:
        raise ValueError("no versions found on Uptodown")
    return results


def list_versions(pkg: str, limit: int = 10) -> list:
    """Return up to `limit` older versions for pkg. Each dict: version, size, date, _dl_page, _source."""
    for fn in (_list_versions_apkcombo, _list_versions_uptodown):
        try:
            return fn(pkg, limit)
        except Exception:
            continue
    return []


def _download_apkcombo_from_page(dl_page: str, pkg: str, save_dir: Path, progress_cb: ProgressCallback) -> Path:
    s = make_session()
    r = s.get(dl_page, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    link = soup.find("a", href=lambda h: h and "/d?u=" in str(h))
    if not link:
        raise ValueError("no download link on APKCombo version page")
    href = link["href"]
    if href.startswith("/"):
        href = "https://apkcombo.com" + href
    r2 = s.get(href, allow_redirects=True, timeout=30)
    r2.raise_for_status()
    cdn = r2.url
    fname = _safe_filename(cdn.split("?")[0].split("/")[-1] or f"{pkg}.apk")
    path = stream_download(cdn, save_dir / fname, progress_cb)
    return extract_base_apk(path)


def download_version(pkg: str, version_info: dict, save_dir: Path, progress_cb: ProgressCallback = None) -> Path:
    """Download a specific version given a dict returned by list_versions()."""
    source = version_info.get("_source", "")
    dl_page = version_info.get("_dl_page", "")
    if source == "apkcombo":
        return _download_apkcombo_from_page(dl_page, pkg, save_dir, progress_cb)
    if source == "uptodown":
        fname = _safe_filename(f"{pkg}.apk")
        path = stream_download(dl_page, save_dir / fname, progress_cb)
        return extract_base_apk(path)
    raise ValueError(f"unknown source: {source}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SOURCES = [
    ("APKCombo",       search_apkcombo,       download_apkcombo),
    ("APKPure",        search_apkpure,         download_apkpure),
    ("Aptoide",        search_aptoide,         download_aptoide),
    ("Uptodown",       search_uptodown,        download_uptodown),
    ("APKPure (scan)", search_apkpure_scrape,  download_apkpure_scrape),
    ("mi9",            search_mi9,             download_mi9),
]


def search_app(package_name: str) -> dict:
    """Try each source in order; return first hit or raise RuntimeError."""
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
    """Download APK/XAPK from a direct URL; extracts base.apk if it's a bundle."""
    fname = _safe_filename(url.split("?")[0].split("/")[-1] or "download.apk")
    path = stream_download(url, save_dir / fname, progress_cb)
    return extract_base_apk(path)


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    pkg = sys.argv[1] if len(sys.argv) > 1 else "pdf.scanner.scannerapp.free.pdfscanner"
    print(f"Searching: {pkg}")
    try:
        meta = search_app(pkg)
        print(f"Found: {meta['name']}  |  size: {meta.get('size','?')}")
    except RuntimeError as e:
        print(f"Not found: {e}")
        sys.exit(1)

    def _cb(done, total):
        if total:
            print(f"\r{done*100//total}%  {done//1024}KB/{total//1024}KB", end="", flush=True)

    path, src = download_by_app_id(pkg, progress_cb=_cb)
    print(f"\nSaved: {path}  (via {src})")
