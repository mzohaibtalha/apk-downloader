import tempfile
from pathlib import Path

import streamlit as st

from downloader import (
    detect_input_type,
    search_app,
    download_by_app_id,
    download_from_url,
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
for key in ("meta", "search_error", "apk_bytes", "apk_filename"):
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
            c1, c2 = st.columns(2)
            v = meta.get("version") or "—"
            s = meta.get("size") or "—"
            c1.metric("Version", v)
            c2.metric("Size", s)

    st.divider()

    if st.button("⬇️  Download APK", use_container_width=True, type="primary"):
        st.session_state.apk_bytes = None
        st.session_state.apk_filename = None

        tmp_dir = Path(tempfile.mkdtemp())
        progress_bar = st.progress(0, text="Preparing download…")

        def progress_cb(done: int, total: int) -> None:
            if total:
                frac = min(done / total, 1.0)
                mb_done = done / 1_048_576
                mb_total = total / 1_048_576
                progress_bar.progress(frac, text=f"Downloading…  {mb_done:.1f} MB / {mb_total:.1f} MB")
            else:
                progress_bar.progress(0, text=f"Downloading…  {done / 1_048_576:.1f} MB")

        try:
            if is_url:
                apk_path = download_from_url(meta["_url"], tmp_dir, progress_cb)
            else:
                apk_path, _ = download_by_app_id(meta["_package"], tmp_dir, progress_cb)

            progress_bar.progress(1.0, text="Download complete!")

            with open(apk_path, "rb") as f:
                st.session_state.apk_bytes = f.read()
            st.session_state.apk_filename = apk_path.name

        except Exception as e:
            progress_bar.empty()
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
