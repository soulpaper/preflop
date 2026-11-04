"""차례 고리(acting_seat/acting_progress) · 말풍선 색 기준 · handlog 차례 기록 테스트. 실행: python test_ring.py

합성 그림 테스트는 늘 돈다. 실제 화면 테스트는 로컬에만 있는 파일(samples/, dist/recordings/ — 다른 사람 닉네임이 보여서
저장소에 없음)을 읽고, 없으면 SKIP.
기본 위치는 이 파일이 있는 card-reader 폴더, 다른 곳이면 환경변수 CARD_READER_DATA 로 지정. 녹화 프레임의 기대값은 2026-11-04 에 눈으로 확인한 것.
"""
import glob
import json
import math
import os
import re
import sys
from pathlib import Path

import cv2
import numpy as np

import handlog
import layout
import tablestate
import test_handlog

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

APP = Path(os.environ.get("CARD_READER_DATA") or Path(__file__).resolve().parent)
REC = APP / "dist" / "recordings"
SAMPLE = APP / "samples" / "turn_ring_2026-11-04_top_seat.png"
results, skipped = [], []


def check(name, ok, info=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info else ""))


def skip(name, why):
    skipped.append(name)
    print(f"[SKIP] {name} — {why}")


# --- 합성 고리 ---------------------------------------------------------------------------------------
def ring_image(progress, color=(255, 120, 40), gap=None, size=200, r=40):
    """어두운 바닥에 아바타(회색 원) + 12시에서 시계 방향으로 progress 만큼 지나간 고리 (남은 쪽은 315°까지 빛남)."""
    img = np.full((size, size, 3), 25, np.uint8)
    c = size // 2
    cv2.circle(img, (c, c), r - 2, (90, 90, 90), -1)
    start, end = 360 * progress, 315.0
    for deg in np.arange(start, end, 0.5):
        if gap and gap[0] <= deg < gap[1]:
            continue
        t = math.radians(deg)
        for rf in np.arange(1.0, 1.22, 0.02):
            x, y = int(round(c + rf * r * math.sin(t))), int(round(c - rf * r * math.cos(t)))
            img[y, x] = color
    return img, (c, c, r)


def synthetic():
    for p in (0.05, 0.3, 0.55):
        img, av = ring_image(p)
        res = tablestate.TableReader._ring(cv2.cvtColor(img, cv2.COLOR_BGR2HSV), av)
        ok = res is not None and res["progress"] is not None and abs(res["progress"] - p) <= 0.05
        check(f"합성 고리: 진행 {p:.2f} → 잡고 진행도 맞음", ok, res)
    img, av = ring_image(0.2, color=(255, 90, 200))  # 보라
    res = tablestate.TableReader._ring(cv2.cvtColor(img, cv2.COLOR_BGR2HSV), av)
    check("합성 고리: 보라색도 잡음", res is not None, res)
    img, av = ring_image(0.1, gap=(150, 200))  # 카드 아이콘이 아래를 가림
    res = tablestate.TableReader._ring(cv2.cvtColor(img, cv2.COLOR_BGR2HSV), av)
    check("합성 고리: 일부 가려져도 잡음", res is not None, res)
    # 고리 없음 / 짧은 파란 호(= '콜' 말풍선 자리) / 빨강(올인 불꽃) → None
    img, av = ring_image(0.0)
    img[:] = 25
    cv2.circle(img, (100, 100), 38, (90, 90, 90), -1)
    check("합성: 고리 없음 → None", tablestate.TableReader._ring(cv2.cvtColor(img, cv2.COLOR_BGR2HSV), av) is None)
    img, av = ring_image(0.70)  # 파랑인데 짧음 (252°~315°)
    check("합성: 짧은 파란 호 → None (말풍선과 구별 못 함)",
          tablestate.TableReader._ring(cv2.cvtColor(img, cv2.COLOR_BGR2HSV), av) is None)
    img, av = ring_image(0.1, color=(30, 30, 255))  # 빨강
    check("합성: 빨간 고리 모양(올인 불꽃) → None",
          tablestate.TableReader._ring(cv2.cvtColor(img, cv2.COLOR_BGR2HSV), av) is None)
    img, av = ring_image(0.1)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hsv[:, :] = hsv[100, 100 + 46]  # 화면 전체가 같은 색 (쇼다운 보라 화면 등)
    check("합성: 고리 바깥까지 같은 색 → None", tablestate.TableReader._ring(hsv, av) is None)


