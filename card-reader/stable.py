"""화면 영역이 '의미 있게' 바뀌었는지 판단 (작은 애니메이션은 무시).

게임 화면은 가만히 있어도 조금씩 움직인다: 차례인 사람 아바타의 빛 고리/반짝이, 내 카드 옆 별 깜빡임,
칩이 미끄러지는 연출, 1px 정도의 창 흔들림. 바이트 비교로는 이 때마다 '바뀜'이 되어 OCR/카드 인식을 다시 돈다.

방법: 회색조로 줄이고(긴 변 MAX_SIDE px), 살짝 흐린 뒤 넓게 흐린 것을 빼서(고역 통과) 완만한 빛/밝기 변화를 없앤다.
두 이미지를 ±SHIFT px 어긋나게 맞춰 본 것 중 가장 작은 차이를 쓴다.
차이 = 작은 칸(CELL)별 평균 절대 차이 중 최댓값. 숫자 한 글자가 바뀌면 그 글자 칸에서 크게 튀고,
넓게 퍼진 빛이나 영역 가장자리의 작은 별 깜빡임은 칸 평균을 거의 못 올린다.

    sig = Signature(crop)
    if changed(prev_sig, crop):       # prev_sig 가 None 이면 True
        ...                           # 다시 읽기
    # 또는 영역 여러 개를 키로 관리
    watch = Watcher()
    if watch.changed("pot", crop): ...

TOL 은 samples/ 7장으로 맞춤 (스택/벳 숫자 영역 약 90곳, 내 카드 영역):
  같은 숫자, 다른 스크린샷                     최대 1.2
  1~2px 어긋나게 자름                          최대 12.4
  완만한 빛 (가로로 0→40 밝아짐)                  1.0
  내 카드 옆 별 하나 깜빡임 (+90 밝기)               8.5
  숫자 한 글자 바뀜 (49.3BB→49.2BB, 1BB→4BB 등)    최소 21.7   ← TOL 16 은 이 사이
  다른 카드                                     96
  숫자 위에 직접 겹친 반짝이 (8px)                 ~75  ← 이건 바뀜으로 잡힘 (숫자 바뀜과 구별 불가)
"""
import cv2
import numpy as np

MAX_SIDE = 64   # 줄인 이미지의 긴 변
MIN_SIDE = 10   # 짧은 변이 이보다 작아지지 않게 (작은 숫자 영역은 덜 줄임)
CELL = 6        # 칸 크기 (줄인 이미지 px). 숫자 한 글자 ≈ 1~2칸
SHIFT = 2       # 이만큼(줄인 px) 어긋난 것까지 같은 화면으로 봄
BLUR = 0.8      # 비교 전 흐림 (줄인 px, 시그마)
HIGHPASS = 4.0  # 이보다 넓게 퍼진 밝기 변화는 무시 (줄인 px, 시그마)
TOL = 16.0      # 기본 허용치 (0~255 밝기 차). 아래 실측 참고


class Signature:
    """영역 이미지의 요약. 원본 바이트를 들고 있지 않으므로 오래 보관해도 가벼움."""
    __slots__ = ("small", "shape")

    def __init__(self, img):
        self.shape = img.shape[:2]
        gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        if h == 0 or w == 0:
            self.small = np.zeros((1, 1), np.float32)
            return
        scale = min(1.0, MAX_SIDE / max(h, w))
        scale = max(scale, min(1.0, MIN_SIDE / min(h, w)))
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        small = cv2.resize(gray, size, interpolation=cv2.INTER_AREA).astype(np.float32)
        # 살짝 흐리게(1px 미만 어긋남/계단 현상 무시) + 넓게 흐린 것을 빼서 완만한 밝기 변화(빛 고리, 화면 전체 어두워짐) 제거
        small = cv2.GaussianBlur(small, (0, 0), BLUR)
        self.small = small - cv2.GaussianBlur(small, (0, 0), HIGHPASS)

    def distance(self, other):
        return distance(self, other)


def _cell_max(diff, cell):
    h, w = diff.shape
    ch, cw = min(cell, h), min(cell, w)
    # 칸 평균 = 적분 영상으로 (겹치는 칸: 반 칸씩 이동해서 경계에 걸친 글자도 잡음)
    integ = cv2.integral(diff)
    sy = max(1, ch // 2)
    sx = max(1, cw // 2)
    ys = np.arange(0, h - ch + 1, sy)
    xs = np.arange(0, w - cw + 1, sx)
    y0, x0 = np.meshgrid(ys, xs, indexing="ij")
    s = integ[y0 + ch, x0 + cw] - integ[y0, x0 + cw] - integ[y0 + ch, x0] + integ[y0, x0]
    return float(s.max()) / (ch * cw)


def distance(a, b):
    """두 Signature 의 차이 (0 = 같음). 크기가 다르면 inf."""
    if a is None or b is None or a.small.shape != b.small.shape or a.shape != b.shape:
        return float("inf")
    x, y = a.small, b.small
    h, w = x.shape
    best = float("inf")
    for dy in range(-SHIFT, SHIFT + 1):
        for dx in range(-SHIFT, SHIFT + 1):
            xa = x[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)]
            yb = y[max(0, -dy):h + min(0, -dy), max(0, -dx):w + min(0, -dx)]
            if xa.shape[0] > 4 and xa.shape[1] > 4:  # 가장자리 1px 은 잘린 위치에 따라 달라지므로 뺌
                xa, yb = xa[1:-1, 1:-1], yb[1:-1, 1:-1]
            if xa.size == 0:
                continue
            best = min(best, _cell_max(np.abs(xa - yb), CELL))
            if best == 0.0:
                return 0.0
    return best


def changed(prev, img, tol=TOL):
    """prev(Signature 또는 None) 대비 img 가 tol 넘게 바뀌었는지."""
    if prev is None:
        return True
    return distance(prev, Signature(img)) > tol


class Watcher:
    """키별로 마지막 '확정' 서명을 기억. changed() 가 True 일 때만 서명을 새로 저장한다
    (작은 변화가 조금씩 쌓여도 마지막으로 읽은 화면과 비교하므로 결국 잡힌다)."""

    def __init__(self, tol=TOL):
        self.tol = tol
        self.sigs = {}

    def changed(self, key, img, tol=None):
        sig = Signature(img)
        prev = self.sigs.get(key)
        if prev is not None and distance(prev, sig) <= (self.tol if tol is None else tol):
            return False
        self.sigs[key] = sig
        return True

    def forget(self, key=None):
        if key is None:
            self.sigs.clear()
        else:
            self.sigs.pop(key, None)
