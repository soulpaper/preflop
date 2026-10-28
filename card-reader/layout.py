"""게임 창 스크린샷 한 장에서 숫자/카드 영역을 자동으로 찾는다 ([자동 지정]).

찾는 것 (모두 이미지 좌표 [x, y, w, h]):
  pot_total  POT 라벨 숫자 (= 가운데 팟 + 바닥에 깔린 벳 전부)
  pill       POT 라벨 아래 알약 모양 (지난 스트리트까지 모인 팟). 참고용, 팟 값으로 쓰지 않음
  my_stack   화면 아래 가운데에 가장 가까운 닉네임 밑 스택
  opp1..N    상대 스택, 내 왼쪽부터 시계 방향
  board      보드 카드 5장 자리 (카드가 없으면 알약 위치 기준 비율로 추정)
  hero       내 카드 자리 (앞면 기울어진 카드 / 뒷면 모두 들어가게)
  bets       바닥(펠트)에 깔린 벳 숫자 (참고용)
  blinds     왼쪽 위 'SB : 50 / BB : 100 / Ante 15' (확실히 읽힌 경우만)

방법
  1. 창 전체를 여러 배율로 OCR 해서 글자 줄을 모은다 (배율마다 놓치는 줄이 달라 합친다).
     정확한 글자보다 '숫자+BB' 모양과 배치로 판단한다.
  2. 숫자 후보는 픽셀로 글자 경계를 다시 잡고, 프레임마다 쓰는 것과 같은 방식(ocr.read_texts + resolve)으로
     다시 읽어 확인한다.
  3. POT 라벨 = 바로 아래에 알약 모양이 있는 숫자. 알약 위치/크기가 테이블 배치의 기준이 된다.
  4. 스택 = 바로 위에 닉네임 줄이 있는 숫자. 벳 = 펠트 위 칩 아래 숫자 (닉네임 없음).
"""
import asyncio
import math
import re

import cv2
import numpy as np

import ocr

# --- 샘플에서 잰 상대 위치. 단위: 알약 폭(pw), 첫 샘플에서 228px -------------------------------
BOARD_FROM_PILL = (-1.07, 0.06, 2.14, 0.56)    # 보드 (x0, y0, w, h): x는 알약 중심, y는 알약 아래 끝 기준
BOARD_CARD_H = 0.50                             # 보드 카드 높이
HERO_FROM_STACK = (0.32, -0.71, 0.86, 0.61)     # 내 카드 (x0, y0, w, h): 내 스택 글자 중심 기준 (앞면/뒷면 모두 포함)
CALL_FROM_STACK = (1.77, 0.41)                  # 콜 버튼 금액 글자 중심: 내 스택 글자 중심 기준
RING = (0.0, 0.50, 2.10, 1.20)                  # 좌석이 놓인 타원 (cx, cy, a, b): 알약 중심 기준
STACK_RING = (0.88, 1.30)                       # 스택: 타원 반지름 비율 이 범위 (닉네임이 보이면 0.75~1.45)
PILL_ASPECT = (3.5, 7.0)                        # 알약 폭/높이
OCR_SCALES = (1.0, 1.5, 2.0)
BASE_WIDTH = 1190                               # 이 폭 기준으로 OCR 배율을 맞춤

HANGUL = re.compile(r"[가-힣]")
LETTER = re.compile(r"[A-Za-z가-힣]")


# --- 전체 OCR -------------------------------------------------------------------
def _rotate_back(x, y, angle, cx, cy):
    """OCR이 기울기를 보정한 좌표계 → 원래 이미지 좌표."""
    t = math.radians(angle)
    dx, dy = x - cx, y - cy
    return cx + dx * math.cos(t) - dy * math.sin(t), cy + dx * math.sin(t) + dy * math.cos(t)


async def _recognize(images):
    engine = ocr._engine()
    out = []
    for im in images:
        out.append(await engine.recognize_async(ocr._bitmap(im)))
    return out


