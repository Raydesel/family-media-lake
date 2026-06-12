#!/usr/bin/env python3
"""Family Media Lake — Instagram-style browse UI (Phase 4).

Photos load automatically when you sign in (20 per page). Tap a year to filter,
optionally search by tag, swipe through pages, and download originals.

Environment:
  SEARCH_API_URL, COGNITO_CLIENT_ID, AWS_REGION (default us-east-1)

Run:
  pip install -r demo/requirements.txt
  streamlit run demo/app.py
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

import boto3
import requests
import streamlit as st
from botocore.exceptions import ClientError

API_URL = os.environ.get("SEARCH_API_URL", "").rstrip("/")
CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
PAGE_SIZE = 20
DEBUG = os.environ.get("STREAMLIT_DEBUG", "").lower() in ("1", "true", "yes")

# Logs go to the terminal where you ran `streamlit run` (stdout/stderr).
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("family-photos")


def cognito_login(email: str, password: str) -> str:
    client = boto3.client("cognito-idp", region_name=REGION)
    try:
        resp = client.initiate_auth(
            ClientId=CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "AuthError")
        log.error("Cognito login failed: %s", code)
        raise RuntimeError(f"Could not sign in ({code}). Check email and password.") from exc

    if "AuthenticationResult" in resp:
        log.info("Cognito login OK for %s", email)
        return resp["AuthenticationResult"]["IdToken"]

    # Admin-created users with a temporary password hit this challenge first.
    challenge = resp.get("ChallengeName", "unknown")
    log.warning("Cognito challenge instead of token: %s", challenge)
    if challenge == "NEW_PASSWORD_REQUIRED":
        raise RuntimeError(
            "Your account still needs a permanent password. Run:\n"
            f"  aws cognito-idp admin-set-user-password --user-pool-id <pool> "
            f"--username {email!r} --password 'YourSecurePass1!' --permanent"
        )
    raise RuntimeError(f"Sign-in requires extra step ({challenge}). Contact the admin.")


def api_get(token: str, path: str, params: dict | None = None) -> dict:
    if not API_URL:
        raise RuntimeError("SEARCH_API_URL is not set")
    url = f"{API_URL}{path}"
    log.info("GET %s params=%s", url, params)
    resp = requests.get(
        url,
        params=params or {},
        headers={"Authorization": f"Bearer {token}"},
        timeout=40,
    )
    log.info("→ %s %s", resp.status_code, resp.url)
    if DEBUG:
        log.debug("response body: %s", resp.text[:500])
    if resp.status_code != 200:
        try:
            body = resp.json()
            detail = body.get("message") or body.get("error")
        except Exception:
            detail = resp.text[:200]
        log.error("API error %s: %s", resp.status_code, detail)
        raise RuntimeError(detail or f"Request failed ({resp.status_code})")
    return resp.json()


def fetch_photos(
    token: str, page: int, year: str, tag: str, when: str, media_type: str
) -> dict:
    params: dict[str, str | int] = {
        "page": page,
        "page_size": PAGE_SIZE,
        "year": year,
        "when": when,
    }
    if tag.strip():
        params["label"] = tag.strip()
    if media_type in ("photo", "video"):
        params["media_type"] = media_type
    return api_get(token, "/search", params)


def fetch_years(token: str, when: str) -> list[int]:
    data = api_get(token, "/years", {"when": when})
    return data.get("years") or []


def fetch_download_url(token: str, file_id: str) -> dict:
    return api_get(token, "/download", {"file_id": file_id})


# --- UI styling --------------------------------------------------------------

def inject_css() -> None:
    st.markdown(
        """
        <style>
        /* Hide Streamlit chrome, but NOT the whole header — it holds the
           sidebar expand/collapse control. Hiding header stranded users when
           the sidebar was collapsed (only fix was clearing cookies). */
        #MainMenu, footer {visibility: hidden;}
        [data-testid="stToolbar"], .stDeployButton {display: none;}
        .block-container {padding-top: 0.5rem; max-width: 935px;}
        .ig-title {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            font-size: 1.35rem; font-weight: 600; text-align: center;
            padding: 0.6rem 0 0.8rem; margin-bottom: 0.5rem;
            border-bottom: 1px solid #dbdbdb;
        }
        .ig-sub {text-align: center; color: #8e8e8e; font-size: 0.9rem; margin-bottom: 1rem;}
        div[data-testid="stHorizontalBlock"] button[kind="primary"] {
            background: linear-gradient(45deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888) !important;
            border: none !important; color: white !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def init_browse_state() -> None:
    defaults = {
        "page": 1,
        "year": "all",
        "tag": "",
        "tag_input": "",
        # capture = EXIF date taken; upload = date added to the lake
        "date_mode": "capture",
        # all | photo | video
        "media_filter": "all",
    }
    for key, val in defaults.items():
        st.session_state.setdefault(key, val)


def reset_page() -> None:
    st.session_state.page = 1


def _gallery_cache_key() -> tuple:
    return (
        st.session_state.page,
        st.session_state.year,
        st.session_state.tag,
        st.session_state.date_mode,
        st.session_state.media_filter,
    )


def _invalidate_gallery_cache() -> None:
    st.session_state.pop("_gallery_key", None)
    st.session_state.pop("_gallery_data", None)


def get_cached_years(token: str, when: str) -> list[int]:
    cache_key = f"cached_years_{when}"
    if cache_key not in st.session_state:
        try:
            st.session_state[cache_key] = fetch_years(token, when)
        except RuntimeError:
            st.session_state[cache_key] = list(range(datetime.now().year, datetime.now().year - 6, -1))
    return st.session_state[cache_key]


def get_cached_photos(token: str) -> dict:
    """Only hit /search when browse filters or page change."""
    key = _gallery_cache_key()
    if st.session_state.get("_gallery_key") != key:
        with st.spinner("Loading photos…"):
            st.session_state._gallery_data = fetch_photos(
                token,
                st.session_state.page,
                st.session_state.year,
                st.session_state.tag,
                st.session_state.date_mode,
                st.session_state.media_filter,
            )
            st.session_state._gallery_key = key
    return st.session_state._gallery_data


# --- Components --------------------------------------------------------------

def login_sidebar() -> str | None:
    with st.sidebar:
        st.markdown("### Family Photos")
        st.caption("Sign in with your family account")

        if not st.session_state.get("token"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            if st.button("Log in", type="primary", use_container_width=True):
                try:
                    st.session_state.token = cognito_login(email.strip(), password)
                    st.session_state.email = email.strip()
                    reset_page()
                    _invalidate_gallery_cache()
                    for k in list(st.session_state.keys()):
                        if k.startswith("cached_years_"):
                            del st.session_state[k]
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
            return None

        st.success(f"Hi, {st.session_state.get('email', 'family')}!")
        if st.button("Sign out", use_container_width=True):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()

        st.divider()
        st.markdown("**Date filter**")
        mode_label = st.radio(
            "Show years for",
            ["Photo taken (EXIF)", "Date uploaded"],
            index=0 if st.session_state.date_mode == "capture" else 1,
            label_visibility="collapsed",
        )
        new_mode = "capture" if mode_label.startswith("Photo") else "upload"
        if new_mode != st.session_state.date_mode:
            st.session_state.date_mode = new_mode
            reset_page()
            _invalidate_gallery_cache()
            st.rerun()

        st.divider()
        st.markdown("**Media type**")
        media_labels = ["All", "Photos", "Videos"]
        media_values = ["all", "photo", "video"]
        media_index = media_values.index(st.session_state.media_filter)
        media_label = st.radio(
            "Show",
            media_labels,
            index=media_index,
            label_visibility="collapsed",
        )
        new_media = media_values[media_labels.index(media_label)]
        if new_media != st.session_state.media_filter:
            st.session_state.media_filter = new_media
            reset_page()
            _invalidate_gallery_cache()
            st.rerun()

        st.divider()
        st.markdown("**Search by tag**")
        st.caption("Examples: Beach, Dog, Birthday")
        tag_input = st.text_input("Tag", value=st.session_state.tag_input, label_visibility="collapsed")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Apply", use_container_width=True):
                st.session_state.tag = tag_input.strip()
                st.session_state.tag_input = tag_input
                reset_page()
                _invalidate_gallery_cache()
                st.rerun()
        with c2:
            if st.button("Clear", use_container_width=True):
                st.session_state.tag = ""
                st.session_state.tag_input = ""
                reset_page()
                _invalidate_gallery_cache()
                st.rerun()

        if st.session_state.tag:
            st.info(f"Showing: **{st.session_state.tag}**")

        return st.session_state.token


def year_filter_bar(token: str) -> None:
    when = st.session_state.date_mode
    years = get_cached_years(token, when)
    options = ["all"] + [str(y) for y in years]
    labels = ["All years"] + [str(y) for y in years]
    cols = st.columns(min(len(labels), 8))
    for i, (opt, label) in enumerate(zip(options, labels)):
        with cols[i % len(cols)]:
            selected = st.session_state.year == opt
            if st.button(
                label,
                key=f"year_{opt}",
                type="primary" if selected else "secondary",
                use_container_width=True,
            ):
                st.session_state.year = opt
                reset_page()
                _invalidate_gallery_cache()
                st.rerun()


def format_date(item: dict) -> str:
    for key in ("capture_ts", "upload_ts"):
        val = item.get(key)
        if val:
            return str(val)[:10]
    return ""


def photo_card(token: str, item: dict, col) -> None:
    file_id = item.get("file_id", "")
    with col:
        url = item.get("thumbnail_url")
        if url:
            st.image(url, use_container_width=True)
        else:
            st.container(border=True).write("No preview")

        name = item.get("original_filename") or file_id[:8]
        when = format_date(item)
        st.caption(f"**{name}**" + (f" · {when}" if when else ""))

        labels = item.get("rekognition_labels") or []
        if labels:
            tag_cols = st.columns(min(3, len(labels[:3])))
            for j, lbl in enumerate(labels[:3]):
                with tag_cols[j]:
                    if st.button(lbl, key=f"tag_{file_id}_{lbl}", use_container_width=True):
                        st.session_state.tag = lbl
                        st.session_state.tag_input = lbl
                        reset_page()
                        _invalidate_gallery_cache()
                        st.rerun()

        download_url = item.get("download_url")
        if download_url:
            st.link_button(
                "⬇ Save original",
                download_url,
                use_container_width=True,
                help=f"Save {name}",
            )
        else:
            # Fallback when search API predates download_url in results.
            if st.button(
                "⬇ Save original",
                key=f"btn_{file_id}",
                use_container_width=True,
                help="Fetch download link",
            ):
                try:
                    dl = fetch_download_url(token, file_id)
                    st.link_button(
                        "⬇ Save original",
                        dl["download_url"],
                        use_container_width=True,
                        help=f"Save {dl.get('filename', name)}",
                    )
                except RuntimeError as exc:
                    st.error(str(exc))


def pagination_bar(has_more: bool) -> None:
    page = st.session_state.page
    st.divider()
    prev_col, mid_col, next_col = st.columns([1, 2, 1])
    with prev_col:
        if page > 1:
            if st.button("← Previous", use_container_width=True):
                st.session_state.page = page - 1
                _invalidate_gallery_cache()
                st.rerun()
    with mid_col:
        st.markdown(f"<p style='text-align:center;margin-top:0.5rem;'>Page <b>{page}</b></p>", unsafe_allow_html=True)
    with next_col:
        if has_more:
            if st.button("Next →", use_container_width=True):
                st.session_state.page = page + 1
                _invalidate_gallery_cache()
                st.rerun()


def photo_grid(token: str, results: list[dict]) -> None:
    cols = st.columns(3)
    for i, item in enumerate(results):
        photo_card(token, item, cols[i % 3])


# --- Main --------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Family Photos", page_icon="📷", layout="wide", initial_sidebar_state="expanded")
    inject_css()
    init_browse_state()

    st.markdown('<div class="ig-title">📷 Family Photos</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="ig-sub">Browse your memories — pick a year or search a tag</div>',
        unsafe_allow_html=True,
    )

    if not CLIENT_ID:
        st.error("Set COGNITO_CLIENT_ID (terraform output cognito_client_id).")
        st.stop()
    if not API_URL:
        st.error("Set SEARCH_API_URL (terraform output search_api_endpoint).")
        st.stop()
    if DEBUG:
        st.sidebar.caption("🔧 Debug logging ON → watch the terminal")

    token = login_sidebar()
    if not token:
        st.info("👋 Sign in on the left to see your photos.")
        st.stop()

    year_filter_bar(token)

    active_tag = st.session_state.tag
    when_label = "taken" if st.session_state.date_mode == "capture" else "uploaded"
    media_labels = {"all": "media", "photo": "photos", "video": "videos"}
    media_word = media_labels[st.session_state.media_filter]
    subtitle = f"All {media_word} ({when_label})"
    if st.session_state.year != "all":
        subtitle = f"{media_word.capitalize()} {when_label} in {st.session_state.year}"
    if active_tag:
        subtitle += f' tagged "{active_tag}"'
    st.markdown(f"**{subtitle}** · page {st.session_state.page}")

    try:
        data = get_cached_photos(token)
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    results = data.get("results") or []
    if not results:
        st.warning("Nothing here yet. Try **All years**, **All** media type, or clear the tag filter.")
        if st.session_state.page > 1:
            st.session_state.page = 1
            st.rerun()
        st.stop()

    photo_grid(token, results)
    pagination_bar(bool(data.get("has_more")))


if __name__ == "__main__":
    main()