# --- 실제 화면 ---------------------------------------------------------------------------------------
def frame(session, idx):
    folder = REC / session
    files = glob.glob(str(folder / f"{idx:06d}_*.jpg"))
    if not files:
        return None, None
    t = int(Path(files[0]).stem.split("_")[1]) / 1000
    cal = []
    for p in glob.glob(str(folder / "calib_*.json")):  # 녹화 중 다시 보정: calib_<시각>.json 은 그 시각 전까지
        cal.append((int(re.match(r"calib_(\d+)", Path(p).name).group(1)), p))
    cal.sort()
    cal.append((10 ** 12, str(folder / "calib.json")))
    path = next(p for stamp, p in cal if t < stamp)
    return cv2.imread(files[0]), json.loads(Path(path).read_text(encoding="utf-8"))


# (세션, 프레임, 지금 차례인 자리, 진행도 범위 또는 None)
RING_FRAMES = [
    ("20261030_014157", 56, "seat_BL", (0.0, 0.1)), ("20261030_014157", 57, "seat_BL", (0.1, 0.25)),
    ("20261030_014157", 58, "seat_UL", (0.0, 0.1)), ("20261030_014157", 59, "seat_TC", (0.0, 0.1)),
    ("20261030_014157", 60, "seat_BR", (0.0, 0.1)), ("20261030_014157", 63, "hero", (0.0, 0.1)),
    ("20261030_014157", 124, "seat_TC", (0.0, 0.1)), ("20261030_014157", 13, "hero", (0.0, 0.1)),
    ("20261030_014157", 226, "seat_UR", None),  # 머리카락이 오른쪽 위 고리를 가림 → 자리만 (진행도는 확인 안 함)
    ("20261029_021253", 566, "hero", (0.0, 0.12)), ("20261029_021253", 568, "hero", (0.22, 0.35)),
    ("20261029_021253", 570, "hero", (0.45, 0.6)),
]
NO_RING = [("20261029_021253", 572), ("20261029_021253", 573), ("20261029_021253", 574),  # 끝 무렵: 고리가 흐림
           ("20261029_021253", 54), ("20261029_021253", 742)]                              # 고리 없음
PROGRESS_RUN = ("20261030_014157", range(101, 106))   # 같은 차례 연속 프레임: 진행도가 늘어남

# (세션, 프레임, 자리, 기대 말풍선) — 예전 기준으로는 틀리던 것들
BUBBLES = [
    ("20261030_014157", 703, "seat_UR", "check"), ("20261030_014157", 703, "seat_BR", "check"),
    ("20261030_013933", 51, "seat_BL", "check"), ("20261030_014157", 402, "seat_UR", "check"),
    ("20261030_014157", 84, "hero", "check"),
    ("20261030_014157", 703, "seat_BL", "allin"), ("20261030_014157", 363, "seat_UR", "allin"),
    ("20261029_233140", 665, "seat_TL", "allin"),
    ("20261029_021253", 117, "seat_UL", "raise"),   # '벳' 말풍선 (채도가 조금 낮은 주황)
    ("20261029_021253", 556, "seat_UR", "call"), ("20261029_021253", 297, "seat_UR", "call"),
    ("20261030_013933", 51, "hero", None), ("20261030_013933", 38, "hero", None),  # 내 차례 고리 ≠ '콜' 말풍선
]


def recorded():
    tr = tablestate.TableReader()
    if not REC.exists():
        skip("녹화 프레임", f"{REC} 없음")
        return
    for ses, i, want, prog in RING_FRAMES:
        img, calib = frame(ses, i)
        if img is None:
            skip(f"{ses}/{i}", "프레임 없음")
            continue
        st = tr.read(img, calib)
        p = st["acting_progress"]
        ok = st["acting_seat"] == want and (prog is None or (p is not None and prog[0] <= p <= prog[1]))
        check(f"녹화 {ses}/{i}: 차례 {want} 진행 {prog}", ok, f"{st['acting_seat']} {p}")
    for ses, i in NO_RING:
        img, calib = frame(ses, i)
        if img is None:
            skip(f"{ses}/{i}", "프레임 없음")
            continue
        st = tr.read(img, calib)
        check(f"녹화 {ses}/{i}: 고리 없음/흐림 → None (추측 안 함)", st["acting_seat"] is None and st["acting_progress"] is None,
              f"{st['acting_seat']} {st['acting_progress']}")
    ses, rng = PROGRESS_RUN
    ps = []
    for i in rng:
        img, calib = frame(ses, i)
        if img is None:
            break
        st = tr.read(img, calib)
        ps.append((st["acting_seat"], st["acting_progress"]))
    if len(ps) == len(rng):
        vals = [p for _, p in ps]
        ok = all(k == "hero" for k, _ in ps) and None not in vals and all(b > a for a, b in zip(vals, vals[1:]))
        slope = (vals[-1] - vals[0]) / (len(vals) - 1) if None not in vals else None
        check("녹화: 같은 차례 연속 프레임 진행도가 늘어남 (~0.1/초, 프레임 ~1.1초)", ok and slope and 0.07 <= slope <= 0.15,
              ps)
    else:
        skip("진행도 연속", "프레임 없음")
    for ses, i, key, want in BUBBLES:
        img, calib = frame(ses, i)
        if img is None:
            skip(f"말풍선 {ses}/{i}", "프레임 없음")
            continue
        st = tr.read(img, calib)
        got = next((s["bubble"] for s in st["seats"] if s["key"] == key), "?")
        check(f"말풍선 {ses}/{i} {key}: {want}", got == want, got)


