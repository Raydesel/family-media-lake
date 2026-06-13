#!/usr/bin/env python3
"""Family Media Lake — Instagram-style browse UI (Phase 4).

Photos load automatically when you sign in (20 per page). Tap a year to filter,
optionally search by tag, swipe through pages, and download originals.

Environment:
  SEARCH_API_URL, COGNITO_CLIENT_ID, AWS_REGION

  Set via shell exports OR `.streamlit/secrets.toml` at the **repo root**
  (see `.streamlit/secrets.toml.example`).

Run:
  pip install -r demo/requirements.txt
  streamlit run demo/app.py
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
from datetime import datetime

import boto3
import requests
import streamlit as st
import streamlit_js_eval
from botocore.exceptions import ClientError

API_URL = ""
CLIENT_ID = ""
REGION = "us-east-1"
PAGE_SIZE = 20
DEBUG = os.environ.get("STREAMLIT_DEBUG", "").lower() in ("1", "true", "yes")
LS_EMAIL = "fml_email"
LS_REFRESH = "fml_refresh"

# Logs go to the terminal where you ran `streamlit run` (stdout/stderr).
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("family-photos")


def _secret(key: str, default: str = "") -> str:
    """Env var first, then demo/.streamlit/secrets.toml (Streamlit Cloud too)."""
    env = os.environ.get(key)
    if env:
        return env
    try:
        return str(st.secrets[key])
    except (KeyError, FileNotFoundError, AttributeError):
        return default


def load_config() -> None:
    """Load API/Cognito settings once Streamlit has started."""
    global API_URL, CLIENT_ID, REGION
    API_URL = _secret("SEARCH_API_URL").rstrip("/")
    CLIENT_ID = _secret("COGNITO_CLIENT_ID")
    REGION = _secret("AWS_REGION", "us-east-1")
    # boto3 reads these from the environment for Cognito login.
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not os.environ.get(key):
            val = _secret(key)
            if val:
                os.environ[key] = val


def _cognito_client():
    return boto3.client("cognito-idp", region_name=REGION)


def cognito_login(email: str, password: str) -> dict[str, str]:
    try:
        resp = _cognito_client().initiate_auth(
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
        return _auth_tokens(resp["AuthenticationResult"])

    challenge = resp.get("ChallengeName", "unknown")
    log.warning("Cognito challenge instead of token: %s", challenge)
    if challenge == "NEW_PASSWORD_REQUIRED":
        raise RuntimeError(
            "Your account still needs a permanent password. Run:\n"
            f"  aws cognito-idp admin-set-user-password --user-pool-id <pool> "
            f"--username {email!r} --password 'YourSecurePass1!' --permanent"
        )
    raise RuntimeError(f"Sign-in requires extra step ({challenge}). Contact the admin.")


def cognito_refresh(refresh_token: str) -> dict[str, str]:
    try:
        resp = _cognito_client().initiate_auth(
            ClientId=CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "AuthError")
        log.error("Cognito refresh failed: %s", code)
        raise RuntimeError(f"Session expired ({code}). Please sign in again.") from exc
    if "AuthenticationResult" not in resp:
        raise RuntimeError("Could not refresh session. Please sign in again.")
    return _auth_tokens(resp["AuthenticationResult"], refresh_token)


def _auth_tokens(result: dict, prior_refresh: str = "") -> dict[str, str]:
    return {
        "id_token": result["IdToken"],
        "refresh_token": result.get("RefreshToken") or prior_refresh,
    }


def _storage_set(key: str, value: str) -> None:
    streamlit_js_eval.streamlit_js_eval(
        js_expressions=f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)})",
        key=f"fml_ls_set_{key}",
    )


def _storage_get(key: str, read_key: str) -> str | None:
    val = streamlit_js_eval.get_local_storage(key, component_key=read_key)
    if not val or val == "null":
        return None
    return val


def persist_auth_storage(email: str, refresh_token: str) -> None:
    if refresh_token:
        _storage_set(LS_REFRESH, refresh_token)
    if email:
        _storage_set(LS_EMAIL, email)


def clear_auth_storage() -> None:
    streamlit_js_eval.remove_local_storage(LS_REFRESH, component_key="fml_ls_rm_refresh")
    streamlit_js_eval.remove_local_storage(LS_EMAIL, component_key="fml_ls_rm_email")


def read_auth_storage() -> tuple[str | None, str | None]:
    return (
        _storage_get(LS_EMAIL, "fml_ls_read_email"),
        _storage_get(LS_REFRESH, "fml_ls_read_refresh"),
    )


def hydrate_browser_storage() -> None:
    """streamlit-js-eval often needs two passes before localStorage reads work."""
    n = int(st.session_state.get("_storage_hydrations", 0))
    if n >= 2:
        return
    streamlit_js_eval.get_local_storage(LS_REFRESH, component_key=f"fml_ls_hydrate_refresh_{n}")
    streamlit_js_eval.get_local_storage(LS_EMAIL, component_key=f"fml_ls_hydrate_email_{n}")
    st.session_state._storage_hydrations = n + 1
    st.rerun()


def apply_auth(email: str, tokens: dict[str, str]) -> None:
    refresh = tokens.get("refresh_token") or ""
    if not refresh:
        log.warning("Cognito did not return a refresh token — reload will require login again")
    st.session_state.token = tokens["id_token"]
    st.session_state.email = email
    st.session_state.refresh_token = refresh
    persist_auth_storage(email, refresh)


def try_restore_session() -> bool:
    if st.session_state.get("token"):
        return True
    email, refresh = read_auth_storage()
    if not refresh:
        return False
    try:
        tokens = cognito_refresh(refresh)
        apply_auth(email or "", tokens)
        log.info("Restored session for %s", email or "(unknown)")
        return True
    except RuntimeError as exc:
        log.warning("Session restore failed: %s", exc)
        clear_auth_storage()
        return False


def logout() -> None:
    clear_auth_storage()
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()


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


# --- UI styling --------------------------------------------------------------

def inject_css() -> None:
    st.markdown(
        """
        <style>
        /* Visual polish only — do not hide Streamlit header/sidebar controls. */
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
        a.media-download img {display: block; width: 100%; border-radius: 8px;}
        a.media-download:hover img {opacity: 0.92;}
        a.media-download-placeholder {
            display: block; padding: 3rem 1rem; text-align: center;
            border: 1px solid #dbdbdb; border-radius: 8px;
            color: #262626; text-decoration: none; cursor: pointer;
        }
        a.media-download-placeholder:hover {background: #fafafa;}
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

def render_login() -> None:
    """Sign-in form in the main page (avoids fragile sidebar collapse on Cloud)."""
    _, center, _ = st.columns([1, 2, 1])
    with center:
        st.markdown("### Sign in")
        st.caption("Use your family account — stays signed in on this device")
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_password")
        if st.button("Log in", type="primary", use_container_width=True):
            try:
                tokens = cognito_login(email.strip(), password)
                apply_auth(email.strip(), tokens)
                reset_page()
                _invalidate_gallery_cache()
                for k in list(st.session_state.keys()):
                    if k.startswith("cached_years_"):
                        del st.session_state[k]
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))


