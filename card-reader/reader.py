"""화면의 카드 영역을 캡처해 인식하고 승률/아웃츠를 표시하는 테스트 도구.

사용 순서
  1. [영역 지정]  : 스크린샷 위에서 보드 영역, 내 카드 영역을 차례로 드래그
  2. [카드 학습]  : 화면에 보이는 카드에 라벨(예: Ah, Td)을 입력해 템플릿 저장
  3. 이후 자동 인식 → 승률/아웃츠 계산
"""
import base64
import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

import cv2
import mss
import numpy as np

import evaluator as ev
import glyphs
from fastmatch import FastMatcher
import ocr
import odds
import preflop as pf

MSS = getattr(mss, "MSS", None) or mss.mss

try:  # 고해상도 배율(125%, 150%)에서 좌표가 어긋나지 않도록 물리 픽셀 기준으로 동작
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# exe(PyInstaller)로 실행하면 설정/템플릿을 exe 옆에 저장
BASE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
TEMPLATE_DIR = BASE / "templates"

REGIONS = [("board", "보드"), ("hero", "내 카드")]
NUMBER_STEPS = [("pot", "팟"), ("my_stack", "내 스택")]
OCR_INTERVAL = 0.8  # 초
UNKNOWN = "?"
NORM_SIZE = (52, 72)   # 비교용 정규화 크기 (w, h)
INSET = 4              # 템플릿 가장자리를 잘라 몇 픽셀 위치 오차를 허용
CORNER = (24, 36)      # 정규화 이미지에서 숫자+무늬가 있는 왼쪽 위 영역 (w, h)
CARD_MAX_SAT = 50      # 카드 앞면 = 채도 낮은(흰/회색) 영역. 노란 하이라이트 테두리, 펠트는 제외됨
CARD_MIN_VALUE = (130, 90)  # 밝기 기준: 보통 카드 / 어둡게 처리된 카드(쇼다운 패배 카드 등)
CARD_MIN_HEIGHT = 0.6  # 가장 큰 카드 대비 이보다 낮은 영역(글자 등)은 무시
MERGED_RATIO = 1.7     # 다른 카드보다 이만큼 넓으면 붙어 있는 카드로 보고 분리
CARD_ASPECT = 0.8      # 카드 1장의 폭/높이 (실측 0.75~0.85)
MERGED_ASPECT = 1.2    # 혼자 있는 영역도 폭/높이가 이보다 크면 겹친 카드인지 경계를 찾아봄 (위가 잘린 1장 ≈ 1.1)
WAIST_SPAN = (0.35, 0.65)  # 겹친 두 카드의 경계는 영역 폭의 이 범위 안에서만 찾음
WAIST_DEPTH = 0.15     # 경계 = 윤곽이 높이의 이만큼 이상 파인 V자 홈 (기울어진 카드 모서리끼리 만나는 곳)
WAIST_WIDTH = 0.1      # 홈은 좁아야 함(높이 대비). 잘린 숫자/무늬가 만드는 넓은 홈은 제외
EDGE_LENGTH = 0.75     # 홈이 없으면 영역 안을 세로로 가로지르는 이 길이(높이 대비) 이상의 테두리 선을 경계로 봄
TOP_THRESHOLD = 0.97   # 아래가 가려진 카드: 위쪽 절반 점수 기준
TOP_MARGIN = 0.03      # 위쪽 절반 점수가 2등 카드보다 이만큼 높아야 인정 (K♣/K♠ 위쪽 최대 0.954)

DEFAULT_CONFIG = {
    "regions": {},
    "opponents": 1,
    "threshold": 0.95,  # 실측: 정답 카드 0.965 이상, 다른 카드 최대 0.923 (K♣ vs K♠)
    "iterations": 10000,
    "interval_ms": 400,
}

# --- 테마 -------------------------------------------------------------------
BG = "#0f1215"
PANEL = "#171b20"
LINE = "#252b33"
SLOT = "#3a434f"
TEXT = "#e8eaed"
MUTED = "#8b94a1"
ACCENT = "#2dd4bf"
WARN = "#fbbf24"
WIN_C, TIE_C, LOSE_C = "#34d399", "#64748b", "#f87171"
CARD_FACE = "#f5f5f2"
RED_SUIT, BLACK_SUIT = "#d93a44", "#1b1e23"

F_UI = "Malgun Gothic"
F_NUM = "Segoe UI"
P_SIDE = 22


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def grab(sct, rect):
    x, y, w, h = rect
    return np.array(sct.grab({"left": x, "top": y, "width": w, "height": h}))[:, :, :3].copy()


def to_photo(img, max_w=None, max_h=None):
    h, w = img.shape[:2]
    scale = min(1.0, (max_w or w) / w, (max_h or h) / h)
    if scale != 1.0:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    ok, png = cv2.imencode(".png", img)
    return tk.PhotoImage(data=base64.b64encode(png.tobytes()))


