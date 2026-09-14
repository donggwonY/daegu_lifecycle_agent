"""야간 배치: 인허가 원본 CSV → 분석 테이블 사전 계산.

실행:  python -m pipeline.build
산출:  data/processed/{stores,units,transitions,area_year,area_cycle,survival,vacancy_area}.parquet, meta.json

무거운 계산은 전부 여기서 끝내고, 에이전트 Tool 은 이 산출물을 조회만 한다 (기획서 8장 설계 원칙).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as C  # noqa: E402
from core import address as A  # noqa: E402
from core.cycle import classify_stage  # noqa: E402
from core.survival import YEAR, summarize  # noqa: E402

USECOLS = [
    "개방자치단체코드", "관리번호", "인허가일자", "영업상태명", "폐업일자", "소재지면적",
    "사업장명", "업태구분명", "위생업태명", "데이터갱신구분", "도로명주소", "지번주소",
    "좌표정보(X)", "좌표정보(Y)", "최종수정시점", "데이터갱신시점",
]
DAY = pd.Timedelta(days=1)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ─────────────────────────── 1. 적재 ───────────────────────────
def load_raw() -> tuple[pd.DataFrame, dict]:
    frames = []
    for path in sorted(C.RAW_DIR.rglob("*.csv")):
        service = next((s for s in C.SERVICES if s in path.name), None)
        if service is None:
            log(f"  건너뜀(업종 미식별): {path.name}")
            continue
        df = pd.read_csv(path, encoding="cp949", encoding_errors="replace", dtype=str,
                         usecols=lambda c: c in USECOLS)
        df["service"] = service
        df["service_id"] = C.SERVICES[service]
        frames.append(df)
        log(f"  {path.name}: {len(df):,}행")
    if not frames:
        raise SystemExit(f"{C.RAW_DIR} 에 인허가 CSV 가 없습니다.")
    df = pd.concat(frames, ignore_index=True)
    n_raw = len(df)
    # 고유키 = 지자체코드 + 관리번호 + 개방서비스ID. 증분(I/U) 파일이 섞여 있으면 최종수정시점이 가장 늦은 행 채택
    df = df.sort_values(["최종수정시점", "데이터갱신시점"], na_position="first")
    df = df.drop_duplicates(["개방자치단체코드", "관리번호", "service_id"], keep="last")
    return df.reset_index(drop=True), {"raw_rows": n_raw, "duplicates_removed": n_raw - len(df)}


# ─────────────────────────── 2. 정제 ───────────────────────────
def clean(df: pd.DataFrame, quality: dict) -> tuple[pd.DataFrame, pd.Timestamp]:
    df["ld"] = pd.to_datetime(df["인허가일자"], errors="coerce")
    df["cd"] = pd.to_datetime(df["폐업일자"], errors="coerce")
    df["closed"] = df["영업상태명"].fillna("").str.contains("폐업|취소|말소")

    quality["missing_license_date"] = int(df["ld"].isna().sum())
    quality["closed_without_close_date"] = int((df["closed"] & df["cd"].isna()).sum())
    quality["date_contradiction"] = int((df["cd"] < df["ld"]).sum())

    in_daegu = df["지번주소"].map(A.is_daegu) | df["도로명주소"].map(A.is_daegu)
    quality["non_daegu_address"] = int((~in_daegu).sum())
    quality["non_daegu_examples"] = (df.loc[~in_daegu, "지번주소"].fillna(df.loc[~in_daegu, "도로명주소"])
                                     .head(5).tolist())

    ok = in_daegu & df["ld"].notna() & ~(df["closed"] & df["cd"].isna()) & ~(df["cd"] < df["ld"])
    df = df[ok].copy()
    ref = max(df["ld"].max(), df["cd"].max()).normalize()

    df["category"] = (df["업태구분명"].fillna("").str.strip()
                      .replace("", np.nan).fillna(df["위생업태명"]).fillna("기타").str.strip())
    df["name"] = df["사업장명"].fillna("").str.strip()
    df["end"] = df["cd"].where(df["closed"], ref)
    df["dur_days"] = (df["end"] - df["ld"]).dt.days.clip(lower=0)
    df["area_m2"] = pd.to_numeric(df["소재지면적"], errors="coerce")
    return df, ref


# ─────────────────────────── 3. 주소·좌표 ───────────────────────────
FLOOR_DOMINANCE = 0.8


def assign_floor_units(df: pd.DataFrame) -> pd.Series:
    """자리 키 = 건물 기본주소 + 층.

    층 기재는 2010년 이후에야 보편화되었으므로(2000년대 18% → 2020년대 86%),
    층이 없는 과거 레코드는 그 건물의 층 기재가 한 층에 80% 이상 몰려 있을 때만 그 층으로 합류시키고,
    아니면 '층 미상' 건물 키로 남긴다.
    """
    floored = df.dropna(subset=["floor"])
    share = floored.groupby(["building_key", "floor"]).size()
    share = (share / share.groupby(level=0).transform("sum")).reset_index(name="share")
    dominant = (share[share["share"] >= FLOOR_DOMINANCE]
                .drop_duplicates("building_key").set_index("building_key")["floor"])
    floor = df["floor"].fillna(df["building_key"].map(dominant))
    return np.where(floor.notna(), df["building_key"] + " " + floor.fillna(""), df["building_key"])


def build_address(df: pd.DataFrame, quality: dict) -> pd.DataFrame:
    jb = df["지번주소"].map(A.parse_jibun)
    rd = df["도로명주소"].map(A.parse_road)
    df["jibun_key"] = jb.map(lambda x: x["key"] if x else None)
    df["road_key"] = rd.map(lambda x: x["key"] if x else None)

    # 도로명 → 지번 매핑 학습 (둘 다 있는 레코드에서 최빈값)
    both = df.dropna(subset=["jibun_key", "road_key"])
    road2jibun = (both.groupby(["road_key", "jibun_key"]).size().reset_index(name="n")
                  .sort_values("n").drop_duplicates("road_key", keep="last")
                  .set_index("road_key")["jibun_key"])
    fallback_raw = df["지번주소"].fillna(df["도로명주소"]).fillna("").str.strip()
    df["uid"] = (df["jibun_key"].fillna(df["road_key"].map(road2jibun))
                 .fillna(df["road_key"]).fillna(fallback_raw))

    gu_j = jb.map(lambda x: x["gu"] if x else None)
    gu_r = rd.map(lambda x: x["gu"] if x else None)
    df["gu"] = gu_j.fillna(gu_r)
    df["gu"] = df["gu"].fillna(df["uid"].map(A.normalize_gu))
    # 동네 단위: 군 지역은 읍·면, 구 지역은 법정동
    dong_j = jb.map(lambda x: (x["eup"] or x["dong"]) if x else None)
    dong_r = rd.map(lambda x: (x["eup"] or x["dong"]) if x else None)
    df["dong"] = dong_j.fillna(dong_r)
    df["floor"] = [A.extract_floor(r, j) for r, j in zip(df["도로명주소"], df["지번주소"])]
    df["building_key"] = df["uid"]
    df["uid"] = assign_floor_units(df)

    quality["address_unparsed"] = int((df["jibun_key"].isna() & df["road_key"].isna()).sum())

    x = pd.to_numeric(df["좌표정보(X)"].str.strip(), errors="coerce").to_numpy()
    y = pd.to_numeric(df["좌표정보(Y)"].str.strip(), errors="coerce").to_numpy()
    lon, lat = Transformer.from_crs(C.SRC_CRS, C.DST_CRS, always_xy=True).transform(x, y)
    valid = np.isfinite(lat) & (lat > 35.4) & (lat < 36.5) & (lon > 128.1) & (lon < 129.1)
    df["lat"] = np.where(valid, lat, np.nan)
    df["lon"] = np.where(valid, lon, np.nan)
    quality["coord_fill_pct"] = {
        s: round(float(g["lat"].notna().mean() * 100), 1) for s, g in df.groupby("service")
    }
    return df


# ─────────────────────────── 4. 자리(unit) ───────────────────────────
def max_concurrency(df: pd.DataFrame, ref: pd.Timestamp, tolerance_days: int) -> pd.Series:
    """시작=+1, 종료=-1 이벤트 누적합의 최댓값 = 동시 영업 점포 수 (기획서 문제2).

    같은 날 종료·시작은 '교체'이므로 종료(-1)를 먼저 정렬하고,
    양도·양수 시 신·구 인허가가 tolerance 이내로 겹치는 것도 교체로 본다.
    """
    end = df["cd"].where(df["closed"], ref + DAY)
    if tolerance_days:
        end = np.maximum(end - pd.Timedelta(days=tolerance_days), df["ld"] + DAY)
    ev = pd.DataFrame({
        "uid": np.concatenate([df["uid"].to_numpy(), df["uid"].to_numpy()]),
        "t": np.concatenate([df["ld"].to_numpy(), pd.to_datetime(end).to_numpy()]),
        "d": np.concatenate([np.ones(len(df), int), -np.ones(len(df), int)]),
    }).sort_values(["uid", "t", "d"])
    ev["cum"] = ev.groupby("uid")["d"].cumsum()
    return ev.groupby("uid")["cum"].max()


def build_units(st: pd.DataFrame, ref: pd.Timestamp) -> pd.DataFrame:
    st = st.assign(active=~st["closed"],
                   closed_2010=st["closed"] & (st["ld"].dt.year >= C.SURVIVAL_START_YEAR))
    g = st.groupby("uid")
    units = g.agg(
        gu=("gu", "first"), dong=("dong", "first"),
        n_records=("ld", "size"), n_active=("active", "sum"), n_closed=("closed", "sum"),
        closures_since_2010=("closed_2010", "sum"),
        first_open=("ld", "min"), last_open=("ld", "max"), last_close=("cd", "max"),
        lat=("lat", "median"), lon=("lon", "median"),
        n_services=("service", "nunique"), n_categories=("category", "nunique"),
    )
    closed = st[st["closed"]]
    units["avg_life_closed_years"] = (closed.groupby("uid")["dur_days"].mean() / YEAR).round(2)
    c10 = st[st["closed_2010"]]
    units["avg_life_since_2010_years"] = (c10.groupby("uid")["dur_days"].mean() / YEAR).round(2)

    road = (st.dropna(subset=["road_key"]).groupby(["uid", "road_key"]).size().reset_index(name="n")
            .sort_values("n").drop_duplicates("uid", keep="last").set_index("uid")["road_key"])
    units["floor"] = st.groupby("uid")["floor"].agg(lambda s: s.mode().iat[0] if s.notna().any() else None)
    units["building_key"] = g["building_key"].first()
    units["road_addr"] = road
    units["jibun_addr"] = units["building_key"]
    units["addr"] = (units["road_addr"].fillna(units["jibun_addr"])
                     + units["floor"].map(lambda f: f" {f}" if isinstance(f, str) else ""))

    ordered = st.sort_values(["uid", "ld", "end"])
    last = ordered.groupby("uid").tail(1).set_index("uid")
    units["last_name"] = last["name"]
    units["last_category"] = last["category"]
    units["last_service"] = last["service"]
    units["last_life_years"] = (last["dur_days"] / YEAR).round(2)
    units["category_path"] = ordered.groupby("uid")["category"].agg(lambda s: " → ".join(s.tolist()[-8:]))
    units["current_stores"] = (st[st["active"]].groupby("uid")["name"]
                               .agg(lambda s: ", ".join(s.tolist()[:3])))

    units["max_concurrent"] = max_concurrency(st, ref, C.HANDOVER_TOLERANCE_DAYS)
    units["max_concurrent_naive"] = max_concurrency(st, ref, 0)
    units["is_single_unit"] = units["max_concurrent"] <= 1

    units["vacant"] = units["n_active"] == 0
    units["vacant_days"] = np.where(units["vacant"], (ref - units["last_close"]).dt.days, np.nan)
    units["recent_vacancy"] = units["vacant"] & units["vacant_days"].between(C.VACANCY_MIN_DAYS, C.VACANCY_MAX_DAYS)
    units["vacancy_bucket"] = pd.cut(units["vacant_days"], [-1, 89, 365, 365 * 3, 365 * 10, 1e9],
                                     labels=["3개월 미만", "3개월~1년", "1~3년", "3~10년", "10년 이상"]).astype(str)
    units.loc[~units["vacant"], "vacancy_bucket"] = "영업중"
    units.index.name = "uid"
    return units.reset_index()


# ─────────────────────────── 5. 업종 전이 ───────────────────────────
def build_transitions(st: pd.DataFrame, units: pd.DataFrame) -> pd.DataFrame:
    single = set(units.loc[units["is_single_unit"], "uid"])
    s = st[st["uid"].isin(single)].sort_values(["uid", "ld", "end"]).reset_index(drop=True)
    nxt = s.groupby("uid")[["ld", "category", "service", "name", "dur_days", "closed"]].shift(-1)
    m = nxt["ld"].notna() & s["closed"]
    t = pd.DataFrame({
        "uid": s["uid"], "gu": s["gu"], "dong": s["dong"],
        "from_name": s["name"], "from_category": s["category"], "from_service": s["service"],
        "from_life_years": (s["dur_days"] / YEAR).round(2),
        "to_name": nxt["name"], "to_category": nxt["category"], "to_service": nxt["service"],
        "to_open": nxt["ld"], "to_dur_days": nxt["dur_days"], "to_closed": nxt["closed"].astype("boolean"),
        "gap_days": (nxt["ld"] - s["cd"]).dt.days,
    })[m]
    return t[t["gap_days"] >= -C.HANDOVER_TOLERANCE_DAYS].reset_index(drop=True)


# ─────────────────────────── 6. 상권 사이클 ───────────────────────────
def build_cycle(st: pd.DataFrame, ref: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    w = pd.DateOffset(years=C.CYCLE_WINDOW_YEARS)
    t1, t0 = ref - w, ref - w - w
    st = st.assign(
        open_recent=st["ld"] > t1, close_recent=st["closed"] & (st["cd"] > t1),
        open_prev=(st["ld"] > t0) & (st["ld"] <= t1),
        close_prev=st["closed"] & (st["cd"] > t0) & (st["cd"] <= t1),
        active_now=~st["closed"],
        active_then=(st["ld"] <= t1) & (~st["closed"] | (st["cd"] > t1)),
    )
    cols = ["open_recent", "close_recent", "open_prev", "close_prev", "active_now", "active_then"]
    parts = []
    for level, keys in (("구군", ["gu"]), ("동", ["gu", "dong"]), ("대구", [])):
        agg = (st.groupby(keys)[cols].sum().reset_index() if keys
               else st[cols].sum().to_frame().T)
        agg["level"] = level
        parts.append(agg)
    cyc = pd.concat(parts, ignore_index=True)
    cyc[cols] = cyc[cols].astype(int)
    staged = cyc.apply(lambda r: classify_stage(r["open_recent"], r["close_recent"], r["open_prev"],
                                                r["close_prev"], r["active_now"], r["active_then"]),
                       axis=1, result_type="expand")
    cyc = pd.concat([cyc, staged], axis=1)
    cyc["window_recent"] = f"{t1.date()} ~ {ref.date()}"
    cyc["window_prev"] = f"{t0.date()} ~ {t1.date()}"

    # 연도별 개업·폐업·연말 영업점포 수
    yrs = []
    for level, keys in (("구군", ["gu"]), ("동", ["gu", "dong"]), ("대구", [])):
        o = st.assign(year=st["ld"].dt.year).groupby(keys + ["year"]).size().rename("opens")
        c = (st[st["closed"]].assign(year=st["cd"].dt.year).groupby(keys + ["year"]).size().rename("closes"))
        y = pd.concat([o, c], axis=1).fillna(0).astype(int).reset_index().sort_values(keys + ["year"])
        net = y["opens"] - y["closes"]
        y["active_end_of_year"] = net.groupby([y[k] for k in keys]).cumsum() if keys else net.cumsum()
        y["level"] = level
        y["year"] = y["year"].astype(int)
        yrs.append(y[y["year"] >= 2000])
    area_year = pd.concat(yrs, ignore_index=True)
    area_year["partial_year"] = area_year["year"] == ref.year
    return cyc, area_year


# ─────────────────────────── 7. 생존분석 ───────────────────────────
def build_survival(st: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    coh = st[st["ld"].dt.year >= C.SURVIVAL_START_YEAR]
    rows = []

    def add(level, frame, **keys):
        if len(frame) < C.MIN_GROUP_N:
            return
        s = summarize(frame["dur_days"], frame["closed"])
        rows.append({"level": level, "service": keys.get("service"), "category": keys.get("category"),
                     "gu": keys.get("gu"), "dong": keys.get("dong"),
                     **{k: v for k, v in s.items() if k != "curve"},
                     "curve_json": json.dumps(s["curve"], ensure_ascii=False)})

    add("전체", coh)
    for sv, f in coh.groupby("service"):
        add("업종", f, service=sv)
    for cat, f in coh.groupby("category"):
        add("업태", f, category=cat, service=f["service"].mode().iat[0])
    for gu, f in coh.groupby("gu"):
        add("구군", f, gu=gu)
        for sv, ff in f.groupby("service"):
            add("구군×업종", ff, gu=gu, service=sv)
        for cat, ff in f.groupby("category"):
            add("구군×업태", ff, gu=gu, category=cat)
        for dong, ff in f.groupby("dong"):
            add("동", ff, gu=gu, dong=dong)
    surv = pd.DataFrame(rows)

    # 생존편향 점검용: 개업 연대별 1년 생존율 (전 기간)
    decade = []
    for dec, f in st.assign(decade=(st["ld"].dt.year // 10) * 10).groupby("decade"):
        if dec < 1980 or len(f) < C.MIN_GROUP_N:
            continue
        s = summarize(f["dur_days"], f["closed"], curve_years=0)
        decade.append({"decade": f"{dec}년대", "n": s["n"], "survival_1y_pct": s["survival_1y_pct"]})
    return surv, decade


# ─────────────────────────── 8. 공실률 ───────────────────────────
def build_vacancy_area(units: pd.DataFrame) -> pd.DataFrame:
    live = units[~units["vacant"] | (units["vacant_days"] <= C.VACANCY_MAX_DAYS)]
    out = []
    for level, keys in (("구군", ["gu"]), ("동", ["gu", "dong"])):
        g = live.groupby(keys).agg(units_3y=("uid", "size"), vacant=("recent_vacancy", "sum"),
                                   occupied=("vacant", lambda v: int((~v).sum())))
        g["vacancy_rate_pct"] = (g["vacant"] / g["units_3y"] * 100).round(1)
        g["level"] = level
        out.append(g.reset_index())
    return pd.concat(out, ignore_index=True)


# ─────────────────────────── main ───────────────────────────
def main() -> None:
    t_start = time.time()
    C.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    log("1/8 원본 적재")
    df, quality = load_raw()
    log("2/8 정제")
    df, ref = clean(df, quality)
    log(f"    기준일(데이터 최신일) = {ref.date()}")
    log("3/8 주소 정규화 · 좌표 변환(EPSG:5174→WGS84)")
    df = build_address(df, quality)

    keep = ["uid", "building_key", "service", "category", "name", "ld", "cd", "closed", "dur_days", "end", "gu", "dong",
            "floor", "도로명주소", "지번주소", "road_key", "lat", "lon", "area_m2", "관리번호", "개방자치단체코드"]
    st = df[keep].rename(columns={"도로명주소": "road_addr_raw", "지번주소": "jibun_addr_raw",
                                  "관리번호": "mgmt_no", "개방자치단체코드": "org_code"})

    log("4/8 자리(unit) 집계 · 동시영업 수 계산")
    units = build_units(st, ref)
    log("5/8 업종 전이")
    trans = build_transitions(st, units)
    log("6/8 상권 사이클")
    cycle, area_year = build_cycle(st, ref)
    log("7/8 Kaplan-Meier 생존분석")
    surv, decade = build_survival(st)
    log("8/8 공실률")
    vac = build_vacancy_area(units)

    # 업종 확장 효과 (문제4): 일반음식점만 봤다면 공실로 오판했을 자리
    latest = st.sort_values("ld").groupby("uid").tail(1).set_index("uid")
    switch = trans[(trans["from_service"] != trans["to_service"]) & (trans["to_closed"] == False)]  # noqa: E712
    switch_units = switch[switch["uid"].isin(latest.index[~latest["closed"]])]["uid"].nunique()

    naive_top = units.sort_values("n_closed", ascending=False).head(5)
    single = units[units["is_single_unit"]]
    single_top = single.sort_values("n_closed", ascending=False).head(5)

    meta = {
        "reference_date": str(ref.date()),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "quality": quality,
        "records": {
            sv: {"total": int(len(f)), "closed": int(f["closed"].sum()), "active": int((~f["closed"]).sum()),
                 "unique_units": int(f["uid"].nunique()),
                 "date_min": str(f["ld"].min().date()), "date_max": str(max(f["ld"].max(), f["cd"].max()).date())}
            for sv, f in st.groupby("service")
        } | {"통합": {"total": int(len(st)), "closed": int(st["closed"].sum()),
                     "active": int((~st["closed"]).sum()), "unique_units": int(st["uid"].nunique())}},
        "records_by_gu": st["gu"].value_counts().to_dict(),
        "vacancy_buckets_all_vacant_units": units.loc[units["vacant"], "vacancy_bucket"].value_counts().to_dict(),
        "recent_vacancy_units": int(units["recent_vacancy"].sum()),
        "single_unit_ratio_pct": round(float(units["is_single_unit"].mean() * 100), 1),
        "max_closures_all_units_top5": naive_top[["addr", "n_closed", "max_concurrent"]].to_dict("records"),
        "max_closures_single_units_top5": single_top[["addr", "n_closed", "category_path"]].to_dict("records"),
        "service_switch_units": int(switch_units),
        "survival_by_decade_1y": decade,
        "params": {k: getattr(C, k) for k in ("SURVIVAL_START_YEAR", "VACANCY_MIN_DAYS", "VACANCY_MAX_DAYS",
                                               "HANDOVER_TOLERANCE_DAYS", "CYCLE_WINDOW_YEARS", "CYCLE_MIN_EVENTS")},
    }

    st.drop(columns=["end"]).to_parquet(C.PROCESSED_DIR / "stores.parquet", index=False)
    units.to_parquet(C.PROCESSED_DIR / "units.parquet", index=False)
    trans.to_parquet(C.PROCESSED_DIR / "transitions.parquet", index=False)
    cycle.to_parquet(C.PROCESSED_DIR / "area_cycle.parquet", index=False)
    area_year.to_parquet(C.PROCESSED_DIR / "area_year.parquet", index=False)
    surv.to_parquet(C.PROCESSED_DIR / "survival.parquet", index=False)
    vac.to_parquet(C.PROCESSED_DIR / "vacancy_area.parquet", index=False)
    (C.PROCESSED_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                                               encoding="utf-8")
    log(f"완료 ({time.time() - t_start:.0f}초) → {C.PROCESSED_DIR}")


if __name__ == "__main__":
    main()