def _groups_from_result(result, scale, shape):
    """OCR 결과 → [[(단어, 박스)]]. 줄 안에서 멀리 떨어진 단어는 다른 묶음으로 나눔.

    OCR이 화면이 기울었다고 판단하면(text_angle) 좌표가 회전된 채로 나오므로 되돌린다.
    이 경우 박스가 부정확해서(세로로 부풀거나 글자가 뒤집혀 읽힘) 다른 배율 결과가 있으면 그쪽을 쓴다.
    """
    h, w = shape[:2]
    angle = result.text_angle or 0.0
    groups = []
    for line in result.lines:
        words = []
        for word in line.words:
            r = word.bounding_rect
            x0, y0, x1, y1 = r.x, r.y, r.x + r.width, r.y + r.height
            if abs(angle) > 0.05:
                pts = [_rotate_back(px, py, angle, w / 2, h / 2) for px, py in
                       ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
                x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
                x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
            words.append((word.text, (x0 / scale, y0 / scale, x1 / scale, y1 / scale)))
        if not words:
            continue
        words.sort(key=lambda wb: wb[1][0])
        current = [words[0]]
        for wb in words[1:]:
            prev = current[-1][1]
            height = max(prev[3] - prev[1], wb[1][3] - wb[1][1])
            if wb[1][0] - prev[2] > 0.9 * height:
                groups.append(current)
                current = []
            current.append(wb)
        groups.append(current)
    return groups


def _box_union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def read_lines(img, scales=OCR_SCALES):
    """창 전체를 여러 배율로 OCR → [{"box", "texts", "words"}] (같은 자리의 줄은 하나로 합침)."""
    base = min(2.5, max(0.6, BASE_WIDTH / img.shape[1]))
    images = [cv2.resize(img, None, fx=base * s, fy=base * s, interpolation=cv2.INTER_CUBIC) for s in scales]
    results = asyncio.run(_recognize(images))
    merged = []
    # 기울기 보정이 안 된(정상) 결과부터 넣어서 박스는 정상 결과를 우선 사용
    order = sorted(zip(images, results, scales), key=lambda r: abs(r[1].text_angle or 0.0) > 0.05)
    for im, res, s in order:
        rotated = abs(res.text_angle or 0.0) > 0.05
        for words in _groups_from_result(res, base * s, im.shape):
            text = " ".join(w for w, _ in words)
            box = _box_union([b for _, b in words])
            for g in merged:
                if _iou(g["box"], box) > 0.3 or (rotated and _contains(box, g["box"])):
                    g["texts"].append(text)
                    if not rotated:
                        g["variants"].append(words)
                    break
            else:
                merged.append({"box": box, "texts": [text], "variants": [words], "rotated": rotated})
    return merged


def _contains(outer, inner):
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


# --- 숫자 모양 판별 ----------------------------------------------------------------
def _normalize(word):
    t = word.strip("~•“”\"'`:;_-—「」|")
    t = t.replace(",", "")
    t = re.sub(r"[Il|](?=[\d.]*[B83]B)|(?<=[\d.])[Il|]|[Il|](?=[\d.])", "1", t)
    t = re.sub(r"[Oo](?=[\d.]*[B83]B)|(?<=[\d.])[Oo]|[Oo](?=[\d.])", "0", t)
    return t


NUMERIC = re.compile(r"\d*\.?\d+(?:BB|B|8B|3B|B8|B3)?|\d+(?:만|안|한|인|민|Zh|2h)\d{0,4}|\d{1,4}")


def is_numeric_word(word):
    return bool(NUMERIC.fullmatch(_normalize(word)))


def is_label_word(word):
    """POT 배지처럼 숫자 앞에 붙어도 되는 단어 (글자가 없거나 POT)."""
    t = word.strip()
    return not re.search(r"[A-Za-z가-힣0-9]", t) or re.fullmatch(r"[Pp][O0o][Tt]", t) is not None


def numeric_box(variants):
    """한 자리의 OCR 결과들 중 '숫자(+단위)'로 읽힌 단어들의 박스. 숫자가 아니면 None."""
    best = None
    for words in variants:
        nums = [i for i, (w, _) in enumerate(words) if is_numeric_word(w)]
        if not nums:
            continue
        others = [w for i, (w, _) in enumerate(words) if i not in nums]
        if not all(is_label_word(w) for w in others):
            continue
        if nums[0] > 0 and not all(is_label_word(words[i][0]) for i in range(nums[0])):
            continue
        box = _box_union([words[i][1] for i in nums])
        if best is None or box[2] - box[0] > best[2] - best[0]:
            best = box
    return best


def has_letters(texts):
    return any(len(LETTER.findall(t)) >= 1 for t in texts)


# --- 픽셀로 글자 경계 다듬기 ------------------------------------------------------------
def _runs(mask_1d, gap=0):
    """True 구간들 [(start, end_exclusive)] (gap 이하의 빈칸은 이어 붙임)."""
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


def text_mask(gray):
    """밝은 글자 픽셀 (배경보다 확실히 밝은 것)."""
    bg = float(np.median(gray))
    hi = float(np.percentile(gray, 99))
    thr = max(bg + 0.45 * (hi - bg), bg + 35)
    return gray > thr


def refine_text(gray, box):
    """OCR 박스 → 실제 글자 픽셀에 딱 맞는 박스 (x0, y0, x1, y1). 실패하면 원래 박스."""
    H, W = gray.shape
    x0, y0, x1, y1 = box
    h = max(4.0, y1 - y0)
    wx0, wx1 = int(max(0, x0 - 1.2 * h)), int(min(W, x1 + 1.2 * h))
    wy0, wy1 = int(max(0, y0 - 0.5 * h)), int(min(H, y1 + 0.5 * h))
    win = gray[wy0:wy1, wx0:wx1]
    if win.size == 0:
        return box
    mask = text_mask(win)
    # 세로: OCR 박스 가로 범위 안에서, 박스 중심 줄을 포함하는 연속된 줄
    cx0, cx1 = int(x0) - wx0, int(math.ceil(x1)) - wx0
    rows = mask[:, max(0, cx0):max(cx0 + 1, cx1)].sum(1) > 0
    runs = _runs(rows)
    cy = (y0 + y1) / 2 - wy0
    if not runs:
        return box
    ry0, ry1 = min(runs, key=lambda r: 0 if r[0] <= cy < r[1] else min(abs(r[0] - cy), abs(r[1] - cy)))
    # 가로: 그 줄들 안에서, OCR 박스와 겹치는 글자 덩어리. 글자 사이 간격(0.35h 이하)으로 이어진 것만
    # 박스 밖으로 넓힘 (OCR이 앞자리를 빠뜨린 경우 '.8BB' → '49.8BB'). POT 배지 같은 옆 물체는 간격이 더 큼
    cols = mask[ry0:ry1].sum(0) > 0
    runs = [r for r in _runs(cols, gap=max(2, int(0.35 * h))) if r[1] > cx0 and r[0] < cx1]
    if not runs:
        return box
    rx0 = max(runs[0][0], int(cx0 - 2.5 * h))
    rx1 = min(runs[-1][1], int(cx1 + 2.5 * h))
    tight = (wx0 + rx0, wy0 + ry0, wx0 + rx1, wy0 + ry1)
    th = tight[3] - tight[1]
    if th < 0.4 * h or th > 1.6 * h:
        return box
    return tight


# --- 알약 (가운데 팟) ------------------------------------------------------------------
def find_pill(img, gray, label):
    """POT 라벨 박스 바로 아래 알약 모양을 찾아 (x0, y0, x1, y1). 없으면 None."""
    H, W = gray.shape
    x0, y0, x1, y1 = label
    th = y1 - y0
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    best = None
    for dy in (1.4, 2.0, 2.8, 3.6):
        for dx in (-2.5, -1.5, 1.0, 2.0, 3.0):
            sx = int(x1 + dx * th) if dx > 0 else int(x0 + dx * th)
            sy = int(y1 + dy * th)
            if not (0 <= sx < W and 0 <= sy < H):
                continue
            mask = np.zeros((H + 2, W + 2), np.uint8)
            flags = 4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8)
            cv2.floodFill(blur, mask, (sx, sy), 0, 3, 3, flags)
            ys, xs = np.nonzero(mask[1:-1, 1:-1])
            if len(xs) == 0:
                continue
            bx0, bx1, by0, by1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
            bw, bh = bx1 - bx0, by1 - by0
            if not (2.0 * th <= bh <= 5.5 * th and PILL_ASPECT[0] <= bw / bh <= PILL_ASPECT[1]):
                continue
            if not (y1 - 0.2 * th <= by0 <= y1 + 2.0 * th):
                continue
            if bx1 < x0 or bx0 > x1:  # 라벨과 가로로 겹쳐야 함
                continue
            fill = len(xs) / float(bw * bh)
            if fill < 0.6:
                continue
            cand = (int(bx0), int(by0), int(bx1), int(by1))
            if best is None or bw * bh > (best[2] - best[0]) * (best[3] - best[1]):
                best = cand
    return best