def dark_title_bar(win):
    """Windows 10/11 제목 표시줄을 어둡게."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def parse_label(text):
    """사용자 입력 → 'Ah' / None(건너뛰기)."""
    text = text.strip()
    if text in ("", UNKNOWN):
        return None
    return ev.card_str(ev.parse_card(text))


def _overlap_split(img, contour, rect):
    """겹쳐서 한 덩어리로 잡힌 두 카드의 경계 x(rect 기준). 못 찾으면 None.

    1) 윤곽의 좁고 깊은 V자 홈: 기울어진 두 카드의 모서리가 만나는 곳
    2) 덩어리 안쪽을 세로로 가로지르는 긴 선: 위에 놓인 카드의 테두리
    """
    x, y, w, h = rect
    fill = np.zeros((h, w), np.uint8)
    cv2.drawContours(fill, [contour - (x, y)], -1, 1, -1)  # 숫자/무늬 구멍은 채움
    lo, hi = int(w * WAIST_SPAN[0]), int(w * WAIST_SPAN[1])
    prof = fill.sum(0).astype(np.int32)
    depth = np.minimum(np.maximum.accumulate(prof), np.maximum.accumulate(prof[::-1])[::-1]) - prof
    k = max(2, int(WAIST_WIDTH * h))
    for i in sorted(range(lo, hi + 1), key=lambda i: -depth[i]):
        if depth[i] < WAIST_DEPTH * h:
            break
        if 2 * max(depth[max(0, i - k)], depth[min(w - 1, i + k)]) < depth[i]:
            return i
    gray = cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 120) * cv2.erode(fill, np.ones((7, 7), np.uint8))
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, int(EDGE_LENGTH * h / 2),
                            minLineLength=int(EDGE_LENGTH * h), maxLineGap=max(2, int(0.08 * h)))
    best = None
    for x1, y1, x2, y2 in (() if lines is None else lines[:, 0]):
        mid = (int(x1) + int(x2)) // 2
        if abs(int(x2) - int(x1)) <= 0.25 * abs(int(y2) - int(y1)) and lo <= mid <= hi:
            if best is None or abs(int(y2) - int(y1)) > best[0]:
                best = (abs(int(y2) - int(y1)), mid)
    return None if best is None else best[1]


def _card_rects(img, min_value):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 1] < CARD_MAX_SAT) & (hsv[:, :, 2] > min_value)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    blobs = [(r, c) for r, c in ((cv2.boundingRect(c), c) for c in contours) if r[3] >= 12]
    if not blobs:
        return []
    max_h = max(r[3] for r, _ in blobs)
    blobs = sorted((b for b in blobs if b[0][3] >= CARD_MIN_HEIGHT * max_h), key=lambda b: b[0][0])
    # 카드가 서로 붙어 하나의 영역으로 잡힌 경우 보통 카드 폭 기준으로 균등 분할
    widths = sorted(r[2] for r, _ in blobs)
    typical = widths[len(widths) // 2]
    split = []
    for (x, y, w, h), contour in blobs:
        n = max(1, round(w / typical)) if w > MERGED_RATIO * typical else 1
        # 영역이 하나뿐이면(내 카드 2장이 겹친 경우 등) 위 기준으로는 못 나누므로 모양으로 판단
        if n == 1 and w > MERGED_ASPECT * h:
            cut = _overlap_split(img, contour, (x, y, w, h))
            if cut is not None:
                split += [(x, y, cut, h), (x + cut, y, w - cut, h)]
                continue
            n = max(1, int(w / (CARD_ASPECT * h)))  # 경계가 안 보이면 카드 2장 폭 이상일 때만 균등 분할
        split += [(x + i * w // n, y, w // n, h) for i in range(n)]
    return split


def detect_cards(img):
    """영역 이미지에서 카드들을 찾아 왼쪽→오른쪽 순서의 crop 리스트로 반환.

    보통 밝기 기준과 어두운 카드 기준으로 각각 찾아 더 많이 찾은 쪽을 쓴다
    (같으면 보통 기준 — 낮은 기준에서는 인접 카드가 붙어 잡히기 쉬움).
    """
    best = []
    for min_value in CARD_MIN_VALUE:
        rects = _card_rects(img, min_value)
        if len(rects) > len(best):
            best = rects
    return [img[y:y + h, x:x + w] for x, y, w, h in best]


class Matcher:
    def __init__(self):
        TEMPLATE_DIR.mkdir(exist_ok=True)
        self.items = []
        self.glyphs = glyphs.Prototypes()  # 숫자/무늬 원형 (통째로 학습 안 된 카드용)
        self.fast = FastMatcher(norm_size=NORM_SIZE, inset=INSET, corner=CORNER)
        for f in sorted(TEMPLATE_DIR.glob("*.png")):
            img = cv2.imread(str(f))
            if img is not None:
                self.items.append((f.stem.split("_")[0], img))
                self.glyphs.add(f.stem.split("_")[0], img)
                self.fast.add(f.stem.split("_")[0], img)

    @property
    def labels(self):
        return {label for label, _ in self.items if ev.is_card(label)}

    def add(self, label, crop):
        norm = cv2.resize(crop, NORM_SIZE, interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(TEMPLATE_DIR / f"{label}_{int(time.time() * 1000)}.png"), norm)
        self.items.append((label, norm))
        self.glyphs.add(label, norm)
        self.fast.add(label, norm)

    def match(self, crop, threshold):
        """(라벨, 점수). 매칭 실패 시 UNKNOWN.

        1차: 카드 전체 + 모서리 비교 (밝기 변화에 무관 → 어둡게 처리된 카드도 그대로 인식)
        2차: 아래쪽이 가려진 경우(아바타 등) 위쪽 절반만 비교, 단 2등과 차이가 충분할 때만
        3차: 숫자와 무늬를 따로 인식해 조합 (학습 안 된 카드, 예: 6♦ + A♣ 학습 → 6♣ 인식)
        """
        norm = cv2.resize(crop, NORM_SIZE, interpolation=cv2.INTER_AREA)
        # 템플릿 전체를 한 번에 비교 (카드 전체 / 모서리 / 위쪽 절반). 모서리 점수로 무늬만 다른 카드를 낮춤
        score, corner, upper = self.fast.scores(norm)
        full, top = {}, {}
        for label, s, up in zip(self.fast.labels, np.minimum(score, corner).tolist(), upper.tolist()):
            full[label] = max(full.get(label, -1.0), s)
            top[label] = max(top.get(label, -1.0), up)
        if not full:
            return UNKNOWN, 0.0
        label = max(full, key=full.get)
        if full[label] >= threshold:
            return label, full[label]
        ranked = sorted(top.values(), reverse=True)
        label = max(top, key=top.get)
        second = ranked[1] if len(ranked) > 1 else 0.0
        if top[label] >= TOP_THRESHOLD and top[label] - second >= TOP_MARGIN:
            return label, top[label]
        found = self.glyphs.match(crop)
        if found:
            return found
        return UNKNOWN, full[max(full, key=full.get)]


def analyze(hero, board, opponents, iterations):
    res = {"equity": ev.equity(hero, board, opponents, iterations)}
    res["hand"] = ev.hand_name(ev.evaluate(hero + board)) if board else preflop_name(hero)
    res["hero"], res["board"] = list(hero), list(board)
    if len(board) in (3, 4):
        d = ev.draws(hero, board)
        n = len(d["outs"])
        left = 5 - len(board)
        res["outs"] = {
            "groups": d["groups"],
            "count": n,
            "combo": d["combo"],
            "left": left,
            "next": ev.hit_probability(n, d["deck"], 1),
            "by_river": ev.hit_probability(n, d["deck"], left),
        }
    return res


def preflop_name(hero):
    a, b = sorted(hero, reverse=True)
    ra, rb = ev.RANKS[a >> 2], ev.RANKS[b >> 2]
    if ra == rb:
        return f"포켓 페어 {ra}{rb}"
    return f"{ra}{rb}{'s' if (a & 3) == (b & 3) else 'o'}  ·  {'수딧' if (a & 3) == (b & 3) else '오프수딧'}"


ACTION_COLORS = {"raise": "#e0a53b", "aggro": "#e05a52", "call": "#3fae78", "jam": "#f07a3a", "fold": "#1f262e"}
DEFAULT_PREFLOP = {"players": 6, "situation": "open", "hero": "BTN", "villain": "CO"}

DRAW_SHORT = {
    "포쁠 (플러시 드로우)": "포쁠", "양차 (오픈엔디드)": "양차", "빵꾸 (것샷)": "빵꾸",
    "원페어 → 투페어/셋": "투페어/셋", "풀하우스/포카드": "풀하우스+",
}


# --- 캔버스 그리기 도우미 ---------------------------------------------------
def round_rect(c, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, splinesteps=12, **kw)


def draw_card(c, x, y, w, h, label):
    """label: 'Ah' / UNKNOWN / None(빈 자리)."""
    r = max(3, w // 8)
    if label is None:
        round_rect(c, x, y, x + w, y + h, r, fill="", outline=SLOT, width=1.5, dash=(3, 3))
        return
    if label == UNKNOWN:
        round_rect(c, x, y, x + w, y + h, r, fill=PANEL, outline=WARN, width=1.5)
        c.create_text(x + w / 2, y + h / 2, text="?", fill=WARN, font=(F_NUM, int(h * 0.32), "bold"))
        return
    rank, suit = label[0], label[1]
    color = RED_SUIT if suit in "hd" else BLACK_SUIT
    round_rect(c, x, y, x + w, y + h, r, fill=CARD_FACE, outline="")
    rank_text = "10" if rank == "T" else rank
    c.create_text(x + w * 0.12, y + h * 0.05, anchor="nw", text=rank_text, fill=color,
                  font=(F_NUM, max(7, int(h * 0.2)), "bold"))
    c.create_text(x + w * 0.5, y + h * 0.66, text=ev.SUIT_SYMBOLS[suit], fill=color,
                  font=("Segoe UI Symbol", max(8, int(h * 0.36))))


class Button:
    """캔버스 위의 납작한 버튼 (hover 표시 포함)."""

    def __init__(self, app, key, command):
        self.app, self.key, self.command = app, key, command
        self.poly = None
        self.primary = False

    def fill(self):
        hover = self.app.hover == self.key
        if self.primary:
            return "#5eead4" if hover else ACCENT
        return "#2c333d" if hover else "#232931"

    def refresh(self, c):
        if self.poly is not None:
            c.itemconfig(self.poly, fill=self.fill())

    def draw(self, c, x0, y0, x1, y1, text, primary=False, font=(F_UI, 9)):
        self.primary = primary
        tag = f"btn_{self.key}"
        self.poly = round_rect(c, x0, y0, x1, y1, 7, fill=self.fill(), outline="", tags=tag)
        c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=text, font=font,
                      fill=BG if primary else TEXT, tags=tag)
        c.tag_bind(tag, "<Button-1>", lambda e: self.command())
        c.tag_bind(tag, "<Enter>", lambda e: self.app.set_hover(self.key))
        c.tag_bind(tag, "<Leave>", lambda e: self.app.set_hover(None))


# --- 영역 지정 --------------------------------------------------------------
class RegionSelector:
    """전체 화면 스크린샷 위에서 영역을 차례로 드래그.

    steps: [(키, 이름)] 순서대로 지정. repeat가 있으면 그 뒤로 (키접두어, 이름 형식)을 원하는 만큼
    추가 지정하고 Enter로 끝낸다.
    """

    def __init__(self, root, on_done, steps=REGIONS, repeat=None,
                 hint="카드가 놓이는 자리를 넉넉하게 드래그"):
        self.on_done, self.steps, self.repeat = on_done, steps, repeat
        with MSS() as sct:
            self.mon = sct.monitors[0]
            shot = np.array(sct.grab(self.mon))[:, :, :3]
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.scale = min(1.0, sw * 0.92 / shot.shape[1], sh * 0.82 / shot.shape[0])
        self.disp = cv2.resize(shot, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)
        dim = (self.disp * 0.45).astype(np.uint8)

        self.win = tk.Toplevel(root, bg=BG)
        self.win.title("영역 지정")
        self.win.attributes("-topmost", True)
        dark_title_bar(self.win)
        bar = tk.Frame(self.win, bg=BG)
        bar.pack(fill="x", padx=16, pady=(12, 10))
        self.step = tk.Label(bar, bg=BG, fg=ACCENT, font=(F_UI, 12, "bold"))
        self.step.pack(side="left")
        tk.Label(bar, bg=BG, fg=MUTED, font=(F_UI, 9),
                 text=hint + ("   ·   Enter 완료" if repeat else "") + "   ·   우클릭 되돌리기   ·   Esc 취소"
                 ).pack(side="right")

        self.photo = to_photo(dim)
        self.canvas = tk.Canvas(self.win, width=self.disp.shape[1], height=self.disp.shape[0],
                                bg=BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=16, pady=(0, 16))
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        self.rects, self.items, self.patches = {}, {}, {}
        self.start = self.temp = None
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Button-3>", lambda e: self.undo())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.win.bind("<Return>", lambda e: self.index >= len(self.steps) and self.finish())
        self.update_step()

    @property
    def index(self):
        return len(self.rects)

    def step_at(self, i):
        if i < len(self.steps):
            return self.steps[i]
        n = i - len(self.steps) + 1
        return f"{self.repeat[0]}{n}", self.repeat[1].format(n)

    def update_step(self):
        key, name = self.step_at(self.index)
        if self.repeat and self.index >= len(self.steps):
            self.step.config(text=f"{name} 영역을 드래그하세요   (다 지정했으면 Enter)")
        else:
            self.step.config(text=f"{self.index + 1}/{len(self.steps)}   {name} 영역을 드래그하세요")

    def press(self, e):
        self.start = (e.x, e.y)
        self.temp = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline=ACCENT, width=2)

    def drag(self, e):
        if self.start:
            self.canvas.coords(self.temp, *self.start, e.x, e.y)

    def release(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        self.start = None
        self.canvas.delete(self.temp)
        x, y, w, h = min(x0, e.x), min(y0, e.y), abs(e.x - x0), abs(e.y - y0)
        if w < 8 or h < 8:
            return
        key, name = self.step_at(self.index)
        # 선택한 부분만 원래 밝기로 보여줌
        patch = to_photo(self.disp[y:y + h, x:x + w])
        self.patches[key] = patch
        self.items[key] = [
            self.canvas.create_image(x, y, anchor="nw", image=patch),
            self.canvas.create_rectangle(x, y, x + w, y + h, outline=ACCENT, width=2),
            self.canvas.create_rectangle(x, y - 22, x + 16 + tkfont.Font(family=F_UI, size=9, weight="bold")
                                         .measure(name), y, fill=ACCENT, outline=""),
            self.canvas.create_text(x + 6, y - 11, anchor="w", text=name, fill=BG, font=(F_UI, 9, "bold")),
        ]
        self.rects[key] = [
            self.mon["left"] + int(x / self.scale), self.mon["top"] + int(y / self.scale),
            int(w / self.scale), int(h / self.scale),
        ]
        if self.index == len(self.steps) and not self.repeat:
            self.win.after(250, self.finish)
        else:
            self.update_step()

    def undo(self):
        if not self.rects:
            return
        key = self.step_at(self.index - 1)[0]
        self.rects.pop(key)
        for item in self.items.pop(key, ()):
            self.canvas.delete(item)
        self.update_step()

    def finish(self):
        self.win.destroy()
        self.on_done(self.rects)


# --- 카드 학습 --------------------------------------------------------------
class LearnDialog:
    """현재 화면에서 찾은 카드에 라벨을 붙여 템플릿으로 저장."""

    def __init__(self, root, detected, matcher, on_saved):
        self.matcher, self.on_saved = matcher, on_saved
        self.win = tk.Toplevel(root, bg=BG)
        self.win.title("카드 학습")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        dark_title_bar(self.win)

        tk.Label(self.win, text="카드 학습", bg=BG, fg=TEXT, font=(F_UI, 13, "bold")).pack(
            anchor="w", padx=20, pady=(16, 2))
        tk.Label(self.win, bg=BG, fg=MUTED, font=(F_UI, 9), justify="left",
                 text="화면에서 찾은 카드입니다. 틀렸거나 ? 인 칸에 카드를 입력하세요.\n"
                      "예: Ah  Td  10s  7c   ·   비워 두거나 ? 이면 저장하지 않습니다.").pack(
            anchor="w", padx=20, pady=(0, 12))

        self.photos, self.entries = [], []
        for key, name in REGIONS:
            cards = detected.get(key, [])
            box = tk.Frame(self.win, bg=PANEL, padx=14, pady=12)
            box.pack(fill="x", padx=20, pady=(0, 10))
            tk.Label(box, text=f"{name}  ·  {len(cards)}장", bg=PANEL, fg=MUTED,
                     font=(F_UI, 9, "bold")).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))
            if not cards:
                tk.Label(box, text="카드를 찾지 못했습니다", bg=PANEL, fg=MUTED,
                         font=(F_UI, 9)).grid(row=1, column=0, sticky="w")
            for i, (crop, label) in enumerate(cards):
                photo = to_photo(crop, 90, 110)
                self.photos.append(photo)
                tk.Label(box, image=photo, bg=PANEL).grid(row=1, column=i, padx=5)
                entry = tk.Entry(box, width=5, justify="center", font=("Consolas", 13),
                                 bg=BG, fg=WARN if label == UNKNOWN else TEXT, insertbackground=TEXT,
                                 relief="flat", highlightthickness=1, highlightbackground=LINE,
                                 highlightcolor=ACCENT)
                entry.insert(0, label)
                entry.grid(row=2, column=i, padx=5, pady=(8, 0), ipady=3)
                self.entries.append((entry, crop, label))

        self.error = tk.Label(self.win, bg=BG, fg=LOSE_C, font=(F_UI, 9))
        self.error.pack(anchor="w", padx=20)
        buttons = tk.Frame(self.win, bg=BG)
        buttons.pack(fill="x", padx=20, pady=(4, 18))
        self._flat_button(buttons, "저장", self.save, primary=True).pack(side="right")
        self._flat_button(buttons, "취소", self.win.destroy).pack(side="right", padx=8)
        self.win.bind("<Return>", lambda e: self.save())
        self.win.bind("<Escape>", lambda e: self.win.destroy())

    @staticmethod
    def _flat_button(parent, text, command, primary=False):
        normal, hover = (ACCENT, "#5eead4") if primary else ("#232931", "#2c333d")
        b = tk.Label(parent, text=text, bg=normal, fg=BG if primary else TEXT, font=(F_UI, 10, "bold"),
                     padx=18, pady=6, cursor="hand2")
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.config(bg=hover))
        b.bind("<Leave>", lambda e: b.config(bg=normal))
        return b

    def save(self):
        try:
            labels = [(parse_label(e.get()), crop, old) for e, crop, old in self.entries]
        except ValueError as exc:
            self.error.config(text=str(exc))
            return
        saved = 0
        for label, crop, old in labels:
            if label is not None and label != old:
                self.matcher.add(label, crop)
                saved += 1
        self.win.destroy()
        self.on_saved(saved)


# --- 메인 창 ----------------------------------------------------------------
class App:
    W = 420
    P = 22
    SIDE_W = 360

    def __init__(self):
        self.cfg = load_config()
        self.matcher = Matcher()
        self.sct = MSS()
        self.detected = {}
        self.view = {}
        self.result = None
        self.computing = False
        self.toast = None
        self.hover = None
        self.pending_state = self.computed_state = None
        self.generation = 0
        self.last_render = None
        self.ocr_queue = queue.Queue()
        self.ocr_pending = None
        self.ocr_values = {}

        self.root = tk.Tk()
        self.root.title("Card Reader")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        dark_title_bar(self.root)
        self.canvas = tk.Canvas(self.root, width=self.W, height=300, bg=BG, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="n")
        tk.Frame(self.root, bg=LINE, width=1).grid(row=0, column=1, sticky="ns", pady=P_SIDE)
        side = tk.Frame(self.root, bg=BG)
        side.grid(row=0, column=2, sticky="n")
        self.inputs = {}
        self.build_inputs(side)
        self.villain_box = tk.Frame(side, bg=BG)
        self.side_canvas = tk.Canvas(side, width=self.SIDE_W, height=200, bg=BG, highlightthickness=0)
        self.side_canvas.pack(side="bottom")
        self.last_side = None
        self.refresh_auto_toggle()
        self.fonts = {
            "title": tkfont.Font(family=F_UI, size=12, weight="bold"),
            "btn": tkfont.Font(family=F_UI, size=8),
        }
        self.buttons = {
            "region": Button(self, "region", self.select_regions),
            "learn": Button(self, "learn", self.learn),
            "numbers": Button(self, "numbers", self.select_numbers),
            "minus": Button(self, "minus", lambda: self.change_opponents(-1)),
            "plus": Button(self, "plus", lambda: self.change_opponents(1)),
        }
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if ocr.available():
            threading.Thread(target=self.ocr_worker, daemon=True).start()
        self.root.after(100, self.tick)

    # --- 동작 ---------------------------------------------------------------
    def select_regions(self):
        def done(rects):
            if len(rects) == len(REGIONS):
                self.cfg["regions"] = rects
                save_config(self.cfg)
                self.reset_state()
                self.show_toast("영역을 저장했습니다")
        RegionSelector(self.root, done)

    def select_numbers(self):
        if not ocr.available():
            self.show_toast("이 PC에서는 Windows 한국어 OCR을 쓸 수 없습니다")
            return

        def done(rects):
            if rects:
                self.cfg["number_regions"] = rects
                self.cfg["auto_numbers"] = True
                save_config(self.cfg)
                self.ocr_values, self.ocr_pending = {}, None
                self.refresh_auto_toggle()
                self.show_toast(f"숫자 영역 {len(rects)}개를 저장했습니다")
        RegionSelector(self.root, done, steps=NUMBER_STEPS, repeat=("opp", "상대 스택 {}"),
                       hint="숫자가 표시되는 자리를 글자에 맞게 드래그")

    # --- 팟/스택 숫자 자동 읽기 ----------------------------------------------
    def ocr_worker(self):
        """백그라운드: 숫자 영역을 읽어 {키: (값, 단위)}를 큐에 넣는다. 바뀐 영역만 OCR."""
        cache, last_unit = {}, None
        with MSS() as sct:
            while True:
                time.sleep(OCR_INTERVAL)
                regions = self.cfg.get("number_regions")
                if not regions or not self.cfg.get("auto_numbers"):
                    continue
                try:
                    crops, changed = {}, {}
                    for key, rect in regions.items():
                        img = grab(sct, rect)
                        sig = cv2.resize(img, (32, 8), interpolation=cv2.INTER_AREA).tobytes()
                        crops[key] = img
                        if cache.get(key, (None,))[0] != sig:
                            changed[key] = img
                    if changed:
                        for key, texts in ocr.read_texts(changed).items():
                            sig = cv2.resize(crops[key], (32, 8), interpolation=cv2.INTER_AREA).tobytes()
                            cache[key] = (sig, texts)
                    for key in list(cache):
                        if key not in regions:
                            cache.pop(key)
                    last_unit = ocr.unit_hint(t for _, t in cache.values()) or last_unit
                    values = {k: ocr.resolve(t, last_unit) for k, (_, t) in cache.items()}
                    self.ocr_queue.put(values)
                except Exception as exc:  # 화면 밖 영역 등 — 다음 주기에 재시도
                    self.ocr_queue.put({"error": str(exc)})

    def apply_ocr(self):
        values = None
        while not self.ocr_queue.empty():
            values = self.ocr_queue.get_nowait()
        if values is None or "error" in values or not self.cfg.get("auto_numbers"):
            return
        # 같은 결과가 두 번 연속 나와야 반영 (칩 애니메이션 중 오인식 방지)
        if values != self.ocr_pending:
            self.ocr_pending = values
            return
        if values == self.ocr_values:
            return
        self.ocr_values = values
        if not values:
            return
        for key in ("pot", "my_stack"):
            if values.get(key):
                self.set_input(key, ocr.format_value(*values[key]))
        opp = self.villain_stack()
        if opp:
            self.set_input("opp_stack", ocr.format_value(*opp))
        units = {v[1] for v in values.values() if v}
        if "BB" in units and not self.inputs["bb"].get().strip():
            self.set_input("sb", "0.5BB")
            self.set_input("bb", "1BB")
        self.build_villain_pills()

    def opp_values(self):
        return [(k, v) for k, v in sorted(self.ocr_values.items(), key=lambda kv: (len(kv[0]), kv[0]))
                if k.startswith("opp") and v]

    def villain_stack(self):
        opps = self.opp_values()
        if not opps:
            return None
        seat = self.cfg.get("villain_seat", "max")
        chosen = dict(opps).get(seat)
        return chosen or max((v for _, v in opps), key=lambda v: v[0])

    def pick_villain(self, seat):
        self.cfg["villain_seat"] = seat
        save_config(self.cfg)
        opp = self.villain_stack()
        if opp:
            self.set_input("opp_stack", ocr.format_value(*opp))
        self.build_villain_pills()

    def set_input(self, key, text):
        if self.inputs[key].get() != text:
            self.inputs[key].set(text)

    def toggle_auto(self):
        if not self.cfg.get("number_regions"):
            self.show_toast("먼저 [숫자 영역]을 지정하세요")
            return
        self.cfg["auto_numbers"] = not self.cfg.get("auto_numbers")
        self.ocr_values, self.ocr_pending = {}, None
        save_config(self.cfg)
        self.refresh_auto_toggle()

    def refresh_auto_toggle(self):
        on = bool(self.cfg.get("auto_numbers") and self.cfg.get("number_regions"))
        self.auto_btn.config(text=("● 화면 자동 입력" if on else "○ 화면 자동 입력"),
                             bg="#123b37" if on else "#232931", fg=ACCENT if on else MUTED)
        for key in ("pot", "my_stack", "opp_stack"):
            self.entries[key].config(fg=ACCENT if on else TEXT)
        self.build_villain_pills()

    def build_villain_pills(self):
        for w in self.villain_box.winfo_children():
            w.destroy()
        opps = self.opp_values()
        if not (opps and self.cfg.get("auto_numbers")):
            self.villain_box.pack_forget()
            return
        self.villain_box.pack(fill="x", padx=P_SIDE, pady=(0, 10))
        tk.Label(self.villain_box, text="상대 선택 (유효 스택 기준)", bg=BG, fg=MUTED, font=(F_UI, 8)).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 3))
        seat = self.cfg.get("villain_seat", "max")
        if seat != "max" and seat not in dict(opps):
            seat = "max"
        options = [("max", "가장 큰 스택")] + [(k, f"{k[3:]}: {ocr.format_value(*v)}") for k, v in opps]
        for i, (key, text) in enumerate(options):
            on = key == seat
            b = tk.Label(self.villain_box, text=text, bg=ACCENT if on else "#232931", fg=BG if on else TEXT,
                         font=(F_UI, 8, "bold" if on else "normal"), padx=7, pady=2, cursor="hand2")
            b.grid(row=1 + i // 3, column=i % 3, sticky="w", padx=(0, 4), pady=(0, 4))
            b.bind("<Button-1>", lambda e, k=key: self.pick_villain(k))

    def learn(self):
        if not self.cfg["regions"]:
            self.show_toast("먼저 영역을 지정하세요")
            return
        LearnDialog(self.root, dict(self.detected), self.matcher,
                    lambda n: (self.show_toast(f"카드 {n}장을 학습했습니다"), self.reset_state()))

    def change_opponents(self, delta):
        self.cfg["opponents"] = max(1, min(9, self.cfg["opponents"] + delta))
        save_config(self.cfg)

    def set_hover(self, key):
        old, self.hover = self.hover, key
        self.root.config(cursor="hand2" if key else "")
        for k in (old, key):
            if k:
                self.buttons[k].refresh(self.canvas)

    def show_toast(self, text):
        self.toast = text
        self.root.after(2500, lambda: setattr(self, "toast", None) if self.toast == text else None)

    def reset_state(self):
        self.computed_state = self.pending_state = None

    # --- 메인 루프 ----------------------------------------------------------
    def tick(self):
        try:
            self.apply_ocr()
            self.capture()
            self.render()
        finally:
            self.root.after(self.cfg["interval_ms"], self.tick)

    def capture(self):
        regions = self.cfg["regions"]
        if not regions:
            self.view = {"message": ("영역을 지정하세요", "[영역 지정]을 눌러 보드와 내 카드 위치를 드래그합니다.")}
            return
        self.detected = {}
        for key, _ in REGIONS:
            crops = detect_cards(grab(self.sct, regions[key]))
            self.detected[key] = [(c, self.matcher.match(c, self.cfg["threshold"])[0]) for c in crops]
        hero = [label for _, label in self.detected["hero"]][:2]
        board = [label for _, label in self.detected["board"]][:5]
        self.view = {"hero": hero, "board": board}

        state = self.read_state(hero, board)
        if isinstance(state, tuple) and len(state) == 2 and isinstance(state[0], str):
            self.view["message"] = state
            self.reset_state()
            self.result = None
            return
        # 같은 결과가 두 번 연속 나와야 확정 (딜링/뒤집기 애니메이션 중 오인식 방지)
        if state != self.pending_state:
            self.pending_state = state
            return
        if state != self.computed_state:
            self.computed_state = state
            self.start_analysis(state)

    def read_state(self, hero, board):
        if UNKNOWN in hero + board:
            return "학습되지 않은 카드", "노란색 ? 카드가 있습니다. [카드 학습]에서 라벨을 입력하세요."
        if len(hero) < 2:
            return "핸드 대기 중", "내 카드 2장이 보이면 자동으로 계산합니다."
        if len(board) not in (0, 3, 4, 5):
            return "딜링 중", f"보드 카드 {len(board)}장 인식됨"
        if len(set(hero + board)) != len(hero + board):
            return "중복 인식", "같은 카드가 두 번 인식되었습니다. 학습 데이터를 확인하세요."
        return (tuple(ev.parse_card(c) for c in hero), tuple(ev.parse_card(c) for c in board),
                self.cfg["opponents"])

    def start_analysis(self, state):
        hero, board, opponents = state
        self.generation += 1
        gen = self.generation
        self.computing = True

        def work():
            res = analyze(list(hero), list(board), opponents, self.cfg["iterations"])
            res["opponents"] = opponents
            self.root.after(0, lambda: self.finish_analysis(gen, res))

        threading.Thread(target=work, daemon=True).start()

    def finish_analysis(self, gen, res):
        if gen == self.generation:
            self.result, self.computing = res, False

    # --- 그리기 -------------------------------------------------------------
    def render(self):
        key = (repr(self.view), id(self.result), self.computing, self.toast, self.cfg["opponents"],
               len(self.matcher.labels), repr(self.cfg.get("preflop")), repr(self.cfg.get("inputs")))
        if key == self.last_render:
            return
        self.last_render = key
        c, W, P = self.canvas, self.W, self.P
        c.delete("all")
        y = P

        # 헤더
        c.create_text(P, y, anchor="nw", text="Card Reader", font=self.fonts["title"], fill=TEXT)
        x = W - P
        for bkey, text in (("learn", "카드 학습"), ("numbers", "숫자 영역"), ("region", "카드 영역")):
            w = self.fonts["btn"].measure(text) + 18
            self.buttons[bkey].draw(c, x - w, y - 3, x, y + 23, text, font=self.fonts["btn"])
            x -= w + 6
        y += 26
        learned = len(self.matcher.labels)
        msg = self.view.get("message")
        dot = MUTED if not self.cfg["regions"] else (WARN if msg and msg[0] != "핸드 대기 중" else WIN_C)
        c.create_oval(P, y + 5, P + 7, y + 12, fill=dot, outline="")
        c.create_text(P + 14, y + 8, anchor="w", font=(F_UI, 8), fill=MUTED,
                      text=self.toast or f"학습된 카드 {learned}/52   ·   상대 {self.cfg['opponents']}명 기준")
        y += 30

        # 카드
        hero = self.view.get("hero", [])
        board = self.view.get("board", [])
        cw, ch, gap = 44, 62, 6
        c.create_text(P, y, anchor="nw", text="내 카드", font=(F_UI, 8, "bold"), fill=MUTED)
        bx = P + 2 * cw + gap + 26
        c.create_text(bx, y, anchor="nw", text="보드", font=(F_UI, 8, "bold"), fill=MUTED)
        y += 20
        for i in range(2):
            draw_card(c, P + i * (cw + gap), y, cw, ch, hero[i] if i < len(hero) else None)
        c.create_line(bx - 13, y + 4, bx - 13, y + ch - 4, fill=LINE)
        for i in range(5):
            draw_card(c, bx + i * (cw + gap), y, cw, ch, board[i] if i < len(board) else None)
        y += ch + 22
        c.create_line(P, y, W - P, y, fill=LINE)
        y += 20

        if msg:
            y = self.render_message(c, y, *msg)
        elif self.result:
            y = self.render_result(c, y, self.result)
        else:
            y = self.render_message(c, y, "계산 중…", "")
        c.config(height=y + P)
        self.render_side()

    def render_message(self, c, y, title, body):
        P = self.P
        c.create_text(P, y, anchor="nw", text=title, font=(F_UI, 12, "bold"), fill=TEXT)
        if body:
            c.create_text(P, y + 26, anchor="nw", text=body, font=(F_UI, 9), fill=MUTED, width=self.W - 2 * P)
            return y + 50
        return y + 28

    def render_result(self, c, y, res):
        W, P = self.W, self.P
        eq = res["equity"]
        stale = self.computing

        c.create_text(P, y, anchor="nw", text=res["hand"], font=(F_UI, 13, "bold"), fill=TEXT)
        y += 30

        # 에쿼티 + 상대 수 조절
        c.create_text(P - 2, y, anchor="nw", text=f"{eq['equity'] * 100:.1f}%",
                      font=(F_NUM, 30, "bold"), fill=MUTED if stale else ACCENT)
        c.create_text(P, y + 58, anchor="nw", font=(F_UI, 8), fill=MUTED,
                      text="계산 중…" if stale else
                      f"에쿼티  ·  랜덤 핸드 {res['opponents']}명 상대")
        sx = W - P
        self.buttons["plus"].draw(c, sx - 28, y + 12, sx, y + 40, "+", font=(F_NUM, 12, "bold"))
        c.create_text(sx - 50, y + 26, text=str(self.cfg["opponents"]), font=(F_NUM, 13, "bold"), fill=TEXT)
        self.buttons["minus"].draw(c, sx - 100, y + 12, sx - 72, y + 40, "−", font=(F_NUM, 12, "bold"))
        c.create_text(sx - 50, y + 48, anchor="n", text="상대 수", font=(F_UI, 8), fill=MUTED)
        y += 86

        # 승/무/패 막대
        bw = W - 2 * P
        x = P
        for value, color in ((eq["win"], WIN_C), (eq["tie"], TIE_C), (eq["lose"], LOSE_C)):
            w = bw * value
            if w >= 1:
                c.create_rectangle(x, y, x + w, y + 8, fill=color, outline="")
            x += w
        y += 16
        x = P
        for name, value, color in (("승", eq["win"], WIN_C), ("무", eq["tie"], TIE_C), ("패", eq["lose"], LOSE_C)):
            c.create_oval(x, y + 4, x + 7, y + 11, fill=color, outline="")
            t = c.create_text(x + 12, y + 7, anchor="w", text=f"{name} {value * 100:.1f}%",
                              font=(F_UI, 9), fill=TEXT)
            x = c.bbox(t)[2] + 16
        method = "정확 계산" if eq["exact"] else f"시뮬레이션 {eq['samples']:,}회"
        c.create_text(W - P, y + 7, anchor="e", text=method, font=(F_UI, 8), fill=MUTED)
        y += 30

        if not res["board"]:
            y = self.render_preflop(c, y, res)
        outs = res.get("outs")
        if outs is not None:
            c.create_line(P, y, W - P, y, fill=LINE)
            y += 18
            t = c.create_text(P, y, anchor="nw", text=f"아웃츠 {outs['count']}장", font=(F_UI, 11, "bold"), fill=TEXT)
            if outs["combo"]:
                x0 = c.bbox(t)[2] + 10
                round_rect(c, x0, y + 1, x0 + 74, y + 21, 6, fill="#123b37", outline="")
                c.create_text(x0 + 37, y + 11, text="콤보 드로우", font=(F_UI, 8, "bold"), fill=ACCENT)
            y += 28
            rows = [("리버까지 (2장)", outs["by_river"], 2)] if outs["left"] == 2 else []
            rows.append(("다음 카드 (1장)", outs["next"], 1))
            for name, prob, n in rows:
                c.create_text(P, y, anchor="nw", text=name, font=(F_UI, 9), fill=MUTED)
                c.create_text(P + 150, y, anchor="nw", text=f"{prob * 100:.1f}%", font=(F_NUM, 10, "bold"),
                              fill=ACCENT)
                c.create_text(W - P, y, anchor="ne", font=(F_UI, 9), fill=MUTED,
                              text=f"4-2 법칙 ≈ {odds.rule_of_4_2(outs['count'], n)}%")
                y += 22
            y += 6
            if not outs["groups"]:
                c.create_text(P, y, anchor="nw", text="드로우가 없습니다", font=(F_UI, 9), fill=MUTED)
                y += 24
            mw, mh, mg, label_w = 22, 30, 4, 100
            per_row = int((W - 2 * P - label_w + mg) // (mw + mg))
            for name, cards in outs["groups"]:
                c.create_text(P, y + mh / 2, anchor="w", text=f"{DRAW_SHORT.get(name, name)}  {len(cards)}",
                              font=(F_UI, 9), fill=TEXT)
                for i, card in enumerate(cards):
                    row, col = divmod(i, per_row)
                    draw_card(c, P + label_w + col * (mw + mg), y + row * (mh + mg), mw, mh, ev.card_str(card))
                y += ((len(cards) - 1) // per_row + 1) * (mh + mg) + 8
        return y

    # --- 프리플랍 가이드 ------------------------------------------------------
    def preflop_state(self):
        """저장된 선택값을 현재 인원/상황에 맞게 보정해서 반환."""
        st = dict(DEFAULT_PREFLOP, **self.cfg.get("preflop", {}))
        stacks = [v for v in (self.amount("my_stack"), self.amount("opp_stack")) if v] if self.inputs else []
        bb = self.amount("bb") if self.inputs else None
        st["stack_bb"] = min(stacks) / bb if stacks and bb else None
        st["short"] = st["stack_bb"] is not None and st["stack_bb"] <= pf.SHORT_STACK_BB
        st["ante"] = bool(self.inputs and (self.amount("ante") or 0) > 0)
        situations = pf.SHORT_SITUATIONS if st["short"] else pf.SITUATIONS
        keys = [k for k, _ in situations]
        if st["situation"] not in keys:
            # 딥 ↔ 숏 전환 시 같은 의미의 상황으로
            st["situation"] = {"open": "jam", "vsopen": "jamcall", "vs3bet": "jamcall", "vs4bet": "jamcall",
                               "jam": "open", "jamcall": "vsopen"}.get(st["situation"], keys[0])
        heroes = pf.valid_heroes(st["situation"], st["players"])
        if st["hero"] not in heroes:
            st["hero"] = heroes[-1] if st["situation"] in ("vsopen", "jamcall") else heroes[0]
        villains = pf.valid_villains(st["situation"], st["hero"], st["players"])
        if villains and st["villain"] not in villains:
            st["villain"] = villains[-1] if st["situation"] != "vs3bet" else villains[0]
        st["villains"] = villains
        return st

    def set_preflop(self, **changes):
        st = dict(DEFAULT_PREFLOP, **self.cfg.get("preflop", {}))
        st.update(changes)
        self.cfg["preflop"] = {k: st[k] for k in DEFAULT_PREFLOP}
        save_config(self.cfg)
        self.render()

    def pills(self, c, y, label, options, selected, on_pick, x0=None):
        """라벨 + 알약 버튼 한 줄. options: [(값, 표시)]"""
        W, P = self.W, self.P
        c.create_text(P, y + 11, anchor="w", text=label, font=(F_UI, 8), fill=MUTED)
        x = P + 66 if x0 is None else x0
        avail = W - P - x
        gap = 4
        widths = [max(30, tkfont.Font(family=F_UI, size=8).measure(t) + 14) for _, t in options]
        scale = min(1.0, (avail - gap * (len(options) - 1)) / sum(widths))
        for (value, text), w in zip(options, widths):
            w = w * scale
            on = value == selected
            tag = f"pill_{label}_{value}"
            round_rect(c, x, y, x + w, y + 22, 6, fill=ACCENT if on else "#232931", outline="", tags=tag)
            c.create_text(x + w / 2, y + 11, text=text, font=(F_UI, 8, "bold" if on else "normal"),
                          fill=BG if on else TEXT, tags=tag)
            c.tag_bind(tag, "<Button-1>", lambda e, v=value: on_pick(v))
            c.tag_bind(tag, "<Enter>", lambda e: self.root.config(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: self.root.config(cursor=""))
            x += w + gap
        return y + 28

    def render_preflop(self, c, y, res):
        W, P = self.W, self.P
        st = self.preflop_state()
        c.create_line(P, y, W - P, y, fill=LINE)
        y += 16
        c.create_text(P, y, anchor="nw", text="프리플랍 가이드", font=(F_UI, 11, "bold"), fill=TEXT)
        stack = (f"유효 {st['stack_bb']:.1f}BB" if st["stack_bb"] is not None else "유효 스택 미입력 (딥스택 기준)")
        if st["short"]:
            stack += " · 숏스택 잼/폴드"
        c.create_text(W - P, y + 3, anchor="ne", text=stack, font=(F_UI, 8), fill=WARN if st["short"] else MUTED)
        y += 30

        y = self.pills(c, y, "인원", [(n, f"{n}") for n in (6, 7, 8, 9, 10)], st["players"],
                       lambda v: self.set_preflop(players=v))
        situations = pf.SHORT_SITUATIONS if st["short"] else pf.SITUATIONS
        y = self.pills(c, y, "상황", situations, st["situation"], lambda v: self.set_preflop(situation=v))
        heroes = pf.valid_heroes(st["situation"], st["players"])
        y = self.pills(c, y, "내 포지션", [(h, h) for h in heroes], st["hero"], lambda v: self.set_preflop(hero=v))
        if st["villains"]:
            y = self.pills(c, y, pf.VILLAIN_LABEL[st["situation"]], [(v, v) for v in st["villains"]], st["villain"],
                           lambda v: self.set_preflop(villain=v))
        y += 6

        acts = pf.ranges(st["situation"], st["hero"], st.get("villain"), st["players"],
                         st["stack_bb"] or 100.0, st["ante"])
        hand = pf.hand_code(res["hero"])
        action, color_key = pf.decide(hand, acts)
        color = ACTION_COLORS[color_key] if color_key != "fold" else "#5b6673"

        # 판정
        round_rect(c, P, y, W - P, y + 56, 10, fill=PANEL, outline="")
        rank = pf.DATA["G"].index(hand) + 1
        c.create_text(P + 16, y + 21, anchor="w", text=hand, font=(F_NUM, 15, "bold"), fill=TEXT)
        c.create_text(P + 16, y + 42, anchor="w", text=f"강도 {rank}/169", font=(F_UI, 8), fill=MUTED)
        c.create_text(P + 96, y + 28, anchor="w", text="→", font=(F_NUM, 13), fill=MUTED)
        c.create_text(P + 122, y + 28, anchor="w", text=action, font=(F_UI, 17, "bold"), fill=color)
        y += 68

        # 격자 + 범례
        cell, gap = 14, 1
        grid = pf.grid()
        colors = {}
        for _, hands, ck in reversed(acts):
            for h in hands:
                colors[h] = ACTION_COLORS[ck]
        for i, row in enumerate(grid):
            for j, h in enumerate(row):
                x0, y0 = P + j * (cell + gap), y + i * (cell + gap)
                c.create_rectangle(x0, y0, x0 + cell, y0 + cell, fill=colors.get(h, ACTION_COLORS["fold"]),
                                   outline="")
                if h == hand:
                    mark = (x0 - 2, y0 - 2, x0 + cell + 2, y0 + cell + 2)
        c.create_rectangle(*mark, outline="#ffffff", width=2)
        gx = P + 13 * (cell + gap) + 16
        ly = y + 2
        total = 0.0
        for name, hands, ck in acts:
            pct = pf.range_pct(hands)
            total += pct
            c.create_rectangle(gx, ly + 3, gx + 10, ly + 13, fill=ACTION_COLORS[ck], outline="")
            c.create_text(gx + 16, ly + 8, anchor="w", text=name, font=(F_UI, 9), fill=TEXT)
            c.create_text(W - P, ly + 8, anchor="e", text=f"{pct:.1f}%", font=(F_NUM, 9, "bold"), fill=TEXT)
            ly += 22
        c.create_rectangle(gx, ly + 3, gx + 10, ly + 13, fill="#3a434f", outline="")
        c.create_text(gx + 16, ly + 8, anchor="w", text="폴드", font=(F_UI, 9), fill=MUTED)
        c.create_text(W - P, ly + 8, anchor="e", text=f"{max(0.0, 100 - total):.1f}%", font=(F_NUM, 9), fill=MUTED)
        ly += 30
        c.create_text(gx, ly, anchor="nw", width=W - P - gx, font=(F_UI, 8), fill=MUTED,
                      text=self.preflop_note(st))
        return y + 13 * (cell + gap) + 14

    @staticmethod
    def preflop_note(st):
        if st["situation"] == "open":
            return f"{st['hero']}에서 처음 들어갈 때 오픈 레이즈 범위 (뒤 {pf.behind(st['hero'], st['players'])}명)"
        if st["situation"] == "vsopen":
            return f"{st['villain']} 오픈에 {st['hero']}에서 대응"
        if st["situation"] == "vs3bet":
            return f"{st['hero']} 오픈 후 {st['villain']}의 3벳에 대응"
        if st["situation"] == "vs4bet":
            return "내 3벳에 4벳이 돌아왔을 때 대응"
        if st["situation"] == "jam":
            return f"{st['stack_bb']:.0f}BB에서 {st['hero']} 첫 진입 올인 범위" + (" (앤티 반영)" if st["ante"] else "")
        return f"{st['villain']}의 {st['stack_bb']:.0f}BB 올인에 콜하는 범위" + (" (앤티 반영)" if st["ante"] else "")

    # --- 상황 입력 / 분석 ----------------------------------------------------
    FIELDS = [
        [("pot", "팟 (상대 벳 전)"), ("bet", "상대 벳 (콜 금액)"), ("my_bet", "내 벳 사이즈")],
        [("my_stack", "내 스택"), ("opp_stack", "상대 스택"), ("fold", "상대 폴드율 %")],
        [("sb", "SB"), ("bb", "BB"), ("ante", "앤티 합계")],
    ]

    def build_inputs(self, parent):
        saved = self.cfg.get("inputs", {})
        self.entries = {}
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="x", padx=P_SIDE, pady=(P_SIDE, 10))
        head = tk.Frame(box, bg=BG)
        head.grid(row=0, column=0, columnspan=3, sticky="we", pady=(0, 10))
        tk.Label(head, text="상황 입력", bg=BG, fg=TEXT, font=(F_UI, 12, "bold")).pack(side="left")
        self.auto_btn = tk.Label(head, font=(F_UI, 8, "bold"), padx=8, pady=2, cursor="hand2")
        self.auto_btn.pack(side="right", pady=(3, 0))
        self.auto_btn.bind("<Button-1>", lambda e: self.toggle_auto())
        tk.Label(box, text="단위 자유 (칩·BB) · 13만, 84만 7500, 21BB 형식 가능", bg=BG, fg=MUTED,
                 font=(F_UI, 8)).grid(row=99, column=0, columnspan=3, sticky="w")

        for r, row in enumerate(self.FIELDS):
            for col, (key, label) in enumerate(row):
                f = tk.Frame(box, bg=BG)
                f.grid(row=1 + r * 2, column=col, sticky="w", padx=(0, 10 if col < 2 else 0))
                tk.Label(f, text=label, bg=BG, fg=MUTED, font=(F_UI, 8)).pack(anchor="w")
                var = tk.StringVar(value=saved.get(key, ""))
                entry = tk.Entry(f, textvariable=var, width=10, font=(F_NUM, 10), bg=PANEL, fg=TEXT,
                                 insertbackground=TEXT, relief="flat", highlightthickness=1,
                                 highlightbackground=LINE, highlightcolor=ACCENT)
                entry.pack(anchor="w", ipady=3, pady=(2, 0))
                self.entries[key] = entry
                var.trace_add("write", lambda *_: self.on_input())
                self.inputs[key] = var
                extra = tk.Frame(box, bg=BG)
                extra.grid(row=2 + r * 2, column=col, sticky="w", pady=(3, 8))
                if key in ("bet", "my_bet"):
                    for name, frac in (("⅓", 1 / 3), ("½", 0.5), ("팟", 1.0)):
                        b = tk.Label(extra, text=name, bg="#232931", fg=TEXT, font=(F_UI, 8),
                                     padx=6, pady=0, cursor="hand2")
                        b.pack(side="left", padx=(0, 3))
                        b.bind("<Button-1>", lambda e, k=key, fr=frac: self.apply_preset(k, fr))
                        b.bind("<Enter>", lambda e, w=b: w.config(bg="#2c333d"))
                        b.bind("<Leave>", lambda e, w=b: w.config(bg="#232931"))

    def amount(self, key):
        return odds.parse_amount(self.inputs[key].get())

    def apply_preset(self, key, frac):
        pot = self.amount("pot")
        if pot:
            self.inputs[key].set(odds.format_amount(round(pot * frac, 2)))

    def on_input(self):
        self.cfg["inputs"] = {k: v.get() for k, v in self.inputs.items()}
        if not getattr(self, "_save_pending", False):
            self._save_pending = True

            def flush():
                self._save_pending = False
                save_config(self.cfg)
            self.root.after(800, flush)
        if hasattr(self, "side_canvas"):
            self.render()
            self.render_side()

    def render_side(self):
        values = {k: self.amount(k) for k in self.inputs}
        res = None if self.view.get("message") else self.result
        key = (repr(values), id(res), self.computing)
        if key == self.last_side:
            return
        self.last_side = key
        c, W, P = self.side_canvas, self.SIDE_W, P_SIDE
        c.delete("all")
        y = 6
        pot, bet, my_bet = values["pot"], values["bet"], values["my_bet"]
        stacks = [v for v in (values["my_stack"], values["opp_stack"]) if v]
        eff = min(stacks) if stacks else None
        eq = res["equity"]["equity"] if res else None
        outs = res.get("outs") if res else None

        def section(title):
            nonlocal y
            c.create_line(P, y, W - P, y, fill=LINE)
            y += 12
            c.create_text(P, y, anchor="nw", text=title, font=(F_UI, 10, "bold"), fill=TEXT)
            y += 26

        def line(label, value="", color=TEXT, note=None):
            nonlocal y
            t = c.create_text(P, y, anchor="nw", text=label, font=(F_UI, 9), fill=MUTED,
                              width=W - 2 * P - (100 if value else 0))
            if value:
                c.create_text(W - P, y, anchor="ne", text=value, font=(F_NUM, 10, "bold"), fill=color)
            y = max(y + 20, c.bbox(t)[3] + 6)
            if note:
                t = c.create_text(P, y - 2, anchor="nw", text=note, font=(F_UI, 8), fill=MUTED, width=W - 2 * P)
                y = c.bbox(t)[3] + 8

        def big(text, label, sub=None):
            nonlocal y
            c.create_text(P - 1, y - 8, anchor="nw", text=text, font=(F_NUM, 18, "bold"), fill=ACCENT)
            if sub:
                c.create_text(W - P, y - 2, anchor="ne", text=sub, font=(F_UI, 9, "bold"), fill=TEXT)
            c.create_text(W - P, y + 17, anchor="ne", text=label, font=(F_UI, 8), fill=MUTED)
            y += 42

        def verdict(ok):
            return ("+EV", WIN_C) if ok else ("−EV", LOSE_C)

        # 팟 오즈
        section("팟 오즈")
        if pot and bet:
            need = odds.pot_odds(pot, bet)
            big(f"{need * 100:.1f}%", f"콜 {odds.format_amount(bet)} → 최종 팟 {odds.format_amount(pot + 2 * bet)}",
                f"필요 승률  ·  팟의 {bet / pot * 100:.0f}% 벳")
            if eq is not None:
                tag, color = verdict(eq >= need)
                line(f"내 에쿼티 (랜덤 {res['opponents']}명)", f"{eq * 100:.1f}%  {tag}", color)
            if outs:
                prob = outs["by_river"] if outs["left"] == 2 else outs["next"]
                tag, color = verdict(prob >= need)
                line("드로우 완성 (리버까지)" if outs["left"] == 2 else "드로우 완성 (리버 1장)",
                     f"{prob * 100:.1f}%  {tag}", color,
                     note=f"턴 한 장 기준 {outs['next'] * 100:.1f}% — 턴에 또 벳이 나오면 이 값으로 판단"
                     if outs["left"] == 2 else None)
            if eq is None:
                line("카드가 인식되면 승률과 비교합니다")
        else:
            line("팟과 상대 벳을 입력하면 계산합니다")
        y += 2

        # 내재 배당
        section("내재 배당")
        hero = res["hero"] if res else []
        preflop = bool(res) and not res["board"]
        pocket_pair = len(hero) == 2 and hero[0] >> 2 == hero[1] >> 2
        if preflop and pocket_pair and bet and eff:
            ratio = odds.set_mining_ratio(eff, bet)
            ok = ratio >= 15
            line("셋마이닝  ·  유효 스택 ÷ 콜 금액", f"{ratio:.1f}배", WIN_C if ok else LOSE_C,
                 note="15~20배 이상이면 들어갈 가치가 있음" + ("" if ok else " — 지금은 부족"))
        elif outs and pot and bet:
            need_more = odds.implied_needed(pot, bet, outs["next"])
            if need_more == 0:
                line("다음 카드 기준으로도 팟 오즈 충분", "OK", WIN_C)
            else:
                line("완성 시 추가로 따내야 본전", odds.format_amount(need_more), WARN,
                     note=f"다음 카드 완성 확률 {outs['next'] * 100:.1f}% 기준")
                if values["opp_stack"]:
                    behind = max(0.0, values["opp_stack"] - bet)
                    ok = behind >= need_more
                    line("상대 뒷스택 (콜 이후)", odds.format_amount(behind), WIN_C if ok else LOSE_C,
                         note="뒷스택은 충분 — 상대가 쉽게 못 접는 성향이고 내 패가 숨겨져 있으면 콜 가능"
                         if ok else "뒷스택이 부족 — 내재 배당으로도 맞지 않음")
        elif preflop:
            line("포켓 페어로 상대 벳과 스택을 입력하면 셋마이닝을 판단합니다")
        else:
            line("플랍·턴 드로우에서 팟과 상대 벳을 입력하면 계산합니다")
        y += 2

        # SPR
        section("SPR")
        if eff and pot:
            value = odds.spr(eff, pot)
            zone, tip = odds.spr_zone(value)
            big(f"{value:.1f}", f"유효 스택 {odds.format_amount(eff)} ÷ 팟 {odds.format_amount(pot)}", zone)
            line(tip, note="SPR은 플랍이 열린 직후 팟 기준 수치입니다" if res and len(res["board"]) > 3 else None)
        else:
            line("스택과 팟을 입력하면 계산합니다")
        y += 2

        # 폴드 에쿼티
        section("폴드 에쿼티")
        if pot and my_bet:
            be = odds.breakeven_fold(pot, my_bet)
            line("순수 블러프 본전 폴드율", f"{be * 100:.1f}%", ACCENT)
            fold = values["fold"]
            if fold is not None and eq is not None:
                f = min(max(fold / 100, 0.0), 1.0)
                ev_value = odds.bet_ev(pot, my_bet, f, eq)
                line(f"벳 EV  ·  폴드율 {fold:.0f}%, 콜 당하면 에쿼티 {eq * 100:.1f}%",
                     ("+" if ev_value >= 0 else "−") + odds.format_amount(abs(ev_value)),
                     WIN_C if ev_value >= 0 else LOSE_C)
            elif fold is None:
                line("상대 폴드율을 입력하면 벳 EV를 계산합니다")
        else:
            line("팟과 내 벳 사이즈를 입력하면 계산합니다")
        y += 2

        # M-ratio
        section("M-ratio (토너먼트)")
        sb, bb, ante = values["sb"] or 0, values["bb"] or 0, values["ante"] or 0
        if values["my_stack"] and sb + bb + ante:
            m = odds.m_ratio(values["my_stack"], sb, bb, ante)
            zone, tip = odds.m_zone(m)
            big(f"{m:.1f}", f"내 스택 ÷ (SB + BB + 앤티) = {odds.format_amount(sb + bb + ante)}", zone)
            line(tip)
        else:
            line("내 스택과 SB·BB(·앤티)를 입력하면 계산합니다")
        c.config(height=y + P)


    def close(self):
        self.sct.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
