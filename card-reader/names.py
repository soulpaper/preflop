"""좌석 닉네임 읽기 (Windows OCR, 한국어).

    nr = names.NameReader(resolver=store.resolve_name, on_confirm=store.learn_fingerprint)   # store 는 선택
    nr.reset()                                   # 새 보정 / 새 테이블 세션
    confirmed = nr.read(img, calib, state)       # {seat_key: (name, truncated)} — 확정된 것만
    # seat_key 는 tablestate 와 같음 (geom 있는 보정: 'seat_BL'.. / 'hero'). state 의 빈자리는 기억을 버림

    nr.sure()                                    # 확정 + 여러 전처리가 같은 글자로 읽은 좌석 (안내에 이름을 써도 됨)
    nr.fingerprints()                            # {seat_key: 글자 지문} (자리 주인이 바뀌었는지 볼 때)

닉네임은 스택 숫자 바로 위 한 줄 (연회색 글자). 위로는 아바타 아래쪽/카드 아이콘, 아래로는 스택 숫자가 붙어 있고,
차례인 사람은 빛 고리, 테이블 테두리(구리색/보라 펠트)가 글자 뒤로 지나가서 그대로 OCR 하면 잘 틀린다.
  1. 스택 글자 위 창에서 '채도 낮고 밝은' (가장 어두운 BGR 채널도 밝은) 픽셀만 글자로 보고, 예상 줄 높이에 가장 가까운
     글자 줄을 찾는다.
  2. 그 줄만 잘라 '흰 바탕 검은 글자' 이미지 5장(크기/보간/굵기/밝기 방식 다르게)을 읽는다. 밝기는 주로 BGR 최솟값
     ('min') — 글자 뒤 구리색 테두리·보라 펠트가 지워진다. 같은 글자열이 2장 이상 + 1등이 하나일 때만 이름
     (섞어서 만들지 않음), 3장 이상이면 '확실'(sure). 쇼다운 족보 표시('Q 스트레이트')는 이름 없음으로.
  3. 잘린 닉네임('User12...'): 줄 끝의 작은 점 3개(픽셀), 점 2개 + OCR 끝 점, 또는 OCR 다수가 끝에 '.', '…', '•' → truncated.
  4. 한 좌석의 닉네임은 3번(연속 프레임) 같게 읽혀야 확정 (픽셀이 그대로면 OCR 은 다시 안 하고 같은 결과로 셈,
     못 읽은 프레임은 세지도 끊지도 않음). 확정 뒤에는 stable.Signature 로 닉네임 영역이 바뀌었을 때만 다시 읽고,
     글자 지문(fingerprint)이 같으면 (빛 고리가 지나간 것 등) 그대로 둔다. 다른 글자는 확정을 지우지 않고 '도전자'로
     세어 CONFIRM_READS × REPLACE_FACTOR 번 연속일 때만 바꾼다 (족보·빛 고리·오독 몇 프레임으로 이름이 흔들리지 않게).
  5. 글자 지문: 줄 마스크를 높이 16px 로 정규화. 같은 닉네임 ≥0.80, 다른 닉네임 ≤0.42 (samples/ 실측).
     profiles.Store 에 저장해 두면 다음에 OCR 이 조금 다르게 읽어도 같은 이름으로 묶임 (resolver).

정확도 (한 번 읽기, samples/ 9장 70자리 + 실제 게임 26프레임 144자리; 틀림 = 엉뚱한 이름/잘림 표시가 틀림):
  예전  samples 맞음 47 · 틀림 8 · 못 읽음 15 / 게임 맞음 64 · 틀림 53 · 못 읽음 27
  지금  samples 맞음 47 · 틀림 2 · 못 읽음 21 / 게임 맞음 89 · 틀림 2 · 못 읽음 53  (틀린 4개는 모두 자모 하나 차이)
  확정 이름 (게임 26프레임 × 3번 읽기): 예전 좌석·프레임 362개 중 틀림 202, 확정 이름 바뀜 9번 → 지금 311개 중 틀림 46
  (모두 '뭐'→'워' 한 글자), 바뀜 0번. 못 읽는 것은 Windows OCR 이 짧은 한글 줄에 빈 결과를 주는 경우가 대부분.
좌석당 OCR 약 30ms.
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
MIN_CH_RATIO = 0.75              # 글자 픽셀의 가장 작은 BGR 채널 ≥ 밝기 문턱 × 이 값
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
    # 가장 작은 채널도 밝아야 글자 (구리색 테두리·보라 펠트·빛 고리는 한 채널이 어두움 → 점/글자로 잡히지 않게)
    low = win.min(axis=2).astype(np.int16)
    return grey & (val > thr) & (low > MIN_CH_RATIO * thr), thr


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


# OCR 용 전처리 (samples/ 9장 + 실제 게임 화면 26장, 214자리에서 52가지 중 고름).
# 'min' 밝기는 글자 뒤로 지나가는 구리색 테이블 테두리·보라 펠트를 지워서, 테두리가 지나가는 이름이 엉뚱한 글자로 읽히던 것을
# 줄인다. 'val' 은 예전 방식 (어두운 바탕에서 서로 보완).
OCR_MARGIN = (0.6, 0.4)                     # 글자 줄 박스 바깥 여백 (글자 높이 배)
OCR_VARIANTS = ((72, cv2.INTER_LANCZOS4, False, "min", False, 30), (56, cv2.INTER_CUBIC, True, "min", True, 30),
                (56, cv2.INTER_LANCZOS4, False, "min", True, 30), (72, cv2.INTER_LANCZOS4, False, "val", False, 30),
                (72, cv2.INTER_LANCZOS4, True, "val", False, 30))
# (글자 높이 px, 보간, 굵게, 밝기 방식, 글자 줄 밖 지우기, 흰 테두리 px)
BAND_PAD = 0.15


def _lum(big, mode):
    """밝기 (글자 = 밝은 무채색) → (밝기, 글자일 수 있는 픽셀, 글자색 추정에 쓸 픽셀).
    'min': BGR 중 가장 작은 값 — 구리색 테두리·보라 펠트·빛 고리처럼 색이 있는 밝은 것은 어둡게 나옴.
    'val': HSV 명도, 채도 높은 픽셀은 버림 (예전 방식)."""
    if mode == "min":
        lum = big.min(axis=2).astype(np.float32)
        return lum, np.ones(lum.shape, bool), np.ones(lum.shape, bool)
    hsv = cv2.cvtColor(big, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1].astype(np.float32), hsv[:, :, 2].astype(np.float32)
    return val, sat < TEXT_SAT + 20, sat < TEXT_SAT


def _ocr_images(img, box, th, variants=None):
    """글자 줄 → OCR 용 이미지 여러 장 (흰 바탕 검은 글자, 크기/보간/굵기/밝기 방식 다르게).
    찾은 글자 줄 밖(위 아바타 아래쪽, 아래 스택 글자, 옆 칩/카드 아이콘)은 바탕으로 지운다."""
    x0, y0, x1, y1 = box
    H, W = img.shape[:2]
    mx, my = OCR_MARGIN
    ax0, ay0 = int(max(0, x0 - mx * th)), int(max(0, y0 - my * th))
    ax1, ay1 = int(min(W, x1 + mx * th)), int(min(H, y1 + my * th))
    crop = img[ay0:ay1, ax0:ax1]
    out = []
    if crop.size == 0:
        return out
    pad = BAND_PAD * th
    bx0, bx1 = max(0.0, x0 - ax0 - pad), x1 - ax0 + pad
    by0, by1 = max(0.0, y0 - ay0 - pad), y1 - ay0 + pad
    for height, interp, thick, mode, band, border in (variants or OCR_VARIANTS):
        scale = height / max(1, th)
        big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=interp)
        lum, ok, pool = _lum(big, mode)
        keep = np.ones(lum.shape, bool)
        if band:
            keep[:] = False
            keep[int(by0 * scale):int(np.ceil(by1 * scale)), int(bx0 * scale):int(np.ceil(bx1 * scale))] = True
        bg = float(np.median(lum[keep])) if keep.any() else float(np.median(lum))
        pool = pool & keep
        fg = float(np.percentile(lum[pool], 98)) if pool.any() else 255.0
        soft = np.clip((lum - bg) / max(20.0, fg - bg), 0, 1) * (ok & keep)
        soft = (255 - 255 * soft).astype(np.uint8)
        _, im = cv2.threshold(soft, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if thick:
            k = max(1, int(height / 40)) + 1
            im = cv2.erode(im, np.ones((k, k), np.uint8))
        out.append(cv2.copyMakeBorder(im, border, border, border, border, cv2.BORDER_CONSTANT, value=255))
    return out


# --- 다수결 ---------------------------------------------------------------------------------------
VALID = re.compile(r"^[A-Za-z0-9가-힣_]+$")
HANGUL = re.compile(r"[가-힣]")
ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")
VOTE_MIN = 2        # 같은 글자열이 이만큼 (그리고 다른 어떤 후보보다 많이) 나와야 이름으로 씀
SURE_VOTES = 3      # 이만큼 같으면 '확실' (실측: 5장 중 3장 이상 같으면 124자리 중 틀린 것 2개 — 둘 다 자모 하나 차이)
# 쇼다운 때 닉네임 자리에 잠깐 뜨는 족보 ('Q 스트레이트', 'Q,J 투페어') — 이름이 아님
HAND_WORDS = ("하이카드", "노페어", "원페어", "투페어", "트리플", "스트레이트", "백스트레이트", "마운틴", "플러시", "플러쉬",
              "풀하우스", "포카드", "스트레이트플러시", "로열스트레이트플러시", "로티플", "스티플")
HAND_LABEL = re.compile(r"^[AKQJT0-9,\s]*(" + "|".join(HAND_WORDS) + r")$")


def _ok_name(n):
    # 기호가 섞인 것('경+남힐톁'), 한글과 영문/숫자가 섞인 것('머리0', '멀EI조이')은 오인식 → 버림
    # (한글+숫자 닉네임이 실제로 있으면 확정되지 않음 — 엉뚱한 이름이 붙는 것보다 나음)
    return bool(n) and bool(VALID.match(n)) and not (HANGUL.search(n) and ASCII_ALNUM.search(n))


def tally(names_):
    """정리된 이름 후보들 → (이름 또는 None, 그 이름의 표 수). 같은 글자열이 VOTE_MIN 번 이상이고 1등이 하나일 때만.
    예: ['홍길동', '홍길동', '흥길동', ''] → ('홍길동', 2). ['홍길동', '흥길동'] → (None, 0) — 섞어서 만들지 않음
    (예전 자모 다수결은 맞는 것 1개를 더 얻고 틀린 것 3개를 더 만들었음)."""
    cands = [n for n in names_ if _ok_name(n)]
    if not cands:
        return None, 0
    mc = Counter(cands).most_common(2)
    if mc[0][1] >= VOTE_MIN and (len(mc) == 1 or mc[0][1] > mc[1][1]):
        return mc[0]
    return None, 0


def vote(names_):
    return tally(names_)[0]


def hand_label(texts):
    """OCR 글자들이 쇼다운 족보 표시인가."""
    hits = 0
    for t in texts:
        if HAND_LABEL.match(re.sub(r"[\s.…‥•·]", "", str(t or ""))):
            hits += 1
    return hits * 2 >= max(1, len([t for t in texts if t])) and hits > 0


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
    dotted = bool(re.search(r"(\.{2,}|…|‥|\.•|•\.|\.,|,\.)\W*$", t)) or bool(re.search(r"[A-Za-z0-9가-힣][.•]$", t))
    t = re.sub(r"[.…‥•·,]+\W*$", "", t)
    t = t.strip(JUNK_EDGE + ".…‥")
    return t, dotted


def read_once(img, seat_cfg, pw):
    """한 번 읽기 → {'name', 'truncated', 'texts', 'dots', 'votes', 'sure', 'box', 'fp'} 또는 None (글자 줄 없음).
    sure: 여러 전처리 중 SURE_VOTES 장 이상이 같은 글자열로 읽음."""
    loc = locate(img, seat_cfg, pw)
    if loc is None:
        return None
    texts = _read_images(_ocr_images(img, loc["box"], loc["th"]))
    if hand_label(texts):
        name, votes = None, 0     # 쇼다운 족보 ('Q 스트레이트'): 이름 없음으로 (확정된 이름은 그대로 둠)
    else:
        name, votes = tally([clean(t)[0] for t in texts])
    dotted = sum(1 for t in texts if clean(t)[1])
    truncated = bool(name) and (loc["dots"] >= 3 or (loc["dots"] >= 2 and dotted >= 1) or dotted * 2 >= max(1, len(texts)))
    return {"name": name, "truncated": truncated, "texts": texts, "dots": loc["dots"], "votes": votes,
            "sure": bool(name) and votes >= SURE_VOTES, "box": loc["box"], "fp": fingerprint(img, loc["box"])}


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
REPLACE_FACTOR = 2   # 확정된 이름을 다른 이름으로 바꾸려면 CONFIRM_READS 의 이 배수만큼 연속으로 읽혀야 함
SAME_PIXELS = 2.0    # 서명 거리가 이 이하면 픽셀이 사실상 같음 → OCR 하지 않고 지난 결과를 한 번 더 읽은 것으로 침


class _Seat:
    __slots__ = ("sig", "read_sig", "streak", "candidate", "confirmed", "fp", "last", "cand_fp", "cand_sure", "sure")

    def __init__(self):
        self.sig = None          # 확정 때 닉네임 영역 서명
        self.read_sig = None     # 마지막으로 OCR 한 영역 서명
        self.streak = 0          # candidate 가 연속으로 읽힌 횟수 (못 읽은 프레임은 세지도 끊지도 않음)
        self.candidate = None    # (name, truncated) — 확정 전 후보, 또는 확정된 이름에 도전하는 다른 이름
        self.confirmed = None    # (name, truncated)
        self.fp = None           # 확정된 글자 지문
        self.last = None         # 마지막 read_once 결과
        self.cand_fp = None      # 후보 글자 지문
        self.cand_sure = False   # 후보가 한 번이라도 SURE_VOTES 표로 읽혔는지
        self.sure = False        # 확정된 이름이 한 번이라도 SURE_VOTES 표로 읽혔는지 (안내에 이름을 보여도 되는지)


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

    def sure(self):
        """확정됐고 여러 전처리가 같은 글자열로 읽은 좌석 키 집합 (안내에 닉네임을 보여도 되는 것)."""
        return {k for k, s in self.seats.items() if s.confirmed and s.sure}

    def fingerprints(self):
        """{seat_key: 확정된 닉네임 글자 지문} (메모리 전용 — 자리 주인이 바뀌었는지 볼 때)."""
        return {k: s.fp for k, s in self.seats.items() if s.confirmed and s.fp is not None}

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
            return  # 못 읽음 (빛 고리·족보 표시·OCR 빈 결과): 판단 보류 — 확정도 후보도 그대로
        name, truncated, fp = r["name"], r["truncated"], r.get("fp")
        if not truncated and self.resolver is not None:
            try:
                name = self.resolver(name, fp) or name
            except Exception:
                pass
        got, sure = (name, truncated), bool(r.get("sure"))
        if st.confirmed is not None:
            if got == st.confirmed or (fp is not None and fp_similarity(fp, st.fp) >= FP_SAME):
                st.sig = sig  # 빛 고리 등으로 영역만 바뀜, 글자는 같음
                st.candidate, st.streak = None, 0
                if got == st.confirmed and sure:
                    st.sure = True
                return
            # 다른 글자: 확정은 그대로 두고 도전자로 셈 (빛 고리·족보·오독 몇 프레임으로 이름이 흔들리지 않게)
        need = self.confirm if st.confirmed is None else self.confirm * REPLACE_FACTOR
        if got == st.candidate:
            st.streak += 1
            st.cand_sure = st.cand_sure or sure
        else:
            st.candidate, st.streak, st.cand_fp, st.cand_sure = got, 1, fp, sure
        if st.streak >= need:
            st.confirmed, st.sig = got, sig
            st.fp = st.cand_fp if st.cand_fp is not None else fp
            st.sure = st.cand_sure
            st.candidate, st.streak = None, 0
            if self.on_confirm is not None:
                try:
                    self.on_confirm(key, got[0], got[1], st.fp)
                except Exception:
                    pass


_DEFAULT = NameReader()


def read(img, calib, state=None):
    return _DEFAULT.read(img, calib, state)
