#!/usr/bin/env python3
"""Streamlit demo UI for the Family Media Data Lake (Phase 4).

Configure via environment variables (or a local .env you do not commit):

  SEARCH_API_URL      terraform output search_api_endpoint
  COGNITO_CLIENT_ID   terraform output cognito_client_id
  AWS_REGION          default us-east-1

Run:

  pip install -r demo/requirements.txt
  streamlit run demo/app.py
"""
from __future__ import annotations

import os

import boto3
import requests
import streamlit as st
from botocore.exceptions import ClientError

API_URL = os.environ.get("SEARCH_API_URL", "").rstrip("/")
CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")


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
        raise RuntimeError(f"login failed ({code})") from exc
    return resp["AuthenticationResult"]["IdToken"]


def search(token: str, params: dict) -> dict:
    if not API_URL:
        raise RuntimeError("SEARCH_API_URL is not set")
    resp = requests.get(
        f"{API_URL}/search",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=35,
    )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("message") or resp.json().get("error")
        except Exception:
            detail = resp.text
        raise RuntimeError(f"search failed ({resp.status_code}): {detail}")
    return resp.json()


def main() -> None:
    st.set_page_config(page_title="Family Media Lake", page_icon="📷", layout="wide")
    st.title("Family Media Lake")
    st.caption("Athena-backed search with CloudFront thumbnails")

    if not CLIENT_ID:
        st.error("Set COGNITO_CLIENT_ID (terraform output cognito_client_id).")
        st.stop()

    with st.sidebar:
        st.header("Sign in")
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.button("Log in", type="primary"):
            try:
                st.session_state["token"] = cognito_login(email.strip(), password)
                st.session_state["email"] = email.strip()
                st.success("Signed in")
            except RuntimeError as exc:
                st.error(str(exc))

        if st.session_state.get("token"):
            st.write(f"Signed in as **{st.session_state.get('email', '')}**")
            if st.button("Sign out"):
                st.session_state.pop("token", None)
                st.session_state.pop("email", None)
                st.rerun()

    token = st.session_state.get("token")
    if not token:
        st.info("Sign in with a Cognito family account to search.")
        st.stop()

    col1, col2, col3 = st.columns(3)
    with col1:
        year = st.number_input("Year", min_value=2000, max_value=2035, value=2026)
    with col2:
        month = st.number_input("Month (0 = any)", min_value=0, max_value=12, value=0)
    with col3:
        day = st.number_input("Day (0 = any)", min_value=0, max_value=31, value=0)

    label = st.text_input("Label contains (Rekognition)", placeholder="Beach")
    uploader = st.text_input("Uploader", placeholder="ariel")
    media_type = st.selectbox("Media type", ["", "photo", "video"])

    if st.button("Search", type="primary"):
        params: dict[str, str | int] = {"year": int(year), "limit": 48}
        if month:
            params["month"] = int(month)
        if day:
            params["day"] = int(day)
        if label.strip():
            params["label"] = label.strip()
        if uploader.strip():
            params["uploader"] = uploader.strip()
        if media_type:
            params["media_type"] = media_type

        try:
            with st.spinner("Querying Athena…"):
                data = search(token, params)
        except RuntimeError as exc:
            st.error(str(exc))
            st.stop()

        results = data.get("results") or []
        st.subheader(f"{data.get('count', 0)} result(s)")
        if not results:
            st.write("No matches. Try widening year/month or removing filters.")
            st.stop()

        cols = st.columns(4)
        for i, item in enumerate(results):
            with cols[i % 4]:
                url = item.get("thumbnail_url")
                if url:
                    st.image(url, use_container_width=True)
                st.caption(item.get("original_filename", item.get("file_id", "")))
                labels = item.get("rekognition_labels") or []
                if labels:
                    st.write(", ".join(labels[:4]))


if __name__ == "__main__":
    main()
