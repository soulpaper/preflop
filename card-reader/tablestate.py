"""테이블 상태 읽기: 인원, 핸드에 남은 사람, 딜러, 내 포지션, 바닥 벳, 내 차례/콜 금액, 프리플랍 상황.

    calib = layout.calibrate(img0)          # 한 번 ([자동 지정])
    state = tablestate.read(img, calib)     # 매 프레임 (~1초), 같은 게임 창 크기의 새 캡처

매번 픽셀만 보는 것 (빠름)
  좌석에 사람 있음  스택 줄에 글자가 있음
  폴드              아바타가 흑백 (채도 낮음). 폴드한 사람도 카드 아이콘은 남아 있어서 아이콘으로는 못 가림
  카드 받음          아바타 옆 작은 카드 뒷면 아이콘 (템플릿). 컬러인데 아이콘이 없으면 이번 핸드에 안 낀 사람 (자리 비움/대기)
  나               내 카드가 뒷면이거나 내 아바타가 흑백이면 폴드
  딜러/블라인드      펠트 위 흰 원판 (D: 검은 글자, SB: 파란 글자, BB: 빨간 글자) → 가장 가까운 좌석의 벳 자리
  액션 바           폴드/콜/레이즈 버튼. 버튼 왼쪽에 작은 램프 칸이 있으면 '예약' 버튼 (내 차례 아님)
  말풍선            콜(파랑)/레이즈(주황)/올인(빨강)/폴드(밝은 회색)/자리 비움(커피잔)/체크(초록, 추정) → seats[].bubble
                    'call'/'raise'/'allin'/'fold'/'away'/'check'/None. handlog.Tracker 가 액션 증거로 씀
  좌석              보정 결과에 geom 이 있으면 모든 고정 자리(layout.slot_seats: 9인+6인 합집합 9자리 + 나)를 매 프레임
                    픽셀로 확인 → 보정 뒤에 앉거나 떠난 사람도 반영. 키는 자리 이름 ('seat_BL'.. / 'hero')이라 바뀌지 않음.
                    geom 이 없는 옛 보정이면 보정 때 좌석만 확인하고 notes 에 '다시 자동 지정 필요'
OCR (바뀐 것만, stable.Signature 로 캐시)
  바닥 벳 숫자, 콜 금액, 내 스택, (필요할 때) 상대 스택, 숫자 3글자 이하 스택(0BB = 올인 확인)

street 는 항상 None (앱이 보드 카드 장수로 안다). 프리플랍이 아닐 때 preflop 결과는 무시할 것.
"""
import base64
import math
import time
import zlib
from collections import Counter

import cv2
import numpy as np

import layout
import ocr
import preflop as pf
import stable

# --- 기준값 (samples/ 7장으로 잰 값) ---------------------------------------------------------------
COLOR_SAT = 40          # 이 채도보다 큰 픽셀 = 색 있음
COLOR_MIN_FRAC = 0.3    # 아바타 가운데 원에서 색 있는 픽셀 비율이 이 미만이면 흑백 (실측 컬러 ≥0.96, 흑백 ≤0.03)
AVATAR_CORE = 0.6       # 아바타 반지름의 이 비율 안쪽만 봄 (빛 고리/말풍선 제외)
ICON_MIN = 0.42         # 카드 아이콘 템플릿 점수 (실측 있음 ≥0.56 — 반짝이 겹친 경우, 없음 ≤0.23)
ICON_SEARCH = 0.07      # 아이콘 예상 위치에서 ±이만큼(pw) 찾음
STACK_TEXT_MIN = 0.03   # 스택 칸에서 글자 픽셀 비율이 이 이상이면 사람 있음
BET_MIN_W = 0.09        # 벳 숫자 최소 폭 (pw). '1BB' ≈ 0.114
DISC_SIZE = (0.06, 0.13)  # 딜러/블라인드 원판 지름 (pw), 실측 0.088
BUTTON_STD = 12         # 버튼 영역 밝기 표준편차가 이보다 크면 버튼 있음 (실측 있음 ≥24, 없음 ≤5)
LAMP_STEP = 9           # 예약 버튼 램프 칸: 버튼 바탕과의 밝기 차 (실측 꺼짐 13, 켜짐 180)
SAME_BET = 0.05         # 이 차이 안의 벳은 같은 금액
AVATAR_STD = 40         # 아바타 가운데 원 밝기 표준편차가 이보다 크면 사람 있음 (실측 사람 ≥48, 빈 자리 ≤30)
LEFT_SLOTS = {"BL", "L", "UL", "TL"}  # 테이블 왼쪽 좌석: 카드 아이콘이 아바타 오른쪽, 말풍선은 왼쪽

