"""쇼다운: 상대가 공개한 카드 2장 읽기 (핸드 기록의 seats[k].shown / showdown.shown → 나중에 상대 예상 채점).

이 클라이언트는 쇼다운 때 상대 카드 2장을 그 좌석 아바타 위에 앞면으로 띄운다 (보드 카드의 약 0.6배, 같은 그림).
위치는 아바타 중심 기준으로 고정이라 좌석마다 그 자리만 본다:

    match = ShownMatch(reader.Matcher(dirs=(templates, templates_shown)), threshold, shown_labels(templates_shown))
    w = Watcher()
    shown = w.update(hand_id, img, calib, keys, match, exclude=hero + board)   # {seat_key: ['9c', '7c'] 또는 None}
    tracker.update(..., shown=shown)

  1. locate: 아바타 중심 기준 상자(SHOWN_BOX, 단위 pw = POT 알약 폭)를 조금 넓혀 자르고 카드 면(밝은 저채도) 비율의
     가로/세로 단면으로 두 장 묶음 상자를 잡는다. 크기가 카드 2장과 맞아야 함 (아바타·말풍선·족보 글자·뒷면은 안 맞음).
     진 쪽 카드는 어둡게, 이긴 쪽 카드는 노란 테두리 + 아래에 '+xxBB' 글자가 겹쳐 나와도 됨.
  2. 두 장 경계(테두리 틈 / 아바타 중심 근처 가장 빈 세로줄)에서 나눠 reader.Matcher 로 각각 인식. 정규화 크기(52x72)로
     맞춰 비교하므로 작아도 같은 방식. 공개 카드 템플릿(templates_shown/)이 보드 템플릿보다 훨씬 잘 맞음
     (보드 템플릿만으로는 맞는 카드도 0.85~0.94).
  3. ShownMatch: 짝 무늬(♣↔♠, ♦↔♥) 템플릿이 없으면 무늬 판별이 따로 확인해야 인정. 두 장이 서로 다르고
     보드/내 카드/다른 좌석 카드와 겹치지 않아야 함.
  4. Watcher: 핸드별로 좌석 값을 모음. 같은 값 CONFIRM 번이면 확정(더 안 읽음), 확정 전에 다른 값이면 버림.

추측하지 않는다: 한 장이라도 애매하면 그 좌석은 비워 둔다. 예외를 밖으로 내지 않는다 (Watcher).
"""
from pathlib import Path

import cv2
import numpy as np

import layout

# 아바타 중심 기준 공개 카드 2장 묶음 (x0, y0, w, h), 단위 pw. 실측 (pw 228): x -53..+51, y -32..+38 (TC/UL/R 좌석)
SHOWN_BOX = (-0.232, -0.140, 0.456, 0.307)
PAD = (0.07, 0.06)               # 찾는 영역 여유 (가로, 세로)
FACE_MAX_SAT = 50                # 카드 면: 채도 이하 (reader.CARD_MAX_SAT)
FACE_MIN_VALUE = 90              # 카드 면: 밝기 이상 (어둡게 처리된 진 쪽 카드 포함, reader.CARD_MIN_VALUE[1])
LINE_FACE = 0.5                  # 단면(가로줄/세로줄)에서 카드 면 비율이 이 이상이면 카드 안
ROW_GAP = 2                      # 가로 단면에서 이 픽셀 이하로 끊긴 곳은 이어 봄 (족보 글자 줄은 더 떨어져 있음)
COL_GAP = 3                      # 세로 단면: 두 장 사이 얇은 틈은 이어 봄 (이긴 쪽 노란 테두리 틈 ≈0.05pw 은 끊김)
PAIR_W = (0.85, 1.12)            # 찾은 묶음 폭 / SHOWN_BOX 폭
PAIR_H = (0.75, 1.12)            # 찾은 묶음 높이 / SHOWN_BOX 높이 (아래가 승리 금액 글자에 가려지면 낮아짐)
MIN_FACE = 0.45                  # 묶음 상자 안 카드 면 비율 최소
SPLIT_NEAR = 0.05                # 두 장 경계는 아바타 중심 x 에서 이 거리(pw) 안
CARD_MIN_W = 0.33                # 한 장의 폭 / SHOWN_BOX 폭 최소 (실측 한 장 ≈0.4~0.5)
CONFIRM = 2                      # 같은 결과가 이만큼 나오면 확정 (그 좌석은 더 안 읽음)


def _is_card(c):
    return isinstance(c, str) and len(c) == 2 and c[0] in "23456789TJQKA" and c[1] in "cdhs"


