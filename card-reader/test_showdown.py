"""showdown.py 테스트: 쇼다운 공개 카드 찾기/인식/핸드별 모으기. 실행: python test_showdown.py

합성 이미지·가짜 matcher 테스트는 항상 돈다. 실제 화면 테스트(샘플 스크린샷, 기록 모드 프레임)는 이 PC 의 로컬 파일
(저장소에 없음: 닉네임 포함)을 절대 경로로 읽고, 없으면 건너뛴다.
기록 프레임 정답은 프레임을 눈으로 확인해 적은 것. templates_shown/ 이 이 프레임들에서 뽑은 것이라 여기 인식률은 회귀 확인용
(핸드 하나씩 빼고 만든 템플릿으로 잰 값: 정답을 확인한 공개 좌석 56곳 중 50곳 정확, 틀림 0).
"""
import glob
import json
from pathlib import Path

import cv2
import numpy as np

import showdown

HERE = Path(__file__).resolve().parent
LOCAL = Path(r"C:\Users\soulo\Documents\preflop\card-reader")
SAMPLE = LOCAL / "samples" / "showdown_2026-11-04_97c_J2.png"
RECORDINGS = LOCAL / "dist" / "recordings"
SAMPLE_CALIB = {"geom": {"pill_center": [600.0, 325.0], "pw": 228.0, "size": [1198, 865]},
                "my_stack": [461, 699, 69, 29], "seats": []}