# --- 영역 만들기 -----------------------------------------------------------------------
def _clip(rect, shape):
    x, y, w, h = [int(round(v)) for v in rect]
    H, W = shape[:2]
    x0, y0 = min(max(0, x), W - 1), min(max(0, y), H - 1)
    x1, y1 = min(W, x + w), min(H, y + h)
    return [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]


def number_rect(box, shape, min_chars=0.0, pad=0.6):
    """글자 박스 → 프레임마다 읽을 영역 [x, y, w, h]. 글자에 딱 맞게 + 작은 여백.
    옆에 아바타 테두리/레일/말풍선이 있어서 넓게 잡으면 그 조각을 '1' 등으로 읽음 → 가로 여백은 글자 높이의 pad배."""
    x0, y0, x1, y1 = box
    th = y1 - y0
    m = max(2.0, 0.35 * th)  # 위 닉네임과는 0.4th 정도 떨어져 있음
    cx = (x0 + x1) / 2
    hw = max((x1 - x0) / 2 + pad * th, min_chars * th)
    return _clip((cx - hw, y0 - m, 2 * hw, th + 2 * m), shape)


def _crop(img, rect):
    x, y, w, h = rect
    return img[y:y + h, x:x + w]


def _letters(text):
    return len(LETTER.findall(text))