def sample_screenshot():
    if not SAMPLE.exists():
        skip("샘플 화면", f"{SAMPLE} 없음")
        return
    img = cv2.imread(str(SAMPLE))
    calib = layout.calibrate(img)
    calib.pop("debug", None)
    st = tablestate.TableReader().read(img, calib)
    p = st["acting_progress"]
    check("샘플 화면: 위 왼쪽 자리(TL)가 차례, 진행 0~0.15", st["acting_seat"] == "seat_TL" and p is not None and p <= 0.15,
          f"{st['acting_seat']} {p}")
    bub = {s["key"]: s["bubble"] for s in st["seats"]}
    check("샘플 화면: 올인(불꽃) 자리는 차례 아님 · 말풍선 올인", st["acting_seat"] != "seat_R" and bub.get("seat_R") == "allin",
          bub)


# --- handlog: 차례 기록 · 고리가 지나갔는데 액션을 못 읽음 -----------------------------------------------
class RingTable(test_handlog.Table):
    acting, progress = None, None

    def snap(self):
        s = super().snap()
        s["acting_seat"], s["acting_progress"] = self.acting, self.progress
        return s

    def turn(self, pos, p=0.1, **kw):
        self.acting, self.progress = (self.k(pos) if pos else None), p
        return self.step(**kw)


def tracker_turns():
    tb = RingTable(handlog.Tracker("ring1"))
    tb.step()
    tb.turn("UTG", 0.05)
    tb.turn("UTG", 0.2)
    tb.turn("MP", 0.03, bets={"UTG": 2.5}, bubble={"UTG": "raise"})   # 고리가 넘어가면서 UTG 레이즈가 보임
    tb.turn("CO", 0.04, fold=["MP"], bubble={"MP": "fold"})
    tb.turn("BTN", 0.02)                                              # CO 차례가 끝났는데 아무것도 못 읽음
    tb.turn("BTN", 0.3)
    tb.turn("BTN", 0.4)
    tb.turn(None, None, fold=["CO", "BTN", "SB", "BB"])
    utg, mp, co, btn = (tb.k(p) for p in ("UTG", "MP", "CO", "BTN"))
    rec = test_handlog.finish(tb)
    turns = [(t["seat"], t["street"]) for t in rec["turns"]]
    check("handlog: 차례 기록 (자리 순서)", turns[:4] == [(utg, "preflop"), (mp, "preflop"), (co, "preflop"), (btn, "preflop")],
          rec["turns"])
    first = rec["turns"][0]
    check("handlog: 같은 차례는 한 항목 (t0~t1, 본 최대 진행도)", first["p"] == 0.2 and first["t1"] > first["t0"], first)
    codes = [i for i in rec["issues"] if i["code"] == "ring_no_action"]
    check("handlog: CO 차례가 지나갔는데 액션 못 읽음 → ring_no_action (CO 만)",
          [i["detail"] for i in codes] == [f"{co}@preflop"], rec["issues"])
    check("handlog: ring_no_action 은 참고용 (confidence 유지)", "ring_no_action" not in handlog.FATAL)
    # 고리 정보가 없는 상태(예전 녹화) → turns 비어 있고 issue 없음
    tb = test_handlog.Table(handlog.Tracker("ring2"))
    tb.step()
    tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})
    rec = test_handlog.finish(tb)
    check("handlog: acting_seat 없는 상태 → turns [] · issue 없음",
          rec["turns"] == [] and not any(i["code"] == "ring_no_action" for i in rec["issues"]), rec["turns"])


def main():
    synthetic()
    tracker_turns()
    sample_screenshot()
    recorded()
    print(f"\n{sum(results)}/{len(results)} passed" + (f", {len(skipped)} skipped" if skipped else ""))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