# 카드 뒷면 아이콘 (그레이 36x30, pw=228 화면에서 잘라 냄)
_ICON_B64 = (
    "eNo1k79vHEUUx/+B5Hznndk7zg7EZxMBgiJEoqBEtHRQIEGHKOIoWAZMnPPd/rjdmdmdnV9vZ9cRUSRoU5CGlgIUaEKiBJAiULCM"
    "AAn/PJ9NJArHZm0rr/3ovXnzvt/v+c75D29cmmlhp+mOt9pN10FNjDBGjoMc/NL0e5/feLnVaCM8NnPxh/WDw8O/2sewqjOvvP3W"
    "B/r3J/uH+7sPvswXtS+y+84JrHrfSdaWB5BT1U0FREJrrcSFk06EJp4ZLveszs0gMUAgD3S+oJIjUhVuunc8PyaLxeXrQTCI/AJs"
    "LO48pbjpK0kBiLKQRguCZkx7Gye02q31upZKcqb1+uF/B//ctXPz8Z9PKZ5s3WZdtUTNpw+s8aAnv3j4+Mnh/sEj/Ybr4snJrbL0"
    "Az74WEOhNGOWZl0bgg6uDwFPTmyoSBC/vFlcBR2VSWwh9pW11lNDfKa9LU3kw+CeYqUCb+D1Qz2b9z3CrvyInz27w8tAErkeFDSl"
    "yk+0NCrSprpMjKdm/pVc5BBullzHgouuZsamQupEv4anOo/yWZkFwR70QTClIwZgqc+95HsHT539Lkslh3DHqJDzfqIvlzTgYU/D"
    "LsKdqfd7eZip2S0WJIuyKMq0lLAUaCb26qgzc+6mglx1/9ZcyXgZQvBJZHwvtxsIdZ6fWMmomL/6tUmkjqQ2qRcaKrvLcs9BnXPP"
    "bdIyDkiPB7Qv+noQpANiwTIYOWh6enqog776lnlaVHJVWgU9Tkh5Sa9i9OKkO+RhkK5kRQRwFaJBCn1yrSw4+aaJZprOMKMh32UU"
    "CtDdqE9jsyAVs96748jF7Q1gvtqSS5U6guWZKiwzJodbEwi5Lfee6MvBTgF9anKVsjQi9hPL6R/jDnKRG36Ux/M7QlyRNKLGh2tQ"
    "bQ1yVK/s4eBXpdDeZm5zX6ZpBotgWBEGetSq/INR7VcCn92PzZFW1qTUgKYDEj123Coftfpvo81bNs9VzhVLA9E1gQ4TvjferIxX"
    "r9VxzZuTMauOZfOC+xxMQIu1Bj5699Tpuns38xTxifGtITbupsWS/AXVEGo0xhzsrNokFqXyMlPopZgwYuYIRujE1yhaK1gWZ/NF"
    "L4lEpZ7U8gKqwnyEW41m/U318/bucHf001eSc2nJw3HHaRwnsT2Bxqrst53TTr32wsXbq6Pt4Qp2nNYxxS7GbjXDbWK3+ka7NV4/"
    "hVHdxf8D2MPauA=="
)
_ICON_SHAPE, _ICON_PW = (36, 30), 228.0
# 딜러 'D' 원판 (그레이 23x24, 같은 화면). 창마다 원판 크기가 pw 대비 0~10% 달라서 여러 배율로 찾음
_D_B64 = (
    "eNodkF9IE3EAx28tpB7CGZuzNu/c/7vdrd39/tzu1lxrmrNMGykLFv0hV4nsQVzENKb0EhFU0KsUhFBEReFDUIFiBGsQ+GItehvZ"
    "w2g0ySg1736tvo8fPg8fvhDLCMAgUBPxaCx2QJUgwKhBAEQI4dgxmRekIM8Fe1QYwv+oHELqkXDn+ZuP5l7MXkuLoEtVZNSwZaW/"
    "MzMzX3zzam7hc+XpKTGiKhhiFDqhXH/SN55SXHZGLGiVK76uhq/Ih9FY/f3QcC4pCfROKqHXzjERBLF6UKwQfbUndalYfna0yXhr"
    "vewEEIqK67ZOSFWU8j/I5mszFa9qeRuGEuKWiUZq7c3puk5WYhS3RN62AyBwcF3X9Kq56fjq1sZav4Ge11d4Pwj4hK+artdM1ECd"
    "kG9Ro/Bua4nlAALWh7pGflkMg2tk47HN2FvV7rQEGtfQSo2Qcgsl338wLRh3zGhfOCYAlai0a/JP0UMZtjXvNm03FTb1rCWoQpAQ"
    "Bcu9BYcIJYGPnH6u/bxhBsJ+iMIpj9txebH88UN5+dPv76VRu1dIBCD0XkwBxqqM310slV7OXpBaGXFg0A2hxzOdUV20rc3hdNPm"
    "NjsbTg9zfhhCDvdkfgjQDpZlXbQzfHIk08EIEMv7bOyZwkQ21Rs/1JcczY4laUcjH2PsdzKhs7mpiatT+dxIkm918RKCAAAU8NF2"
    "Woh0d6uczc54eQn8H4YBt6fDvte6h3Z7nU5WQg34F4pMxXg="
)
_D_SHAPE = (23, 24)
D_SCALES = (1.0, 1.05, 1.1)
D_MIN = 0.72            # 실측 진짜 ≥0.96 (배율 맞을 때), 가짜 ≤0.61
_tpl_cache = {}