def seat_avatars(calib):
    """보정 결과 → {seat_key: (ax, ay)} (tablestate 와 같은 좌석 키), pw. 못 구하면 ({}, None)."""
    if not isinstance(calib, dict):
        return {}, None
    geom = calib.get("geom") or {}
    pw = geom.get("pw") or (calib["pill"][2] if calib.get("pill") else None)
    seats = None
    try:
        seats = layout.slot_seats(calib)
    except Exception:
        seats = None
    if not seats:
        seats = calib.get("seats") or (layout.anchors(calib).get("seats") if calib.get("my_stack") else None) or []
    out = {}
    for s in seats:
        av = s.get("avatar")
        if s.get("key") and av and len(av) >= 2:
            out[s["key"]] = (float(av[0]), float(av[1]))
    return out, (float(pw) if pw else None)


def _face_mask(crop):
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    return (hsv[:, :, 1] < FACE_MAX_SAT) & (hsv[:, :, 2] > FACE_MIN_VALUE)


def _runs(prof, thresh, gap):
    """prof 에서 값이 thresh 이상인 구간들 [(start, end)] (gap 이하 빈칸은 이어 붙임)."""
    runs, start, last = [], None, None
    for i, v in enumerate(prof >= thresh):
        if not v:
            continue
        if start is None:
            start = i
        elif i - last - 1 > gap:
            runs.append((start, last + 1))
            start = i
        last = i
    if start is not None:
        runs.append((start, last + 1))
    return runs


def _overlap(run, lo, hi):
    return min(run[1], hi) - max(run[0], lo)


def locate(img, avatar, pw):
    """아바타 위에 앞면 카드 2장이 있으면 이미지 좌표 [(x, y, w, h), (x, y, w, h)] (왼쪽, 오른쪽), 없으면 None.

    카드 면 비율의 가로/세로 단면으로 묶음 상자를 잡는다 (뒤에 비치는 아바타 머리, 바로 아래 족보 글자 줄은 빈 줄로
    떨어져 있거나 줄 전체에서 면 비율이 낮아 빠짐). 두 장 경계: 세로 단면이 두 덩어리로 끊겨 있으면(이긴 쪽 카드의
    노란 테두리) 그 틈, 아니면 아바타 중심 근처에서 면이 가장 적은 세로줄."""
    if img is None or not pw or avatar is None:
        return None
    H, W = img.shape[:2]
    ax, ay = avatar
    bx, by, bw, bh = SHOWN_BOX
    x0 = int(round(ax + (bx - PAD[0]) * pw))
    y0 = int(round(ay + (by - PAD[1]) * pw))
    x1 = int(round(ax + (bx + bw + PAD[0]) * pw))
    y1 = int(round(ay + (by + bh + PAD[1]) * pw))
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    exp_w, exp_h = bw * pw, bh * pw
    if x1 - x0 < 0.9 * exp_w or y1 - y0 < 0.9 * exp_h:
        return None  # 창 밖
    face = _face_mask(img[y0:y1, x0:x1])
    ex0, ex1 = int(round(ax + bx * pw)) - x0, int(round(ax + (bx + bw) * pw)) - x0
    ey0, ey1 = int(round(ay + by * pw)) - y0, int(round(ay + (by + bh) * pw)) - y0
    rows = max(_runs(face[:, max(0, ex0):ex1].mean(axis=1), LINE_FACE, ROW_GAP),
               key=lambda r: _overlap(r, ey0, ey1), default=None)
    if rows is None or _overlap(rows, ey0, ey1) <= 0:
        return None
    top, bottom = rows
    cols = [r for r in _runs(face[top:bottom].mean(axis=0), LINE_FACE, COL_GAP)
            if r[1] - r[0] >= CARD_MIN_W * exp_w and _overlap(r, ex0, ex1) > 0]
    if not cols or len(cols) > 2:
        return None
    left, right = cols[0][0], cols[-1][1]
    uw, uh = right - left, bottom - top
    if not (PAIR_W[0] <= uw / exp_w <= PAIR_W[1] and PAIR_H[0] <= uh / exp_h <= PAIR_H[1]):
        return None
    if face[top:bottom, left:right].mean() < MIN_FACE:
        return None
    c = int(round(ax)) - x0
    s = max(1, int(round(SPLIT_NEAR * pw)))
    if len(cols) == 2:
        (l0, l1), (r0, r1) = cols
        if not (c - s <= (l1 + r0) / 2 <= c + s):
            return None
    else:
        prof = face[top:bottom].mean(axis=0)
        lo, hi = max(left + 1, c - s), min(right - 1, c + s)
        if hi <= lo:
            return None
        l1 = r0 = lo + int(np.argmin(prof[lo:hi + 1]))
        l0, r1 = left, right
    if min(l1 - l0, r1 - r0) < CARD_MIN_W * exp_w:
        return None
    return [(x0 + l0, y0 + top, l1 - l0, uh), (x0 + r0, y0 + top, r1 - r0, uh)]


SHOWN_DIR = "templates_shown"    # 쇼다운 공개 카드 템플릿 폴더 (templates/ 옆). 보드 카드 템플릿과 같은 52x72 정규화
TWIN = {"c": "s", "s": "c", "d": "h", "h": "d"}  # 같은 색 다른 무늬: 작은 카드에서 헷갈리는 짝


