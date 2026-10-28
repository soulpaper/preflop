"""좌석 닉네임 읽기 (Windows OCR, 한국어).

    nr = names.NameReader(resolver=store.resolve_name, on_confirm=store.learn_fingerprint)   # store 는 선택
    nr.reset()                                   # 새 보정 / 새 테이블 세션
    confirmed = nr.read(img, calib, state)       # {seat_key: (name, truncated)} — 확정된 것만
    # seat_key 는 tablestate 와 같음 (geom 있는 보정: 'seat_BL'.. / 'hero'). state 의 빈자리는 기억을 버림

닉네임은 스택 숫자 바로 위 한 줄 (연회색 글자). 위로는 아바타 아래쪽/카드 아이콘, 아래로는 스택 숫자가 붙어 있고,
차례인 사람은 빛 고리, 테이블 테두리(구리색/보라 펠트)가 글자 뒤로 지나가서 그대로 OCR 하면 잘 틀린다.
  1. 스택 글자 위 창에서 '채도 낮고 밝은' 픽셀만 글자로 보고, 예상 줄 높이에 가장 가까운 글자 줄을 찾는다.
  2. 그 줄만 잘라 '흰 바탕 검은 글자' 이미지 4장(크기/보간/굵기 다르게)을 읽어 다수결 (한글은 초/중/종성 단위).
  3. 잘린 닉네임('User84...'): 줄 끝의 작은 점 2~3개(픽셀) 또는 OCR 끝의 '.', '…', '‥' → truncated.
  4. 한 좌석의 닉네임은 3번(연속 프레임) 같게 읽혀야 확정 (픽셀이 그대로면 OCR 은 다시 안 하고 같은 결과로 셈).
     확정 뒤에는 stable.Signature 로 닉네임 영역이 바뀌었을 때만 다시 읽고, 글자 지문(fingerprint)이 같으면
     (빛 고리가 지나간 것 등) 그대로 둔다. 지문이 다르면 확정 취소 → 다시 3번.
  5. 글자 지문: 줄 마스크를 높이 16px 로 정규화. 같은 닉네임 ≥0.80, 다른 닉네임 ≤0.42 (samples/ 실측).
     profiles.Store 에 저장해 두면 다음에 OCR 이 조금 다르게 읽어도 같은 이름으로 묶임 (resolver).

정확도 (samples/ 7장, 55자리): 확정 이름이 정확히 맞음 38/55 — 잘리지 않은 이름 27/38, 잘린 이름 표시 14/17
(놓친 3개는 차례 빛 고리가 닉네임을 덮은 경우). 좌석당 OCR 약 24ms.
"""
import re
from collections import Counter

import cv2
import numpy as np

import ocr
import stable

# 스택 글자 중심 기준 닉네임 창 (pw 단위): 세로 [위, 아래], 가로 반폭
NAME_WIN_Y = (-0.175, -0.040)
NAME_WIN_HALF_W = 0.40
NAME_CENTER_DY = -0.098          # 닉네임 줄 중심 (실측: 글자 아래 끝이 스택 위 끝에 거의 붙음)
TEXT_H = (0.035, 0.095)          # 닉네임 줄 높이 범위 (pw)
TEXT_SAT = 80                    # 글자 픽셀 최대 채도 (빛 고리/펠트는 채도 높음)
CONFIRM_READS = 3                # 같은 이름이 이만큼 연속으로 읽혀야 확정
OCR_BUDGET = 3                   # read() 한 번에 OCR 할 최대 좌석 수 (밀린 좌석은 다음 프레임)
JUNK_EDGE = " -_~`'\"•·|,:;!¯—–=\\/()[]{}<>"
OUTER = "…"                 # 잘린 닉네임 표시용 '…'


# --- 픽셀 -------------------------------------------------------------------------------------------
def _window(seat_cfg, pw, shape):
    x, y, w, h = seat_cfg["stack"]
    sx, sy = x + w / 2, y + h / 2
    H, W = shape[:2]
    x0 = int(max(0, sx - NAME_WIN_HALF_W * pw))
    x1 = int(min(W, sx + NAME_WIN_HALF_W * pw))
    y0 = int(max(0, sy + NAME_WIN_Y[0] * pw))
    y1 = int(min(H, sy + NAME_WIN_Y[1] * pw))
    return x0, y0, x1, y1, sy + NAME_CENTER_DY * pw


