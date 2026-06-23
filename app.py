import re
import tempfile
from pathlib import Path

import streamlit as st

# Android version → min SDK number mapping
_ANDROID_SDK = {
    "5.0": 21, "5.1": 22, "6.0": 23, "7.0": 24, "7.1": 25,
    "8.0": 26, "8.1": 27, "9.0": 28, "10.0": 29, "10": 29,
    "11.0": 30, "11": 30, "12.0": 31, "12": 31,
    "13.0": 33, "13": 33, "14.0": 34, "14": 34, "15.0": 35, "15": 35,
}

def _android_to_sdk(min_android: str) -> str:
    if not min_android:
        return "—"
    m = re.search(r"(\d+\.\d+|\d+)", min_android)
    if not m:
        return "—"
    sdk = _ANDROID_SDK.get(m.group(1))
    return f"SDK {sdk}" if sdk else "—"

from downloader import (
    detect_input_type,
    search_app,
    download_by_app_id,
    download_from_url,
    list_versions,
    download_version,
)

st.set_page_config(page_title="APK Downloader", page_icon="📦", layout="centered")

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("📦 APK Downloader")
st.caption("Download any Android app as a pure APK file — fast, free, and hassle-free.")

with st.sidebar:
    st.markdown("### How to use")
    st.markdown(
        """
1. Paste an **app package ID** or a **Google Play link**
2. Click **Search**
3. Click **Download APK**
4. Save the file to your device

**Examples:**
- `com.whatsapp`
- `com.instagram.android`
- Google Play URL
        """
    )
    st.divider()
    st.caption("Supports all Android apps. Files are ready to install on any Android device.")

# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
user_input = st.text_input(
    "App package ID or Google Play URL",
    placeholder="e.g.  com.whatsapp   or paste a Google Play link",
)

search_clicked = st.button("🔍 Search", use_container_width=True)

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
for key in ("meta", "search_error", "apk_bytes", "apk_filename", "versions", "ver_dl_idx"):
    if key not in st.session_state:
        st.session_state[key] = None

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
if search_clicked and user_input.strip():
    st.session_state.meta = None
    st.session_state.search_error = None
    st.session_state.apk_bytes = None
    st.session_state.apk_filename = None
    st.session_state.versions = None
    st.session_state.ver_dl_idx = None

    kind, value = detect_input_type(user_input.strip())

    if kind == "url":
        st.session_state.meta = {
            "name": "Direct download",
            "version": "—",
            "size": "—",
            "_url": value,
        }
    else:
        with st.spinner("Looking up the app…"):
            try:
                meta = search_app(value)
                meta["_package"] = value
                st.session_state.meta = meta
            except RuntimeError:
                st.session_state.search_error = "App not found. Please check the package ID and try again."
        if st.session_state.meta:
            with st.spinner("Loading version history…"):
                # Store empty list on failure so the expander still renders
                st.session_state.versions = list_versions(value, limit=10) or []

# ---------------------------------------------------------------------------
# Result card
# ---------------------------------------------------------------------------
if st.session_state.search_error:
    st.error(f"❌  {st.session_state.search_error}")

if st.session_state.meta:
    meta = st.session_state.meta
    is_url = "_url" in meta

    with st.container(border=True):
        if is_url:
            st.success("✅  Ready to download")
            st.markdown(f"`{meta['_url']}`")
        else:
            st.success(f"✅  {meta.get('name', 'App found')}")
            cols = st.columns(4)
            cols[0].metric("Version", meta.get("version") or "—")
            cols[1].metric("File Size", meta.get("size") or "—")
            cols[2].metric("Min Android", meta.get("min_android") or "—")
            cols[3].metric("Min SDK", _android_to_sdk(meta.get("min_android", "")))

    st.divider()

    if st.button("⬇️  Download Latest APK", use_container_width=True, type="primary"):
        st.session_state.apk_bytes = None
        st.session_state.apk_filename = None
        st.session_state.ver_dl_idx = None

        tmp_dir = Path(tempfile.mkdtemp())
        progress_bar = st.progress(0, text="Preparing download…")

        def _progress_cb(done: int, total: int) -> None:
            if total:
                frac = min(done / total, 1.0)
                progress_bar.progress(frac, text=f"Downloading…  {done/1_048_576:.1f} MB / {total/1_048_576:.1f} MB")
            else:
                progress_bar.progress(0, text=f"Downloading…  {done/1_048_576:.1f} MB")

        try:
            if is_url:
                apk_path = download_from_url(meta["_url"], tmp_dir, _progress_cb)
            else:
                apk_path, _ = download_by_app_id(meta["_package"], tmp_dir, _progress_cb)

            progress_bar.progress(1.0, text="Download complete!")
            with open(apk_path, "rb") as f:
                st.session_state.apk_bytes = f.read()
            st.session_state.apk_filename = apk_path.name
        except Exception:
            progress_bar.empty()
            st.error("❌  Download failed. Please try again.")

    # -----------------------------------------------------------------------
    # Version history
    # -----------------------------------------------------------------------
    if not is_url and st.session_state.versions is not None:
        versions = st.session_state.versions
        label = f"Version History  ({len(versions)} older versions)" if versions else "Version History"
        with st.expander(label):
            if not versions:
                st.caption("Version history could not be loaded for this app.")
            else:
                header_cols = st.columns([3, 2, 2, 2])
                header_cols[0].markdown("**Version**")
                header_cols[1].markdown("**Size**")
                header_cols[2].markdown("**Date**")
                header_cols[3].markdown("**Action**")
                st.divider()
                for i, v in enumerate(versions):
                    row = st.columns([3, 2, 2, 2])
                    row[0].write(v.get("version") or "—")
                    row[1].write(v.get("size") or "—")
                    row[2].write(v.get("date") or "—")
                    if row[3].button("⬇️", key=f"dl_v_{i}", help=f"Download v{v.get('version', '')}"):
                        st.session_state.ver_dl_idx = i
                        st.session_state.apk_bytes = None
                        st.session_state.apk_filename = None

    # -----------------------------------------------------------------------
    # Download a selected older version
    # -----------------------------------------------------------------------
    if st.session_state.ver_dl_idx is not None and st.session_state.versions:
        v = st.session_state.versions[st.session_state.ver_dl_idx]
        pkg = meta.get("_package", "")
        ver_label = v.get("version", "")
        with st.spinner(f"Downloading version {ver_label}…"):
            tmp_dir = Path(tempfile.mkdtemp())
            try:
                apk_path = download_version(pkg, v, tmp_dir)
                with open(apk_path, "rb") as f:
                    st.session_state.apk_bytes = f.read()
                st.session_state.apk_filename = apk_path.name
                st.session_state.ver_dl_idx = None
            except Exception:
                st.session_state.ver_dl_idx = None
                st.error("❌  Download failed. Please try again.")

# ---------------------------------------------------------------------------
# Save to device
# ---------------------------------------------------------------------------
if st.session_state.apk_bytes:
    size_mb = len(st.session_state.apk_bytes) / 1_048_576
    st.success(f"✅  {st.session_state.apk_filename}  —  {size_mb:.1f} MB")
    st.download_button(
        label="💾  Save APK to Device",
        data=st.session_state.apk_bytes,
        file_name=st.session_state.apk_filename,
        mime="application/vnd.android.package-archive",
        use_container_width=True,
        type="primary",
    )
