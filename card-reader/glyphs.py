"""카드 crop에서 숫자/무늬 글리프를 분리해 숫자와 무늬를 따로 인식 (통째로 학습하지 않은 카드용).

reader.Matcher 의 2차 단계. 실제 템플릿에서 숫자/무늬 원형을 모아 두고, 처음 보는 카드도
숫자는 다른 무늬의 같은 숫자에서, 무늬는 다른 숫자의 같은 무늬에서 찾아 조합한다 (예: 6♦ + A♣ → 6♣).

- 글리프 모양만 비교 (잉크 세기 알파를 bbox에 맞춰 정규화 → 색/밝기/가로 배율 무관)
- 잉크 색이 무늬와 맞아야 함 (빨강 = d/h, 검정 = c/s)
- 위가 잘린 내 카드(코너 숫자 없음), 아래가 잘린 큰 숫자는 보이는 부분만 비교
- 실제 샘플이 없는 숫자는 글꼴(Bahnschrift) 원형을 경쟁자로만 둔다: 그 숫자가 이기면 '모름'

    python glyphs.py --fonts   # font_glyphs/*.png 생성 (pillow 필요, 한 번만)
"""
import sys
from pathlib import Path

import cv2
import numpy as np

BASE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
FONT_DIR = BASE / "font_glyphs"
FONT_PATH = r"C:\Windows\Fonts\bahnschrift.ttf"
FONT_AXES = [550, 100]  # 실제 숫자 2-6,8,9 와 가장 비슷한 굵기

RANKS = "23456789TJQKA"
SUIT_COLOR = {"c": "black", "s": "black", "d": "red", "h": "red"}
SCALE = 2
WORK = (52 * SCALE, 72 * SCALE)  # 분석 해상도 (정규화 크기 x2)
VEC = (16, 22)                   # 글리프 비교 크기 (w, h)
SUIT_RATIO = 0.55   # 코너 무늬 높이 / 큰 숫자 높이 (실측 0.51~0.58)
CORNER_K = 2.0      # 큰 숫자 높이 / 코너 숫자 높이 (실측 2.03)

# 검증(서로 다른 카드끼리 leave-one-out)에서 오인식 0건이 되도록 정한 값
RANK_MIN, RANK_MARGIN = 0.85, 0.15  # 실측: 정답 0.907 이상(차이 0.24 이상), 다른 숫자 최대 0.75
# 보조 기준: 점수가 조금 낮아도 2등과 차이가 압도적이면 인정 (많이 기울어진 10 등).
# 정답 숫자를 원형에서 통째로 뺀 검증에서 틀린 1등은 점수 최대 0.731, 차이 최대 0.223
RANK_MIN_WIDE, RANK_MARGIN_WIDE = 0.75, 0.35
SUIT_MIN, SUIT_MARGIN = 0.90, 0.30  # 무늬 margin = 판별값 t (1 = 1등 무늬 평균, 0 = 두 무늬 중간). 실측: 정답 t 0.44 이상, 틀린 무늬 점수 최대 0.55