def _text_mask(win):
    hsv = cv2.cvtColor(win, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1].astype(np.int16), hsv[:, :, 2].astype(np.int16)
    grey = sat < TEXT_SAT
    bg = float(np.median(val))
    hi = float(np.percentile(val[grey], 99)) if grey.any() else 255.0
    thr = max(bg + 0.45 * (hi - bg), bg + 40, 110)
    return grey & (val > thr), thr


def _runs(mask_1d, gap=0):
    runs, start, last = [], None, None
    for i, v in enumerate(mask_1d):
        if v:
            if start is None:
                start = i
            elif i - last - 1 > gap:
                runs.append((start, last + 1))
                start = i
            last = i
    if start is not None:
        runs.append((start, last + 1))
    return runs


def locate(img, seat_cfg, pw):
    """닉네임 글자 줄 → dict(box=(x0, y0, x1, y1) 이미지 좌표, dots=끝의 점 개수, th) 또는 None."""
    x0, y0, x1, y1, cy_exp = _window(seat_cfg, pw, img.shape)
    if x1 - x0 < 8 or y1 - y0 < 6:
        return None
    win = img[y0:y1, x0:x1]
    mask, _ = _text_mask(win)
    counts = mask.sum(1)
    if counts.max() < 2:
        return None
    rows = counts >= max(2, 0.12 * counts.max())
    # 위 아바타 아래쪽/카드 아이콘과 1줄 간격으로 붙어 있어서 간격 없이 나눈 뒤, 글자 높이를 넘지 않을 때만 이어 붙임
    raw = _runs(rows, gap=0)
    if not raw:
        return None
    cy = cy_exp - y0
    i = min(range(len(raw)), key=lambda k: abs((raw[k][0] + raw[k][1]) / 2 - cy))
    ry0, ry1 = raw[i]
    lo, hi = i - 1, i + 1
    while True:
        grown = False
        for j in (lo, hi):
            if 0 <= j < len(raw):
                a, b = min(ry0, raw[j][0]), max(ry1, raw[j][1])
                gap = raw[j][0] - ry1 if j > i else ry0 - raw[j][1]
                if gap <= 2 and b - a <= TEXT_H[1] * pw * 0.85:
                    ry0, ry1 = a, b
                    grown = True
                    if j == lo:
                        lo -= 1
                    else:
                        hi += 1
        if not grown:
            break
    if not TEXT_H[0] * pw <= ry1 - ry0 <= TEXT_H[1] * pw:
        return None
    th = ry1 - ry0
    cols = mask[ry0:ry1].sum(0) >= 1
    groups = _runs(cols, gap=max(2, int(0.6 * th)))
    if not groups:
        return None
    cx = win.shape[1] / 2
    # 가운데 정렬된 닉네임: 가운데에 가장 가까운 묶음 (옆의 카드 아이콘/빛 조각은 간격이 커서 따로 묶임)
    gx0, gx1 = min(groups, key=lambda g: 0 if g[0] <= cx < g[1] else min(abs(g[0] - cx), abs(g[1] - cx)))
    if gx1 - gx0 < 0.6 * th:
        return None
    # 끝의 점: 줄 아래쪽 1/3 에만 있는 작은 덩어리
    sub = mask[ry0:ry1, gx0:gx1].astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(sub, connectivity=8)
    comps = sorted((stats[i] for i in range(1, n)), key=lambda s: s[0])
    dots = 0
    for s in reversed(comps):
        x, y, w, h, area = s
        if h <= 0.32 * th and w <= 0.32 * th and y >= 0.55 * th:
            dots += 1
        else:
            break
    return {"box": (x0 + gx0, y0 + ry0, x0 + gx1, y0 + ry1), "dots": dots, "th": th}


# OCR 용 전처리 (samples/ 7장 52자리에서 27가지 중 고름: 이 넷의 자모 다수결 38/52, 하나만 쓰면 최고 36/52)
OCR_MARGIN = (0.6, 0.4)                     # 글자 줄 박스 바깥 여백 (글자 높이 배)
OCR_VARIANTS = ((72, cv2.INTER_CUBIC, False), (72, cv2.INTER_LANCZOS4, False),
                (72, cv2.INTER_LANCZOS4, True), (56, cv2.INTER_CUBIC, True))   # (글자 높이 px, 보간, 굵게)


