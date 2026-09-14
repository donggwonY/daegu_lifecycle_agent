"""Kaplan-Meier 생존분석 (영업 중 점포 = 중도절단)."""
from __future__ import annotations

import numpy as np

YEAR = 365.25


def kaplan_meier(durations_days, events) -> tuple[np.ndarray, np.ndarray]:
    """고유 사건시점 t 와 S(t) 반환. events: 1=폐업(사건), 0=영업중(중도절단)."""
    d = np.asarray(durations_days, dtype=float)
    e = np.asarray(events, dtype=int)
    if d.size == 0:
        return np.array([0.0]), np.array([1.0])
    order = np.argsort(d, kind="mergesort")
    d, e = d[order], e[order]
    times, idx = np.unique(d, return_index=True)
    n = d.size
    at_risk = n - idx
    deaths = np.add.reduceat(e, idx)
    s = np.cumprod(1.0 - deaths / at_risk)
    keep = deaths > 0
    return np.concatenate([[0.0], times[keep]]), np.concatenate([[1.0], s[keep]])


def survival_at(times: np.ndarray, surv: np.ndarray, t_days: float) -> float:
    i = np.searchsorted(times, t_days, side="right") - 1
    return float(surv[max(i, 0)])


def median_survival(times: np.ndarray, surv: np.ndarray) -> float | None:
    below = np.nonzero(surv <= 0.5)[0]
    return float(times[below[0]]) if below.size else None


def summarize(durations_days, events, curve_years: int = 10) -> dict:
    """Tool 반환용 요약 — 근거 수치(n, 폐업, 중도절단)를 반드시 포함."""
    d = np.asarray(durations_days, dtype=float)
    e = np.asarray(events, dtype=int)
    t, s = kaplan_meier(d, e)
    med = median_survival(t, s)
    max_follow = float(d.max()) if d.size else 0.0

    def pct(years):
        if max_follow < years * YEAR:  # 관측기간을 넘는 시점은 추정 불가
            return None
        return round(survival_at(t, s, years * YEAR) * 100, 1)

    return {
        "n": int(d.size),
        "closed": int(e.sum()),
        "censored_active": int(d.size - e.sum()),
        "median_survival_years": round(med / YEAR, 1) if med is not None else None,
        "survival_1y_pct": pct(1),
        "survival_3y_pct": pct(3),
        "survival_5y_pct": pct(5),
        "curve": [
            {"year": y, "survival_pct": round(survival_at(t, s, y * YEAR) * 100, 1)}
            for y in range(0, curve_years + 1)
            if max_follow >= y * YEAR
        ],
    }