def card_face(face):
    """밝은 카드 면 마스크 → 가장 큰 면의 볼록 껍질(글자 구멍 포함). 면을 못 찾으면 전체."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats(face.astype(np.uint8))
    if n <= 1:
        return np.ones_like(face, bool)
    biggest = (lab == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))).astype(np.uint8)
    pts = cv2.findNonZero(biggest)
    hull = np.zeros_like(biggest)
    cv2.fillConvexPoly(hull, cv2.convexHull(pts), 1)
    # 카드 테두리의 어두운 가장자리 픽셀이 글자로 잡히지 않게 조금 안쪽으로
    hull = cv2.erode(hull, np.ones((2 * SCALE + 1, 2 * SCALE + 1), np.uint8))
    return hull > 0


def segment(img):
    """crop → {'color', 'parts': {이름: (alpha crop, cut dict)}} / None.

    부위: big(큰 숫자), corner_rank(코너 숫자), corner_suit(코너 무늬)."""
    img = cv2.resize(img, WORK, interpolation=cv2.INTER_CUBIC if img.shape[1] < WORK[0] else cv2.INTER_AREA)
    H, W = img.shape[:2]
    b, g, r = [img[:, :, i].astype(np.float32) for i in range(3)]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1].astype(np.float32), hsv[:, :, 2].astype(np.float32)
    low = s < 45
    face_v = float(np.percentile(v[low], 90)) if low.any() else 220.0
    inside = card_face(low & (v > 0.72 * face_v))
    # 강한 잉크 (면 밝기 기준 상대 판정 → 어둡게 처리된 카드도 동작). 기울어진 카드의 crop 모서리에 들어온
    # 테이블/이펙트(아바타 링 등)는 카드 면 바깥이므로 제외
    red = (r - g > 0.2 * face_v) & (r - b > 0.13 * face_v) & (r > 0.3 * face_v) & inside
    black = (v < 0.5 * face_v) & ((s < 90) | (v < 0.3 * face_v)) & inside
    color = "red" if red.sum() > black.sum() else "black"
    ink = red if color == "red" else black
    # 잉크 세기 (0~1): 빨강은 r-g, 검정은 어두운 정도
    if color == "red":
        strength = r - g
        weak = (r - g > 0.07 * face_v) & (r - b > 0.04 * face_v)
    else:
        strength = face_v - v
        weak = (v < 0.8 * face_v) & (s < 110)
    if not ink.any():
        return None
    alpha = np.clip(strength / max(float(np.median(strength[ink])), 1.0), 0, 1) * weak

    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8))
    comps = [(i, *stats[i][:4]) for i in range(1, n) if stats[i][4] >= 5 * SCALE ** 2 and stats[i][3] >= 3 * SCALE
             and not (min(stats[i][2], stats[i][3]) <= 2 * SCALE and (stats[i][0] == 0 or stats[i][0] + stats[i][2] == W))]
    if not comps:
        return None
    # 큰 숫자 = 면적 최대 성분 + 같은 높이대에서 옆에 붙은 성분('10'의 1)
    seed = max(comps, key=lambda c: stats[c[0]][4])
    big = [c for c in comps if c is seed or (
        c[4] >= 0.6 * seed[4] and min(c[2] + c[4], seed[2] + seed[4]) - max(c[2], seed[2]) > 0.5 * c[4]
        and c[1] + c[3] / 2 > seed[1] - 1.2 * seed[3])]
    bx0 = min(c[1] for c in big)
    corner = [c for c in comps if c not in big and c[1] + c[3] / 2 < bx0 and c[4] >= 0.12 * seed[4]]
    rows = []  # 코너 성분을 세로 줄로 묶음: 위 = 숫자, 아래 = 무늬
    for c in sorted(corner, key=lambda c: c[2]):
        if rows and c[2] < rows[-1]["y1"] - 0.3 * c[4]:
            rows[-1]["c"].append(c)
            rows[-1]["y1"] = max(rows[-1]["y1"], c[2] + c[4])
        else:
            rows.append({"c": [c], "y1": c[2] + c[4]})
    ids = {"big": big}
    if len(rows) >= 2:
        ids["corner_rank"], ids["corner_suit"] = rows[-2]["c"], rows[-1]["c"]
    elif len(rows) == 1:
        ids["corner_suit"] = rows[0]["c"]

    pad = np.ones((2 * SCALE + 1, 2 * SCALE + 1), np.uint8)
    parts = {}
    for k, cs in ids.items():
        x0, y0 = min(c[1] for c in cs), min(c[2] for c in cs)
        x1, y1 = max(c[1] + c[3] for c in cs), max(c[2] + c[4] for c in cs)
        m = cv2.dilate(np.isin(lab[y0:y1, x0:x1], [c[0] for c in cs]).astype(np.uint8), pad) > 0
        cut = {"top": y0 <= 0, "bottom": y1 >= H, "left": x0 <= 0, "right": x1 >= W}
        parts[k] = (alpha[y0:y1, x0:x1] * m, cut)
    return {"color": color, "parts": parts}


def uncut(seg, part):
    return part in seg["parts"] and not any(seg["parts"][part][1].values())


def height(seg, part):
    return seg["parts"][part][0].shape[0]


def deskew(a):
    """카드 기울기(원근)로 생긴 글자 기울임을 모멘트로 펴고 잉크 범위로 다시 자름."""
    m = cv2.moments(a)
    if m["m00"] < 1e-3 or m["mu02"] < 1e-3:
        return a
    k = m["mu11"] / m["mu02"]
    h, w = a.shape
    pad = int(np.ceil(abs(k) * h)) + 1
    yc = m["m01"] / m["m00"]
    a = cv2.warpAffine(a, np.float32([[1, -k, k * yc + pad], [0, 1, 0]]), (w + 2 * pad, h), flags=cv2.INTER_LINEAR)
    cols = np.where(a.max(axis=0) > 0.05)[0]
    return a[:, cols[0]:cols[-1] + 1] if len(cols) else a


def vec(a):
    """기울임 보정 → bbox 기준으로 늘린 뒤 흐리게 → 평균 0, 길이 1 벡터 (점수 = 내적 = 상관계수)."""
    if a.shape[0] < 3 or a.shape[1] < 2:
        return None
    a = deskew(a.astype(np.float32))
    a = cv2.GaussianBlur(cv2.resize(a.astype(np.float32), VEC, interpolation=cv2.INTER_AREA), (0, 0), 0.8)
    a = (a - a.mean()).ravel()
    n = float(np.linalg.norm(a))
    return a / n if n > 1e-6 else None


def strip(a, f, keep):
    """글리프의 위(keep='top') 또는 아래 f 비율만 남기고, 잉크가 있는 가로 범위로 다시 자름."""
    h = max(3, round(a.shape[0] * min(f, 1.0)))
    a = a[:h] if keep == "top" else a[-h:]
    cols = np.where(a.max(axis=0) > 0.3)[0]
    return a[:, cols[0]:cols[-1] + 1] if len(cols) else a


class Prototypes:
    """숫자/무늬 원형. 실제 템플릿을 add() 할 때마다 늘어난다."""

    def __init__(self, font_dir=FONT_DIR):
        self.ranks = []   # (rank, kind, alpha, vec)
        self.suits = []   # (suit, alpha, vec)
        self.font = {}    # rank → (alpha, vec), 실제 샘플이 없는 숫자의 경쟁자
        for f in sorted(Path(font_dir).glob("*.png")) if Path(font_dir).exists() else []:
            a = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
            if a is not None:
                a = a.astype(np.float32) / 255
                self.font[f.stem] = (a, vec(a))
        self._partial = {}

    def add(self, label, img):
        seg = segment(img)
        if seg is None or len(label) != 2 or SUIT_COLOR.get(label[1]) != seg["color"]:
            return
        p = seg["parts"]
        if uncut(seg, "big"):
            self.ranks.append((label[0], "big", p["big"][0], vec(p["big"][0])))
        if uncut(seg, "corner_rank") and uncut(seg, "corner_suit") and \
                0.6 <= height(seg, "corner_rank") / height(seg, "corner_suit") <= 1.3:
            self.ranks.append((label[0], "corner", p["corner_rank"][0], vec(p["corner_rank"][0])))
        if uncut(seg, "corner_suit") and (not uncut(seg, "big") or 1.5 <= height(seg, "big") / height(seg, "corner_suit") <= 2.5):
            self.suits.append((label[1], p["corner_suit"][0], vec(p["corner_suit"][0])))
        self._partial.clear()

    def _rank_scores(self, seg):
        """숫자별 최고 점수 (실제 원형 + 실제 샘플이 없는 숫자의 글꼴 원형). 증거: 큰 숫자, 코너 숫자 평균."""
        p, real = seg["parts"], {r for r, *_ in self.ranks}
        protos = [(r, a, v) for r, k, a, v in self.ranks if v is not None]
        protos += [(r, a, v) for r, (a, v) in self.font.items() if r not in real and v is not None]
        big_protos = [(r, a) for r, k, a, v in self.ranks if k == "big"]
        big_protos += [(r, a) for r, (a, v) in self.font.items() if r not in real]
        evidence = []
        if uncut(seg, "big"):
            evidence.append(self._scores(vec(p["big"][0]), protos))
        if uncut(seg, "corner_rank") and uncut(seg, "corner_suit") and \
                0.6 <= height(seg, "corner_rank") / height(seg, "corner_suit") <= 1.3:
            evidence.append(self._scores(vec(p["corner_rank"][0]), protos))
        if not evidence and "big" in p:
            # 큰 숫자 아래가 잘림: 무늬/코너 숫자 크기로 전체 높이를 추정해 위쪽만 비교
            cut = p["big"][1]
            if cut["bottom"] and not (cut["top"] or cut["left"] or cut["right"]):
                if uncut(seg, "corner_suit"):
                    full = height(seg, "corner_suit") / SUIT_RATIO
                elif uncut(seg, "corner_rank"):
                    full = height(seg, "corner_rank") * CORNER_K
                else:
                    return None
                f = round(height(seg, "big") / full, 2)
                if f >= 0.4:
                    q = vec(p["big"][0])
                    evidence.append(self._scores(q, [(r, a, self._strip_vec(("r", i), a, f, "top"))
                                                     for i, (r, a) in enumerate(big_protos)]))
        if not evidence or any(e is None for e in evidence):
            return None
        keys = set.intersection(*(set(e) for e in evidence))
        return {r: float(np.mean([e[r] for e in evidence])) for r in keys}, real

    def _suit_scores(self, seg):
        p = seg["parts"]
        allowed = [s for s, c in SUIT_COLOR.items() if c == seg["color"]]
        protos = [(s, a, v) for s, a, v in self.suits if s in allowed and v is not None]
        if {s for s, *_ in protos} != set(allowed) or "corner_suit" not in p:
            return None  # 같은 색의 두 무늬 원형이 모두 있어야 비교 가능
        cut = p["corner_suit"][1]
        if any(cut.values()):
            if not (cut["top"] and not (cut["bottom"] or cut["left"] or cut["right"]) and uncut(seg, "big")):
                return None
            # 위가 잘린 무늬: 큰 숫자 높이로 전체 높이 추정, 아래쪽만 비교
            f = round(height(seg, "corner_suit") / (height(seg, "big") * SUIT_RATIO), 2)
            if f < 0.4:
                return None
            protos = [(s, a, self._strip_vec(("s", id(a)), a, f, "bottom")) for s, a, v in protos]
        q = vec(p["corner_suit"][0])
        scores = self._scores(q, protos)
        if scores is None or len(scores) < 2:
            return None
        # 같은 색 두 무늬(c/s, d/h)는 모양이 비슷해 상관계수 차이가 작음 → 두 무늬 평균의 차이 방향으로 판별
        # t = +1: 1등 무늬 평균 위치, 0: 두 평균의 중간, 음수: 반대쪽
        top = max(scores, key=scores.get)
        m1 = np.mean([v for s, a, v in protos if s == top and v is not None], axis=0)
        m2 = np.mean([v for s, a, v in protos if s != top and v is not None], axis=0)
        w = m1 - m2
        t = float((q - (m1 + m2) / 2) @ w / max(float(w @ w) / 2, 1e-9))
        return scores, t

    def _strip_vec(self, key, a, f, keep):
        k = (key, f, keep)
        if k not in self._partial:
            self._partial[k] = vec(strip(a, f, keep))
        return self._partial[k]

    @staticmethod
    def _scores(q, protos):
        if q is None:
            return None
        out = {}
        for key, a, v in protos:
            if v is not None:
                out[key] = max(out.get(key, -1.0), float(q @ v))
        return out or None

    def evaluate(self, crop):
        """(숫자 점수표, 실제 숫자 집합, 무늬 점수표, 무늬 판별값 t) / None — 검증/디버그용."""
        seg = segment(crop)
        if seg is None:
            return None
        rs, ss = self._rank_scores(seg), self._suit_scores(seg)
        if rs is None or ss is None:
            return None
        return rs[0], rs[1], ss[0], ss[1]

    def suit(self, crop):
        """확신할 수 있을 때만 무늬 한 글자, 아니면 None."""
        seg = segment(crop)
        ss = self._suit_scores(seg) if seg else None
        if ss is None:
            return None
        suit, score, _ = best(ss[0])
        return suit if score >= SUIT_MIN and ss[1] >= SUIT_MARGIN else None

    def match(self, crop):
        """(라벨, 점수) / None. 숫자와 무늬 모두 점수와 2등과의 차이가 기준을 넘어야 인정."""
        ev = self.evaluate(crop)
        if ev is None:
            return None
        ranks, real, suits, s_margin = ev
        rank, r_score, r_margin = best(ranks)
        suit, s_score, _ = best(suits)
        if rank not in real:  # 글꼴 원형(실제 샘플 없는 숫자)이 이기면 모름
            return None
        rank_ok = (r_score >= RANK_MIN and r_margin >= RANK_MARGIN) or \
            (r_score >= RANK_MIN_WIDE and r_margin >= RANK_MARGIN_WIDE)
        if rank_ok and s_score >= SUIT_MIN and s_margin >= SUIT_MARGIN:
            return rank + suit, min(r_score, s_score)
        return None


def best(scores):
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    return ranked[0][0], ranked[0][1], ranked[0][1] - second


def make_font_glyphs(size=160):
    """font_glyphs/{rank}.png: 실제 샘플이 없을 때 쓰는 글꼴 숫자 (bbox에 맞춰 자른 알파)."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(FONT_PATH, size)
    font.set_variation_by_axes(FONT_AXES)
    FONT_DIR.mkdir(exist_ok=True)
    for rank in RANKS:
        im = Image.new("L", (size * 3, size * 2), 0)
        ImageDraw.Draw(im).text((size // 4, size // 4), "10" if rank == "T" else rank, fill=255, font=font)
        a = np.asarray(im)
        ys, xs = np.where(a > 127)
        a = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        h = 64  # 실제 큰 숫자(작업 해상도) 정도 크기로 저장
        a = cv2.resize(a, (max(1, round(a.shape[1] * h / a.shape[0])), h), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(FONT_DIR / f"{rank}.png"), a)


if __name__ == "__main__":
    if "--fonts" in sys.argv:
        make_font_glyphs()
        print("saved", FONT_DIR)
