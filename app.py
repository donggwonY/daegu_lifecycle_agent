"""Streamlit UI.  실행: streamlit run app.py"""
from __future__ import annotations

import json
import os
import uuid

import anthropic
import pandas as pd
import streamlit as st

from core import tools as T
from ui_visuals import cycle_chart, history_chart, render_tool_result, survival_chart, _map

st.set_page_config(page_title="대구 상권 생애주기 AI 에이전트", page_icon="🏪", layout="wide")

try:
    S = T.store()
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()
META = S.meta
GUS = ["중구", "동구", "서구", "남구", "북구", "수성구", "달서구", "달성군", "군위군"]
EXAMPLES = [
    "수성구에서 카페 열려는데 어때?",
    "그럼 뭐가 더 오래 가?",
    "수성구, 중구, 달서구 중 카페 하기엔 어디가 나아?",
    "중구 삼덕동1가 요즘 분위기는?",
    "그 동네에 최근 빈자리 있어?",
    "대구 군위군 중앙길 90 이전엔 뭐였어?",
]


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


# ─────────────────────────── 사이드바 ───────────────────────────
with st.sidebar:
    st.title("🏪 상권 생애주기 에이전트")
    st.caption("그 자리의 과거를 데이터로 확인합니다")
    rec = META["records"]["통합"]
    st.metric("인허가 레코드 (일반+휴게음식점)", f"{rec['total']:,}")
    c1, c2 = st.columns(2)
    c1.metric("폐업", f"{rec['closed']:,}")
    c2.metric("영업 중", f"{rec['active']:,}")
    st.caption(f"기준일 {META['reference_date']} · 배치 {META['built_at']}")
    st.divider()
    if has_credentials():
        st.success("Claude API 연결 준비됨", icon="✅")
    else:
        key = st.text_input("ANTHROPIC_API_KEY", type="password", help="AI 상담 탭에만 필요합니다. 대시보드·자리 조회는 키 없이 동작합니다.")
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            st.rerun()
    st.subheader("예시 질문")
    for q in EXAMPLES:
        if st.button(q, width="stretch"):
            st.session_state.pending = q
    if st.button("🔄 대화 초기화", width="stretch"):
        st.session_state.pop("agent", None)
        st.session_state.history = []
        st.rerun()

tab_chat, tab_dash, tab_unit = st.tabs(["💬 AI 상담", "📊 대구 상권 대시보드", "🔎 자리 이력 조회"])


# ─────────────────────────── 1. AI 상담 ───────────────────────────
def render_tools(tool_events: list[dict], msg_id: str):
    for i, ev in enumerate(tool_events):
        args = json.dumps(ev["input"], ensure_ascii=False)
        with st.expander(f"🔧 {ev['name']} {args}", expanded=False):
            if ev["error"]:
                st.warning(ev["error"])
            else:
                st.json(ev["output"], expanded=False)
        if not ev["error"]:
            render_tool_result(ev["name"], ev["output"], key=f"{msg_id}-{i}")


with tab_chat:
    st.session_state.setdefault("history", [])
    for m in st.session_state.history:
        with st.chat_message(m["role"]):
            if m["role"] == "assistant":
                render_tools(m.get("tools", []), m["id"])
            st.markdown(m["content"])

    prompt = st.chat_input("예) 수성구 범어동에서 치킨집 하면 어때?") or st.session_state.pop("pending", None)
    if prompt:
        st.session_state.history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            if not has_credentials():
                st.error("사이드바에 ANTHROPIC_API_KEY 를 입력하세요. (대시보드·자리 조회 탭은 키 없이 사용 가능)")
                st.stop()
            if "agent" not in st.session_state:
                from agent import LifecycleAgent
                st.session_state.agent = LifecycleAgent()
            msg_id = uuid.uuid4().hex[:8]
            tool_box = st.container()
            text_box = st.empty()
            text, tool_events = "", []
            try:
                with st.spinner("데이터 확인 중…"):
                    for ev in st.session_state.agent.ask(prompt):
                        if ev["type"] == "text":
                            text += ev["text"]
                            text_box.markdown(text + "▌")
                        elif ev["type"] == "tool_call":
                            tool_box.caption(f"🔧 `{ev['name']}` 호출")
                        elif ev["type"] == "tool_result":
                            tool_events.append(ev)
                        elif ev["type"] == "error":
                            text += f"\n\n⚠️ {ev['message']}"
            except anthropic.AuthenticationError:
                text += "\n\n⚠️ API 인증 실패 — 키를 확인하세요."
            except anthropic.RateLimitError:
                text += "\n\n⚠️ 요청 한도 초과 — 잠시 후 다시 시도하세요."
            except anthropic.APIConnectionError:
                text += "\n\n⚠️ 네트워크 연결 실패."
            except anthropic.APIStatusError as e:
                text += f"\n\n⚠️ API 오류 {e.status_code}: {e.message}"
            text_box.empty()
            with tool_box:
                render_tools(tool_events, msg_id)
            st.markdown(text)
        st.session_state.history.append({"role": "assistant", "content": text, "tools": tool_events, "id": msg_id})