def _center(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


# --- 블라인드 (왼쪽 위 'SB : 50 / BB : 100 / Ante 15') ----------------------------------------
BLIND_KEYS = (("sb", re.compile(r"^[S5$][B8]")), ("bb", re.compile(r"^[^A-Za-z0-9]?[B8][B8]")),
              ("ante", re.compile(r"^[Aa]nte")))


def _shear(img, k=0.25):
    """기울어진(이탤릭) 글자를 바로 세움. 그대로 읽으면 이탤릭 '1'을 '7'로 읽음 (BB : 100 → 700)."""
    h, w = img.shape[:2]
    m = np.float32([[1, k, 0], [0, 1, 0]])
    return cv2.warpAffine(img, m, (w + int(k * h) + 1, h), borderMode=cv2.BORDER_REPLICATE)


def _blind_value(text):
    t = text.replace(" ", "").replace(",", "")
    t = re.sub(r"^.*?(?:[S5$][B8]|[B8][B8]|[Aa]nte)[^0-9IlO]*", "", t)
    t = re.sub(r"[Il|]", "1", t)
    t = re.sub(r"[Oo]", "0", t)
    m = re.fullmatch(r"(\d+)만(\d{1,4})?|(\d+)", t)
    if not m:
        return None
    if m.group(3):
        return int(m.group(3))
    return int(m.group(1)) * 10000 + int(m.group(2) or 0)


def read_blinds(img, lines):
    """{'sb', 'bb', 'ante'} (칩, 정수). 세 줄 모두 2개 이상 전처리에서 같게 읽히고 SB×2 = BB일 때만, 아니면 None."""
    from collections import Counter
    H, W = img.shape[:2]
    crops = {}
    for g in lines:
        x0, y0, x1, y1 = g["box"]
        if x0 > 0.2 * W or y1 > 0.35 * H:
            continue
        for key, pat in BLIND_KEYS:
            if key not in crops and any(pat.search(t.strip()) for t in g["texts"]):
                th = y1 - y0
                rect = _clip((x0 - 0.3 * th, y0 - 0.25 * th, x1 - x0 + 2.0 * th, 1.5 * th), img.shape)
                crops[key] = _shear(_crop(img, rect))
    if len(crops) < 3:
        return None
    out = {}
    for key, texts in ocr.read_texts(crops, suppress_warm=False).items():
        votes = Counter(v for v in (_blind_value(t) for t in texts) if v is not None)
        if not votes or votes.most_common(1)[0][1] < 2:
            return None
        out[key] = votes.most_common(1)[0][0]
    if out["sb"] * 2 != out["bb"] or out["ante"] > out["bb"]:
        return None
    return out


# --- 전체 --------------------------------------------------------------------------------
def calibrate(img):
    """게임 창 이미지(BGR) → 찾은 영역들 (이미지 좌표 [x, y, w, h]).

    키: pot_total, pill, my_stack, opp1.., board, hero, call (모두 있을 때만)
    그 외: values {키: (값, 단위)}, found {키: 화면에서 직접 찾았는지(False = 비율로 추정)},
          bets [(영역, 값)], blinds {'sb','bb','ante'} 또는 None, board_cards, unit
    """
    import reader  # 카드 찾기 함수 재사용 (순환 import 방지로 여기서)

    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    lines = read_lines(img)

    # 1. 숫자 후보 + 글자 경계 다듬기
    cands = []
    for g in lines:
        nb = numeric_box(g["variants"])
        if nb is None:
            continue
        box = refine_text(gray, nb)
        if any(_iou(box, c["box"]) > 0.5 for c in cands):
            continue
        cands.append({"box": box, "texts": g["texts"]})
    # 전체 OCR이 스택 줄을 놓친 경우: 닉네임(글자 2개 이상) 바로 아래를 픽셀로 찾아 후보에 추가
    for g in lines:
        if numeric_box(g["variants"]) is not None or max(_letters(t) for t in g["texts"]) < 2:
            continue
        x0, y0, x1, y1 = g["box"]
        th = y1 - y0
        if any(y1 - 0.2 * th <= c["box"][1] <= y1 + 1.2 * th and x0 - th <= _center(c["box"])[0] <= x1 + th
               for c in cands):
            continue
        cx = (x0 + x1) / 2
        guess = (cx - 0.3 * th, y1 + 0.5 * th, cx + 0.3 * th, y1 + 1.4 * th)
        box = refine_text(gray, guess)
        if box != guess and box[1] > y1 and not any(_iou(box, c["box"]) > 0.3 for c in cands):
            cands.append({"box": box, "texts": ["(닉네임 아래)"]})

    # 2. POT 라벨 = 바로 아래 알약이 있는 숫자
    label = pill = None
    for c in sorted(cands, key=lambda c: c["box"][1]):
        p = find_pill(img, gray, c["box"])
        if p is not None:
            label, pill = c, p
            break
    if label is None:  # 알약을 못 찾으면 'POT' 글자가 붙은 숫자
        label = next((c for c in cands if any(re.search(r"P[O0]T", t) for t in c["texts"])), None)

    # 3. 후보마다 실제로 쓸 영역을 정하고, 프레임마다 쓰는 방식 그대로 다시 읽어 값 확인
    for c in cands:
        c["rect"] = number_rect(c["box"], img.shape)
    if label is not None:  # POT 라벨 숫자는 왼쪽 정렬(오른쪽으로 자람). 왼쪽 POT 배지는 제외
        x0, y0, x1, y1 = label["box"]
        th = y1 - y0
        m = max(2.0, 0.22 * th)  # POT 라벨은 위아래 여백이 작을 때 더 잘 읽힘 (배율별 실험)
        label["rect"] = _clip((x0 - 0.25 * th, y0 - m, max(x1 - x0 + 0.6 * th, 6.0 * th), th + 2 * m), img.shape)
    texts = ocr.read_texts({i: _crop(img, c["rect"]) for i, c in enumerate(cands)})
    unit = ocr.unit_hint(texts.values())
    for i, c in enumerate(cands):
        c["value"] = ocr.resolve(texts[i], unit)
        c["read"] = texts[i]
    th_med = float(np.median([c["box"][3] - c["box"][1] for c in cands])) if cands else 14.0

    # 4. 테이블 기준 = 알약 (못 찾으면 글자 크기와 창 크기로 대략)
    if pill is not None:
        pw = pill[2] - pill[0]
        pcx, pcy = _center(pill)
    else:
        pw = th_med * 228 / 14.0
        pcx, pcy = W / 2, H * 0.4
    rcx, rcy = pcx + RING[0] * pw, pcy + RING[1] * pw
    ra, rb = RING[2] * pw, RING[3] * pw

    def ring_r(box):
        cx, cy = _center(box)
        return math.hypot((cx - rcx) / ra, (cy - rcy) / rb)

    def name_above(box):
        """바로 위에 닉네임 줄(글자 2개 이상)이 있는지. 칩 무늬는 보통 글자 1개 이하로 읽힘."""
        x0, y0, x1, y1 = box
        th = y1 - y0
        for g in lines:
            if max(_letters(t) for t in g["texts"]) < 2:
                continue
            gx, _ = _center(g["box"])
            if y0 - 1.4 * th <= g["box"][3] <= y0 + 0.2 * th and x0 - th <= gx <= x1 + th:
                return True
        return False

    # 5. 분류: 스택(닉네임 아래, 좌석 타원 위) / 벳(펠트 안쪽, 닉네임 없음) / 기타(콜 버튼 등)
    stacks, bets, others = [], [], []
    for c in cands:
        if c is label or c["value"] is None:
            continue
        cx, cy = _center(c["box"])
        if pill is not None and pill[0] <= cx <= pill[2] and pill[1] <= cy <= pill[3]:
            continue  # 알약 안 숫자
        if c["value"][1] not in ("BB", "만") and not (c["value"][1] is None and unit == "만"):
            continue
        if c["box"][3] - c["box"][1] > 1.6 * th_med:
            continue  # 글자가 너무 큼 (카드 숫자 등)
        r = ring_r(c["box"])
        c["named"] = name_above(c["box"])
        if STACK_RING[0] <= r <= STACK_RING[1] or (c["named"] and 0.75 <= r <= 1.45):
            stacks.append(c)
        elif r < STACK_RING[0] and not c["named"]:
            bets.append(c)
        else:
            others.append(c)

    result = {"debug": [(c["texts"][0], [int(v) for v in c["box"]], c["value"], round(ring_r(c["box"]), 2),
                         c.get("named")) for c in cands], "values": {}, "found": {}, "bets": [], "blinds": None, "board_cards": 0, "unit": unit}

    def put(key, rect, value=None, found=True):
        result[key] = [int(v) for v in rect]
        result["found"][key] = found
        if value is not None:
            result["values"][key] = value

    if label is not None:
        put("pot_total", label["rect"], label["value"])
    if pill is not None:
        put("pill", (pill[0], pill[1], pill[2] - pill[0], pill[3] - pill[1]))

    # 6. 콜 버튼 금액 (내 차례에만 보임): 내 스택보다 오른쪽 아래, 닉네임 없는 숫자
    hero = None
    if stacks:
        bottom = (rcx, rcy + rb)
        hero = min(stacks, key=lambda c: math.hypot(_center(c["box"])[0] - bottom[0], _center(c["box"])[1] - bottom[1]))
        hx, hy = _center(hero["box"])
        call = next((c for c in others + stacks if _center(c["box"])[0] > hx + 0.8 * pw
                     and _center(c["box"])[1] > hy + 0.15 * pw and not c["named"]), None)
        if call is not None:
            if call in stacks:
                stacks.remove(call)
            put("call", number_rect(call["box"], img.shape, min_chars=2.5), call["value"])
        else:  # 안 보이면 샘플에서 잰 위치 (금액 글자 크기 ≈ 내 스택 글자의 0.82배)
            cth = 0.82 * (hero["box"][3] - hero["box"][1])
            ccx, ccy = hx + CALL_FROM_STACK[0] * pw, hy + CALL_FROM_STACK[1] * pw
            put("call", number_rect((ccx - 1.7 * cth, ccy - cth / 2, ccx + 1.7 * cth, ccy + cth / 2), img.shape,
                                    min_chars=2.5), found=False)

        # 7. 내 스택 = 테이블 아래 가운데에 가장 가까운 스택, 상대는 내 왼쪽부터 시계 방향
        put("my_stack", hero["rect"], hero["value"])

        def clockwise(c):
            cx, cy = _center(c["box"])
            return math.atan2(-(cx - rcx) / ra, (cy - rcy) / rb) % (2 * math.pi)
        for i, c in enumerate(sorted((c for c in stacks if c is not hero), key=clockwise), 1):
            put(f"opp{i}", c["rect"], c["value"])

        # 8. 내 카드: 내 스택 기준 (앞면은 기울어져 더 넓고 아래로 내려옴 → 앞면/뒷면 모두 들어가는 크기)
        x, y, w, h = HERO_FROM_STACK
        put("hero", _clip((hx + x * pw, hy + y * pw, w * pw, h * pw), img.shape), found=True)
    result["bets"] = [(c["rect"], c["value"]) for c in bets]

    # 9. 보드: 카드가 보이면 첫 카드 위치에 맞추고, 아니면 알약 기준 비율
    if pill is not None:
        bx, by, bw, bh = BOARD_FROM_PILL
        board = [pcx + bx * pw, pill[3] + by * pw, bw * pw, bh * pw]
        search = _clip((board[0] - 0.15 * pw, board[1] - 0.1 * pw, board[2] + 0.3 * pw, board[3] + 0.2 * pw),
                       img.shape)
        rects = []
        for mv in reader.CARD_MIN_VALUE:
            found = [r for r in reader._card_rects(_crop(img, search), mv)
                     if 0.75 * BOARD_CARD_H * pw <= r[3] <= 1.25 * BOARD_CARD_H * pw]
            if len(found) > len(rects):
                rects = found
        if rects:  # 첫 장은 항상 맨 왼쪽 자리
            board[0] = search[0] + min(r[0] for r in rects) - 0.06 * pw
            board[1] = search[1] + min(r[1] for r in rects) - 0.03 * pw
            result["board_cards"] = len(rects)
        put("board", _clip(board, img.shape), found=bool(rects))

    result["blinds"] = read_blinds(img, lines)
    return result


def summary(res):
    """토스트용 한 줄. 예: '보드·내 카드·팟·내 스택·상대 5명'."""
    parts = [name for key, name in (("board", "보드"), ("hero", "내 카드"), ("pot_total", "팟"),
                                    ("my_stack", "내 스택")) if key in res]
    n = sum(1 for k in res if k.startswith("opp"))
    if n:
        parts.append(f"상대 {n}명")
    if res.get("found", {}).get("call"):
        parts.append("콜 금액")
    return "·".join(parts) if parts else "찾은 영역 없음"