# CASES: (녹화 폴더, 프레임 번호, 좌석, 정답, 지금 읽혀야 함). 읽힌 값은 정답과 같아야 하고, 읽혀야 하는 것이 안 읽히면 실패
# NEG: (녹화 폴더, 프레임, 좌석) — 좌석이 None 이면 그 프레임의 모든 상대 좌석. 아무것도 읽히면 안 됨
#      (풍경/카드 뒷면/보정과 어긋난 화면의 큰 카드, 평범한 핸드 중 프레임)
CASES = [
    ('20261029_021253', 225, 'seat_UR', '9d4c', True),
    ('20261029_021253', 276, 'seat_UR', 'Ts6c', True),
    ('20261029_021253', 277, 'seat_UR', 'Ts6c', True),
    ('20261029_021253', 278, 'seat_UR', 'Ts6c', True),
    ('20261029_021253', 277, 'seat_BR', 'Js7d', True),
    ('20261029_021253', 279, 'seat_BR', 'Js7d', True),
    ('20261029_021253', 282, 'seat_BR', 'Js7d', True),
    ('20261029_021253', 478, 'seat_UR', 'AhTd', True),
    ('20261029_021253', 482, 'seat_UR', 'AhTd', True),
    ('20261029_021253', 486, 'seat_UR', 'AhTd', True),
    ('20261029_021253', 478, 'seat_BR', 'QsQh', True),
    ('20261029_021253', 480, 'seat_BR', 'QsQh', True),
    ('20261029_021253', 482, 'seat_BR', 'QsQh', True),
    ('20261029_021253', 600, 'seat_TC', '3d5h', True),
    ('20261029_021253', 603, 'seat_TC', '3d5h', True),
    ('20261029_021253', 607, 'seat_TC', '3d5h', True),
    ('20261029_021253', 601, 'seat_UR', 'Jd6s', True),
    ('20261029_021253', 604, 'seat_UR', 'Jd6s', True),
    ('20261029_021253', 607, 'seat_UR', 'Jd6s', True),
    ('20261029_021253', 602, 'seat_BR', '2hAh', True),
    ('20261029_021253', 603, 'seat_BR', '2hAh', True),
    ('20261029_021253', 655, 'seat_BL', 'AsAh', True),
    ('20261029_021253', 656, 'seat_BL', 'AsAh', True),
    ('20261029_021253', 657, 'seat_BL', 'AsAh', True),
    ('20261029_021253', 655, 'seat_UL', 'JcKd', True),
    ('20261029_021253', 658, 'seat_UL', 'JcKd', True),
    ('20261029_021253', 661, 'seat_UL', 'JcKd', True),
    ('20261029_021253', 661, 'seat_UR', '8c4d', True),
    ('20261029_233140', 176, 'seat_L', 'KcTc', True),
    ('20261029_233140', 177, 'seat_L', 'KcTc', False),
    ('20261029_233140', 179, 'seat_L', 'KcTc', False),
    ('20261029_233140', 176, 'seat_UL', 'Kh7s', False),
    ('20261029_233140', 180, 'seat_UL', 'Kh7s', True),
    ('20261029_233140', 184, 'seat_UL', 'Kh7s', True),
    ('20261029_233140', 248, 'seat_UL', 'As9h', True),
    ('20261029_233140', 250, 'seat_UL', 'As9h', True),
    ('20261029_233140', 252, 'seat_UL', 'As9h', True),
    ('20261029_233140', 248, 'seat_R', 'Qd5d', True),
    ('20261029_233140', 251, 'seat_R', 'Qd5d', True),
    ('20261029_233140', 254, 'seat_R', 'Qd5d', True),
    ('20261029_233140', 314, 'seat_UL', 'Ad5d', True),
    ('20261029_233140', 317, 'seat_UL', 'Ad5d', True),
    ('20261029_233140', 320, 'seat_UL', 'Ad5d', True),
    ('20261029_233140', 316, 'seat_BR', 'JsJd', True),
    ('20261029_233140', 318, 'seat_UR', '7cKc', True),
    ('20261029_233140', 319, 'seat_UR', '7cKc', True),
    ('20261029_233140', 320, 'seat_UR', '7cKc', True),
    ('20261029_233140', 373, 'seat_TR', 'Jh9h', True),
    ('20261029_233140', 374, 'seat_TR', 'Jh9h', True),
    ('20261029_233140', 375, 'seat_TR', 'Jh9h', True),
    ('20261029_233140', 374, 'seat_L', '3sKd', True),
    ('20261029_233140', 376, 'seat_L', '3sKd', True),
    ('20261029_233140', 379, 'seat_L', '3sKd', True),
    ('20261029_233140', 431, 'seat_UL', '5d4s', True),
    ('20261029_233140', 434, 'seat_UL', '5d4s', False),
    ('20261029_233140', 437, 'seat_UL', '5d4s', False),
    ('20261029_233140', 488, 'seat_UL', 'JcAh', True),
    ('20261029_233140', 491, 'seat_UL', 'JcAh', True),
    ('20261029_233140', 494, 'seat_UL', 'JcAh', True),
    ('20261029_233140', 488, 'seat_BR', 'Ad2h', True),
    ('20261029_233140', 489, 'seat_BR', 'Ad2h', True),
    ('20261029_233140', 490, 'seat_BR', 'Ad2h', True),
    ('20261029_233140', 560, 'seat_BR', 'ThAs', True),
    ('20261029_233140', 563, 'seat_BR', 'ThAs', True),
    ('20261029_233140', 566, 'seat_BR', 'ThAs', True),
    ('20261029_233140', 562, 'seat_L', '9s8s', True),
    ('20261029_233140', 563, 'seat_L', '9s8s', True),
    ('20261029_233140', 602, 'seat_UR', 'AsJs', True),
    ('20261029_233140', 606, 'seat_UR', 'AsJs', True),
    ('20261029_233140', 610, 'seat_UR', 'AsJs', True),
    ('20261029_233140', 602, 'seat_R', 'ThJc', True),
    ('20261029_233140', 606, 'seat_R', 'ThJc', True),
    ('20261029_233140', 610, 'seat_R', 'ThJc', True),
    ('20261029_233140', 602, 'seat_BR', '7c6h', True),
    ('20261029_233140', 603, 'seat_BR', '7c6h', True),
    ('20261029_233140', 605, 'seat_BR', '7c6h', True),
    ('20261029_233140', 643, 'seat_UR', '2c2h', True),
    ('20261029_233140', 644, 'seat_UR', '2c2h', True),
    ('20261029_233140', 646, 'seat_UR', '2c2h', True),
    ('20261029_233140', 643, 'seat_BR', '5s9s', True),
    ('20261029_233140', 646, 'seat_BR', '5s9s', True),
    ('20261029_233140', 650, 'seat_BR', '5s9s', True),
    ('20261029_233140', 679, 'seat_UL', '4sQs', True),
    ('20261029_233140', 682, 'seat_UL', '4sQs', True),
    ('20261029_233140', 686, 'seat_UL', '4sQs', True),
    ('20261029_233140', 679, 'seat_TL', 'KcAh', True),
    ('20261029_233140', 680, 'seat_TL', 'KcAh', True),
    ('20261029_233140', 682, 'seat_TL', 'KcAh', True),
    ('20261029_233140', 722, 'seat_R', '7h5s', True),
    ('20261029_233140', 725, 'seat_R', '7h5s', True),
    ('20261029_233140', 728, 'seat_R', '7h5s', True),
    ('20261029_233140', 722, 'seat_BR', 'QdJc', True),
    ('20261029_233140', 723, 'seat_BR', 'QdJc', True),
    ('20261029_233140', 724, 'seat_BR', 'QdJc', True),
    ('20261029_233140', 752, 'seat_L', 'Ac3c', True),
    ('20261029_233140', 755, 'seat_L', 'Ac3c', True),
    ('20261029_233140', 759, 'seat_L', 'Ac3c', True),
    ('20261029_233140', 797, 'seat_R', 'Jc8c', True),
    ('20261029_233140', 800, 'seat_R', 'Jc8c', True),
    ('20261029_233140', 803, 'seat_R', 'Jc8c', True),
    ('20261029_233140', 798, 'seat_TR', 'Ah8h', True),
    ('20261029_233140', 800, 'seat_TR', 'Ah8h', True),
    ('20261029_233140', 803, 'seat_TR', 'Ah8h', True),
    ('20261030_013933', 12, 'seat_TC', 'Ad6d', True),
    ('20261030_013933', 13, 'seat_TC', 'Ad6d', True),
    ('20261030_013933', 14, 'seat_TC', 'Ad6d', True),
    ('20261030_013933', 13, 'seat_BL', 'As4d', True),
    ('20261030_013933', 15, 'seat_BL', 'As4d', True),
    ('20261030_013933', 18, 'seat_BL', 'As4d', True),
    ('20261030_014157', 1, 'seat_BL', 'JdQs', True),
    ('20261030_014157', 1, 'seat_UR', '4h3h', True),
    ('20261030_014157', 3, 'seat_UR', '4h3h', True),
    ('20261030_014157', 5, 'seat_UR', '4h3h', True),
    ('20261030_014157', 128, 'seat_BL', '6sJs', True),
    ('20261030_014157', 131, 'seat_BL', '6sJs', True),
    ('20261030_014157', 135, 'seat_BL', '6sJs', True),
    ('20261030_014157', 128, 'seat_TC', '9hQd', True),
    ('20261030_014157', 133, 'seat_TC', '9hQd', False),
    ('20261030_014157', 134, 'seat_TC', '9hQd', False),
    ('20261030_014157', 135, 'seat_TC', '9hQd', False),
    ('20261030_014157', 177, 'seat_BL', '4cAh', True),
    ('20261030_014157', 180, 'seat_BL', '4cAh', True),
    ('20261030_014157', 184, 'seat_BL', '4cAh', True),
    ('20261030_014157', 177, 'seat_TC', 'KsKd', True),
    ('20261030_014157', 178, 'seat_TC', 'KsKd', True),
    ('20261030_014157', 180, 'seat_TC', 'KsKd', True),
    ('20261030_014157', 251, 'seat_UL', 'AcJd', True),
    ('20261030_014157', 253, 'seat_UL', 'AcJd', True),
    ('20261030_014157', 256, 'seat_UL', 'AcJd', True),
    ('20261030_014157', 251, 'seat_BR', 'Ks5c', True),
    ('20261030_014157', 349, 'seat_BL', '2sJh', True),
    ('20261030_014157', 350, 'seat_BL', '2sJh', True),
    ('20261030_014157', 352, 'seat_BL', '2sJh', True),
    ('20261030_014157', 433, 'seat_UL', '8sAc', True),
    ('20261030_014157', 434, 'seat_UL', '8sAc', True),
    ('20261030_014157', 642, 'seat_BL', 'Ad9s', True),
    ('20261030_014157', 644, 'seat_BL', 'Ad9s', True),
    ('20261030_014157', 647, 'seat_BL', 'Ad9s', True),
    ('20261030_014157', 718, 'seat_BL', 'Kd3h', True),
    ('20261030_014157', 941, 'seat_BL', 'Ts8c', True),
    ('20261030_014157', 944, 'seat_BL', 'Ts8c', True),
    ('20261030_014157', 947, 'seat_BL', 'Ts8c', True),
    ('20261030_014157', 942, 'seat_UL', '5s3s', True),
    ('20261030_014157', 944, 'seat_UL', '5s3s', True),
    ('20261030_014157', 947, 'seat_UL', '5s3s', True),
    ('20261029_233140', 561, 'seat_UL', 'QdKd', True),
    ('20261030_014157', 643, 'seat_TC', '5c5d', True),
]
NEG = [
    ('20261029_233140', 386, 'seat_TR'),
    ('20261029_233140', 2240, 'seat_TR'),
    ('20261029_233140', 2252, 'seat_TL'),
    ('20261029_233140', 3466, 'seat_BL'),
    ('20261029_233140', 2414, 'seat_R'),
    ('20261029_233140', 3204, 'seat_R'),
    ('20261029_233140', 3599, 'seat_R'),
    ('20261029_233140', 3661, 'seat_R'),
    ('20261029_233140', 4415, 'seat_R'),
    ('20261029_233140', 4564, 'seat_R'),
    ('20261029_233140', 5228, 'seat_R'),
    ('20261029_233140', 5862, 'seat_R'),
    ('20261029_233140', 6013, 'seat_R'),
    ('20261029_233140', 7908, 'seat_R'),
    ('20261029_233140', 7926, 'seat_R'),
    ('20261029_233140', 8118, 'seat_R'),
    ('20261029_233140', 100, None),
    ('20261029_233140', 1000, None),
    ('20261029_233140', 2500, None),
    ('20261029_233140', 5000, None),
    ('20261029_233140', 8000, None),
    ('20261030_014157', 60, None),
    ('20261030_014157', 500, None),
    ('20261029_021253', 100, None),
]

