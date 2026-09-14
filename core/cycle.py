"""상권 사이클 단계 판정 — 개업/폐업 비율의 시계열 추세."""
from __future__ import annotations

import config as C

STAGE_DESC = {
    "성장기": "개업이 폐업보다 뚜렷하게 많고 이전 기간에도 증가 추세",
    "회복기": "직전 기간엔 폐업 우위였으나 최근 개업 우위로 반전",
    "성숙기": "개업과 폐업이 비슷해 점포 수가 안정적",
    "쇠퇴 진입기": "직전 기간엔 균형 이상이었으나 최근 폐업 우위로 전환",
    "쇠퇴기": "두 기간 연속 폐업이 개업보다 많아 점포 수 감소",
    "판정 보류": "개·폐업 표본이 적어 추세 판단 불가",
}


def classify_stage(open_recent, close_recent, open_prev, close_prev, active_now, active_then) -> dict:
    events = int(open_recent + close_recent)
    r = open_recent / close_recent if close_recent else None
    rp = open_prev / close_prev if close_prev else None
    change = (active_now - active_then) / active_then * 100 if active_then else None
    if events < C.CYCLE_MIN_EVENTS or r is None or rp is None:
        stage = "판정 보류"
    elif r >= 1.1:
        stage = "회복기" if rp < 0.9 else "성장기"
    elif r >= 0.9:
        stage = "성숙기"
    else:
        stage = "쇠퇴 진입기" if rp >= 0.9 else "쇠퇴기"
    return {
        "stage": stage,
        "stage_desc": STAGE_DESC[stage],
        "open_close_ratio_recent": round(r, 2) if r is not None else None,
        "open_close_ratio_prev": round(rp, 2) if rp is not None else None,
        "active_change_pct": round(change, 1) if change is not None else None,
    }