# ─────────────────────────── 2. 대시보드 ───────────────────────────
with tab_dash:
    gu_sel = st.selectbox("지역", ["대구 전체"] + GUS, key="dash_gu")
    gu = None if gu_sel == "대구 전체" else gu_sel

    cyc = T.get_market_cycle(gu)
    surv = T.get_survival_curve(gu=gu)
    vac = T.find_vacant_units(gu=gu, limit=1000)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("상권 사이클", cyc["stage"], f"개업/폐업 {cyc['open_close_ratio_recent']} (직전 {cyc['open_close_ratio_prev']})", delta_color="off")
    k2.metric("중앙생존기간 (2010~ 개업)", f"{surv['median_survival_years']}년", f"대구 {surv['daegu_baseline_same_cohort']['median_survival_years']}년", delta_color="off")
    k3.metric("1년 내 폐업", f"{surv['close_within_1y_pct']}%", f"n={surv['n']:,}", delta_color="off")
    k4.metric("최근 공실 후보 (3개월~3년)", f"{vac['total_vacant']:,}곳")

    left, right = st.columns(2)
    with left:
        st.subheader("업태별 중앙생존기간")
        survival_chart(T.get_survival_curve(gu=gu, group_by="category", min_n=100 if gu else 300), key="dash-surv")
    with right:
        st.subheader("연도별 개업·폐업")
        cycle_chart(cyc, key="dash-cycle")

    st.subheader("구·군 비교" if not gu else f"{gu} 동별 비교 (표본 상위)")
    if not gu:
        render_tool_result("compare_areas", T.compare_areas(GUS), key="dash-compare")
    else:
        rank = T.get_survival_curve(gu=gu, group_by="dong", min_n=150, top_n=40)
        vac_d = S.vacancy[(S.vacancy["level"] == "동") & (S.vacancy["gu"] == gu)][["dong", "units_3y", "vacancy_rate_pct"]]
        cyc_d = S.cycle[(S.cycle["level"] == "동") & (S.cycle["gu"] == gu)][["dong", "stage", "open_close_ratio_recent"]]
        df = (pd.DataFrame(rank["ranking_by_median_survival"])[["dong", "n", "median_survival_years", "survival_1y_pct", "survival_3y_pct"]]
              .merge(vac_d, on="dong", how="left").merge(cyc_d, on="dong", how="left"))
        df.columns = ["동", "표본", "중앙생존(년)", "1년 생존%", "3년 생존%", "최근3년 자리", "공실률%", "사이클", "개폐업비"]
        st.dataframe(df, hide_index=True, width="stretch")

    st.subheader("최근 공실 후보 지도")
    st.caption(vac["basis"]["definition"] + " · " + vac["basis"]["caveat"])
    _map(vac["units"], "vacant_days", ["last_store", "last_category", "vacant_days", "closures_since_2010"], key="dash-map")

    with st.expander("📐 데이터 품질 · 방법론 (기획서 5장 검증 결과 재현)"):
        q = META["quality"]
        st.markdown(f"""
- **레코드**: 원본 {q['raw_rows']:,}건 · 중복 제거 {q['duplicates_removed']}건 · 인허가일 결측 {q['missing_license_date']}건 · 날짜 모순 {q['date_contradiction']}건 · 대구 외 주소 {q['non_daegu_address']}건 제외
- **좌표 채움률**: {', '.join(f'{k} {v}%' for k, v in q['coord_fill_pct'].items())} (EPSG:5174 → WGS84 변환)
- **문제1 공실 과대판정**: 공실 상태 자리의 공백기간 분포 {META['vacancy_buckets_all_vacant_units']} → 90일~3년만 유효 매물 **{META['recent_vacancy_units']:,}곳**
- **문제2 다점포 건물**: 동시영업 수로 판별, 단일 점포 자리 비율 **{META['single_unit_ratio_pct']}%** · 전체 최다 폐업 {META['max_closures_all_units_top5'][0]['n_closed']}건({META['max_closures_all_units_top5'][0]['addr']}) → 단일 점포 기준 최다 {META['max_closures_single_units_top5'][0]['n_closed']}건
- **문제4 업종 확장**: 일반→휴게(또는 반대) 전환으로 현재 영업 중인 자리 **{META['service_switch_units']:,}곳** (한 업종만 봤다면 공실로 오판)
""")
        st.markdown("**문제3 생존편향** — 개업 연대별 1년 생존율 (그래서 생존분석은 2010년 이후 개업만)")
        st.dataframe(pd.DataFrame(META["survival_by_decade_1y"]), hide_index=True)


# ─────────────────────────── 3. 자리 이력 ───────────────────────────
with tab_unit:
    addr = st.text_input("주소 또는 상호", placeholder="예) 대구 중구 동성로5길 83 / 삼덕동1가 28-6 / 빽다방 대구군위점")
    whole = st.checkbox("층 구분 없이 건물 전체 보기")
    if addr:
        try:
            h = T.get_unit_history(addr, whole_building=whole)
        except T.ToolError as e:
            st.warning(str(e))
        else:
            u = h["unit"]
            st.markdown(f"### {u['address']}")
            st.caption(f"자리 ID: {u['unit_id']} · {u['status']}")
            if not u["is_single_unit"]:
                st.info(u["single_unit_note"])
            s = h["summary"]
            c = st.columns(4)
            c[0].metric("인허가 레코드", s["total_records"])
            c[1].metric("2010년 이후 폐업", s["closures_since_2010"])
            c[2].metric("2010년 이후 폐업점 평균 존속", f"{s['avg_years_closed_since_2010'] or '-'}년")
            c[3].metric("최대 동시영업", u["max_concurrent_stores"])
            st.caption("업태 변천: " + s["category_path"])
            history_chart(h, key="unit-tl")
            st.markdown("**업태별 존속기간 (이 자리 vs 대구 중앙생존)**")
            st.dataframe(pd.DataFrame(h["by_category"]), hide_index=True, width="stretch")
            st.dataframe(pd.DataFrame(h["timeline"]), hide_index=True, width="stretch")
            if h["other_candidates"]:
                st.markdown("**다른 후보 자리**")
                st.dataframe(pd.DataFrame(h["other_candidates"])[["address", "floor", "records", "status"]], hide_index=True)