def render_filter_panel() -> None:
    """Browse filters — always on the main page inside an expander."""
    with st.expander("Filters & account", expanded=True):
        head_l, head_r = st.columns([3, 1])
        with head_l:
            st.markdown(f"Signed in as **{st.session_state.get('email', 'family')}**")
        with head_r:
            if st.button("Sign out", use_container_width=True):
                logout()

        f1, f2, f3 = st.columns(3)
        with f1:
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

        with f2:
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

        with f3:
            st.markdown("**Search by tag**")
            st.caption("Beach, Dog, Birthday…")
            tag_input = st.text_input("Tag", value=st.session_state.tag_input, label_visibility="collapsed")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Apply tag", use_container_width=True):
                    st.session_state.tag = tag_input.strip()
                    st.session_state.tag_input = tag_input
                    reset_page()
                    _invalidate_gallery_cache()
                    st.rerun()
            with c2:
                if st.button("Clear tag", use_container_width=True):
                    st.session_state.tag = ""
                    st.session_state.tag_input = ""
                    reset_page()
                    _invalidate_gallery_cache()
                    st.rerun()
            if st.session_state.tag:
                st.info(f"Tag: **{st.session_state.tag}**")


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


def render_clickable_media(item: dict) -> None:
    """Thumbnail (or placeholder) links to the presigned original download."""
    download_url = item.get("download_url")
    if not download_url:
        thumb = item.get("thumbnail_url")
        if thumb:
            st.image(thumb, use_container_width=True)
        else:
            st.container(border=True).write("No preview")
        return

    name = item.get("original_filename") or item.get("file_id", "")[:8]
    safe_dl = html.escape(download_url, quote=True)
    title = html.escape(f"Save {name}")
    thumb = item.get("thumbnail_url")
    if thumb:
        safe_thumb = html.escape(thumb, quote=True)
        st.markdown(
            f'<a class="media-download" href="{safe_dl}" title="{title}">'
            f'<img src="{safe_thumb}" alt="{title}" /></a>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<a class="media-download media-download-placeholder" href="{safe_dl}" '
            f'title="{title}">⬇ Save {html.escape(name)}</a>',
            unsafe_allow_html=True,
        )