_MATCH = None


def _skip(msg):
    print("  SKIP:", msg)
    return "skip"


def shown_match():
    global _MATCH
    if _MATCH is None:
        import reader
        folder = HERE / showdown.SHOWN_DIR
        _MATCH = showdown.ShownMatch(reader.Matcher(dirs=(reader.TEMPLATE_DIR, folder)), 0.95,
                                     showdown.shown_labels(folder))
    return _MATCH


def _frame(rec, n):
    files = glob.glob(str(RECORDINGS / rec / f"{n:06d}_*.jpg"))
    return cv2.imread(files[0]) if files else None


_CALIBS = {}


def _calib(rec):
    if rec not in _CALIBS:
        _CALIBS[rec] = json.loads((RECORDINGS / rec / "calib.json").read_text(encoding="utf-8"))
    return _CALIBS[rec]


# --- 합성 / 가짜 ------------------------------------------------------------------------------------------
def _synthetic(gap=2, width=1.0):
    """어두운 바탕에 SHOWN_BOX 자리 카드 2장 (흰 면 + 검은 글자) → (이미지, 아바타, pw)."""
    pw = 228.0
    img = np.full((400, 600, 3), 40, np.uint8)
    ax, ay = 300.0, 200.0
    bx, by, bw, bh = showdown.SHOWN_BOX
    x0, y0 = int(ax + bx * pw), int(ay + by * pw)
    w, h = int(bw * pw * width), int(bh * pw)
    half = int(bw * pw) // 2
    spans = ((x0, x0 + half - gap // 2), (x0 + half + (gap - gap // 2), x0 + w)) if width == 1.0 else ((x0, x0 + w),)
    for cx0, cx1 in spans:
        cv2.rectangle(img, (cx0, y0), (cx1 - 1, y0 + h - 1), (240, 240, 240), -1)
        cv2.putText(img, "9", (cx0 + 8, y0 + h - 12), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (20, 20, 20), 3)
    return img, (ax, ay), pw


def test_locate_synthetic():
    img, av, pw = _synthetic()
    r = showdown.locate(img, av, pw)
    assert r is not None and len(r) == 2, r
    (lx, ly, lw, lh), (rx, ry, rw, rh) = r
    assert abs(lh - showdown.SHOWN_BOX[3] * pw) <= 2 and ly == ry and lx < rx and abs(lw - rw) <= 4, r
    img, av, pw = _synthetic(gap=12)                    # 이긴 쪽 노란 테두리처럼 두 장 사이가 벌어짐
    assert showdown.locate(img, av, pw) is not None
    img, av, pw = _synthetic(width=0.5)                  # 한 장 크기 덩어리만 → 카드 2장 아님
    assert showdown.locate(img, av, pw) is None
    assert showdown.locate(np.full((400, 600, 3), 40, np.uint8), av, pw) is None
    assert showdown.locate(np.full((400, 600, 3), 235, np.uint8), av, pw) is None   # 전부 흰색: 경계 없음
    assert showdown.locate(img, (5.0, 5.0), pw) is None                              # 창 밖


class _FakeMatcher:
    def __init__(self, label, suit):
        self.label = label
        self.glyphs = type("G", (), {"suit": staticmethod(lambda crop: suit)})()

    def match(self, crop, threshold):
        return self.label, 0.97


def test_shown_match_twin_guard():
    crop = np.zeros((70, 50, 3), np.uint8)
    # 10♠ 으로 읽혔는데 10♣ 공개 카드 템플릿이 없음: 무늬 판별이 ♠ 이라고 확신해야 인정
    assert showdown.ShownMatch(_FakeMatcher("Ts", None), 0.95, {"Ts"})(crop)[0] == "?"
    assert showdown.ShownMatch(_FakeMatcher("Ts", "c"), 0.95, {"Ts"})(crop)[0] == "?"
    assert showdown.ShownMatch(_FakeMatcher("Ts", "s"), 0.95, {"Ts"})(crop)[0] == "Ts"
    assert showdown.ShownMatch(_FakeMatcher("Ts", None), 0.95, {"Ts", "Tc"})(crop)[0] == "Ts"
    assert showdown.ShownMatch(_FakeMatcher("?", None), 0.95, set())(crop)[0] == "?"


def test_read_shown_filters():
    calib = SAMPLE_CALIB
    av, _ = showdown.seat_avatars(calib)
    reads = {}
    orig = showdown.read_seat
    showdown.read_seat = lambda img, a, pw, match: reads.get(a)
    try:
        reads = {av["seat_TC"]: ["9c", "7c"], av["seat_BR"]: ["Jd", "2h"], av["seat_UL"]: ["Jd", "Ah"],
                 av["hero"]: ["As", "Ks"]}
        got = showdown.read_shown(None, calib, ["seat_TC", "seat_BR", "seat_UL", "hero"], None, exclude=["7c", "back"])
        # TC: 보드와 겹침, BR/UL: 같은 카드(Jd)를 서로 주장 → 둘 다 버림, hero: 안 봄
        assert got == {}, got
        got = showdown.read_shown(None, calib, ["seat_TC", "seat_BR"], None, exclude=["8c", "4d"])
        assert got == {"seat_TC": ["9c", "7c"], "seat_BR": ["Jd", "2h"]}, got
    finally:
        showdown.read_seat = orig


def test_watcher_policy():
    script, calls = [], []
    orig = showdown.read_shown

    def fake(img, calib, keys, match, exclude=()):
        calls.append(list(keys))
        out = script.pop(0) if script else {}
        if isinstance(out, Exception):
            raise out
        return {k: v for k, v in out.items() if k in keys}
    showdown.read_shown = fake
    try:
        w = showdown.Watcher()
        script[:] = [{"seat_UL": ["As", "9h"]}]
        assert w.update("h1", None, {}, ["seat_UL", "seat_R", "hero"], None) == {"seat_UL": ["As", "9h"]}
        assert calls[-1] == ["seat_UL", "seat_R"]                      # hero 는 안 봄
        script[:] = [{"seat_UL": ["As", "9h"], "seat_R": ["Qd", "5d"]}]
        assert w.update("h1", None, {}, ["seat_UL", "seat_R"], None) == {"seat_UL": ["As", "9h"], "seat_R": ["Qd", "5d"]}
        script[:] = [{"seat_R": ["Qd", "5c"]}]                          # UL 확정 → 더 안 읽음, R 은 다른 값 → 버림
        assert w.update("h1", None, {}, ["seat_UL", "seat_R"], None) == {"seat_UL": ["As", "9h"], "seat_R": None}
        assert calls[-1] == ["seat_R"]
        n = len(calls)
        assert w.update("h1", None, {}, ["seat_UL", "seat_R"], None) == {"seat_UL": ["As", "9h"], "seat_R": None}
        assert len(calls) == n, "모든 좌석이 확정/어긋남이면 그 핸드에서는 더 읽지 않음"
        script[:] = [RuntimeError("boom")]                               # 새 핸드: 처음부터, 예외는 삼킴
        assert w.update("h2", None, {}, ["seat_UL"], None) == {} and w.errors == 1
    finally:
        showdown.read_shown = orig


def test_templates_shown():
    folder = HERE / showdown.SHOWN_DIR
    files = sorted(folder.glob("*.png"))
    assert len(files) >= 100, len(files)
    labels = showdown.shown_labels(folder)
    assert len(labels) >= 45 and all(showdown._is_card(c) for c in labels), labels
    for f in files:
        im = cv2.imread(str(f))
        assert im is not None and im.shape[:2] == (72, 52), (f, None if im is None else im.shape)


# --- 실제 화면 (로컬 파일, 없으면 건너뜀) ----------------------------------------------------------------
def test_sample_screenshot():
    if not SAMPLE.exists():
        return _skip(f"{SAMPLE} 없음")
    img = cv2.imread(str(SAMPLE))
    keys = [k for k in showdown.seat_avatars(SAMPLE_CALIB)[0] if k != "hero"]
    board = ["8c", "4d", "Jh", "3c", "7s"]
    got = showdown.read_shown(img, SAMPLE_CALIB, keys, shown_match(), exclude=board)
    assert got == {"seat_TC": ["9c", "7c"], "seat_BR": ["Jd", "2h"]}, got
    w = showdown.Watcher()
    for _ in range(3):
        res = w.update("h", img, SAMPLE_CALIB, keys, shown_match(), exclude=board)
    assert res == got, res


def test_recorded_frames():
    if not RECORDINGS.is_dir():
        return _skip(f"{RECORDINGS} 없음")
    ok = wrong = missing = 0
    for rec, n, seat, truth, readable in CASES:
        img = _frame(rec, n) if (RECORDINGS / rec).is_dir() else None
        if img is None:
            missing += 1
            continue
        av, pw = showdown.seat_avatars(_calib(rec))
        got = showdown.read_seat(img, av[seat], pw, shown_match())
        got = "".join(got) if got else None
        if got is not None and got != truth:
            wrong += 1
            print("  WRONG", rec, n, seat, truth, got)
        elif readable and got is None:
            wrong += 1
            print("  NOT READ (was readable)", rec, n, seat, truth)
        else:
            ok += got == truth
    print(f"  recorded frames: {ok} read correctly, {len(CASES) - ok - wrong - missing} not read (allowed), "
          f"{wrong} wrong/regressed, {missing} missing")
    if missing == len(CASES):
        return _skip("녹화 프레임 없음")
    assert wrong == 0


def test_recorded_negatives():
    if not RECORDINGS.is_dir():
        return _skip(f"{RECORDINGS} 없음")
    for rec, n, seat in NEG:
        img = _frame(rec, n) if (RECORDINGS / rec).is_dir() else None
        if img is None:
            continue
        calib = _calib(rec)
        keys = [seat] if seat else [k for k in showdown.seat_avatars(calib)[0] if k != "hero"]
        got = showdown.read_shown(img, calib, keys, shown_match())
        assert got == {}, (rec, n, got)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = skipped = 0
    for fn in tests:
        try:
            if fn() == "skip":
                skipped += 1
                print("SKIP", fn.__name__)
            else:
                print("PASS", fn.__name__)
        except AssertionError as exc:
            failed += 1
            print("FAIL", fn.__name__, exc)
    print(f"{len(tests) - failed - skipped}/{len(tests)} passed, {skipped} skipped")
    raise SystemExit(1 if failed else 0)
