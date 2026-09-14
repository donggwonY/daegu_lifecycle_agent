"""주소 정규화.

'자리(unit)' 의 키는 건물 단위 기본주소다.
  1순위: 지번 기본주소  (대구광역시 중구 삼덕동1가 28-6)   ← 1960년대 레코드까지 채워져 있음
  2순위: 도로명 기본주소 (대구광역시 중구 동성로5길 83)   → 지번이 함께 있는 레코드로 학습한 매핑으로 지번 키에 합류
층·호수는 연도별 기재 방식이 달라 키에서 제외하고, 다점포 건물 여부는 동시영업 수로 따로 판별한다.
"""
from __future__ import annotations

import re

SIDO = "대구광역시"

_WS = re.compile(r"\s+")
_JIBUN = re.compile(
    r"^대구광역시\s+(?P<gu>\S+?[구군])\s+"
    r"(?:(?P<eup>\S+?[읍면])\s+)?"
    r"(?P<dong>\S+?(?:동|가|리|로))\s+"
    r"(?P<san>산\s*)?(?P<main>\d+)(?:\s*-\s*(?P<sub>\d+))?"
)
_ROAD = re.compile(
    r"^대구광역시\s+(?P<gu>\S+?[구군])\s+"
    r"(?:(?P<eup>\S+?[읍면])\s+)?"
    r"(?P<road>\S+?(?:로|길))\s+"
    r"(?P<under>지하\s*)?(?P<main>\d+)(?:\s*-\s*(?P<sub>\d+))?"
)
_PAREN_DONG = re.compile(r"\((?P<dong>[^,()]+?(?:동|가|리))(?:,|\))")
_FLOOR = re.compile(r"(지하\s*\d+\s*층|\d+\s*층)")


def _clean(s: str | None) -> str:
    if not isinstance(s, str):
        return ""
    s = _WS.sub(" ", s.replace("　", " ")).strip()
    # 사용자가 "대구 수성구 ..." 처럼 줄여 쓴 경우
    if s.startswith("대구시 "):
        s = SIDO + s[3:]
    elif s.startswith("대구 "):
        s = SIDO + s[2:]
    return s


def _num(main: str, sub: str | None) -> str:
    main = str(int(main))
    if sub and int(sub) != 0:
        return f"{main}-{int(sub)}"
    return main


def parse_jibun(addr: str | None) -> dict | None:
    s = _clean(addr)
    m = _JIBUN.match(s)
    if not m:
        return None
    parts = [SIDO, m["gu"]]
    if m["eup"]:
        parts.append(m["eup"])
    parts.append(m["dong"])
    lot = ("산" if m["san"] else "") + _num(m["main"], m["sub"])
    parts.append(lot)
    return {"key": " ".join(parts), "gu": m["gu"], "eup": m["eup"], "dong": m["dong"]}


def parse_road(addr: str | None) -> dict | None:
    s = _clean(addr)
    m = _ROAD.match(s)
    if not m:
        return None
    parts = [SIDO, m["gu"]]
    if m["eup"]:
        parts.append(m["eup"])
    parts.append(m["road"])
    parts.append(("지하 " if m["under"] else "") + _num(m["main"], m["sub"]))
    pd_ = _PAREN_DONG.search(s)
    dong = pd_["dong"].strip() if pd_ else None
    return {"key": " ".join(parts), "gu": m["gu"], "eup": m["eup"], "dong": dong, "road": m["road"]}


def extract_floor(*addrs: str | None) -> str | None:
    for a in addrs:
        if isinstance(a, str):
            m = _FLOOR.search(a)
            if m:
                return _WS.sub("", m.group(1))
    return None


def normalize_gu(text: str | None) -> str | None:
    """'수성', '수성구', '대구 수성구' → '수성구'."""
    if not text:
        return None
    t = _clean(text).replace(SIDO, "").strip()
    for gu in ("중구", "동구", "서구", "남구", "북구", "수성구", "달서구", "달성군", "군위군"):
        if t == gu or t == gu[:-1] or t.startswith(gu + " ") or t.startswith(gu[:-1] + " "):
            return gu
    return None


def is_daegu(addr: str | None) -> bool:
    return _clean(addr).startswith(SIDO)
