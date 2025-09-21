import os, base64, time, datetime as dt
from typing import List, Dict, Any, Optional
import altair as alt

import streamlit as st
import pandas as pd
import numpy as np
import requests

st.set_page_config(page_title="Zoom Phone MOS", layout="wide")
st.write("Zoom Phone Call Queue Average Call Handling Time. 📊")

BASE_URL = "https://api.zoom.us/v2"
TOKEN_URL = "https://zoom.us/oauth/token"

# ---------------------------
# Session helpers
# ---------------------------
def _get_saved_creds():
    return st.session_state.get("zoom_creds", {})

def _save_creds(account_id: str, client_id: str, client_secret: str):
    st.session_state["zoom_creds"] = {
        "account_id": account_id.strip(),
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
    }


# ---------------------------
# Auth (Server-to-Server OAuth)
# ---------------------------
@st.cache_data(ttl=3300)  # ~55 min; cache is keyed on args
def get_access_token(account_id: str, client_id: str, client_secret: str) -> str:
    if not (account_id and client_id and client_secret):
        raise RuntimeError("Missing Zoom credentials")

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    params = {"grant_type": "account_credentials", "account_id": account_id}
    r = requests.post(TOKEN_URL, params=params, headers={"Authorization": f"Basic {auth}"}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]