def shown_labels(folder):
    """템플릿 폴더 → 들어 있는 카드 라벨 집합 (파일 이름 '9c_....png')."""
    try:
        return {p.stem.split("_")[0] for p in Path(folder).glob("*.png") if _is_card(p.stem.split("_")[0])}
    except Exception:
        return set()


class ShownMatch:
    """공개 카드 한 장 인식: match(crop) -> (라벨, 점수). reader.Matcher(templates/ + templates_shown/) 결과에 안전장치 하나:
    읽은 카드의 짝(♣↔♠, ♦↔♥)이 공개 카드 템플릿에 없으면 그 짝과는 비교해 본 적이 없으므로 (작은 코너 무늬만 다름)
    무늬 판별(glyphs.suit)이 따로 같은 무늬라고 확신할 때만 인정. 실측: 10♣ 템플릿 없이 10♣ 이 10♠ 으로 0.95 이상."""

    def __init__(self, matcher, threshold, labels):
        self.matcher = matcher
        self.threshold = threshold
        self.labels = set(labels)

    def __call__(self, crop):
        label, score = self.matcher.match(crop, self.threshold)
        if not _is_card(label):
            return label, score
        if label[0] + TWIN[label[1]] not in self.labels and self.matcher.glyphs.suit(crop) != label[1]:
            return "?", score
        return label, score


def read_seat(img, avatar, pw, match):
    """좌석 하나 → ['9c', '7c'] / None. match(crop) -> (label, score) (reader.Matcher.match 에 기준값을 묶은 것)."""
    rects = locate(img, avatar, pw)
    if rects is None:
        return None
    out = []
    for x, y, w, h in rects:
        crop = img[y:y + h, x:x + w]
        if crop.size == 0:
            return None
        label = match(crop)[0]
        if not _is_card(label):
            return None
        out.append(label)
    if out[0] == out[1]:
        return None
    return out


def read_shown(img, calib, keys, match, exclude=()):
    """좌석 키들 → {key: [c1, c2]} (이번 프레임에서 두 장 다 읽힌 좌석만). 보드/내 카드/다른 좌석과 겹치는 결과는 버림."""
    avatars, pw = seat_avatars(calib)
    found = {}
    for k in keys:
        if k == "hero" or k not in avatars:
            continue
        cards = read_seat(img, avatars[k], pw, match)
        if cards:
            found[k] = cards
    used = {c for c in exclude if _is_card(c)}
    counts = {}
    for cards in found.values():
        for c in cards:
            counts[c] = counts.get(c, 0) + 1
    return {k: v for k, v in found.items() if not any(c in used or counts[c] > 1 for c in v)}


class Watcher:
    """핸드마다 좌석별 공개 카드를 모은다.

    처음 두 장이 다 읽힌 프레임에서 바로 기록하고(쇼다운 화면이 1프레임만 잡히는 경우가 많음), 같은 값이 CONFIRM 프레임
    나오면 확정해서 그 좌석은 더 읽지 않는다. 확정 전에 다른 값이 읽히면 어긋남 → 그 좌석은 None(기록에서 지움)으로 두고
    그 핸드에서는 더 읽지 않는다. 남은 좌석이 모두 확정/어긋남이면 그 핸드는 더 찾지 않음."""

    def __init__(self, confirm=CONFIRM):
        self.confirm = confirm
        self.hand = None
        self.seen = {}       # key -> [cards, 같은 값 읽은 횟수]
        self.bad = set()     # 어긋난 좌석
        self.errors = 0
        self.last_error = None

    def reset(self, hand=None):
        self.hand = hand
        self.seen = {}
        self.bad = set()

    def result(self):
        """{key: cards} (읽은 좌석) + {key: None} (어긋나서 지울 좌석)."""
        out = {k: list(v[0]) for k, v in self.seen.items()}
        out.update({k: None for k in self.bad})
        return out

    def update(self, hand, img, calib, keys, match, exclude=()):
        """hand: 핸드 id (바뀌면 새로 시작). keys: 볼 좌석 (남은 상대). → result()."""
        if hand != self.hand:
            self.reset(hand)
        todo = [k for k in keys if k != "hero" and k not in self.bad
                and self.seen.get(k, (None, 0))[1] < self.confirm]
        if not todo:
            return self.result()
        try:
            others = [c for k, v in self.seen.items() if k not in todo for c in v[0]]
            got = read_shown(img, calib, todo, match, list(exclude) + others)
        except Exception as exc:
            self.errors += 1
            self.last_error = repr(exc)
            got = {}
        for k, cards in got.items():
            prev = self.seen.get(k)
            if prev is None:
                self.seen[k] = [list(cards), 1]
            elif prev[0] == list(cards):
                prev[1] += 1
            else:
                self.seen.pop(k)
                self.bad.add(k)
        return self.result()