def _ocr_images(img, box, th):
    """글자 줄 → OCR 용 이미지 여러 장 (흰 바탕 검은 글자, 크기/보간/굵기 다르게)."""
    x0, y0, x1, y1 = box
    H, W = img.shape[:2]
    mx, my = OCR_MARGIN
    ax0, ay0 = int(max(0, x0 - mx * th)), int(max(0, y0 - my * th))
    ax1, ay1 = int(min(W, x1 + mx * th)), int(min(H, y1 + my * th))
    crop = img[ay0:ay1, ax0:ax1]
    out = []
    if crop.size == 0:
        return out
    for height, interp, thick in OCR_VARIANTS:
        scale = height / max(1, th)
        big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=interp)
        hsv = cv2.cvtColor(big, cv2.COLOR_BGR2HSV)
        sat, val = hsv[:, :, 1].astype(np.float32), hsv[:, :, 2].astype(np.float32)
        grey = sat < TEXT_SAT
        bg = float(np.median(val))
        fg = float(np.percentile(val[grey], 98)) if grey.any() else 255.0
        soft = np.clip((val - bg) / max(20.0, fg - bg), 0, 1) * (sat < TEXT_SAT + 20)
        soft = (255 - 255 * soft).astype(np.uint8)
        _, im = cv2.threshold(soft, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if thick:
            k = max(1, int(height / 40)) + 1
            im = cv2.erode(im, np.ones((k, k), np.uint8))
        out.append(cv2.copyMakeBorder(im, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255))
    return out


# --- 다수결 (한글은 자모 단위) ------------------------------------------------------------------
VALID = re.compile(r"^[A-Za-z0-9가-힣_]+$")
HANGUL = re.compile(r"[가-힣]")
ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")


def _jamo(ch):
    o = ord(ch) - 0xAC00
    if 0 <= o < 11172:
        return ("H", o // 588, (o % 588) // 28, o % 28)
    return ("C", ch)


def _unjamo(p):
    return chr(0xAC00 + p[1] * 588 + p[2] * 28 + p[3]) if p[0] == "H" else p[1]


def vote(names_):
    """정리된 이름 후보들 → 하나. 글자 수가 가장 많이 나온 길이의 후보만, 자리마다 (한글은 초성/중성/종성마다) 다수결.
    예: ['홍길동', '흥길동', '홍길둥', '홍2동'] → 홍길동."""
    # 기호가 섞인 것('경+남힐톁'), 한글과 영문/숫자가 섞인 것('머리0', '멀EI조이')은 오인식 → 버림
    # (한글+숫자 닉네임이 실제로 있으면 확정되지 않아 통계가 안 쌓임 — 엉뚱한 이름으로 쌓이는 것보다 나음)
    cands = [n for n in names_ if n and VALID.match(n) and not (HANGUL.search(n) and ASCII_ALNUM.search(n))]
    if not cands:
        return None
    length = Counter(len(n) for n in cands).most_common(1)[0][0]
    same = [n for n in cands if len(n) == length]
    whole = Counter(same).most_common(1)[0]
    if whole[1] * 2 > len(same):
        return whole[0]  # 과반이면 그대로
    out = []
    for i in range(length):
        parts = [_jamo(n[i]) for n in same]
        kind = Counter(p[0] for p in parts).most_common(1)[0][0]
        parts = [p for p in parts if p[0] == kind]
        if kind == "C":
            out.append(Counter(p[1] for p in parts).most_common(1)[0][0])
        else:
            out.append(_unjamo(("H",) + tuple(Counter(p[j] for p in parts).most_common(1)[0][0] for j in (1, 2, 3))))
    return "".join(out)


# --- 글자 모양 지문 (OCR 이 틀려도 같은 닉네임은 같은 키로) ---------------------------------------------
FP_H = 16           # 지문 높이 px
FP_MAX_W = 192


def fingerprint(img, box):
    """글자 줄 → 높이 FP_H 로 줄인 글자 마스크 (uint8 0..255) 또는 None. 창 크기가 달라도 비교할 수 있게 정규화.
    줄 위쪽에 걸친 카드 아이콘/아바타 조각(위 30% 안에만 있는 납작한 덩어리)은 뺀다."""
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1].astype(np.int16), hsv[:, :, 2].astype(np.int16)
    bg = float(np.median(val))
    hi = float(np.percentile(val, 99))
    mask = ((val > max(bg + 0.45 * (hi - bg), bg + 35)) & (sat < 130)).astype(np.uint8)
    th = mask.shape[0]
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if y + h <= 0.3 * th + 1 and h < 0.35 * th:
            mask[lab == i] = 0
    cols, rows = np.nonzero(mask.sum(0))[0], np.nonzero(mask.sum(1))[0]
    if len(cols) < 2 or len(rows) < 2:
        return None
    m = mask[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1].astype(np.float32)
    w = max(1, min(FP_MAX_W, int(round(m.shape[1] * FP_H / max(1, m.shape[0])))))
    return (cv2.resize(m, (w, FP_H), interpolation=cv2.INTER_AREA) * 255).astype(np.uint8)


def fp_similarity(a, b):
    """두 지문의 유사도 0..1 (폭이 12% 넘게 다르면 0)."""
    if a is None or b is None:
        return 0.0
    a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
    if a.ndim != 2 or b.ndim != 2 or a.shape[0] != b.shape[0]:
        return 0.0
    if abs(a.shape[1] - b.shape[1]) > 0.12 * max(a.shape[1], b.shape[1]):
        return 0.0
    b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    best = 0.0
    for dx in (-1, 0, 1):  # 1px 어긋남 허용
        bb = np.roll(b, dx, axis=1)
        a0, b0 = a - a.mean(), bb - bb.mean()
        den = float(np.sqrt((a0 * a0).sum() * (b0 * b0).sum()))
        if den > 0:
            best = max(best, float((a0 * b0).sum()) / den)
    return best


def clean(text):
    """OCR 문자열 → (이름, 끝에 점이 있었는지). 앞뒤 헛글자 제거."""
    t = text.strip()
    t = t.replace(" ", "")
    dotted = bool(re.search(r"(\.{2,}|…|‥|\.•|•\.|\.,|,\.)\W*$", t)) or bool(re.search(r"[A-Za-z0-9가-힣]\.$", t))
    t = re.sub(r"[.…‥•·,]+\W*$", "", t)
    t = t.strip(JUNK_EDGE + ".…‥")
    return t, dotted


def read_once(img, seat_cfg, pw):
    """한 번 읽기 → {'name', 'truncated', 'texts', 'dots', 'agree', 'box', 'fp'} 또는 None (글자 줄 없음)."""
    loc = locate(img, seat_cfg, pw)
    if loc is None:
        return None
    texts = _read_images(_ocr_images(img, loc["box"], loc["th"]))
    cleaned = [clean(t) for t in texts]
    dotted = sum(d for _, d in cleaned)
    name = vote([n for n, _ in cleaned])
    agree = sum(1 for n, _ in cleaned if n == name) / max(1, len(texts))
    truncated = bool(name) and (loc["dots"] >= 2 or dotted * 2 >= max(1, len(texts)))
    return {"name": name, "truncated": truncated, "texts": texts, "dots": loc["dots"], "agree": agree,
            "box": loc["box"], "fp": fingerprint(img, loc["box"])}


def _read_images(ims):
    """이미지 리스트 → OCR 문자열 리스트 (전처리 없이 그대로)."""
    import asyncio

    async def run():
        engine = ocr._engine()
        out = []
        for im in ims:
            res = await engine.recognize_async(ocr._bitmap(im))
            out.append(" ".join(line.text for line in res.lines))
        return out
    if ocr._engine() is None:
        return []
    return ocr.run_locked(run())


def display(name, truncated):
    return f"{name}{OUTER}" if truncated and name else name


# --- 좌석별 확정 ---------------------------------------------------------------------------------
FP_SAME = 0.70       # 지문 유사도가 이 이상이면 같은 글자 (실측 같은 닉네임 ≥0.80, 다른 닉네임 ≤0.42)
SAME_PIXELS = 2.0    # 서명 거리가 이 이하면 픽셀이 사실상 같음 → OCR 하지 않고 지난 결과를 한 번 더 읽은 것으로 침


class _Seat:
    __slots__ = ("sig", "read_sig", "streak", "candidate", "confirmed", "fp", "last")

    def __init__(self):
        self.sig = None          # 확정 때 닉네임 영역 서명
        self.read_sig = None     # 마지막으로 OCR 한 영역 서명
        self.streak = 0          # candidate 가 연속으로 읽힌 횟수
        self.candidate = None    # (name, truncated)
        self.confirmed = None    # (name, truncated)
        self.fp = None           # 확정/후보 글자 지문
        self.last = None         # 마지막 read_once 결과


class NameReader:
    """좌석별 닉네임 확정기. 백그라운드 스레드 하나에서 쓸 것. 어떤 예외도 밖으로 내지 않음.

    resolver(name, fp) -> name : 잘리지 않은 이름을 읽을 때마다 호출 (예: profiles.Store.resolve_name — 글자 지문이
                                 DB 의 닉네임과 같으면 그 이름으로 바꿔서 OCR 오타가 다른 사람으로 갈라지지 않게)
    on_confirm(key, name, truncated, fp) : 확정될 때 한 번 (예: Store.learn_fingerprint)
    """

    def __init__(self, confirm=CONFIRM_READS, budget=OCR_BUDGET, tol=stable.TOL, resolver=None, on_confirm=None):
        self.confirm, self.budget, self.tol = confirm, budget, tol
        self.resolver, self.on_confirm = resolver, on_confirm
        self.seats = {}
        self.session = 0
        self.last_ocr = 0
        self.errors = 0
        self._rr = 0

    def reset(self):
        """새 보정/새 테이블: 모든 좌석 기억(잘린 닉네임 포함)을 버림."""
        self.seats = {}
        self.session += 1

    def forget(self, key):
        self.seats.pop(key, None)

    def confirmed(self):
        return {k: s.confirmed for k, s in self.seats.items() if s.confirmed}

    def details(self):
        """디버그용 {key: {confirmed, candidate, streak, last_texts}}."""
        return {k: {"confirmed": s.confirmed, "candidate": s.candidate, "streak": s.streak,
                    "texts": (s.last or {}).get("texts")} for k, s in self.seats.items()}

    def read(self, img, calib, state=None):
        """img + calib (+ tablestate 결과로 빈자리 확인) → {seat_key: (name, truncated)} (확정만)."""
        self.last_ocr = 0
        try:
            return self._read(img, calib, state)
        except Exception:
            self.errors += 1
            return self.confirmed()

    @staticmethod
    def _seats_cfg(calib):
        import layout
        try:
            dyn = layout.slot_seats(calib)
        except Exception:
            dyn = None
        return dyn if dyn is not None else (calib.get("seats") or [])

    def _read(self, img, calib, state):
        seats_cfg = self._seats_cfg(calib)
        pw = float((calib.get("geom") or {}).get("pw") or 228.0)
        occupied = None
        if state and state.get("seats"):
            occupied = {s["key"]: s.get("occupied") for s in state["seats"]}
        todo = []
        for sc in seats_cfg:
            key = sc["key"]
            if occupied is not None and not occupied.get(key, False):
                self.seats.pop(key, None)  # 빈자리: 이름 버림 (잘린 이름도)
                continue
            st = self.seats.setdefault(key, _Seat())
            x0, y0, x1, y1, _ = _window(sc, pw, img.shape)
            crop = img[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            sig = stable.Signature(crop)
            if st.confirmed is not None and stable.distance(st.sig, sig) <= self.tol:
                continue  # 그대로
            todo.append((key, sc, st, sig))
        # 확정 안 된 좌석 우선, 예산만큼만 (밀리면 돌아가며)
        todo.sort(key=lambda t: t[2].confirmed is not None)
        if len(todo) > self.budget:
            self._rr = (self._rr + 1) % len(todo)
            todo = (todo[self._rr:] + todo[:self._rr])[:self.budget]
        for key, sc, st, sig in todo:
            if st.last is not None and st.read_sig is not None and stable.distance(st.read_sig, sig) <= SAME_PIXELS:
                r = st.last  # 같은 픽셀 → 같은 결과 (OCR 은 결정적)
            else:
                r = read_once(img, sc, pw)
                self.last_ocr += 1
                st.last, st.read_sig = r, sig
            self._update(key, st, r, sig)
        return self.confirmed()

    def _update(self, key, st, r, sig):
        if not r or not r["name"]:
            st.streak, st.candidate = 0, None
            return
        name, truncated, fp = r["name"], r["truncated"], r.get("fp")
        if not truncated and self.resolver is not None:
            try:
                name = self.resolver(name, fp) or name
            except Exception:
                pass
        got = (name, truncated)
        if st.confirmed is not None:
            if got == st.confirmed or (fp is not None and fp_similarity(fp, st.fp) >= FP_SAME):
                st.sig = sig  # 빛 고리 등으로 영역만 바뀜, 글자는 같음
                return
            st.confirmed = None  # 다른 닉네임 (자리 주인이 바뀜): 확정 취소하고 다시 3번
            st.candidate, st.streak = None, 0
        if got == st.candidate:
            st.streak += 1
        else:
            st.candidate, st.streak, st.fp = got, 1, fp
        if st.streak >= self.confirm:
            st.confirmed, st.sig, st.fp = got, sig, fp if fp is not None else st.fp
            st.streak = 0
            if self.on_confirm is not None:
                try:
                    self.on_confirm(key, got[0], got[1], st.fp)
                except Exception:
                    pass


_DEFAULT = NameReader()


def read(img, calib, state=None):
    return _DEFAULT.read(img, calib, state)