def auth_header(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

# ---------------------------
# API: Call Queue Analytics
# ---------------------------
@st.cache_data(ttl=90)
def list_call_queue_analytics(
    token: str,
    date_from: dt.date,
    date_to: dt.date,
    page_size: int = 100
) -> List[Dict[str, Any]]:
    """
    GET /phone/call_queue_analytics
    Returns analytics per call queue; we read avg_handle_time.
    """
    url = f"{BASE_URL}/phone/call_queue_analytics"
    items: List[Dict[str, Any]] = []
    next_token: Optional[str] = None

    while True:
        params = {
            "from": date_from.strftime("%Y-%m-%d"),
            "to": date_to.strftime("%Y-%m-%d"),
            "page_size": min(page_size, 300),
        }
        if next_token:
            params["next_page_token"] = next_token

        r = requests.get(url, headers=auth_header(token), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        items.extend(
            data.get("analytics")
            or data.get("queues")
            or data.get("list")
            or data.get("call_queues")
            or []
        )

        next_token = data.get("next_page_token")
        if not next_token:
            break

        time.sleep(0.2)  # be gentle on rate limits
    return items

# ---------------------------
# Normalize -> DataFrame
# ---------------------------
def _pick_first(d: Dict[str, Any], *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None

def _fmt_hms(sec: Optional[float]) -> Optional[str]:
    if sec is None or pd.isna(sec):
        return None
    sec = int(round(float(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def build_aht_df(items: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    Build rows with: queue_id, queue_name, avg_handle_time_sec, avg_handle_time_min, avg_handle_time_hms
    """
    rows = []
    for it in items:
        q_id = _pick_first(it, "queue_id", "call_queue_id", "id")
        q_name = _pick_first(it, "queue_name", "name") or (_pick_first(it, "call_queue_name") or "Unknown Queue")
        aht_sec = _pick_first(it, "avg_handle_time", "average_handle_time", "avg_handle_time_seconds")
        try:
            aht_sec = float(aht_sec) if aht_sec is not None else None
        except Exception:
            aht_sec = None

        rows.append({
            "queue_id": q_id,
            "queue_name": q_name,
            "avg_handle_time_sec": aht_sec,
            "avg_handle_time_min": round(aht_sec / 60.0, 2) if aht_sec is not None else None,
            "avg_handle_time_hms": _fmt_hms(aht_sec),
        })

    df = pd.DataFrame(rows)
    df = df.dropna(subset=["avg_handle_time_sec"]).reset_index(drop=True)
    return df

# ---------------------------
# UI — Credentials gate
# ---------------------------
with st.container(border=True):
    st.subheader("Zoom Credentials")
    with st.form("zoom_creds_form", clear_on_submit=False):
        account_id = st.text_input("Account ID", value="", help="Zoom Server-to-Server OAuth account_id")
        client_id = st.text_input("Client ID", value="", help="Zoom app client_id")
        client_secret = st.text_input("Client Secret", value="", type="password", help="Zoom app client_secret")
        remember = st.checkbox("Remember for this session", value=True)
        submitted = st.form_submit_button("Save & Authenticate")

    token: Optional[str] = None
    if submitted:
        try:
            token = get_access_token(account_id, client_id, client_secret)
            if remember:
                _save_creds(account_id, client_id, client_secret)
            st.success("Authenticated with Zoom ✅")
            st.session_state["zoom_token"] = token  # store token for immediate use
        except requests.HTTPError as e:
            st.error(f"Zoom auth error: {e.response.status_code} {e.response.text}")
        except Exception as ex:
            st.error(f"Unexpected auth error: {ex}")

# Reuse a previously obtained token in this session if present
if token is None:
    token = st.session_state.get("zoom_token")

# ---------------------------
# UI — AHT with searchable queue filter
# ---------------------------
with st.container(border=True):
    st.subheader("Call Queue — Average Handle Time (AHT)")

    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        date_from = st.date_input("From", dt.date.today() - dt.timedelta(days=7))
    with c2:
        date_to = st.date_input("To", dt.date.today())
    with c3:
        page_size = st.slider("Page size", 50, 300, 100, step=50)

    fetch_btn = st.button("Fetch Call Queue Analytics", disabled=not bool(token))

if not token:
    st.info("Enter your Zoom credentials above and click **Save & Authenticate** to enable the dashboard.")
else:
    # Persist the latest fetched items so filter UI stays usable on edits
    if fetch_btn:
        try:
            st.session_state["cq_items"] = list_call_queue_analytics(token, date_from, date_to, page_size=page_size)
        except requests.HTTPError as e:
            st.error(f"Zoom API error: {e.response.status_code} {e.response.text}")
        except Exception as ex:
            st.error(f"Unexpected error: {ex}")

    items = st.session_state.get("cq_items")
    if items is not None:
        if not items:
            st.warning("No call queue analytics returned for the selected window.")
        else:
            with st.expander("Sample analytics item"):
                st.json(items[0])

            df = build_aht_df(items)
            if df.empty:
                st.warning("No avg_handle_time found in the analytics payload. Check scopes/permissions or widen the date range.")
            else:
                # ---- Queue filter (keyword + multiselect) ----
                st.markdown("### Filter call queues")
                q_choices = (
                    df[["queue_id", "queue_name"]]
                    .drop_duplicates()
                    .sort_values(["queue_name", "queue_id"], na_position="last")
                    .reset_index(drop=True)
                )
                q_choices["label"] = q_choices.apply(
                    lambda r: f"{r['queue_name']} ({r['queue_id']})" if pd.notna(r["queue_id"]) else r["queue_name"],
                    axis=1
                )
                labels_all = q_choices["label"].tolist()

                keyword = st.text_input("Search queues (keywords)", placeholder="e.g., Support, Kobe, 90202")
                if keyword:
                    toks = [t.strip().lower() for t in keyword.split() if t.strip()]
                    labels_filtered = [lab for lab in labels_all if all(t in lab.lower() for t in toks)]
                else:
                    labels_filtered = labels_all

                selected_labels = st.multiselect(
                    "Select call queues",
                    options=labels_filtered,
                    default=labels_filtered,
                )

                sel_ids = q_choices.loc[q_choices["label"].isin(selected_labels), "queue_id"].tolist()
                df_sel = df[df["queue_id"].isin(sel_ids)] if sel_ids else df.iloc[0:0]

                # --- Aggregate and chart ---
                if df_sel.empty:
                    st.info("No queues selected or no data after filtering.")
                else:
                    agg = (
                        df_sel.groupby(["queue_id", "queue_name"], dropna=False)["avg_handle_time_min"]
                             .mean()
                             .reset_index()
                             .sort_values("avg_handle_time_min", ascending=False)
                    )

                    # Clean + prepare
                    agg["queue_name"] = agg["queue_name"].fillna("Unnamed queue")
                    agg = agg.dropna(subset=["avg_handle_time_min"])
                    agg = agg.rename(columns={"avg_handle_time_min": "AHT (minutes)"})

                    # Top-N slider (safe bounds)
                    max_n = int(max(1, len(agg)))
                    top_n = st.slider("Show top N queues by AHT", 1, max_n, min(10, max_n))
                    chart_data = agg.head(top_n)

                    with st.expander("Chart data (first rows)"):
                        st.dataframe(chart_data, use_container_width=True, height=240)

                    st.markdown("### Average Handle Time by Queue")
                    bar_color = st.color_picker("Bar color", "#5B8FF9")

                    chart = (
                        alt.Chart(chart_data)
                        .mark_bar(color=bar_color)
                        .encode(
                            x=alt.X("queue_name:N", sort="-y", title="Call queue"),
                            y=alt.Y("AHT (minutes):Q", title="AHT (minutes)"),
                            tooltip=[
                                alt.Tooltip("queue_name:N", title="Queue"),
                                alt.Tooltip("AHT (minutes):Q", title="AHT (min)", format=".2f"),
                            ],
                        )
                        .properties(height=320)
                    )
                    st.altair_chart(chart, use_container_width=True)

                    st.markdown("### Details")
                    st.dataframe(
                        df_sel.sort_values("avg_handle_time_sec", ascending=False)[
                            ["queue_name", "avg_handle_time_min", "avg_handle_time_sec", "avg_handle_time_hms"]
                        ],
                        use_container_width=True,
                        height=320,
                    )

                    st.caption("Tip: Use the keyword box to narrow the list, then select queues in the multiselect (it's searchable too).")