def photo_card(item: dict, col) -> None:
    file_id = item.get("file_id", "")
    with col:
        render_clickable_media(item)

        name = item.get("original_filename") or file_id[:8]
        when = format_date(item)
        hint = " · tap to save" if item.get("download_url") else ""
        st.caption(f"**{name}**" + (f" · {when}" if when else "") + hint)


def render_page_picker(total_pages: int, location: str) -> None:
    """Numbered page buttons for header and footer — every page is listed."""
    current = st.session_state.page
    if total_pages <= 1:
        st.caption(f"Page 1 · {PAGE_SIZE} items per page")
        return

    st.caption(f"Page {current} of {total_pages}")
    pages_per_row = 12
    all_pages = list(range(1, total_pages + 1))
    for row_start in range(0, len(all_pages), pages_per_row):
        chunk = all_pages[row_start : row_start + pages_per_row]
        cols = st.columns(len(chunk))
        for col, page_num in zip(cols, chunk):
            with col:
                if page_num == current:
                    st.button(
                        str(page_num),
                        type="primary",
                        disabled=True,
                        key=f"{location}_page_{page_num}",
                        use_container_width=True,
                    )
                elif st.button(
                    str(page_num),
                    key=f"{location}_page_{page_num}",
                    use_container_width=True,
                ):
                    st.session_state.page = page_num
                    _invalidate_gallery_cache()
                    st.rerun()


def photo_grid(results: list[dict]) -> None:
    cols = st.columns(3)
    for i, item in enumerate(results):
        photo_card(item, cols[i % 3])


# --- Main --------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Family Photos", page_icon="📷", layout="wide")
    load_config()
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
        st.caption("🔧 Debug logging ON")

    hydrate_browser_storage()
    if not st.session_state.get("token") and try_restore_session():
        st.rerun()

    token = st.session_state.get("token")
    if not token:
        render_login()
        st.stop()

    render_filter_panel()
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
    st.markdown(f"**{subtitle}**")

    try:
        data = get_cached_photos(token)
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    total_pages = int(data.get("total_pages") or 0)
    if not total_pages:
        # Older search API without total_pages — show at least current + next.
        total_pages = st.session_state.page + (1 if data.get("has_more") else 0)
    if total_pages and st.session_state.page > total_pages:
        st.session_state.page = total_pages
        _invalidate_gallery_cache()
        st.rerun()

    render_page_picker(total_pages, "header")
    st.divider()

    results = data.get("results") or []
    if not results:
        st.warning("Nothing here yet. Try **All years**, **All** media type, or clear the tag filter.")
        if st.session_state.page > 1:
            st.session_state.page = 1
            st.rerun()
        st.stop()

    photo_grid(results)
    st.divider()
    render_page_picker(total_pages, "footer")


if __name__ == "__main__":
    main()
