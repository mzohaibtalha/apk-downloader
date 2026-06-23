import tempfile
from pathlib import Path

import streamlit as st

from downloader import (
    detect_input_type,
    search_app,
    download_by_app_id,
    download_from_url,
    DOWNLOAD_DIR,
)

st.set_page_config(page_title="APK Downloader", page_icon="📦", layout="centered")

st.title("📦 APK Downloader")
st.caption("Download pure APK files by package ID or direct URL. XAPK bundles are unpacked automatically.")

# ---------------------------------------------------------------------------
# Sidebar info
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("ℹ️ How it works")
    st.markdown(
        """
**By Package ID** (e.g. `com.whatsapp`):
- Searches APKCombo → Uptodown → APKPure
- First match wins

**By URL**:
- Downloads the file directly from the URL you paste

**XAPK → APK**:
Many modern apps on Google Play use App Bundles.
Mirror sites deliver them as `.xapk` files — a ZIP
containing `base.apk` + split APKs.
This tool automatically extracts `base.apk` so you
always get a plain `.apk`.
        """
    )

# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
user_input = st.text_input(
    "Enter a package ID or direct APK URL",
    placeholder="e.g.  com.whatsapp   or   https://example.com/app.apk",
)

col_search, col_clear = st.columns([1, 5])
search_clicked = col_search.button("🔍 Search", use_container_width=True)

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
if "meta" not in st.session_state:
    st.session_state.meta = None
if "search_error" not in st.session_state:
    st.session_state.search_error = None
if "apk_bytes" not in st.session_state:
    st.session_state.apk_bytes = None
if "apk_filename" not in st.session_state:
    st.session_state.apk_filename = None

# ---------------------------------------------------------------------------
# Search step
# ---------------------------------------------------------------------------
if search_clicked and user_input.strip():
    st.session_state.meta = None
    st.session_state.search_error = None
    st.session_state.apk_bytes = None
    st.session_state.apk_filename = None

    kind, value = detect_input_type(user_input.strip())

    if kind == "url":
        st.session_state.meta = {
            "name": "Direct URL download",
            "version": "—",
            "size": "Unknown",
            "source": "Direct URL",
            "_url": value,
        }
    else:
        with st.spinner("Searching across APKCombo, APKPure…"):
            try:
                meta = search_app(value)
                meta["_package"] = value
                st.session_state.meta = meta
            except RuntimeError as e:
                st.session_state.search_error = str(e)

# ---------------------------------------------------------------------------
# Result card
# ---------------------------------------------------------------------------
if st.session_state.search_error:
    st.error(f"❌ {st.session_state.search_error}")

if st.session_state.meta:
    meta = st.session_state.meta
    is_url = "_url" in meta

    with st.container(border=True):
        if is_url:
            st.success("✅ Ready to download from URL")
            st.markdown(f"**URL:** `{meta['_url']}`")
        else:
            st.success(f"✅ Found on **{meta['source']}**")
            c1, c2, c3 = st.columns(3)
            c1.metric("App", meta.get("name", "—"))
            c2.metric("Version", meta.get("version", "—") or "—")
            c3.metric("Size", meta.get("size", "—") or "—")
            st.caption("XAPK bundles will be automatically unpacked → `base.apk`")

    # Download button
    if st.button("⬇️ Download APK", use_container_width=True):
        st.session_state.apk_bytes = None
        st.session_state.apk_filename = None

        tmp_dir = Path(tempfile.mkdtemp())
        progress_bar = st.progress(0, text="Starting download…")
        status_text = st.empty()

        def progress_cb(done: int, total: int):
            if total:
                frac = min(done / total, 1.0)
                mb_done = done / 1_048_576
                mb_total = total / 1_048_576
                progress_bar.progress(frac, text=f"Downloading… {mb_done:.1f} MB / {mb_total:.1f} MB")
            else:
                mb_done = done / 1_048_576
                progress_bar.progress(0, text=f"Downloading… {mb_done:.1f} MB")

        try:
            if is_url:
                status_text.info("Downloading from URL…")
                apk_path = download_from_url(meta["_url"], tmp_dir, progress_cb)
            else:
                status_text.info("Downloading and extracting base.apk…")
                apk_path, source_used = download_by_app_id(meta["_package"], tmp_dir, progress_cb)

            progress_bar.progress(1.0, text="Done!")
            status_text.empty()

            with open(apk_path, "rb") as f:
                st.session_state.apk_bytes = f.read()
            st.session_state.apk_filename = apk_path.name

        except Exception as e:
            progress_bar.empty()
            status_text.empty()
            st.error(f"❌ Download failed: {e}")

# ---------------------------------------------------------------------------
# Save-to-computer button
# ---------------------------------------------------------------------------
if st.session_state.apk_bytes:
    size_mb = len(st.session_state.apk_bytes) / 1_048_576
    st.success(f"✅ Ready — **{st.session_state.apk_filename}** ({size_mb:.1f} MB)")
    st.download_button(
        label="💾 Save APK to Computer",
        data=st.session_state.apk_bytes,
        file_name=st.session_state.apk_filename,
        mime="application/vnd.android.package-archive",
        use_container_width=True,
    )