def _template(b64, shape, pw, scale=1.0):
    key = (b64[:16], round(pw), scale)
    if key not in _tpl_cache:
        base = np.frombuffer(zlib.decompress(base64.b64decode(b64)), np.uint8).reshape(shape)
        s = pw / _ICON_PW * scale
        _tpl_cache[key] = cv2.resize(base, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    return _tpl_cache[key]


def _icon(pw):
    return _template(_ICON_B64, _ICON_SHAPE, pw)


# --- 작은 도구 -------------------------------------------------------------------------------
def _crop(a, rect, margin=0):
    x, y, w, h = [int(v) for v in rect]
    H, W = a.shape[:2]
    x0, y0, x1, y1 = max(0, x - margin), max(0, y - margin), min(W, x + w + margin), min(H, y + h + margin)
    if x1 <= x0 or y1 <= y0:
        return a[0:0, 0:0]
    return a[y0:y1, x0:x1]


def _inside(rect, shape):
    x, y, w, h = rect
    return x >= 0 and y >= 0 and x + w <= shape[1] and y + h <= shape[0] and w > 2 and h > 2


def _text_box(img, rect, pw, min_w=0.03, h_range=(0.03, 0.09), min_glyphs=1):
    """rect 안 밝은 글자 덩어리 → ((x0, y0, x1, y1) 이미지 좌표, 글자 수). 가운데에 가장 가까운 글자 줄/묶음.
    없으면 None. 구리색 테이블 테두리(따뜻한 색)는 글자에서 뺌 (ocr.WARM 과 같은 기준)."""
    win = _crop(img, rect)
    if win.size == 0:
        return None
    gray = cv2.cvtColor(win, cv2.COLOR_BGR2GRAY)
    mask = layout.text_mask(gray) & ~(win[:, :, 2].astype(np.int16) - win[:, :, 0].astype(np.int16) > ocr.WARM)
    counts = mask.sum(1)
    if counts.max() == 0:
        return None
    rows = counts >= max(2, 0.15 * counts.max())  # 가는 선(줄마다 1~3px)은 무시
    runs = layout._runs(rows, gap=1)
    runs = [r for r in runs if h_range[0] * pw <= r[1] - r[0] <= h_range[1] * pw]
    if not runs:
        return None
    cy = win.shape[0] / 2
    ry0, ry1 = min(runs, key=lambda r: abs((r[0] + r[1]) / 2 - cy))
    th = ry1 - ry0
    cols = mask[ry0:ry1].sum(0) >= 1
    groups = layout._runs(cols, gap=max(2, int(0.7 * th)))
    if not groups:
        return None
    cx = win.shape[1] / 2
    gx0, gx1 = min(groups, key=lambda g: 0 if g[0] <= cx < g[1] else min(abs(g[0] - cx), abs(g[1] - cx)))
    glyphs = len(layout._runs(cols[gx0:gx1]))
    if gx1 - gx0 < min_w * pw or glyphs < min_glyphs:
        return None
    x, y = int(rect[0]), int(rect[1])
    return (x + gx0, y + ry0, x + gx1, y + ry1), glyphs


def _ocr_rect(box, shape):
    return layout.number_rect(box, shape)


def _resolve(texts, unit, glyphs=None):
    """ocr.resolve 와 같은 다수결 + 화면 글자 수 확인: 'xBB' 형태로 썼을 때 글자 수가 맞는 값을 우선.
    예: '4BB' 를 '48B' 로 읽는 일이 잦음 → 48 은 '48BB' 4글자라서 3글자 화면과 안 맞음."""
    votes, exact = Counter(), set()
    for text in texts:
        c = ocr.candidates(text)
        if c is None:
            continue
        value, u, is_exact = c
        if not is_exact:
            number, tail, guess = value
            u = unit or guess
            value = int(number) * 10000 + int(tail or 0) if u == "만" else number
        votes[(value, u)] += 2 if is_exact else 1
        if is_exact:
            exact.add((value, u))
    if not votes:
        return None
    pool = list(votes)
    if glyphs and unit == "BB":
        fit = [k for k in pool if k[1] in ("BB", None) and len(f"{k[0]:g}BB") == glyphs]
        pool = fit or pool
    return max(pool, key=lambda k: (votes[k], k in exact))


def _disc_mask(shape, cx, cy, r):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r


# --- 본체 --------------------------------------------------------------------------------------
class TableReader:
    """read() 사이에 OCR 결과를 기억 (영역 서명이 TOL 이상 바뀐 것만 다시 읽음). 스레드 하나에서 쓸 것."""

    def __init__(self, tol=stable.TOL):
        self.tol = tol
        self.cache = {}          # key -> (Signature, value)
        self.last_ocr = 0        # 직전 read() 에서 실제로 OCR 한 영역 수
        self.last_slow = 0       # 그중 여러 장 다수결까지 간 수
        self.dealer_at = None    # 직전 'D' 원판 (x, y, 배율) — 먼저 그 자리만 확인
        self._slots = None       # (보정 키, layout.slot_seats 결과)

    # OCR: {key: (여백 붙은 crop, 글자 수)} 중 캐시와 다른 것만 읽어서 {key: (값, 단위) 또는 None}
    def _ocr(self, crops, unit):
        out, todo, sigs = {}, {}, {}
        for key, (img, _) in crops.items():
            sig = stable.Signature(img)
            hit = self.cache.get(key)
            if hit is not None and stable.distance(hit[0], sig) <= self.tol:
                out[key] = hit[1]
            else:
                todo[key], sigs[key] = img, sig
        self.last_ocr += len(todo)
        if todo:
            # 1차: 여백 없이 한 장 (전처리 3가지). 셋이 같은 값으로 정확히 읽히고 글자 수도 맞으면 채택
            m = ocr.JITTER_MARGIN
            fast = ocr.read_texts({k: im[m:im.shape[0] - m, m:im.shape[1] - m] for k, im in todo.items()})
            slow = {}
            for key, texts in fast.items():
                cs = [ocr.candidates(t) for t in texts]
                vals = {(c[0], c[1]) for c in cs if c is not None and c[2]}
                glyphs = crops[key][1]
                ok = all(c is not None and c[2] for c in cs) and len(vals) == 1
                if ok and unit == "BB" and glyphs:
                    v, u = next(iter(vals))
                    ok = u in ("BB", None) and len(f"{v:g}BB") == glyphs
                if ok:
                    value = next(iter(vals))
                    self.cache[key] = (sigs[key], value)
                    out[key] = value
                else:
                    slow[key] = todo[key]
            # 2차: 몇 px씩 다르게 자른 여러 장 다수결 (작은 글씨에서 소수점/빈 문자열 문제 보완)
            if slow:
                for key, t in ocr.read_jittered(slow).items():
                    value = _resolve(t, unit, crops[key][1])
                    self.cache[key] = (sigs[key], value)
                    out[key] = value
            self.last_slow += len(slow)
        return out

    @staticmethod
    def _crop_for_ocr(img, box):
        (x0, y0, x1, y1), glyphs = box
        return _crop(img, _ocr_rect((x0, y0, x1, y1), img.shape), ocr.JITTER_MARGIN), glyphs

    def read(self, img, calib):
        t0 = time.perf_counter()
        self.last_ocr = self.last_slow = 0
        notes = []
        anchors = calib if calib.get("seats") else layout.anchors(calib)
        seats_cfg, bar = anchors.get("seats") or [], anchors.get("action_bar") or {}
        dynamic = self._slot_cfg(calib)
        if dynamic is not None:
            seats_cfg = dynamic  # 모든 고정 자리를 매 프레임 확인 (보정 뒤에 앉은/떠난 사람 반영)
        elif seats_cfg:
            notes.append("다시 자동 지정 필요: 고정 좌석을 알 수 없어 보정 때 좌석만 확인" if not calib.get("geom")
                         else "고정 좌석 배치가 아님: 보정 때 좌석만 확인")
        geom = calib.get("geom") or {}
        pw = float(geom.get("pw") or (calib["pill"][2] if "pill" in calib else 228.0))
        unit = calib.get("unit") or "BB"
        bb_chips = (calib.get("blinds") or {}).get("bb")
        H, W = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def to_bb(v):
            if v is None:
                return None
            value, u = v
            if u == "BB" or (u is None and unit == "BB"):
                return float(value)
            if bb_chips:  # 칩 표기 (만) 화면
                return round(float(value) / bb_chips, 2)
            return None

        # 1. 좌석별 픽셀 판정
        seats = []
        icon = _icon(pw)
        ih, iw = icon.shape
        for sc in seats_cfg:
            key, is_hero = sc["key"], sc["key"] == "hero"
            ax, ay, ar = sc["avatar"]
            st = {"key": key, "slot": sc.get("slot"), "cfg": sc}
            # 사람 있음: 스택 줄 글자
            stack_box = _text_box(img, sc["stack"], pw, min_w=0.04, min_glyphs=2)
            occupied = stack_box is not None
            if occupied and not is_hero and (dynamic is not None or not sc.get("occupied", True)):
                # 고정 자리 확인 / 보정 때 빈자리였던 곳: 아바타도 있어야
                occupied = self._avatar_present(gray, ax, ay, ar)
            st["occupied"] = occupied or is_hero
            st["stack_box"] = stack_box
            # 흑백(폴드) 판정
            core = _crop(hsv, (ax - ar, ay - ar, 2 * ar, 2 * ar))
            if core.size:
                m = _disc_mask(core.shape, core.shape[1] / 2, core.shape[0] / 2, AVATAR_CORE * ar)
                sat = core[:, :, 1][m]
                st["color_frac"] = float((sat > COLOR_SAT).mean()) if sat.size else 0.0
            else:
                st["color_frac"] = 0.0
            st["grey"] = st["color_frac"] < COLOR_MIN_FRAC
            # 카드 아이콘
            score = 0.0
            if not is_hero and st["occupied"]:
                for ix, iy in sc["icons"]:
                    m = ICON_SEARCH * pw
                    win = _crop(gray, (ix - iw / 2 - m, iy - ih / 2 - m, iw + 2 * m, ih + 2 * m))
                    if win.shape[0] >= ih and win.shape[1] >= iw:
                        score = max(score, float(cv2.matchTemplate(win, icon, cv2.TM_CCOEFF_NORMED).max()))
            st["icon_score"] = round(score, 2)
            st["icons"] = score >= ICON_MIN
            seats.append(st)

        # 2. 나: 카드 앞면/뒷면
        hero = next((s for s in seats if s["key"] == "hero"), None)
        hero_cards = 0
        hero_backs = False
        if hero is not None and "hero" in calib:
            import reader  # 카드 찾기 재사용 (순환 import 방지로 여기서)
            hero_crop = _crop(img, calib["hero"])
            cards = reader.detect_cards(hero_crop) if hero_crop.size else []  # 창 밖(다른 크기의 캡처)이면 빈 영역
            hero_cards = len(cards)
            hero_backs = hero_cards >= 2 and all(reader.is_card_back(c) for c in cards)
        for s in seats:
            if not s["occupied"]:
                s["dealt"] = s["in_hand"] = s["folded"] = False
            elif s["key"] == "hero":
                s["folded"] = hero_backs or s["grey"]
                s["dealt"] = hero_cards >= 2 or s["folded"]
                s["in_hand"] = s["dealt"] and not s["folded"]
            else:
                s["folded"] = s["grey"]
                s["dealt"] = s["icons"] or s["grey"]
                s["in_hand"] = s["icons"] and not s["grey"]

        # 3. 딜러 / 블라인드 원판
        discs = self._dealer(gray, geom, pw) + self._discs(img, hsv, calib, geom, pw)
        bet_center = {s["key"]: _center(s["cfg"]["bet_text"]) for s in seats}

        def nearest_seat(pt, pool):
            pool = [s for s in pool if s["occupied"]]
            if not pool:
                return None
            return min(pool, key=lambda s: math.dist(pt, bet_center[s["key"]]))["key"]
        disc_seat = {}
        for kind, pt in discs:
            if kind not in disc_seat:
                disc_seat[kind] = nearest_seat(pt, seats)

        # 4. 바닥 벳 (픽셀로 있는지 보고, 있는 것만 OCR)
        crops, boxes = {}, {}
        for s in seats:
            s["bet"] = None
            if not s["occupied"]:
                continue
            box = _text_box(img, s["cfg"]["bet_text"], pw, min_w=BET_MIN_W, min_glyphs=3)  # 최소 'xBB'
            if box is None:
                continue
            boxes[s["key"]] = box
            crops["bet:" + s["key"]] = self._crop_for_ocr(img, box)
        # 3글자 스택 (xBB) → 올인(0BB)인지 OCR, 내 스택은 항상 (유효 스택용)
        for s in seats:
            box = s["stack_box"]
            if box is None or not s["occupied"]:
                continue
            if s["key"] == "hero" or (s["in_hand"] and box[1] <= 3):
                crops["stack:" + s["key"]] = self._crop_for_ocr(img, box)

        # 5. 액션 바
        buttons = {}
        for name in ("fold", "call", "raise"):
            r = bar.get(name)
            buttons[name] = r is not None and _inside(r, img.shape) and self._button(gray, r)
        lamp = any(buttons[n] and self._lamp(gray, bar[n]) for n in ("fold", "call"))
        amount_box = None
        if buttons["call"]:
            x, y, w, h = bar["call"]
            amount_box = _text_box(img, (int(x + 0.12 * w), int(y + 0.05 * h), int(0.76 * w), int(0.45 * h)), pw,
                                   min_w=0.04, min_glyphs=3)
            if amount_box is not None:
                crops["call"] = self._crop_for_ocr(img, amount_box)

        values = self._ocr(crops, unit)
        for s in seats:
            s["bet_seen"] = s["key"] in boxes
            if s["bet_seen"]:
                s["bet"] = to_bb(values.get("bet:" + s["key"]))
            v = values.get("stack:" + s["key"])
            s["stack"] = to_bb(v) if v is not None else None
            s["allin"] = bool(s["in_hand"] and s["stack"] == 0.0)

        to_act = buttons["fold"] and buttons["call"] and not lamp
        call = to_bb(values.get("call")) if amount_box is not None else None
        can_check = bool(to_act and amount_box is None)  # 가운데 버튼에 금액이 없음 = '체크' (샘플 없음, 추정)

        # 6. 포지션
        order = [s for s in seats]  # 시계 방향 (내 왼쪽부터, 나는 마지막)
        dealt = [s for s in order if s["dealt"]]
        n = len(dealt)
        dealer = disc_seat.get("D")
        pos_players = min(10, max(6, n)) if n else None
        clamped = bool(n) and n != pos_players
        if clamped:
            notes.append(f"dealt {n} → position names of {pos_players}-max" if n < 6 else f"dealt {n} > 10")
        positions = self._positions(order, dealt, dealer, disc_seat, notes)
        for s in seats:
            s["pos"] = positions.get(s["key"])

        # 7. 말풍선 (콜/레이즈) — 동점 레이즈 가리기용
        for s in seats:
            s["bubble"] = self._bubble(hsv, s, pw) if s["occupied"] else None

        hero_folded = bool(hero and hero["folded"])
        in_hand = [s for s in seats if s["in_hand"]]
        # 내 스택 글자는 앉아 있는 한 늘 보임 (samples/ 7장 모두). 안 보이면 화면이 보정과 어긋남 (창 이동/크기 변경,
        # 다른 화면에서 보정) → 좌석 판정을 믿을 수 없음. 내 자리는 늘 '사람 있음'으로 세므로 그대로 두면 '1명 중 0명'
        layout_ok = hero is None or hero["stack_box"] is not None
        if not layout_ok:
            notes.append("내 스택이 안 보임: 화면이 보정과 다름 (창 이동/크기 변경?) → 다시 자동 지정")
        state = {
            "players": sum(1 for s in seats if s["occupied"]) if layout_ok else None,
            "layout_ok": layout_ok,
            "no_hand": n < 2,  # 카드 받은 사람이 2명 미만 (핸드 사이/휴식 시간/대기)
            "dealt": n,
            "in_hand": len(in_hand),
            "opponents_in_hand": sum(1 for s in in_hand if s["key"] != "hero"),
            "hero_folded": hero_folded,
            "hero_in_hand": bool(hero and hero["in_hand"]),
            "dealer": dealer,
            "blind_seats": {"sb": disc_seat.get("SB"), "bb": disc_seat.get("BB")},
            "hero_pos": positions.get("hero"),
            "pos_players": pos_players,
            "pos_clamped": clamped,
            "seats": [{"key": s["key"], "slot": s["slot"], "pos": s["pos"], "occupied": s["occupied"], "dealt": s["dealt"],
                       "in_hand": s["in_hand"], "folded": s["folded"], "allin": s["allin"], "bet": s["bet"],
                       "stack": s["stack"], "bubble": s["bubble"]} for s in seats],
            "to_act": bool(to_act),
            "preaction": bool(lamp and buttons["fold"]),
            "call": call,
            "can_check": can_check,
            "can_raise": bool(to_act and buttons["raise"]),
            "street": None,
            "facing_allin": False,
            "preflop": None,
            "notes": notes,
        }
        state["preflop"] = self._preflop(seats, positions, pos_players, unit, img, gray, pw, to_bb)
        if state["preflop"]:
            state["facing_allin"] = state["preflop"].pop("_facing_allin", False)
        # 상대 스택/올인은 프리플랍 판단 중에 더 읽었을 수 있음
        for out, s in zip(state["seats"], seats):
            out["stack"], out["allin"] = s["stack"], s["allin"]
        state["ocr_reads"] = self.last_ocr
        state["ocr_slow"] = self.last_slow
        state["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return state

    # --- 픽셀 판정들 ------------------------------------------------------------------------
    def _slot_cfg(self, calib):
        """layout.slot_seats(calib) 를 보정마다 한 번만 계산. 쓸 수 없으면 None."""
        key = repr((calib.get("geom"), calib.get("my_stack"), [(s.get("key"), s.get("slot")) for s in calib.get("seats") or []]))
        if self._slots is None or self._slots[0] != key:
            try:
                self._slots = (key, layout.slot_seats(calib))
            except Exception:
                self._slots = (key, None)
        return self._slots[1]

    @staticmethod
    def _avatar_present(gray, ax, ay, ar):
        """아바타 원 안이 고르지 않음 (얼굴 그림). 빈 자리는 펠트/바닥 무늬라 고름."""
        disc = _crop(gray, (ax - AVATAR_CORE * ar, ay - AVATAR_CORE * ar, 2 * AVATAR_CORE * ar, 2 * AVATAR_CORE * ar))
        return disc.size > 0 and float(disc.std()) > AVATAR_STD

    @staticmethod
    def _button(gray, rect):
        win = _crop(gray, rect)
        return win.size > 0 and float(win.std()) > BUTTON_STD

    @staticmethod
    def _lamp(gray, rect):
        """예약 버튼: 버튼 왼쪽 10~15% 자리에 바탕과 밝기가 다른 세로 칸 (꺼짐 = 어두움, 켜짐 = 흰색)."""
        x, y, w, h = rect
        win = _crop(gray, (x, y + 0.3 * h, 0.3 * w, 0.4 * h)).astype(np.float32)
        if win.size == 0:
            return False
        prof = win.mean(0)
        bg = float(np.median(prof))
        lo, hi = int(0.05 * w), int(0.25 * w)
        diff = prof[lo:hi] - bg
        need = max(3, int(0.025 * w))
        for sign in (1, -1):
            runs = layout._runs(sign * diff > LAMP_STEP)
            if any(b - a >= need and b - a <= 0.08 * w for a, b in runs):
                return True
        return False

    def _dealer(self, gray, geom, pw):
        """'D' 원판: 템플릿 (흰 바탕이 글자 테두리로 조각나서 덩어리 찾기로는 안 잡힘) → [('D', (x, y))] 또는 [].
        딜러는 핸드마다 한 번 움직이므로 직전 자리를 먼저 확인하고, 없을 때만 펠트 전체를 찾음."""
        ring = geom.get("ring")
        if not ring:
            return []
        H, W = gray.shape

        def search(x0, y0, x1, y1, scales):
            x0, y0, x1, y1 = int(max(0, x0)), int(max(0, y0)), int(min(W, x1)), int(min(H, y1))
            win = gray[y0:y1, x0:x1]
            best = (0.0, None, None)
            for s in scales:
                t = _template(_D_B64, _D_SHAPE, pw, s)
                if win.shape[0] < t.shape[0] or win.shape[1] < t.shape[1]:
                    continue
                _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED))
                if mx > best[0]:
                    best = (mx, (x0 + loc[0] + t.shape[1] / 2, y0 + loc[1] + t.shape[0] / 2), s)
            return best
        if self.dealer_at is not None:
            (px, py), s = self.dealer_at
            r = 0.15 * pw
            score, pt, s = search(px - r, py - r, px + r, py + r, (s,))
            if score >= D_MIN:
                self.dealer_at = (pt, s)
                return [("D", pt)]
        rcx, rcy, ra, rb = ring
        score, pt, s = search(rcx - ra, rcy - rb, rcx + ra, rcy + rb, D_SCALES)
        self.dealer_at = (pt, s) if score >= D_MIN else None
        return [("D", pt)] if score >= D_MIN else []

    @staticmethod
    def _discs(img, hsv, calib, geom, pw):
        """펠트 위 흰 원판 중 SB/BB → [(종류, (x, y))]."""
        H, W = img.shape[:2]
        ring = geom.get("ring")
        if not ring:
            return []
        rcx, rcy, ra, rb = ring
        x0, x1 = int(max(0, rcx - ra)), int(min(W, rcx + ra))
        y0, y1 = int(max(0, rcy - rb)), int(min(H, rcy + rb))
        sub = hsv[y0:y1, x0:x1]
        mask = ((sub[:, :, 1] < 60) & (sub[:, :, 2] > 200)).astype(np.uint8)
        yy, xx = np.ogrid[y0:y1, x0:x1]
        mask[((xx - rcx) / ra) ** 2 + ((yy - rcy) / rb) ** 2 > 0.95 ** 2] = 0
        for key, grow in (("pill", 0.35), ("board", 0.0), ("hero", 0.15)):
            if key in calib:
                x, y, w, h = calib[key]
                g = grow * pw
                mask[max(0, int(y - g - y0)):max(0, int(y + h + 0.05 * pw - y0)),
                     max(0, int(x - 0.1 * pw - x0)):max(0, int(x + w + g - x0))] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        count, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        out = []
        lo, hi = DISC_SIZE[0] * pw, DISC_SIZE[1] * pw
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if not (lo <= w <= hi and lo <= h <= hi) or abs(w - h) > 0.35 * max(w, h) or area < 0.3 * w * h:
                continue
            cx, cy = x0 + x + w / 2, y0 + y + h / 2
            inner = _crop(hsv, (cx - 0.3 * w, cy - 0.3 * h, 0.6 * w, 0.6 * h)).reshape(-1, 3).astype(int)
            if inner.size == 0:
                continue
            hue, sat = inner[:, 0], inner[:, 1]
            blue = ((hue >= 100) & (hue <= 130) & (sat > 100)).mean()
            red = (((hue <= 8) | (hue >= 165)) & (sat > 100)).mean()
            if blue > 0.12 and blue > red:
                kind = "SB"
            elif red > 0.12:
                kind = "BB"
            else:
                continue
            out.append((kind, (cx, cy), max(blue, red)))
        out.sort(key=lambda d: -d[2])  # 같은 종류가 여러 개면 가장 확실한 것
        return [(k, p) for k, p, _ in out]

    @staticmethod
    def _bubble(hsv, s, pw):
        """아바타 바깥쪽 말풍선 → 액션 이름 또는 None. 빛 고리(더 밝고 진함)는 범위 밖.

        색으로 가림 (samples/ 7장 실측, 창 0.18x0.16pw):
          'call'  파랑 (≥0.14)            'raise' 주황 (≥0.13) — 벳도 같은 색일 수 있음 (샘플 없음, 앞선 벳으로 구별)
          'allin' 빨강 빛 (빨강 0.17 > 주황 0.09)
          'fold'  밝은 회색 말풍선 + 검은 글자 (밝은 회색 0.24~0.35, 흰색 ≤0.007)
          'away'  회색 말풍선 + 흰 커피잔 (자리 비움; 흰색 0.029)
          'check' 초록 (샘플 없음 — 추정)
        """
        ax, ay, ar = s["cfg"]["avatar"]
        is_hero = s["key"] == "hero"
        side = 1 if is_hero else (-1 if s["slot"] in LEFT_SLOTS else 1)
        cx = ax + side * (0.28 if is_hero else 0.25) * pw
        win = _crop(hsv, (cx - 0.09 * pw, ay - 0.08 * pw, 0.18 * pw, 0.16 * pw)).reshape(-1, 3).astype(int)
        if win.size == 0:
            return None
        h, sat, v = win[:, 0], win[:, 1], win[:, 2]
        blue = ((h >= 100) & (h <= 112) & (sat >= 140) & (sat <= 195) & (v >= 180) & (v <= 235)).mean()
        orange = ((h >= 5) & (h <= 25) & (sat > 140) & (v > 180)).mean()
        red = (((h <= 4) | (h >= 170)) & (sat > 140) & (v > 150)).mean()
        green = ((h >= 40) & (h <= 90) & (sat > 120) & (v > 150)).mean()
        light = ((sat < 35) & (v >= 150) & (v <= 240)).mean()
        white = ((sat < 25) & (v > 235)).mean()
        if red > 0.10 and red > orange:
            return "allin"
        if blue > 0.12:
            return "call"
        if orange > 0.08:
            return "raise"
        if green > 0.12:
            return "check"
        if light > 0.15:
            return "away" if white > 0.015 else "fold"
        return None

    # --- 포지션 ---------------------------------------------------------------------------
    @staticmethod
    def _positions(order, dealt, dealer, disc_seat, notes):
        """dealt(시계 방향) + 딜러 자리 → {key: 포지션}. 딜러를 못 찾으면 BB/SB 원판으로."""
        n = len(dealt)
        if n < 2:
            return {}
        keys = [s["key"] for s in order]
        dealt_keys = [s["key"] for s in dealt]
        idx = {k: i for i, k in enumerate(keys)}

        def after(seat_key):
            """seat_key 다음(시계 방향)부터 받은 사람 순서."""
            start = idx[seat_key]
            return sorted(dealt_keys, key=lambda k: (idx[k] - start - 1) % len(keys))
        if dealer is not None:
            seq = after(dealer)            # SB, BB, ..., 딜러
            if n == 2:                     # 헤즈업: 딜러가 SB → [BB, 딜러] 를 다른 경우와 같은 SB, BB 순서로
                seq = seq[::-1]
        elif disc_seat.get("BB") in dealt_keys:
            notes.append("no dealer disc: positions from BB disc")
            seq = after(disc_seat["BB"])   # UTG, ..., BB
            seq = seq[-2:] + seq[:-2]
        elif disc_seat.get("SB") in dealt_keys:
            notes.append("no dealer disc: positions from SB disc")
            seq = after(disc_seat["SB"])   # BB, ..., SB
            seq = seq[-1:] + seq[:-1]
        else:
            notes.append("no dealer/blind disc: positions unknown")
            return {}
        if n == 2:  # 헤즈업: seq = [SB(딜러), BB]
            return {seq[0]: "SB", seq[1]: "BB"}
        names = pf.seats(min(10, max(6, n)))[-n:] if n <= 10 else pf.seats(10)
        action = seq[2:] + seq[:2]         # UTG.. 딜러, SB, BB
        return {k: names[i] for i, k in enumerate(action[:len(names)])}

    # --- 프리플랍 상황 ---------------------------------------------------------------------
    def _preflop(self, seats, positions, pos_players, unit, img, gray, pw, to_bb):
        hero = next((s for s in seats if s["key"] == "hero"), None)
        if hero is None or not hero["dealt"] or not positions.get("hero"):
            return None
        if any(s["bet_seen"] and s["bet"] is None for s in seats):
            return None  # 벳은 보이는데 숫자를 못 읽음
        bets = {s["key"]: s["bet"] for s in seats if s["bet"] is not None}
        # 레이즈 금액 단계 (1BB 초과, 같은 금액은 한 단계)
        levels = []
        for v in sorted(v for v in bets.values() if v > 1.0 + SAME_BET):
            if not levels or v - levels[-1] > SAME_BET:
                levels.append(v)
        raises = len(levels)
        hero_bet = bets.get("hero", 0.0)
        out = {"situation": None, "villain_key": None, "villain_pos": None, "raises": raises,
               "last_raise_bb": levels[-1] if levels else None, "hero_bet": hero_bet,
               "limpers": 0, "stack_bb": None, "valid": False, "why": None}
        hero_pos, n = positions.get("hero"), pos_players
        bb_key = next((k for k, p in positions.items() if p == "BB"), None)
        sb_key = next((k for k, p in positions.items() if p == "SB"), None)
        out["limpers"] = sum(1 for k, v in bets.items() if abs(v - 1.0) <= SAME_BET and k not in (bb_key, sb_key, "hero"))

        # 마지막 레이즈한 사람: 그 금액을 낸 사람 중 말풍선이 '콜'이 아닌 사람 (여럿이면 '레이즈' 말풍선, 그다음 액션 순서가 빠른 사람)
        def raiser(level):
            tied = [s for s in seats if s["bet"] is not None and abs(s["bet"] - level) <= SAME_BET]
            if len(tied) > 1:
                pick = [s for s in tied if s["bubble"] in ("raise", "allin")] or \
                       [s for s in tied if s["bubble"] != "call"] or tied
                names = pf.seats(n) if n else []
                pick.sort(key=lambda s: names.index(s["pos"]) if s["pos"] in names else 99)
                tied = pick
            return tied[0] if tied else None
        villain = raiser(levels[-1]) if levels else None
        if villain is not None and villain["key"] == "hero":
            villain = None
        if villain is not None:
            out["villain_key"], out["villain_pos"] = villain["key"], villain["pos"]
            out["_facing_allin"] = bool(villain["allin"] or self._villain_allin(villain, img, gray, pw, unit, to_bb))
            villain["allin"] = villain["allin"] or out["_facing_allin"]
        # 유효 스택: (내 스택 + 내 벳) vs (상대 스택 + 상대 벳)
        if hero["stack"] is not None:
            eff = hero["stack"] + hero_bet
            if villain is not None:
                vs = villain["stack"] if villain["stack"] is not None else self._stack(villain, img, gray, pw, unit, to_bb)
                if vs is not None:
                    eff = min(eff, vs + (villain["bet"] or 0.0))
            out["stack_bb"] = round(eff, 1)
        short = out["stack_bb"] is not None and out["stack_bb"] <= pf.SHORT_STACK_BB

        if hero["folded"]:
            out["why"] = "hero folded"
            return out
        hero_raised_first = raises >= 1 and abs(hero_bet - levels[0]) <= SAME_BET and hero["bubble"] != "call"
        if raises == 0:
            if hero_pos == "BB":
                out["why"] = "unopened, hero is BB"
            else:
                out["situation"] = "jam" if short else "open"
        elif short and villain is not None:
            out["situation"] = "jamcall"
        elif villain is None:
            out["why"] = "hero made the last raise"
        elif raises == 1:
            out["situation"] = "vsopen"
        elif raises == 2 and hero_raised_first:
            out["situation"] = "vs3bet"
        elif raises >= 3 and abs(hero_bet - levels[-2]) <= SAME_BET and hero["bubble"] != "call":
            out["situation"] = "vs4bet"
        else:
            out["why"] = f"{raises} raises, hero did not make the earlier raise (cold / squeezed)"
        sit = out["situation"]
        if sit and n:
            ok_hero = hero_pos in pf.valid_heroes(sit, n)
            vv = pf.valid_villains(sit, hero_pos, n) if ok_hero else []
            out["valid"] = ok_hero and (not vv or out["villain_pos"] in vv)
        return out

    def _stack(self, s, img, gray, pw, unit, to_bb):
        box = s["stack_box"] or _text_box(img, s["cfg"]["stack"], pw, min_w=0.04, min_glyphs=2)
        if box is None:
            return None
        v = self._ocr({"stack:" + s["key"]: self._crop_for_ocr(img, box)}, unit)
        s["stack"] = to_bb(v.get("stack:" + s["key"]))
        return s["stack"]

    def _villain_allin(self, s, img, gray, pw, unit, to_bb):
        st = s["stack"] if s["stack"] is not None else self._stack(s, img, gray, pw, unit, to_bb)
        return st == 0.0


def _center(rect):
    x, y, w, h = rect
    return (x + w / 2, y + h / 2)


_DEFAULT = TableReader()


def read(img, calib):
    """img: 보정 때와 같은 크기의 게임 창 캡처(BGR), calib: layout.calibrate() 결과 → 상태 dict."""
    return _DEFAULT.read(img, calib)
