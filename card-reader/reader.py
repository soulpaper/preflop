"""화면의 카드 영역을 캡처해 인식하고 승률/아웃츠를 표시하는 테스트 도구.

사용 순서
  1. [자동 지정]  : 게임 창을 한 번 드래그하면 보드/내 카드/팟/스택/콜 금액 자리를 자동으로 찾음
     (직접 하려면 [카드]로 보드·내 카드 영역, [숫자]로 팟·스택 영역을 차례로 드래그)
  2. [카드 학습]  : 화면에 보이는 카드에 라벨(예: Ah, Td)을 입력해 템플릿 저장
  3. 이후 자동 인식 → 승률/아웃츠 계산
"""
import base64
import ctypes
import inspect
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from pathlib import Path

import cv2
import mss
import numpy as np

import evaluator as ev
import glyphs
import layout
from fastmatch import FastMatcher
import ocr
import odds
import preflop as pf

try:  # 화면 변화 판정 (±1px 어긋남·반짝임 무시, samples/로 맞춘 허용치). 없으면 아래 간단한 판정으로 대신함
    import stable
except Exception:
    stable = None

MSS = getattr(mss, "MSS", None) or mss.mss

try:  # 고해상도 배율(125%, 150%)에서 좌표가 어긋나지 않도록 물리 픽셀 기준으로 동작
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# exe(PyInstaller)로 실행하면 설정/템플릿을 exe 옆에 저장
BASE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
TEMPLATE_DIR = BASE / "templates"
LOG_PATH = BASE / "debug.log"
LOG_MAX = 1_000_000     # 이 크기를 넘으면 debug.log.1 로 넘기고 새로 씀


def log_debug(title, ctx=None):
    """except 블록에서 호출: 시각 + 제목 + traceback + (있으면) 압축한 ctx 를 debug.log 에 덧붙임. 로그 실패는 무시."""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX:
            os.replace(LOG_PATH, LOG_PATH.with_name("debug.log.1"))
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {title}\n{traceback.format_exc()}")
            if ctx is not None:
                try:
                    f.write("ctx: " + json.dumps(ctx, ensure_ascii=False, default=str, separators=(",", ":"))[:3000] + "\n")
                except Exception:
                    f.write(f"ctx: {str(ctx)[:3000]}\n")
            f.write("\n")
    except Exception:
        pass

REGIONS = [("board", "보드"), ("hero", "내 카드")]
NUMBER_STEPS = [("pot", "팟"), ("my_stack", "내 스택")]
OCR_INTERVAL = 0.8  # 초
UNKNOWN = "?"
BACK = "back"          # 뒷면 카드 (폴드 후 내 카드 등)
BACK_MAX_INK = 0.03    # 뒷면: 빨강/검정 글자 픽셀 비율이 이 미만 (앞면은 어둡게 처리돼도 0.14 이상)
BACK_MAX_VALUE = 160   # 뒷면: 회색 (실측 밝기 88, 앞면 220 / 어두운 앞면 113)
BACK_MAX_STD = 25      # 뒷면: 무늬가 고른 회색 (실측 편차 5)
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
    "ui_scale": 1.5,    # 처음 창 크기의 배율 (이후 배율은 창 크기에 맞춰 자동, A− / A+는 창 크기를 바꿈)
    "explain": True,    # 용어 설명 표시
    "analysis_mode": "auto",  # "auto": 내 차례에 자동 분석 / "click": [지금 계산]을 누를 때만 무거운 분석
}
SCALE_MIN, SCALE_MAX, SCALE_STEP = 1.0, 2.0, 0.1  # A- / A+ 단계. 실제 배율은 창 크기에 맞춰 정해짐
FIT_MIN = 0.5          # 창이 작으면 이 배율까지 줄여서라도 한 화면에 모두 보임 (스크롤 없음)
MIN_WINDOW_SCALE = 0.6  # 창 최소 크기 = 3열 배치가 이 배율로 들어가는 크기

# 화면 변화 판정: 반짝임·노이즈·은은한 글로우는 무시하고 숫자/카드가 바뀐 것만 잡는다
CHANGE_TOL = 16.0      # stable.distance 허용치 (반짝임/1px 흔들림 < 이 값 < 숫자 한 글자 변화)
SIG_SIDE = 64          # (stable.py가 없을 때) 축소 이미지 긴 변
SIG_TOL = (6.0, 48, 0.006)  # (stable.py가 없을 때) (평균 차이, 픽셀 차이 기준, 그 기준을 넘는 픽셀 비율)
SETTLE_MAX = 3         # 계속 움직이는 영역도 이 주기마다 한 번은 다시 읽음
STATE_INTERVAL = 1.0   # 테이블 상태 읽기 주기 (초)
STATE_REFRESH = 5.0    # 화면이 그대로여도 이 시간마다 다시 읽음

# --- 테마 -------------------------------------------------------------------
BG = "#0f1215"
PANEL = "#171b20"
LINE = "#2a313a"
SLOT = "#4a5563"
TEXT = "#eef0f2"
MUTED = "#b3bcc8"      # 보조 글자 (어두운 배경에서 잘 보이도록 밝게)
EXPLAIN_C = "#8fcbea"  # 용어 설명
ACCENT = "#2dd4bf"
WARN = "#fbbf24"
WIN_C, TIE_C, LOSE_C = "#34d399", "#7c8a9c", "#f87171"
CARD_FACE = "#f5f5f2"
RED_SUIT, BLACK_SUIT = "#d93a44", "#1b1e23"
CHIP = "#232931"
CHIP_HOVER = "#2c333d"

F_UI = "Malgun Gothic"
F_NUM = "Segoe UI"

# 글자 크기 (pt, 배율 1.0 기준)
FS_TITLE, FS_H, FS_BODY, FS_NOTE, FS_SMALL = 14, 13, 12, 11, 10
FS_HAND, FS_EQ, FS_BIG, FS_ACTION = 16, 38, 30, 30

# 용어 설명: (용어, 한 줄 요약 — 화면에 항상 표시, 자세한 설명 — 마우스를 올리면)
EXPLAIN = {
    "advice": ("추천", "지금 상황을 종합한 행동", "지금 카드·팟·스택·포지션·남은 인원을 종합한 행동"),
    "equity": ("에쿼티(승률)", "끝까지 갔을 때 이길 확률", "지금 카드로 끝까지 갔을 때 이길 확률"),
    "pot_odds": ("팟 오즈", "콜에 필요한 최소 승률 — 내 승률이 더 높으면 콜",
                 "콜하려면 필요한 최소 승률. 내 승률이 이보다 높으면 콜이 이득"),
    "outs": ("아웃츠", "다음 카드로 패를 완성해 주는 카드 수", "다음 카드로 내 패를 완성시켜 주는 남은 카드 수"),
    "implied": ("내재 배당", "완성되면 더 따낼 수 있어 콜할 가치가 생김",
                "지금은 손해여도, 완성되면 더 따낼 수 있어 콜할 가치가 생기는 경우"),
    "spr": ("SPR", "남은 스택 ÷ 팟 — 낮을수록 올인까지 가기 쉬움", "남은 스택 ÷ 팟. 낮을수록 올인까지 가기 쉬움"),
    "fold_eq": ("폴드 에쿼티", "내 벳에 상대가 접어서 바로 이기는 몫", "내가 벳했을 때 상대가 접어서 바로 이기는 몫"),
    "m": ("M-ratio", "블라인드·앤티를 몇 바퀴 버티나 (토너먼트)",
          "블라인드·앤티를 몇 바퀴 버틸 수 있는지 (토너먼트 생존 지표)"),
    "opp": ("상대 예상", "상대가 이번 핸드에서 한 액션으로 가능한 패를 추정",
            "상대의 프리플랍·포스트플랍 액션과 (믿을 만하면) 누적 성향으로 가능한 핸드 범위를 추정. 내 차례에만 계산"),
    "preflop": ("프리플랍 가이드", "내 자리·상황에서 이 핸드로 할 행동", "내 자리·상황에서 이 핸드로 할 행동"),
}

# 화면 배치: 섹션(추천·상태 / 카드 / 에쿼티 / 아웃츠·프리플랍 / 입력 요약 / 팟 오즈 / 내재 배당 / SPR / 폴드 에쿼티 /
# M-ratio)을 우선순위 순서로 가장 짧은 열에 쌓는다 (masonry). 열 수(1~3)와 배율은 창에 다 들어가는 가장 큰 것으로.
# 단위는 배율 1.0 기준 px
COL_W = 500            # 열 폭 기본값 (실제 폭은 창 모양에 맞춰 CW_MIN~CW_MAX 사이에서 정해짐)
CW_MIN, CW_MAX = 440, 620
PAD, GAP = 20, 16
MIN_H_UNITS = 900      # 창 최소 높이 계산용 (3열 기준 대략 최대 높이)
TABS = (("analysis", "분석"), ("inputs", "입력"))

# 섹션 id (기본 순서). 한 섹션은 항상 같은 열·같은 순서에 놓이고, 내용이 없으면 한 줄로 접힌 제목만 남는다.
# 사용자가 제목줄을 끌어 옮긴 배치는 cfg["section_layout"] = {"cols": [[id, ...], ...], "hidden": [id, ...]}
SECTION_IDS = ("top", "cards", "equity", "opp", "guide", "tabs", "pot", "implied", "spr", "fold", "m", "notes")
ANALYSIS_IDS = ("pot", "implied", "spr", "fold", "m")  # 분석 탭에서만 있는 섹션
SECTION_TITLES = {"top": "추천·상태", "cards": "카드", "equity": "에쿼티", "opp": "상대 예상",
                  "guide": "프리플랍 가이드·아웃츠", "tabs": "입력 요약", "pot": "팟 오즈", "implied": "내재 배당",
                  "spr": "SPR", "fold": "폴드 에쿼티", "m": "M-ratio", "notes": "메모"}
NO_HIDE = ("top", "tabs")  # 숨길 수 없는 섹션 (헤더 버튼·탭이 들어 있음)
COLLAPSED_TEXT = {  # 해당 섹션이 비었을 때 한 줄로 접어 보여줄 글
    "opp": "상대 예상 — 상대 액션이 읽히면 표시",
    "guide": "프리플랍 가이드 · 아웃츠 — 카드가 인식되면 표시",
    "pot": "팟 오즈 — 팟·상대 벳을 입력하면 표시",
    "implied": "내재 배당 — 드로우·포켓페어로 콜할 때 표시",
    "spr": "SPR — 스택·팟을 입력하면 표시",
    "fold": "폴드 에쿼티 — 팟·내 벳을 입력하면 표시",
    "m": "M-ratio — 내 스택·블라인드를 입력하면 표시",
    "notes": "메모 — 없음",
}
# 섹션이 채워졌을 때의 보통 최대 높이 (배율 1.0 기준 px, 측정값): 기본 배치와 배율을 정할 때 지금 작거나 접혀 있어도 이만큼 자리를 본다
NOMINAL_UNITS = {"top": 450, "cards": 108, "equity": 250, "opp": 310, "guide": 520, "tabs": 126, "pot": 180,
                 "implied": 170, "spr": 175, "fold": 97, "m": 126, "notes": 35}
RESERVE = 0.75  # 배치·배율을 정할 때 NOMINAL_UNITS 의 이 비율만큼은 늘 자리를 잡음 (다 잡으면 글자가 너무 작아짐. 넘치면 한 번 줄임)
NODRAG_TAGS = ("btn_", "badge", "tab", "sum")  # 이 태그가 붙은 것을 누르면 끌기가 아니라 그 동작


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


def signature(img):
    """영역 이미지의 변화 비교용 요약."""
    if stable is not None:
        return stable.Signature(img)
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    f = min(1.0, SIG_SIDE / max(1, h, w))
    return cv2.resize(gray, (max(1, round(w * f)), max(1, round(h * f))),
                      interpolation=cv2.INTER_AREA).astype(np.float32)


def sig_changed(prev, cur):
    """두 요약이 '의미 있게' 다른지. 노이즈·밝기 깜빡임·작은 반짝임은 같은 것으로 본다."""
    if prev is None or cur is None:
        return True
    if stable is not None:
        a, b = prev.small, cur.small
        # 빠른 확인: 픽셀 차이의 최댓값이 허용치 이하면 거리(칸 평균의 최댓값)도 허용치 이하 → 안 바뀜
        if a.shape == b.shape and prev.shape == cur.shape and float(np.abs(a - b).max()) <= CHANGE_TOL:
            return False
        return stable.distance(prev, cur) > CHANGE_TOL
    if prev.shape != cur.shape:
        return True
    d = np.abs(prev - cur)
    mean_tol, spot, frac = SIG_TOL
    return float(d.mean()) > mean_tol or float((d > spot).mean()) > frac


class Steady:
    """영역별 변화 추적: 마지막으로 읽었을 때와 '의미 있게' 달라졌을 때만 다시 읽게 한다.

    settle=True 이면 변화가 멈춘 뒤(직전 프레임과 같아진 뒤)에 읽는다 → 칩 이동·글로우 애니메이션 중에는
    읽지 않아 오인식도 줄어든다. 계속 움직이는 영역은 SETTLE_MAX 주기마다 한 번 읽는다."""

    def __init__(self, settle=True):
        self.settle = settle
        self.state = {}

    def check(self, key, img):
        sig = signature(img)
        st = self.state.get(key)
        if st is None:
            self.state[key] = {"ref": sig, "prev": sig, "wait": 0}
            return True
        prev, st["prev"] = st["prev"], sig
        if not sig_changed(st["ref"], sig):
            st["wait"] = 0
            return False
        st["wait"] += 1
        if self.settle and sig_changed(prev, sig) and st["wait"] < SETTLE_MAX:
            return False  # 아직 움직이는 중
        st["ref"], st["wait"] = sig, 0
        return True

    def accept(self, key):
        """지금 프레임을 읽은 기준으로 삼음."""
        st = self.state.get(key)
        if st is not None:
            st["ref"], st["wait"] = st["prev"], 0

    def reset(self):
        self.state = {}


def to_json(value):
    """numpy 값/튜플이 섞인 결과를 config.json에 저장할 수 있게."""
    return json.loads(json.dumps(value, ensure_ascii=False,
                                 default=lambda o: o.item() if hasattr(o, "item") else str(o)))


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
    if text in ("", UNKNOWN, BACK):
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


def is_card_back(crop):
    """글자(빨강/검정 잉크)가 없는 고른 회색 카드 = 뒷면."""
    inner = cv2.resize(crop, NORM_SIZE, interpolation=cv2.INTER_AREA)[8:-8, 8:-8]
    b, g, r = [inner[:, :, i].astype(np.int32) for i in range(3)]
    hsv = cv2.cvtColor(inner, cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1].astype(np.int32), hsv[:, :, 2].astype(np.int32)
    red = (r - g > 45) & (r - b > 30)
    black = (v < 0.5 * np.percentile(v, 95)) & (s < 90)
    return ((red | black).mean() < BACK_MAX_INK and np.median(v) < BACK_MAX_VALUE
            and v.std() < BACK_MAX_STD)


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
            return self.check_suit(crop, label, full[label])
        ranked = sorted(top.values(), reverse=True)
        label = max(top, key=top.get)
        second = ranked[1] if len(ranked) > 1 else 0.0
        if top[label] >= TOP_THRESHOLD and top[label] - second >= TOP_MARGIN:
            return self.check_suit(crop, label, top[label])
        found = self.glyphs.match(crop)
        if found:
            return found
        return UNKNOWN, full[max(full, key=full.get)]

    def check_suit(self, crop, label, score):
        """카드 전체 비교는 작은 코너 무늬(♠/♣, ♥/♦)를 헷갈릴 수 있어 무늬 판별로 한 번 더 확인.
        무늬 판별이 확신하는데 다르면 숫자+무늬 결과를 쓰고, 그것도 없으면 모름."""
        suit = self.glyphs.suit(crop)
        if suit is None or suit == label[1]:
            return label, score
        return self.glyphs.match(crop) or (UNKNOWN, score)


def analyze(hero, board, opponents, iterations=None):
    """iterations=None 이면 에쿼티 시뮬레이션 없이 가벼운 결과만 (카드·족보·아웃츠). 에쿼티는 None."""
    res = {"equity": ev.equity(hero, board, opponents, iterations) if iterations else None}
    res["hand"] = ev.hand_name(ev.evaluate(hero + board)) if board else preflop_name(hero)
    res["hero"], res["board"] = list(hero), list(board)
    res["opponents"] = opponents
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
    if label == BACK:
        round_rect(c, x, y, x + w, y + h, r, fill="#4a4f57", outline="#8b9099", width=1.5)
        return
    if label == UNKNOWN:
        round_rect(c, x, y, x + w, y + h, r, fill=PANEL, outline=WARN, width=1.5)
        c.create_text(x + w / 2, y + h / 2, text="?", fill=WARN, font=(F_NUM, -int(h * 0.42), "bold"))
        return
    rank, suit = label[0], label[1]
    color = RED_SUIT if suit in "hd" else BLACK_SUIT
    round_rect(c, x, y, x + w, y + h, r, fill=CARD_FACE, outline="")
    rank_text = "10" if rank == "T" else rank
    # 글자 크기는 픽셀 단위(음수) → 카드 크기에 정확히 비례 (DPI와 무관)
    c.create_text(x + w * 0.12, y + h * 0.04, anchor="nw", text=rank_text, fill=color,
                  font=(F_NUM, -max(9, int(h * 0.27)), "bold"))
    c.create_text(x + w * 0.5, y + h * 0.66, text=ev.SUIT_SYMBOLS[suit], fill=color,
                  font=("Segoe UI Symbol", -max(10, int(h * 0.48))))


class Button:
    """캔버스 위의 납작한 버튼 (hover 표시 포함)."""

    def __init__(self, app, key, command):
        self.app, self.key, self.command = app, key, command
        self.poly = None
        self.primary = False
        self.canvas = None

    def fill(self):
        hover = self.app.hover == self.key
        if self.primary:
            return "#5eead4" if hover else ACCENT
        return CHIP_HOVER if hover else CHIP

    def refresh(self):
        if self.poly is not None and self.canvas is not None:
            try:
                self.canvas.itemconfig(self.poly, fill=self.fill())
            except tk.TclError:
                pass

    def draw(self, c, x0, y0, x1, y1, text, primary=False, font=(F_UI, 9), r=7):
        self.primary = primary
        self.canvas = c
        tag = f"btn_{self.key}"
        self.poly = round_rect(c, x0, y0, x1, y1, r, fill=self.fill(), outline="", tags=tag)
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
        self.shot = shot  # 원본 해상도 ([자동 지정]은 이 화면을 그대로 분석)
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


# --- 자동 지정 확인 -----------------------------------------------------------
AUTO_COLORS = {"board": "#38bdf8", "hero": "#f472b6", "pot_total": WARN, "my_stack": WIN_C, "call": "#fb923c"}
AUTO_NAMES = {"board": "보드", "hero": "내 카드", "pot_total": "POT", "my_stack": "내 스택", "call": "콜 금액",
              "pill": "가운데 팟"}


class AutoSetupDialog:
    """[자동 지정] 결과 확인: 게임 창 스크린샷 위에 찾은 영역을 표시하고 저장/취소."""

    def __init__(self, root, img, res, on_save):
        self.on_save = on_save
        self.win = tk.Toplevel(root, bg=BG)
        self.win.title("자동 지정")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        dark_title_bar(self.win)

        tk.Label(self.win, text="자동 지정 결과", bg=BG, fg=TEXT, font=(F_UI, 13, "bold")).pack(
            anchor="w", padx=20, pady=(16, 2))
        values = res.get("values", {})
        lines = [f"찾은 영역: {layout.summary(res)}"]
        nums = [f"{AUTO_NAMES.get(k, '상대 ' + k[3:])} {ocr.format_value(*values[k])}"
                for k in ["pot_total", "my_stack", "call"] + sorted((k for k in res if k.startswith("opp")),
                                                                     key=lambda k: int(k[3:]))
                if values.get(k)]
        if nums:
            lines.append("  ·  ".join(nums))
        if not res.get("found", {}).get("board", True):
            lines.append("보드 카드가 안 보여서 보드 자리는 테이블 위치로 추정했습니다 (점선).")
        if "call" in res and not res["found"].get("call"):
            lines.append("콜 버튼이 안 보여서 콜 금액 자리는 추정했습니다 (점선). 내 차례에 콜 금액을 읽습니다.")
        if res.get("blinds"):
            b = res["blinds"]
            lines.append(f"블라인드 SB {b['sb']} / BB {b['bb']} / 앤티 {b['ante']} → SB·BB·앤티 합계 입력")
        tk.Label(self.win, bg=BG, fg=MUTED, font=(F_UI, 9), justify="left", wraplength=860,
                 text="\n".join(lines)).pack(anchor="w", padx=20, pady=(0, 10))

        h, w = img.shape[:2]
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        scale = min(1.0, min(900, sw * 0.8) / w, min(600, sh * 0.65) / h)
        self.photo = to_photo((img * 0.7).astype(np.uint8), int(w * scale), int(h * scale))
        c = tk.Canvas(self.win, width=int(w * scale), height=int(h * scale), bg=BG, highlightthickness=0)
        c.pack(padx=20)
        c.create_image(0, 0, anchor="nw", image=self.photo)
        for rect, _ in res.get("bets", []):
            self._box(c, rect, scale, MUTED, "", dash=(2, 2))
        if "pill" in res:
            self._box(c, res["pill"], scale, MUTED, "", dash=(2, 2))
        for key in [k for k in res if k in AUTO_COLORS or k.startswith("opp")]:
            name = AUTO_NAMES.get(key) or f"상대{key[3:]}"
            dash = () if res.get("found", {}).get(key, True) else (4, 3)
            self._box(c, res[key], scale, AUTO_COLORS.get(key, ACCENT), name, dash=dash)

        buttons = tk.Frame(self.win, bg=BG)
        buttons.pack(fill="x", padx=20, pady=(12, 18))
        tk.Label(buttons, bg=BG, fg=MUTED, font=(F_UI, 8),
                 text="게임 창을 옮기거나 크기를 바꾸면 다시 [자동 지정]을 누르세요").pack(side="left")
        LearnDialog._flat_button(buttons, "저장", self.save, primary=True).pack(side="right")
        LearnDialog._flat_button(buttons, "취소", self.win.destroy).pack(side="right", padx=8)
        self.win.bind("<Return>", lambda e: self.save())
        self.win.bind("<Escape>", lambda e: self.win.destroy())

    @staticmethod
    def _box(c, rect, scale, color, name, dash=()):
        x, y, w, h = [v * scale for v in rect]
        c.create_rectangle(x, y, x + w, y + h, outline=color, width=2 if name else 1, dash=dash)
        if name:
            c.create_text(x + 1, y - 2, anchor="sw", text=name, fill=color, font=(F_UI, 8, "bold"))

    def save(self):
        self.win.destroy()
        self.on_save()


# --- 메인 창 ----------------------------------------------------------------
ADVICE_COLORS = {"raise": "#f0b44c", "call": "#4ade80", "fold": LOSE_C, "jam": "#fb923c", "check": "#60a5fa"}
CONFIDENCE = {"high": "확신 높음", "medium": "확신 보통", "low": "확신 낮음"}
STREET_KEYS = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}
STREET_NAMES = {0: "프리플랍", 3: "플랍", 4: "턴", 5: "리버"}
STATE_KEYS = ("players", "dealt", "in_hand", "opponents_in_hand", "hero_folded", "hero_in_hand", "dealer", "hero_pos",
              "pos_players", "to_act", "preaction", "call", "can_check", "can_raise", "facing_allin", "preflop")


def _optional(name):
    """다른 모듈(테이블 상태 읽기, 추천)이 없거나 import 중 오류가 나면 None → 해당 표시만 숨김."""
    try:
        return __import__(name)
    except Exception:
        return None


def _number(v):
    """숫자 / (값, 단위) / 문자열 → float 또는 None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (list, tuple)):
        return _number(v[0]) if v else None
    if isinstance(v, str):
        return odds.parse_amount(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- 포스트플랍 추천 ctx: 위치(IP) · 주도권 · 플랍 라인 (순수 함수; 상태 스레드/Tk 스레드 어디서든) ----------
HAND_STREETS = ("preflop", "flop", "turn", "river")
TRACK_FATAL = frozenset({"mid_hand_start", "bet_decreased", "ambiguous_order", "tracker_error", "gap",
                         "allin_unread", "incomplete"})   # handlog.FATAL 과 같다 (모듈이 없을 때를 위한 기본값)


def hero_ip_from(ts):
    """테이블 상태 → 내가 남은 모든 상대보다 포스트플랍에 늦게 행동하는가 (preflop.postflop_order, 클수록 늦음). 모르면 None."""
    try:
        n = int(_number(ts.get("pos_players")))
        seats = ts.get("seats") or []
        hero = next((s for s in seats if s.get("key") == "hero"), None)
        hp = ts.get("hero_pos") or (hero or {}).get("pos")
        opps = [s for s in seats if s.get("in_hand") and s.get("key") != "hero"]
        if not hp or not opps:
            return None
        mine = pf.postflop_order(hp, n)
        return all(mine > pf.postflop_order(s.get("pos"), n) for s in opps)   # 포지션을 모르면 ValueError → None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def flop_line_from(acts, turn_pot):
    """플랍 액션 [{act, to}] + 턴 시작 팟(BB) → solverlib 라인 'x_x' / 'x_bet33_c' / 'x_bet75_c' / 'bet33_c' / 'bet75_c'.
    레이즈·폴드가 섞였거나 크기를 못 잡으면 None.  벳 크기 = 벳 / (플랍 시작 팟 = 턴 시작 팟 - 2 × 벳), 0.5 아래면 33 아니면 75."""
    seq = [a for a in acts or [] if a.get("act") != "post"]
    kinds = [a.get("act") for a in seq]
    if kinds == ["check", "check"]:
        return "x_x"
    if kinds == ["check", "bet", "call"]:
        bet, prefix = seq[1], "x_"
    elif kinds == ["bet", "call"]:
        bet, prefix = seq[0], ""
    else:
        return None
    to = _number(bet.get("to"))
    if not to or not turn_pot:
        return None
    start = turn_pot - 2 * to
    if start <= 0:
        return None
    return f"{prefix}bet{33 if to / start < 0.5 else 75}_c"


def summarize_hand(h, fatal=TRACK_FATAL):
    """handlog.Tracker.hand (진행 중 핸드; 내 좌석 'hero' 의 액션도 들어 있다) → 주도권·라인 요약 (상태 스레드에서만 호출).
    {hero_cards, street(0~3), reliable, last_aggr: {스트리트: 마지막 벳/레이즈한 좌석 or None}, pre_raises, pre_seen,
     opener, flop(액션 목록), opp_raise(이번 스트리트에 상대 레이즈)}"""
    acts = {s: [dict(a) for a in h.actions.get(s, [])] for s in HAND_STREETS}
    last = {}
    for s in HAND_STREETS:
        agg = [a for a in acts[s] if a["act"] in ("bet", "raise")]
        last[s] = agg[-1]["seat"] if agg else None
    pre = [a for a in acts["preflop"] if a["act"] != "post"]
    raises = [a for a in pre if a["act"] == "raise"]
    cur = acts[HAND_STREETS[min(max(h.street, 0), 3)]]
    return {"hero_cards": tuple(h.hero_cards or ()), "street": h.street,
            "reliable": not ({i["code"] for i in h.issues} & set(fatal)),
            "last_aggr": last, "pre_raises": len(raises), "pre_seen": bool(pre),
            "opener": raises[0]["seat"] if raises else None, "flop": acts["flop"],
            "opp_raise": any(a["act"] == "raise" and a["seat"] != "hero" for a in cur)}


def pre_aggressor_memo(pre, hero):
    """프리플랍 마지막 프레임들의 테이블 상태 → 내가 마지막 레이저인가 (True/False), 모르면 None.
    레이즈가 있고 마지막 레이즈한 상대가 없으면 내가 올린 것 (hero_bet > 1BB), 상대가 있으면 상대가 마지막."""
    try:
        if not pre or not hero or (pre.get("raises") or 0) < 1:
            return None
        if pre.get("villain_key"):
            return False
        return (_number(pre.get("hero_bet")) or 0.0) > 1.05
    except (TypeError, AttributeError):
        return None


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _Rect), ("rcWork", _Rect), ("dwFlags", ctypes.c_ulong)]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def monitor_work_area(root, point=None):
    """(left, top, right, bottom): 창(또는 point)이 있는 모니터의 작업 영역 (작업 표시줄 제외, 물리 픽셀)."""
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [_Point, ctypes.c_ulong]
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if point is not None:
            mon = user32.MonitorFromPoint(_Point(int(point[0]), int(point[1])), 2)  # 2 = 가장 가까운 모니터
        else:
            mon = user32.MonitorFromWindow(user32.GetParent(root.winfo_id()), 2)
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            r = info.rcWork
            if r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 48


def blend(color, toward, t):
    """색을 toward 쪽으로 t만큼 섞음 (흐리게 표시용)."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(toward[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


class Tooltip:
    """마우스를 올리면 뜨는 설명 창."""

    def __init__(self, app):
        self.app, self.win = app, None

    def show(self, text, x, y):
        self.hide()
        a = self.app
        win = tk.Toplevel(a.root, bg=LINE)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        tk.Label(win, text=text, bg="#222a33", fg=TEXT, font=a.font(FS_NOTE), justify="left",
                 wraplength=a.s(340), padx=a.s(10), pady=a.s(6)).pack(padx=1, pady=1)
        win.geometry(f"+{x + a.s(14)}+{y + a.s(16)}")
        self.win = win

    def hide(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


class App:
    # 지금 그리는 블록의 폭과 안쪽 여백 (배율 1.0 기준 픽셀). 실제 크기는 self.s(...)
    W = COL_W
    P = PAD

    def __init__(self):
        self.cfg = load_config()
        self.matcher = Matcher()
        self.sct = MSS()
        self.tablestate = _optional("tablestate")
        self.advisor = _optional("advisor")
        self.detected = {}
        self.memory = {}         # 핸드 동안 읽은 카드 (가려져도 유지)
        self.card_cache = {}     # 자리별 (축소 이미지, 라벨): 카드 그림이 그대로면 다시 매칭하지 않음
        self.view = {}
        self.result = None
        self.computing = False
        self.analysis_cache = {}  # 같은 카드 상태로 돌아오면 다시 계산하지 않음 (숫자 깜빡임 방지)
        self.analysis_starts = 0
        self.toast = None
        self.hover = None
        self.pending = self.confirmed = None
        self.computed_state = None
        self.generation = 0
        self.last_render = None
        self.ocr_queue = queue.Queue()
        self.ocr_pending = None
        self.ocr_values = {}
        self.ocr_written = {}    # 입력칸별로 OCR이 마지막에 쓴 글자 (같은 값으로 수동 입력을 덮지 않음)
        self.state_queue = queue.Queue()
        self.state_pending = self.state_key = None
        self.state_errors = 0
        self.table_state = None  # tablestate.read 결과 (없으면 상태 줄 숨김)
        self.advice = None       # advisor.recommend 결과 (없으면 추천 숨김)
        self.advice_key = None
        self.advice_after = None
        self.advice_status = None   # 추천이 없을 때 패널에 보여줄 이유 (글자, 종류, 버튼들)
        self.advice_why = None
        self.advice_stale = False   # 내 차례가 아니라 다시 계산하지 않은 '이전 계산'
        self.advice_cards = None
        self.result_state = None    # self.result 가 계산된 (내 카드, 보드, 상대 수)
        self.force_calc = False     # [지금 계산]: 내 차례가 아니어도 한 번 계산
        self.err_note = None        # 상태 읽기 등 예외가 났을 때 패널에 보일 짧은 안내 (자세한 내용은 debug.log)
        self.last_err_sig = None
        self.manual = set()      # 이번 핸드에서 직접 고른 항목 ('opponents', 'preflop') → 자동 채우기 중지
        self.last_hero = None
        self.closed = False
        self.tab = self.cfg.get("tab", "analysis")
        self.track = {}             # 상태 스레드가 채우는 최신 값 {names, profiles, live, reads, t, hist}
        self.pre_memo = None        # {hero, agg}: 이번 핸드 프리플랍 마지막 레이저가 나인지 (테이블 상태에서)
        self.store = self.name_reader = self.tracker = self.recorder = None
        self.popup = None          # 게임 '유저 정보' 팝업 읽기 (userinfo.PopupWatcher)
        self.pending_toast = None  # 상태 스레드 → Tk 스레드 알림
        self.handread = self.rangeq = None
        self.rec_on, self.rec_t0 = False, None   # 기록 모드 (항상 꺼진 채로 시작)
        self.calib_seen = None
        self.cols_used = 0
        self.col_w = COL_W       # 지금 쓰는 열 폭 (배율 1.0 기준 px)
        self.col_bottoms = []
        self.inputs_frame = self.inputs_item = self.inputs_k = None
        self.placed_inputs = None

        self.root = tk.Tk()
        self.root.title("Card Reader")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        dark_title_bar(self.root)
        self.dpi = self.root.winfo_fpixels("1i") / 96.0
        # 창 크기는 자유롭게 (가장자리 드래그). 내용은 항상 창 하나에 다 들어가도록 배율을 맞춤
        self.root.resizable(True, True)
        self.min_w = round((3 * CW_MIN + 2) * MIN_WINDOW_SCALE * self.dpi) + 2
        self.min_h = round(MIN_H_UNITS * MIN_WINDOW_SCALE * self.dpi)
        self.root.minsize(self.min_w, self.min_h)
        self.resize_after = self.save_after = None
        self.set_k(float(self.cfg.get("ui_scale") or DEFAULT_CONFIG["ui_scale"]))
        x, y, w, h = self.initial_rect()
        self.win_w, self.win_h = w, h
        self.layout_lock = None  # (창 w, h, 배율, 열 폭, 열 수): 창 크기가 그대로면 배율·배치 고정
        self.slot_cols = None    # 고정된 섹션 배치: 열마다 섹션 id 목록 (잠금과 함께 정해지고 풀림)
        self.sticky = {}         # {탭: {섹션 id: 본 적 있는 가장 큰 높이 (배율 1.0 기준)}} — 잠금 동안 자리 높이 유지
        self.drag = None         # 제목줄 끌기 상태
        self.handles, self.slot_rects, self.handle_local = [], [], {}
        self._cursor, self._sid = "", None
        self.fold_view = None    # 폴드 후 남겨 두는 직전 핸드 화면 (다음 핸드 카드가 오면 해제)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.tip = Tooltip(self)
        self.canvas = tk.Canvas(self.root, width=w, height=h, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.buttons = {
            "auto": Button(self, "auto", self.auto_setup),
            "region": Button(self, "region", self.select_regions),
            "learn": Button(self, "learn", self.learn),
            "numbers": Button(self, "numbers", self.select_numbers),
            "minus": Button(self, "minus", lambda: self.change_opponents(-1)),
            "plus": Button(self, "plus", lambda: self.change_opponents(1)),
            "rec": Button(self, "rec", self.toggle_record),
            "calc": Button(self, "calc", self.calc_now),
            "calc2": Button(self, "calc2", self.calc_now),
            "st_learn": Button(self, "st_learn", self.learn),
            "st_auto": Button(self, "st_auto", self.auto_setup),
            "smaller": Button(self, "smaller", lambda: self.change_scale(-SCALE_STEP)),
            "bigger": Button(self, "bigger", lambda: self.change_scale(SCALE_STEP)),
            "explain": Button(self, "explain", self.toggle_explain),
            "layout": Button(self, "layout", self.layout_menu),
            "mode": Button(self, "mode", self.toggle_mode),
            "loose_dn": Button(self, "loose_dn", lambda: self.step_looseness(-1)),
            "loose_up": Button(self, "loose_up", lambda: self.step_looseness(1)),
        }
        self.make_inputs()
        self.root.bind("<Configure>", self.on_configure)
        self.canvas.bind("<ButtonPress-1>", self.drag_press, add="+")
        self.canvas.bind("<B1-Motion>", self.drag_move, add="+")
        self.canvas.bind("<ButtonRelease-1>", self.drag_release, add="+")
        self.canvas.bind("<Motion>", self.hover_handle, add="+")
        self.canvas.bind("<Button-3>", self.section_menu, add="+")
        self.root.bind("<Escape>", self.drag_cancel, add="+")
        self.root.after(500, self.reclamp)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if ocr.available():
            threading.Thread(target=self.ocr_worker, daemon=True).start()
        if self.tablestate is not None:
            threading.Thread(target=self.state_worker, daemon=True).start()
        self.root.after(100, self.tick)

    # --- 배율 ---------------------------------------------------------------
    def set_k(self, scale):
        """배율 바꾸기 (글자 캐시 초기화)."""
        self.scale = round(min(SCALE_MAX, max(FIT_MIN, scale)), 3)
        self.k = self.scale * self.dpi
        self._fonts = {}
        self._tag = 0

    def s(self, x):
        """배율 1.0 기준 픽셀 → 실제 픽셀."""
        return int(round(x * self.k))

    def fs(self, pt):
        """배율 1.0 기준 pt → 실제 글자 크기 (픽셀 단위 음수: 배치 계산과 정확히 같은 비율)."""
        return -max(9, int(round(pt * 96 / 72 * self.k)))

    def font(self, pt, weight="normal", family=F_UI):
        return family, self.fs(pt), weight

    def measure(self, text, pt, weight="normal", family=F_UI):
        key = (family, self.fs(pt), weight)
        f = self._fonts.get(key)
        if f is None:
            f = self._fonts[key] = tkfont.Font(family=family, size=key[1], weight=weight)
        return f.measure(text)

    def reset_lock(self):
        """배율·열·섹션 배치·자리 높이 고정을 풀어 다음 그리기에서 한 번 새로 정하게 함."""
        self.layout_lock = None
        self.slot_cols = None
        self.sticky = {}

    # --- 창 크기 <-> 배율 ------------------------------------------------------
    def work_area(self, point=None):
        return monitor_work_area(self.root, point)

    def frame_size(self):
        """제목 표시줄·테두리 두께 (바깥 크기 - 안쪽 크기)."""
        try:
            if self.root.winfo_ismapped():
                r = _Rect()
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    fw = r.right - r.left - self.root.winfo_width()
                    fh = r.bottom - r.top - self.root.winfo_height()
                    if 0 <= fw < 200 and 0 <= fh < 200:
                        return fw, fh
        except Exception:
            pass
        return round(16 * self.dpi), round(40 * self.dpi)

    def clamp_rect(self, x, y, w, h):
        """창(바깥 왼쪽 위 x, y / 안쪽 w, h)이 모니터 작업 영역(작업 표시줄 제외)을 넘지 않게."""
        area = self.work_area((x + w / 2, y + h / 2))
        fw, fh = self.frame_size()
        aw, ah = area[2] - area[0] - fw, area[3] - area[1] - fh
        w = int(min(max(w, self.min_w), aw))
        h = int(min(max(h, self.min_h), ah))
        x = int(max(area[0], min(x, area[2] - w - fw)))
        y = int(max(area[1], min(y, area[3] - h - fh)))
        self.root.maxsize(int(aw), int(ah))
        return x, y, w, h

    def initial_rect(self):
        win = self.cfg.get("window")
        if isinstance(win, list) and len(win) == 4 and all(isinstance(v, (int, float)) for v in win):
            x, y, w, h = win  # 저장된 위치 (모니터 구성이 바뀌었어도 clamp_rect가 화면 안으로)
        else:  # 처음: 3열 배치를 저장된 배율로 (모니터보다 크면 clamp_rect가 줄이고, 배율은 창에 맞춰짐)
            area = self.work_area()
            w = int((3 * COL_W + 2) * self.k)
            h = int(MIN_H_UNITS * self.k)
            x, y = area[0] + 40, area[1] + 20
        return self.clamp_rect(x, y, w, h)

    def reclamp(self):
        """창이 뜬 뒤 실제 테두리 두께로 다시 확인."""
        if self.closed:
            return
        r = self.root
        cur = (r.winfo_x(), r.winfo_y(), r.winfo_width(), r.winfo_height())
        x, y, w, h = self.clamp_rect(*cur)
        if (x, y, w, h) != cur:
            r.geometry(f"{w}x{h}+{x}+{y}")

    def on_configure(self, e):
        if e.widget is not self.root:
            return
        if self.resize_after is not None:
            self.root.after_cancel(self.resize_after)
        self.resize_after = self.root.after(150, self.apply_window_size)  # 드래그 중에는 다시 그리지 않음

    def apply_window_size(self):
        self.resize_after = None
        if self.closed:
            return
        r = self.root
        w, h = r.winfo_width(), r.winfo_height()
        if w < 50 or h < 50:
            return
        self.cfg["window"] = [r.winfo_x(), r.winfo_y(), w, h]
        self.save_later()
        if (w, h) != (self.win_w, self.win_h):
            self.win_w, self.win_h = w, h
            self.reset_lock()
            self.render(force=True)

    def change_scale(self, delta):
        """A- / A+: 배율을 0.1씩. 창 크기를 같은 비율로 바꾸고(모니터 안에서), 배율은 창에 맞춰 다시 정해짐."""
        steps = self.scale / SCALE_STEP
        target = (math.floor(steps + 1e-6) + 1 if delta > 0 else math.ceil(steps - 1e-6) - 1) * SCALE_STEP
        target = min(SCALE_MAX, max(FIT_MIN, target))
        f = target / self.scale
        r = self.root
        x, y, w, h = self.clamp_rect(r.winfo_x(), r.winfo_y(), round(self.win_w * f), round(self.win_h * f))
        if (w, h) == (self.win_w, self.win_h):
            self.show_toast("모니터 크기 안에서 가장 큰 크기입니다" if delta > 0 else "가장 작은 크기입니다")
            return
        self.win_w, self.win_h = w, h
        self.cfg["window"] = [x, y, w, h]
        self.save_later()
        r.geometry(f"{w}x{h}+{x}+{y}")
        self.reset_lock()
        self.render(force=True)

    def window_ok(self):
        try:
            return self.root.winfo_ismapped() and self.root.state() == "normal"
        except tk.TclError:
            return False

    def save_later(self):
        if self.save_after is None:
            def flush():
                self.save_after = None
                save_config(self.cfg)
            self.save_after = self.root.after(800, flush)

    def toggle_explain(self):
        self.cfg["explain"] = not self.cfg.get("explain", True)
        save_config(self.cfg)
        self.reset_lock()
        self.render(force=True)

    # --- 캔버스 도우미 ---------------------------------------------------------
    def new_tag(self, prefix):
        self._tag += 1
        return f"{prefix}{self._tag}"

    def bind_tip(self, c, tag, text):
        c.tag_bind(tag, "<Enter>", lambda e: self.tip.show(text, e.x_root, e.y_root))
        c.tag_bind(tag, "<Leave>", lambda e: self.tip.hide())

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
                self.reset_ocr()
                self.refresh_auto_toggle()
                self.show_toast(f"숫자 영역 {len(rects)}개를 저장했습니다")
        RegionSelector(self.root, done, steps=NUMBER_STEPS, repeat=("opp", "상대 스택 {}"),
                       hint="숫자가 표시되는 자리를 글자에 맞게 드래그")

    def auto_setup(self):
        """[자동 지정]: 게임 창을 한 번 드래그 → 그 화면에서 카드/숫자 영역을 찾아 확인 창을 띄움."""
        if not ocr.available():
            self.show_toast("이 PC에서는 Windows 한국어 OCR을 쓸 수 없습니다")
            return
        selector = None

        def done(rects):
            rect = rects.get("table")
            if not rect:
                return
            x0, y0 = rect[0] - selector.mon["left"], rect[1] - selector.mon["top"]
            img = selector.shot[y0:y0 + rect[3], x0:x0 + rect[2]].copy()
            self.show_toast("게임 화면 분석 중…")

            def work():
                try:
                    res, err = layout.calibrate(img), None
                except Exception as exc:  # OCR 실패 등
                    res, err = None, str(exc)
                self.root.after(0, lambda: self.auto_result(rect, img, res, err))
            threading.Thread(target=work, daemon=True).start()

        selector = RegionSelector(self.root, done, steps=[("table", "게임 창")],
                                  hint="게임 창 전체(테이블, 아래 버튼까지)를 드래그")

    def auto_result(self, rect, img, res, err):
        if err or not res:
            self.show_toast(f"자동 지정 실패: {err}" if err else "자동 지정 실패")
            return
        if "my_stack" not in res and "board" not in res:
            self.show_toast("테이블을 찾지 못했습니다. 게임 창 전체를 드래그하세요")
            return
        AutoSetupDialog(self.root, img, res, lambda: self.save_auto(rect, res))

    def save_auto(self, rect, res):
        """찾은 영역(게임 창 기준 좌표)을 화면 좌표로 바꿔 저장. 게임 창 위치와 분석 결과 전체도 저장
        (테이블 상태 읽기가 게임 창 기준 좌표로 사용)."""
        ox, oy = rect[0], rect[1]

        def screen(r):
            return [r[0] + ox, r[1] + oy, r[2], r[3]]
        if "board" in res and "hero" in res:
            self.cfg["regions"] = {"board": screen(res["board"]), "hero": screen(res["hero"])}
        numbers = {k: screen(res[k]) for k in ("pot_total", "my_stack", "call") if k in res}
        numbers.update({k: screen(v) for k, v in res.items() if k.startswith("opp")})
        if numbers:
            self.cfg["number_regions"] = numbers
            self.cfg["auto_numbers"] = True
            if self.cfg.get("villain_seat", "max") not in numbers:
                self.cfg["villain_seat"] = "max"
        self.cfg["table"] = [int(v) for v in rect]
        self.cfg["layout"] = to_json(res)
        self.fill_blinds(res)
        save_config(self.cfg)
        self.reset_ocr()
        self.table_state, self.state_pending, self.state_key = None, None, None
        self.refresh_auto_toggle()
        self.reset_state()
        self.show_toast(f"자동 지정: {layout.summary(res)}")

    def fill_blinds(self, res):
        """왼쪽 위 블라인드 줄이 확실히 읽혔으면 SB/BB/앤티 합계(앤티 × 인원)를 채움. 단위는 스택 표기를 따름."""
        blinds, unit = res.get("blinds"), res.get("unit")
        if not blinds:
            return
        players = 1 + sum(1 for k in res if k.startswith("opp"))
        ante_total = blinds["ante"] * players
        if unit == "BB":
            bb = blinds["bb"]
            self.set_input("sb", f"{blinds['sb'] / bb:g}BB")
            self.set_input("bb", "1BB")
            self.set_input("ante", f"{round(ante_total / bb, 2):g}BB")
        else:  # 칩 단위
            for key, value in (("sb", blinds["sb"]), ("bb", blinds["bb"]), ("ante", ante_total)):
                self.set_input(key, odds.format_amount(value))

    # --- 팟/스택 숫자 자동 읽기 ----------------------------------------------
    def ocr_worker(self):
        """백그라운드: 숫자 영역을 읽어 {키: (값, 단위)}를 큐에 넣는다.
        영역이 '의미 있게' 바뀌고 움직임이 멈췄을 때만 다시 OCR (반짝임·글로우·칩 애니메이션은 무시)."""
        cache, last_unit, last_regions = {}, None, None
        steady = Steady(settle=True)
        with MSS() as sct:
            while not self.closed:
                time.sleep(OCR_INTERVAL)
                regions = self.cfg.get("number_regions")
                if not regions or not self.cfg.get("auto_numbers"):
                    continue
                try:
                    if regions != last_regions:  # 영역을 다시 지정함
                        cache, last_regions = {}, dict(regions)
                        steady.reset()
                    changed = {}
                    m = ocr.JITTER_MARGIN
                    for key, (x, y, w, h) in regions.items():
                        img = grab(sct, (x - m, y - m, w + 2 * m, h + 2 * m))
                        if steady.check(key, img[m:m + h, m:m + w]) or key not in cache:
                            changed[key] = img
                    if changed:
                        for key, texts in ocr.read_jittered(changed).items():
                            cache[key] = texts
                            steady.accept(key)
                    last_unit = ocr.unit_hint(cache.values()) or last_unit
                    values = {k: ocr.resolve(t, last_unit) for k, t in cache.items()}
                    self.ocr_queue.put(values)
                except Exception as exc:  # 화면 밖 영역 등 — 다음 주기에 재시도
                    self.ocr_queue.put({"error": str(exc)})

    def reset_ocr(self):
        self.ocr_values, self.ocr_pending, self.ocr_written = {}, None, {}

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
        for key in ("pot", "my_stack"):  # "pot": 예전 방식 영역 (읽은 값을 그대로 팟으로)
            if values.get(key):
                self.auto_set(key, ocr.format_value(*values[key]))
        if values.get("call"):  # 콜 버튼 금액 = 상대 벳. 내 차례에만 보임 → 안 보이면 입력값을 그대로 둠
            self.auto_set("bet", ocr.format_value(*values["call"]))
        self.fill_pot()
        opp = self.villain_stack()
        if opp:
            self.auto_set("opp_stack", ocr.format_value(*opp))
        units = {v[1] for v in values.values() if v}
        if "BB" in units and not self.inputs["bb"].get().strip():
            self.set_input("sb", "0.5BB")
            self.set_input("bb", "1BB")
        self.build_villain_pills()

    def auto_set(self, key, text):
        """OCR 값이 지난번에 쓴 값과 다를 때만 입력칸을 바꿈 → 직접 고친 값을 같은 OCR 값으로 덮지 않음."""
        if self.ocr_written.get(key) == text:
            return
        self.ocr_written[key] = text
        self.set_input(key, text)

    def fill_pot(self):
        """팟 입력 = 화면 POT(바닥에 깔린 벳까지 전체) − 상대 벳. 입력칸 정의 '팟 (상대 벳 전)'에 맞춤."""
        total = self.ocr_values.get("pot_total") if self.cfg.get("auto_numbers") else None
        if not total:
            return
        value, unit = total
        bet = self.amount("bet")
        if bet and bet < value:
            value -= bet
        value = round(value, 2) if unit != "만" else int(value)
        self.auto_set("pot", ocr.format_value(value, unit))

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
            self.show_toast("먼저 [자동 지정] 또는 [숫자]로 영역을 지정하세요")
            return
        self.cfg["auto_numbers"] = not self.cfg.get("auto_numbers")
        self.reset_ocr()
        save_config(self.cfg)
        self.refresh_auto_toggle()

    def refresh_auto_toggle(self):
        on = bool(self.cfg.get("auto_numbers") and self.cfg.get("number_regions"))
        self.auto_btn.config(text=("● 화면 자동 입력" if on else "○ 화면 자동 입력"),
                             bg="#123b37" if on else CHIP, fg=ACCENT if on else MUTED)
        regions = self.cfg.get("number_regions") or {}
        for key in ("pot", "my_stack", "opp_stack", "bet"):
            auto = on and (key != "bet" or "call" in regions)
            self.entries[key].config(fg=ACCENT if auto else TEXT)
        self.pot_hint.config(text="= 화면 POT − 상대 벳" if on and "pot_total" in regions else "")
        self.build_villain_pills()

    def build_villain_pills(self):
        for w in self.villain_box.winfo_children():
            w.destroy()
        opps = self.opp_values()
        if not (opps and self.cfg.get("auto_numbers")):
            return
        s = self.s
        tk.Label(self.villain_box, text="상대 선택 (유효 스택 기준)", bg=BG, fg=MUTED, font=self.font(FS_NOTE)).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, s(3)))
        seat = self.cfg.get("villain_seat", "max")
        if seat != "max" and seat not in dict(opps):
            seat = "max"
        options = [("max", "가장 큰 스택")] + [(k, f"{k[3:]}: {ocr.format_value(*v)}") for k, v in opps]
        for i, (key, text) in enumerate(options):
            on = key == seat
            b = tk.Label(self.villain_box, text=text, bg=ACCENT if on else CHIP, fg=BG if on else TEXT,
                         font=self.font(FS_SMALL, "bold" if on else "normal"), padx=s(7), pady=s(2), cursor="hand2")
            b.grid(row=1 + i // 3, column=i % 3, sticky="w", padx=(0, s(4)), pady=(0, s(4)))
            b.bind("<Button-1>", lambda e, k=key: self.pick_villain(k))

    def learn(self):
        if not self.cfg["regions"]:
            self.show_toast("먼저 [자동 지정] 또는 [카드]로 영역을 지정하세요")
            return
        LearnDialog(self.root, dict(self.detected), self.matcher,
                    lambda n: (self.show_toast(f"카드 {n}장을 학습했습니다"), self.reset_state()))

    def change_opponents(self, delta):
        self.cfg["opponents"] = max(1, min(9, self.opponents() + delta))
        if self.table_state:
            self.manual.add("opponents")
        save_config(self.cfg)

    def resume_auto(self, key):
        self.manual.discard(key)
        self.schedule_advice()
        self.render()

    def set_hover(self, key):
        old, self.hover = self.hover, key
        self.root.config(cursor="hand2" if key else "")
        for k in (old, key):
            if k and k in self.buttons:
                self.buttons[k].refresh()

    def show_toast(self, text):
        self.toast = text
        self.root.after(2500, lambda: setattr(self, "toast", None) if self.toast == text else None)

    def reset_state(self):
        self.computed_state = self.pending = self.confirmed = None
        self.memory = {}
        self.card_cache = {}
        self.analysis_cache = {}

    def hold(self, key, labels):
        """같은 핸드 동안 이미 읽은 카드는 말풍선·이펙트에 가려 ?가 돼도 유지.

        카드 수가 줄거나, 뒷면이 보이거나, 읽힌 카드가 다른 카드로 바뀌면 새 핸드로 보고 기억을 지운다."""
        prev = self.memory.get(key, [])
        if BACK in labels or len(labels) < len(prev) or any(
                ev.is_card(a) and ev.is_card(b) and a != b for a, b in zip(labels, prev)):
            prev = []
        out = [p if label == UNKNOWN and ev.is_card(p) else label
               for label, p in zip(labels, prev + [""] * (len(labels) - len(prev)))]
        self.memory[key] = out
        return out

    # --- 테이블 상태 (누가 남았나, 내 차례인가, 포지션) --------------------------
    def state_worker(self):
        """백그라운드: 약 1초마다 게임 창을 찍어 tablestate.read 결과를 큐에 넣는다.
        화면이 그대로면 다시 읽지 않고 지난 결과를 다시 넣음 (확정 판정용).
        같은 스레드에서 닉네임 읽기 · 핸드 기록 · 상대 통계 · (내 차례에만) 상대 핸드 리딩 · 기록 모드도 처리 — 실패해도 앱은 계속."""
        steady = Steady(settle=False)
        last, last_read, last_rect = None, 0.0, None
        self.init_tracking()
        with MSS() as sct:
            while not self.closed:
                time.sleep(STATE_INTERVAL)
                rect, calib = self.cfg.get("table"), self.cfg.get("layout")
                if not (rect and calib):
                    last = None
                    continue
                try:
                    if rect != last_rect:
                        steady.reset()
                        last, last_rect = None, list(rect)
                    img = grab(sct, rect)
                    changed = steady.check("table", img)
                    now = time.time()
                    if changed or last is None or now - last_read > STATE_REFRESH:
                        last, last_read = self.tablestate.read(img, calib), now
                    self.state_queue.put(last if isinstance(last, dict) else {"empty": True})
                except Exception as exc:
                    last = None
                    self.log_once("state_worker")
                    self.state_queue.put({"error": str(exc)})
                    continue
                try:
                    self.track_frame(img, calib, last, now)
                except Exception:
                    self.log_once("track_frame")

    # --- 추적: 이름 · 핸드 기록 · 프로필 · 상대 핸드 리딩 · 기록 모드 (상태 스레드) ---------------------
    def init_tracking(self):
        """상태 스레드 시작 때 한 번. 모듈이 없거나 오류면 해당 기능만 꺼진다."""
        try:
            names_m, handlog_m, profiles_m = (_optional(n) for n in ("names", "handlog", "profiles"))
            self.handread, self.rangeq = _optional("handread"), _optional("rangeq")
            if names_m and handlog_m and profiles_m:
                self.store = profiles_m.Store(BASE / "profiles.db")
                self.name_reader = names_m.NameReader(resolver=self.store.resolve_name,
                                                      on_confirm=self.store.learn_fingerprint)
                self.tracker = handlog_m.Tracker(on_hand=self.store.observe)
                self.store.new_session(self.tracker.session)
            rec_m = _optional("recorder")
            self.recorder = rec_m.Recorder(BASE / "recordings") if rec_m else None
            ui_m = _optional("userinfo")
            self.popup = ui_m.PopupWatcher() if ui_m and self.store is not None else None
        except Exception:
            self.log_once("init_tracking")
            self.store = self.name_reader = self.tracker = None

    def track_new_session(self, calib):
        """새 [자동 지정] / 테이블 창이 바뀜: 진행 중 핸드를 닫고 세션·이름 기억을 새로."""
        self.track = {}
        if self.tracker is not None:
            self.tracker.new_session()
            self.store.new_session(self.tracker.session)
        if self.name_reader is not None:
            self.name_reader.reset()
        if self.recorder is not None and self.recorder.enabled:
            self.recorder.set_calib(calib)

    def click_mode(self):
        """분석 방식: '클릭 분석'이면 무거운 계산(에쿼티·추천·상대 예상)은 [지금 계산]을 누를 때만."""
        return self.cfg.get("analysis_mode") == "click"

    def heavy_allowed_for(self, st):
        """상태 스레드용: 내 차례/차례 직전(preaction)/[지금 계산] 이면 무거운 계산 허용 (클릭 분석은 [지금 계산]만)."""
        if self.force_calc:
            return True
        if self.click_mode():
            return False
        return bool(isinstance(st, dict) and (st.get("to_act") is True or st.get("preaction") is True))

    @staticmethod
    def aggression(seat):
        """좌석의 공격성 점수 (큰 쪽이 주 상대: 마지막 스트리트에서 벳/레이즈한 사람, 없으면 프리플랍 레이저)."""
        w = {"checkraise": 5, "allin": 5, "raise": 4, "bet": 4, "jam": 4, "4bet": 4, "3bet": 3, "open": 2}
        score = 0
        for i, street in enumerate(("preflop", "flop", "turn", "river")):
            for a in seat.get(street) or []:
                a = a.get("a") if isinstance(a, dict) else a
                if a in w:
                    score = max(score, w[a] + 10 * i)
        return score

    def track_frame(self, img, calib, st, now):
        """프레임 하나: 기록 모드 → 닉네임 → 핸드 기록 → 통계 → (내 차례에만) 상대 핸드 리딩."""
        view = self.view
        hero = [c for c in view.get("hero", []) if ev.is_card(c)]
        board = [c for c in view.get("board", []) if ev.is_card(c)]
        if calib is not None and calib is not self.calib_seen:
            self.calib_seen = calib
            self.track_new_session(calib)
        rec = self.recorder
        if rec is not None:
            if self.rec_on and not rec.enabled:
                if rec.start(calib) is None:
                    self.rec_on = False
            elif not self.rec_on and rec.enabled:
                rec.stop()
            if rec.enabled:
                rec.add(img, t=now, meta={"hero": hero, "board": board, "pot": None, "state": st})
        self.read_popup(img)
        if len(hero) != 2:
            self.pre_memo = None
        elif not board and isinstance(st, dict):  # 프리플랍: 마지막 레이저 기억 (추적기가 못 볼 때의 대체; 칩이 치워진 프레임은 덮지 않는다)
            agg = pre_aggressor_memo(st.get("preflop"), hero)
            if agg is not None or (self.pre_memo or {}).get("hero") != tuple(hero):
                self.pre_memo = {"hero": tuple(hero), "agg": agg}
        if self.tracker is None or not isinstance(st, dict) or "seats" not in st:
            return
        names = self.name_reader.read(img, calib, st) if self.name_reader else {}
        self.tracker.update(st, hero, board, names, now)
        profiles = self.store.get_for_table(names, session=self.tracker.session,
                                            tab=self.cfg.get("game_tab")) if self.store else {}
        live = self.tracker.live()
        reads = self.track.get("reads")
        if reads and reads["hero"] != tuple(hero):  # 새 핸드
            reads = None
        if self.handread is not None and len(hero) == 2 and self.heavy_allowed_for(st):
            reads = self.refresh_reads(reads, st, hero, board, live, profiles)
        hist = None
        try:  # 주도권·플랍 라인 요약 (내 액션 포함). 실패해도 추적은 계속
            if self.tracker.hand is not None:
                hist = summarize_hand(self.tracker.hand)
        except Exception:
            self.log_once("summarize_hand")
        self.track = {"names": names, "profiles": profiles, "live": live, "reads": reads, "t": now, "hist": hist}

    def refresh_reads(self, reads, st, hero, board, live, profiles):
        """남은 상대가 바뀌었거나 새 액션이 있으면 상대 예상을 다시 계산 (아니면 그대로)."""
        in_hand = sorted(x["key"] for x in st["seats"] if x.get("in_hand") and x["key"] != "hero" and x["key"] in live)
        key = (tuple(hero), tuple(board), tuple(in_hand), repr([live[k] for k in in_hand]))
        if in_hand and (reads is None or reads["key"] != key):
            reads = self.make_reads(st, hero, board, live, profiles, in_hand, key)
        return reads

    def read_popup(self, img):
        """사용자가 연 게임의 '유저 정보' 팝업을 읽어 상대 통계로 저장 (직접 클릭하지 않음).
        팝업은 지금 하는 게임 종류 탭으로 열리므로 그 탭을 현재 게임 종류로 기억한다."""
        popup = getattr(self, "popup", None)
        if popup is None:
            return
        info = popup.update(img)
        if not info or info.get("vpip") is None:
            return
        if info.get("tab"):
            self.cfg["game_tab"] = info["tab"]
        if not info.get("name_reliable"):
            self.pending_toast = "상대 정보: 닉네임을 확실히 못 읽어 저장하지 않았습니다"
            return
        session = self.tracker.session if self.tracker is not None else None
        self.store.import_site_stats(info["name"], info, info.get("tab") or "홀덤", session=session)
        self.pending_toast = (f"{info['name']} 저장 · {info.get('style_ko') or ''} "
                              f"VPIP {info['vpip']} / PFR {info.get('pfr')} ({info.get('tab') or '홀덤'})")

    def make_reads(self, st, hero, board, live, profiles, in_hand, key):
        """handread.estimate: 남은 상대 각자의 범위·요약 (주 상대 먼저)."""
        players = _number(st.get("pos_players")) or _number(st.get("players")) or 9
        stack = _number((st.get("preflop") or {}).get("stack_bb")) or 100
        seats = {k: live[k] for k in in_hand}
        order = sorted(seats, key=lambda k: (-self.aggression(seats[k]), k))
        kw = {}
        try:  # 상대 범위 모듈이 앤티·느슨함을 받으면 넘긴다 (rangeq.villain_range 의 ante)
            params = inspect.signature(self.handread.estimate).parameters
            if "ante" in params:  # 상태 스레드: Tk 변수 대신 저장된 입력값을 읽는다
                kw["ante"] = bool((odds.parse_amount((self.cfg.get("inputs") or {}).get("ante")) or 0) > 0)
            if "looseness" in params:
                kw["looseness"] = self.looseness()
        except (TypeError, ValueError):
            kw = {}
        est = self.handread.estimate(seats, board, int(players), dead=hero + board,
                                     profiles={k: profiles[k] for k in seats if k in profiles}, stack_bb=stack, **kw)
        return {"key": key, "sig": hash(key), "hero": tuple(hero), "board": tuple(board), "order": order,
                "ranges": {k: est[k]["range"] for k in order if k in est},
                "lines": {k: self.handread.describe(est[k]) for k in order if k in est},
                "width": {k: est[k].get("width_pct") for k in order if k in est}}

    def reads_now(self):
        """지금 내 카드에 해당하는 상대 리딩 (없으면 None)."""
        r = (self.track or {}).get("reads")
        hero = tuple(c for c in self.view.get("hero", []) if ev.is_card(c))
        return r if r and len(hero) == 2 and r["hero"] == hero else None

    def reads_sig(self):
        r = self.reads_now()
        return r["sig"] if r else None

    def toggle_record(self):
        """[기록]: 게임 창 캡처를 1초마다 recordings/ 에 저장 (행동 추적 검증용). 상태 스레드가 시작/중지."""
        if not self.rec_on:
            if not (self.cfg.get("table") and self.cfg.get("layout")):
                self.show_toast("먼저 [자동 지정]으로 게임 창을 지정하세요")
                return
            if self.recorder is None:
                self.show_toast("기록 모듈을 쓸 수 없습니다")
                return
            self.rec_on, self.rec_t0 = True, time.time()
            self.show_toast("기록 시작: recordings 폴더 (exe 옆)")
        else:
            self.rec_on, self.rec_t0 = False, None
            self.show_toast("기록을 멈췄습니다")
        self.render(force=True)

    def rec_elapsed(self):
        return int(time.time() - self.rec_t0) if self.rec_on and self.rec_t0 else 0

    def apply_table_state(self):
        st = None
        while not self.state_queue.empty():
            st = self.state_queue.get_nowait()
        if st is None:
            return
        if "error" in st or "empty" in st:
            self.state_errors += 1
            if self.state_errors >= 3 and self.table_state is not None:
                self.table_state, self.state_key = None, None
                self.schedule_advice()
            return
        self.state_errors = 0
        key = repr([st.get(k) for k in STATE_KEYS])
        if key != self.state_pending:  # 같은 결과가 두 번 연속 나와야 반영
            self.state_pending = key
            return
        if key == self.state_key:
            self.table_state = st  # 좌석 세부값만 갱신
            return
        self.state_key = key
        self.table_state = st
        self.schedule_advice()

    def auto_values(self):
        """테이블 상태로 자동 채울 값. 직접 고른 항목은 이번 핸드 동안 제외."""
        ts = self.table_state
        if not ts:
            return {}
        out = {}
        in_hand = ts.get("hero_in_hand", not ts.get("hero_folded"))
        if "opponents" not in self.manual and in_hand:  # 다자 팟: 남은 상대 수만큼 (최소 1)
            n = _number(ts.get("opponents_in_hand"))
            if n is not None:
                out["opponents"] = max(1, min(9, int(n)))
        board = [b for b in self.view.get("board", []) if ev.is_card(b)]
        if "preflop" not in self.manual and not board:  # 플랍 이후의 preflop 값은 무시
            pre = ts.get("preflop") or {}
            auto = {}
            players = _number(ts.get("pos_players")) or _number(ts.get("players"))
            if players:
                auto["players"] = max(6, min(10, int(players)))
            if pre.get("situation"):
                auto["situation"] = pre["situation"]
            if ts.get("hero_pos"):
                auto["hero"] = ts["hero_pos"]
            if pre.get("villain_pos"):
                auto["villain"] = pre["villain_pos"]
            if auto:
                out["preflop"] = auto
        return out

    def opponents(self):
        return self.auto_values().get("opponents") or self.cfg["opponents"]

    # --- 계산 시점 (무거운 계산은 내 차례가 올 때만) ------------------------------------
    def log_once(self, title, ctx=None):
        """같은 예외가 매 주기 반복돼도 로그가 한 번만 쌓이게 (마지막 줄이 같으면 건너뜀)."""
        sig = (title, traceback.format_exc().strip().splitlines()[-1:])
        if sig != self.last_err_sig:
            self.last_err_sig = sig
            log_debug(title, ctx)

    def heavy_allowed(self):
        """에쿼티 시뮬레이션·추천 계산을 지금 돌려도 되는가.
        (a) 내 차례, (b) 내 차례가 다가옴(preaction), (c) [지금 계산], (d) 테이블 상태 없음/차례 불명 → 허용."""
        if self.force_calc:
            return True
        if self.click_mode():  # 클릭 분석: 누르기 전에는 아무것도 자동으로 돌리지 않음
            return False
        ts = self.table_state
        if not ts:
            return True
        return not (ts.get("to_act") is False and not ts.get("preaction"))

    def calc_now(self):
        self.force_calc = True
        if self.click_mode():  # 상태 스레드는 상대 예상을 안 만들고 있으니 여기서 한 번 만든다
            try:
                self.read_now()
            except Exception:
                self.log_once("read_now")
        self.maybe_run_heavy()
        self.schedule_advice()
        self.render(force=True)

    def read_now(self):
        """[지금 계산] (클릭 분석): 마지막으로 읽은 테이블 상태로 상대 예상을 지금 갱신."""
        tr, ts = self.track or {}, self.table_state
        hero = [c for c in self.view.get("hero", []) if ev.is_card(c)]
        board = [c for c in self.view.get("board", []) if ev.is_card(c)]
        if self.handread is None or len(hero) != 2 or not isinstance(ts, dict) or "seats" not in ts or not tr.get("live"):
            return
        reads = tr.get("reads")
        if reads and reads["hero"] != tuple(hero):
            reads = None
        tr["reads"] = self.refresh_reads(reads, ts, hero, board, tr["live"], tr.get("profiles") or {})

    def toggle_mode(self):
        """헤더 [분석: 자동/클릭]. 자동으로 바꾸면 밀려 있던 계산을 바로 시작, 클릭으로 바꾸면 지금 화면은 그대로."""
        self.cfg["analysis_mode"] = "auto" if self.click_mode() else "click"
        save_config(self.cfg)
        self.force_calc = False
        if not self.click_mode():
            self.maybe_run_heavy()
        self.schedule_advice()
        self.render(force=True)

    # --- 플레이 성향(타이트 ↔ 루즈) -------------------------------------------------------
    def looseness(self):
        """저장된 느슨함 L ∈ [-1, +1] (0.1 단위, 기본 0 = 표준)."""
        try:
            v = float(self.cfg.get("looseness", 0.0))
        except (TypeError, ValueError):
            v = 0.0
        return round(max(-1.0, min(1.0, v)), 1) + 0.0

    @staticmethod
    def looseness_text(v):
        word = ("매우 타이트" if v <= -0.7 else "타이트" if v <= -0.2 else "표준" if v < 0.2 else
                "루즈" if v < 0.7 else "매우 루즈")
        return f"{word} {v:+.1f}" if abs(v) >= 0.05 else f"{word} 0.0"

    def step_looseness(self, d):
        self.set_looseness(self.looseness() + 0.1 * d)

    def set_looseness(self, v):
        """[◀][▶]: 저장하고 추천·프리플랍 가이드를 바로 다시 계산 (내 차례가 아니어도 한 번). 배치는 그대로."""
        v = round(max(-1.0, min(1.0, v)), 1) + 0.0
        if v == self.looseness():
            return
        self.cfg["looseness"] = v
        save_config(self.cfg)
        res = self.result
        if res and res.get("equity") is not None and not self.view.get("message"):
            self.force_calc = True  # [지금 계산]처럼 한 번만 (compute_advice 가 다시 끈다)
        self.advice_key = None
        self.schedule_advice()
        self.render(force=True)

    def maybe_run_heavy(self):
        """허용되는데 에쿼티가 현재 상태 기준이 아니면 (보류돼 있던 계산) 지금 시작."""
        if self.computing or self.computed_state is None or self.view.get("message"):
            return
        if not self.heavy_allowed():
            return
        res = self.result
        reads_changed = bool(res and res.get("equity") is not None and res.get("rkey") != self.reads_sig())
        if self.result_state != self.computed_state or reads_changed:
            self.start_analysis(self.computed_state)

    def result_stale(self):
        """보이는 에쿼티가 최신 카드/상대 수 상태가 아닌 '이전 계산'인가."""
        r = self.result
        return bool(r and r.get("equity") is not None and self.result_state != self.computed_state)

    # --- 추천 ---------------------------------------------------------------
    def schedule_advice(self):
        """입력·상태가 바뀔 때 추천을 다시 계산 (연속 입력은 한 번으로 묶음)."""
        if self.advice_after is not None:
            return
        self.advice_after = self.root.after(150, self.update_advice)

    def update_advice(self):
        self.advice_after = None
        self.compute_advice()
        if self.click_mode() and not self.computing:  # [지금 계산] 한 번에 한 번만
            self.force_calc = False

    def compute_advice(self):
        if self.advisor is None:
            self.advice, self.advice_status = None, None
            return
        if self.computing:
            return  # 계산이 끝나면 다시 부름
        if self.fold_view is not None:  # 폴드 후 복기: 직전 추천을 그대로 (흐리게) 둔다
            self.advice_stale = self.advice is not None
            return
        ctx = None
        try:
            ctx = self.advice_ctx()
        except Exception:
            self.log_once("advice_ctx")
            self.advice, self.advice_key = None, None
            self.advice_status = ("추천 오류 — debug.log", "err", ())
            return
        if ctx is None:  # 숨기지 않고 이유를 패널에 보여준다
            self.advice, self.advice_key, self.advice_stale = None, None, False
            self.advice_status = self.status_for(self.advice_why)
            return
        key = repr(sorted((k, repr(v)) for k, v in ctx.items() if k != "villain_ranges")) + repr(self.result.get("rkey"))
        cards = (tuple(ctx["hero"]), tuple(ctx["board"]))
        if key == self.advice_key and not self.result_stale():
            self.advice_stale = False
            return
        if not self.heavy_allowed():  # 내 차례가 아님: 같은 카드면 지난 결과를 흐리게 유지, 아니면 비움
            if self.advice is not None and self.advice_cards == cards:
                self.advice_stale = True
            else:
                self.advice, self.advice_cards, self.advice_stale = None, None, False
                self.advice_status = (self.wait_text(), "info", ("calc",))
            return
        self.advice_key, self.advice_cards, self.advice_stale, self.force_calc = key, cards, False, False
        try:
            adv = self.advisor.recommend(ctx)
            self.advice = adv if isinstance(adv, dict) and adv.get("action") else None
            self.advice_status = None if self.advice else ("추천 대기: 계산 결과 없음", "info", ())
        except Exception:
            self.log_once("advisor.recommend", ctx)
            self.advice = None
            self.advice_status = ("추천 오류 — debug.log", "err", ())

    def wait_text(self):
        return "클릭해서 분석 —" if self.click_mode() else "추천 대기: 내 차례가 되면 계산합니다 —"

    def status_for(self, why):
        """추천을 못 보여줄 때의 안내: (글자, 종류 'info'/'warn'/'err', 버튼 키들)."""
        msg = self.view.get("message")
        title = msg[0] if msg else None
        if not self.cfg.get("regions"):
            return ("추천 대기: 상황 인식 안 됨 —", "warn", ("auto",))
        if why == "folded" or title == "폴드":
            return ("폴드함 — 다음 핸드 대기", "info", ())
        if title == "학습되지 않은 카드":
            hero = self.view.get("hero", [])
            where = "내 카드" if UNKNOWN in hero else "보드 카드"
            return (f"추천 대기: {where} 학습 필요 (?) —", "warn", ("learn",))
        if title in ("딜링 중", "중복 인식"):
            return ("추천 대기: 카드 인식 중", "info", ())
        if title == "핸드 대기 중":
            return ("추천 대기: 내 카드 2장이 보이면 시작", "info", ())
        if why == "noequity":
            if not self.heavy_allowed():
                return (self.wait_text(), "info", ("calc",))
            return ("추천 대기: 계산 중…", "info", ())
        if not (self.cfg.get("table") and self.cfg.get("layout")) and not self.table_state:
            return ("추천 대기: 카드 인식 중", "info", ())
        return ("추천 대기: 카드 인식 중", "info", ())

    def postflop_ctx(self, ts, hero_str, nboard, opponents, turn_pot):
        """포스트플랍 추천에 넘길 위치·주도권·라인 (advisor 의 hero_ip / aggressor / flop_line, 솔버 조회용 villain_pos 등).
        내 액션도 핸드 추적기에 들어 있다 (좌석 'hero'). 추적을 믿을 수 없으면 프리플랍 마지막 레이저는 테이블 상태 기억(pre_memo)으로,
        그 밖에는 None(모름). 포스트플랍엔 프리플랍 선택값이 의미 없어서 상대 포지션·상황도 여기서 다시 정한다."""
        out = {"situation": None}
        ip = hero_ip_from(ts)
        if ip is not None:
            out["hero_ip"] = ip
        opps = [s for s in ts.get("seats") or [] if s.get("in_hand") and s.get("key") != "hero"]
        if len(opps) == 1 and opponents == 1 and opps[0].get("pos"):
            out["villain_pos"] = opps[0]["pos"]
        hist = (self.track or {}).get("hist")
        stage = {3: 1, 4: 2, 5: 3}.get(nboard)
        if hist and (hist["street"] != stage or set(hist["hero_cards"]) != set(hero_str)):
            hist = None   # 다른 핸드/스트리트의 요약 (프레임이 아직 따라오지 못함)
        if hist and hist["reliable"]:
            raises = hist["pre_raises"]
            if raises >= 2:
                out["situation"] = "vs3bet"   # 3벳 이상 팟 — 솔버 라이브러리(SRP) 대상 아님
            elif raises == 1:
                out["situation"] = "open" if hist["opener"] == "hero" else "vsopen"
            prev = HAND_STREETS[stage - 1]
            if stage == 1 and not hist["pre_seen"]:
                agg = None
            else:
                last = hist["last_aggr"].get(prev)
                agg = None if last is None else last == "hero"
            if hist["opp_raise"]:
                out["villain_raised"] = True
            if stage == 2:
                line = flop_line_from(hist["flop"], turn_pot)
                if line:
                    out["flop_line"] = line
        else:
            memo = self.pre_memo
            agg = memo["agg"] if stage == 1 and memo and set(memo["hero"]) == set(hero_str) else None
        out["aggressor"] = agg
        return out

    def advice_ctx(self):
        self.advice_why = None
        res = self.result
        if self.view.get("message"):
            self.advice_why = "message"
            return None
        if not res:
            self.advice_why = "noresult"
            return None
        ts = self.table_state or {}
        if ts.get("hero_folded"):
            self.advice_why = "folded"
            return None
        if res.get("equity") is None:
            self.advice_why = "noequity"
            return None
        st = self.preflop_state()
        bb = self.amount("bb")

        def in_bb(v):  # 추천 모듈은 BB 단위
            return round(v / bb, 2) if v is not None and bb else None
        board = res["board"]
        pot = self.amount("pot")
        call = in_bb(self.amount("bet"))
        if ts.get("to_act") is not None:  # 테이블 상태의 콜 금액은 이미 BB 단위. 내 차례가 아니면 콜 금액 없음
            ts_call = _number(ts.get("call"))
            call = ts_call if ts_call is not None else (call if ts.get("to_act") else None)
        total = self.ocr_values.get("pot_total") if self.cfg.get("auto_numbers") else None
        pot_total = _number(total)
        ante = self.amount("ante")
        facing_allin = bool(ts.get("facing_allin")) or any(
            isinstance(seat, dict) and seat.get("in_hand", True) and seat.get("allin")
            and (_number(seat.get("bet")) or 0) > 0 for seat in ts.get("seats") or [])
        extra = {}
        if res.get("equity_basis") == "range" and res.get("villain_ranges"):
            profs = (self.track or {}).get("profiles") or {}
            extra = {"equity_basis": "range", "villain_ranges": res["villain_ranges"],
                     "villain_profiles": [profs[k] for k in res.get("villain_keys", []) if k in profs]}
        hero_str = [ev.card_str(c) for c in res["hero"]]
        ctx = {
            **extra,
            "hero": hero_str,
            "board": [ev.card_str(c) for c in board],
            "street": STREET_KEYS.get(len(board), "preflop"),
            "players": _number(ts.get("pos_players")) or _number(ts.get("players")) or st["players"],
            "opponents_in_hand": res["opponents"],
            "hero_pos": ts.get("hero_pos") or st["hero"],
            "situation": st["situation"],
            "villain_pos": st.get("villain") if st["villains"] else None,
            "stack_bb": st["stack_bb"],   # 프리플랍 가이드 격자와 같은 유효 스택
            "looseness": self.looseness(),
            "my_stack": in_bb(self.amount("my_stack")),
            "pot": in_bb(pot),
            "pot_total": in_bb(pot_total),
            "call": call,
            "to_act": ts.get("to_act"),
            "can_check": ts.get("can_check"),
            "ante": in_bb(ante) if ante else False,
            "equity": res["equity"]["equity"],
            "outs": res.get("outs"),
            "hand_name": res["hand"],
            "facing_allin": facing_allin,
        }
        if board:  # 포스트플랍: 위치·주도권·라인 (프리플랍 선택값 대신 이번 핸드에서 읽은 값으로 덮어쓴다)
            try:
                turn_pot = in_bb(pot)
                if turn_pot is None and pot_total and call is not None:
                    turn_pot = in_bb(pot_total) - call
                ctx.update(self.postflop_ctx(ts, hero_str, len(board), res["opponents"], turn_pot))
            except Exception:
                self.log_once("postflop_ctx")
        return ctx

    # --- 메인 루프 ----------------------------------------------------------
    def guard(self, name, fn):
        """한 단계의 예외가 화면 갱신 전체를 멈추지 않게: debug.log 에 남기고 계속."""
        try:
            fn()
            if name == "apply_table_state" and self.err_note:
                self.err_note = None
        except Exception:
            self.log_once(f"tick:{name}")
            if name == "apply_table_state":
                self.err_note = "상태 읽기 오류 — debug.log"

    def tick(self):
        try:
            if self.pending_toast:  # 상태 스레드에서 온 알림은 Tk 스레드에서 표시·저장
                self.show_toast(self.pending_toast)
                self.pending_toast = None
                save_config(self.cfg)
            self.guard("apply_ocr", self.apply_ocr)
            self.guard("apply_table_state", self.apply_table_state)
            self.guard("maybe_run_heavy", self.maybe_run_heavy)
            self.guard("capture", self.capture)
            self.guard("render", self.render)
        finally:
            if not self.closed:
                self.root.after(self.cfg["interval_ms"], self.tick)

    def read_cards(self, key, img):
        """영역 이미지 → [(crop, 라벨)]. 자리별로 카드 그림이 그대로면(반짝임·노이즈 무시) 지난 라벨을 재사용."""
        crops = detect_cards(img)
        cache = self.card_cache.get(key)
        if cache is None or len(cache) != len(crops):
            cache = [None] * len(crops)
        out = []
        for i, crop in enumerate(crops):
            sig = signature(cv2.resize(crop, NORM_SIZE, interpolation=cv2.INTER_AREA))
            hit = cache[i]
            if hit is not None and not sig_changed(hit[0], sig):
                label = hit[1]
            else:
                label = BACK if is_card_back(crop) else self.matcher.match(crop, self.cfg["threshold"])[0]
                cache[i] = (sig, label)
            out.append((crop, label))
        self.card_cache[key] = cache
        return out

    def capture(self):
        regions = self.cfg["regions"]
        if not regions:
            self.view = {"message": ("영역을 지정하세요", "[자동 지정]을 누르고 게임 창을 드래그하면 보드·내 카드·팟·스택 "
                                                       "자리를 찾습니다. 직접 하려면 [카드]로 보드와 내 카드를 드래그합니다.")}
            return
        self.detected = {key: self.read_cards(key, grab(self.sct, regions[key])) for key, _ in REGIONS}
        hero = self.hold("hero", [label for _, label in self.detected["hero"]][:2])
        board = self.hold("board", [label for _, label in self.detected["board"] if label != BACK][:5])
        state = self.read_state(hero, board)

        # 같은 결과가 두 번 연속 나와야 화면에 반영 (딜링/뒤집기 애니메이션 중 깜빡임 방지)
        frame = (tuple(hero), tuple(board), state)
        if frame != self.pending:
            self.pending = frame
            return
        if frame == self.confirmed:
            return
        self.confirmed = frame
        # 폴드 후 복기용: 직전 분석·추천·상대 예상을 다음 핸드 카드가 들어올 때까지 그대로 남김
        if isinstance(state[0], str) and state[0] in ("폴드", "핸드 대기 중", "딜링 중", "학습되지 않은 카드") and (
                self.fold_view is not None or (state[0] == "폴드" and self.result is not None)):
            if self.fold_view is None:
                res = self.result
                self.fold_view = {"hero": [ev.card_str(c) for c in res["hero"]],
                                  "board": [ev.card_str(c) for c in res["board"]], "folded": True}
            self.view = dict(self.fold_view)
            return
        self.fold_view = None
        self.view = {"hero": hero, "board": board}
        if len(hero) == 2 and all(ev.is_card(h) for h in hero) and tuple(hero) != self.last_hero:
            if self.last_hero is not None:
                self.manual.clear()  # 새 핸드 → 자동 채우기 다시 시작
            self.last_hero = tuple(hero)
        if isinstance(state[0], str):  # 메시지
            self.view["message"] = state
            self.memory = {}
            self.computed_state = None
            self.result = self.result_state = None
            self.schedule_advice()
            return
        if state != self.computed_state:
            self.computed_state = state
            self.start_analysis(state)

    def read_state(self, hero, board):
        if BACK in hero:  # 한 장이라도 뒷면이면 폴드 (덮는 애니메이션 중 포함)
            return "폴드", "내 카드가 뒷면입니다. 다음 핸드를 기다립니다."
        if UNKNOWN in hero + board:
            return "학습되지 않은 카드", "노란색 ? 카드가 있습니다. [카드 학습]에서 라벨을 입력하세요."
        if len(hero) < 2:
            return "핸드 대기 중", "내 카드 2장이 보이면 자동으로 계산합니다."
        if len(board) not in (0, 3, 4, 5):
            return "딜링 중", f"보드 카드 {len(board)}장 인식됨"
        if len(set(hero + board)) != len(hero + board):
            return "중복 인식", "같은 카드가 두 번 인식되었습니다. 학습 데이터를 확인하세요."
        return (tuple(ev.parse_card(c) for c in hero), tuple(ev.parse_card(c) for c in board),
                self.opponents())

    def start_analysis(self, state):
        self.generation += 1
        gen = self.generation
        hero, board, opponents = state
        reads = self.reads_now()
        rkey = reads["sig"] if reads else None
        cached = self.analysis_cache.get((state, rkey))
        if cached is not None:  # 같은 상태 → 같은 숫자 (몬테카를로를 다시 돌리면 값이 흔들림)
            self.result, self.computing, self.result_state = cached, False, state
            self.schedule_advice()
            return
        if not self.heavy_allowed():
            # 내 차례가 아님: 시뮬레이션은 미룬다. 같은 카드의 지난 계산이 있으면 그대로(흐리게), 아니면 가벼운 결과만
            prev = self.result
            self.computing = False
            if not (prev and prev.get("equity") is not None and prev["hero"] == list(hero)
                    and prev["board"] == list(board)):
                self.result, self.result_state = analyze(list(hero), list(board), opponents), None
            self.schedule_advice()
            return
        self.computing = True
        self.analysis_starts += 1

        def work():
            res = None
            if reads and self.rangeq is not None:  # 상대 레인지 기준 에쿼티 (없거나 실패하면 랜덤 핸드 기준)
                try:
                    res = self.range_analyze(hero, board, opponents, reads)
                except Exception:
                    self.log_once("range_equity", {"hero": list(hero), "board": list(board), "opp": opponents,
                                                   "order": reads.get("order")})
            if res is None:
                res = analyze(list(hero), list(board), opponents, self.cfg["iterations"])
                res["equity_basis"] = "random"
            res["opponents"] = opponents
            res["rkey"] = rkey
            self.root.after(0, lambda: self.finish_analysis(gen, state, res, rkey))

        threading.Thread(target=work, daemon=True).start()

    def range_analyze(self, hero, board, opponents, reads):
        """남은 상대 각자의 추정 범위로 에쿼티 (주 상대 먼저. 범위를 모르는 상대는 랜덤 핸드로 채움)."""
        order = list(reads["order"])[:opponents]
        ranges = [reads["ranges"][k] for k in order if k in reads["ranges"]]
        while len(ranges) < opponents:
            ranges.append(self.rangeq.random_range())
        res = analyze(list(hero), list(board), opponents)
        res["equity"] = self.rangeq.equity_vs_ranges(list(hero), list(board), ranges,
                                                     min(6000, int(self.cfg["iterations"])))
        res["equity_basis"], res["villain_ranges"], res["villain_keys"] = "range", ranges, order
        return res

    def finish_analysis(self, gen, state, res, rkey=None):
        self.analysis_cache[(state, rkey)] = res
        while len(self.analysis_cache) > 32:
            self.analysis_cache.pop(next(iter(self.analysis_cache)))
        if gen == self.generation:
            self.result, self.computing, self.result_state = res, False, state
            self.update_advice()

    # --- 그리기 -------------------------------------------------------------
    def render(self, force=False):
        """화면 전체를 그림: 섹션들을 그려 높이를 재고, 고정된 열·순서대로 놓는다 (섹션은 높이에 따라 열을 옮겨 다니지
        않는다. 기본 배치는 처음 한 번만 masonry 로 정하고, 사용자가 옮기면 cfg["section_layout"] 을 따른다).
        창에 다 들어가는 가장 큰 배율·열 폭을 찾고 (열 폭은 CW_MIN~CW_MAX), 그 뒤로는 고정."""
        if self.drag is not None and self.drag.get("active"):  # 끄는 동안에는 화면을 건드리지 않음
            return
        st = self.preflop_state()
        w, h = self.win_w, self.win_h
        key = (repr(self.view), id(self.result), self.computing, self.toast, self.opponents(),
               len(self.matcher.labels), repr(st), repr(self.cfg.get("inputs")), w, h, self.tab,
               self.cfg.get("explain", True), repr(self.advice), repr(self.advice_status), self.advice_stale,
               self.result_stale(), self.heavy_allowed(), self.err_note, self.state_key, tuple(sorted(self.manual)),
               bool(self.cfg.get("auto_numbers")), repr(self.ocr_values), self.rec_on, self.rec_elapsed(),
               self.reads_sig(), repr((self.track or {}).get("names")), repr(self.cfg.get("section_layout")))
        if key == self.last_render and not force:
            return
        self.last_render = key
        self.tip.hide()
        # 0) 배율·열·배치 고정: 내용이 바뀔 때마다 새로 찾으면 글자가 커졌다 작아졌다, 섹션이 이리저리 옮겨 다녀서 눈이
        #    아프다. 창 크기/A−A+/설명 토글/배치 편집 때만 새로 정하고, 그 외에는 고정. 넘칠 때만 줄이고 저절로 다시 키우지
        #    않는다. 섹션 자리 높이도 본 적 있는 가장 큰 값으로 유지해서, 내용이 줄어도 아래 섹션이 올라오지 않는다.
        lock = self.layout_lock
        if lock is not None and lock[:2] == (w, h) and self.slot_cols is not None:
            self.set_k(lock[2])
            self.col_w, n = lock[3], lock[4]
            secs = self.draw_sections(st)
            for _ in range(12):
                sized = self.apply_sticky(secs)
                cols = self.arrange(sized)
                if self.col_extent(cols, sized)[0] <= h or self.scale <= FIT_MIN + 1e-6:
                    break
                self.set_k(max(FIT_MIN, self.scale * 0.96))
                secs = self.draw_sections(st)
            else:
                sized = self.apply_sticky(secs)
                cols = self.arrange(sized)
            self.layout_lock = (w, h, self.scale, self.col_w, n)
            self.cols_used = n
            self.place(sized, cols)
            self.cfg["ui_scale"] = round(self.scale, 2)
            return
        self.slot_cols, self.sticky = None, {}
        # 1) 열 폭 탐색: 폭이 좁으면 섹션이 길어지고 넓으면 짧아진다 (넓이 ≈ 일정). 가로/세로 어느 쪽이 먼저
        #    차는지 보고 폭을 조절해 가며 가장 큰 배율을 찾는다 (실제로 그려서 높이를 잼).
        #    지난번 폭에서 시작하고, 더 나아지지 않거나 거의 같으면 바로 멈춰 그리는 횟수를 줄인다
        best, cw, drawn = None, self.col_w, None
        for _ in range(4):
            self.col_w = cw
            secs = self.draw_sections(st)
            drawn = (self.col_w, self.scale)
            scale, n, ratio = self.pick_layout(secs, w, h)
            if best is not None and scale <= best[0] + 1e-6:
                break
            best = (scale, cw, n)
            new_cw = min(CW_MAX, max(CW_MIN, cw * math.sqrt(ratio)))
            if abs(new_cw - cw) < 0.06 * cw:
                break
            cw = new_cw
        scale, self.col_w, n = best
        # 2) 고른 폭·배율로 (필요하면 다시 그려) 실제 높이로 확인 (글자 폭이 정수라 한 번 더 재서 확인)
        for attempt in range(4):
            if drawn != (self.col_w, round(min(SCALE_MAX, max(FIT_MIN, scale)), 3)):
                self.set_k(scale)
                secs = self.draw_sections(st)
                drawn = (self.col_w, self.scale)
            scale2, n2, _ = self.pick_layout(secs, w, h)
            n = n2 if abs(scale2 - scale) <= 0.03 else n
            if abs(scale2 - scale) <= 0.005:
                break
            scale = scale2
        # 3) 측정한 실제 높이가 창을 넘으면 조금씩 줄여서 반드시 한 화면에 들어가게
        for _ in range(10):
            self.slot_cols = self.make_slots(secs, n)
            cols = self.arrange(secs)
            if self.col_extent(cols, secs)[0] <= h or self.scale <= FIT_MIN + 1e-6:
                break
            self.set_k(max(FIT_MIN, self.scale * 0.96))
            secs = self.draw_sections(st)
        else:
            self.slot_cols = self.make_slots(secs, n)
            cols = self.arrange(secs)
        sized = self.apply_sticky(secs)
        self.cols_used = n
        self.place(sized, cols)
        self.cfg["ui_scale"] = round(self.scale, 2)
        self.layout_lock = (w, h, self.scale, self.col_w, n)

    # --- 섹션 배치 (고정) ------------------------------------------------------
    def user_layout(self):
        """cfg["section_layout"] 을 검사·정리해서 ([[id...]...], [숨긴 id...]) — 없거나 잘못됐으면 None.
        알 수 없는 id 는 버리고, 빠진 섹션은 가장 적게 든 열 끝에 붙인다."""
        raw = self.cfg.get("section_layout")
        if not isinstance(raw, dict) or not isinstance(raw.get("cols"), list):
            return None
        hidden = []
        for i in raw.get("hidden") or []:
            if i in SECTION_IDS and i not in NO_HIDE and i not in hidden:
                hidden.append(i)
        seen, cols = set(hidden), []
        for col in raw["cols"][:3]:
            ids = []
            for i in col if isinstance(col, list) else []:
                if i in SECTION_IDS and i not in seen:
                    ids.append(i)
                    seen.add(i)
            if ids:
                cols.append(ids)
        if not cols:
            return None
        for i in SECTION_IDS:
            if i not in seen:
                min(cols, key=len).append(i)
        if cols[0][:1] != ["top"]:  # 제목·버튼·추천 칸은 항상 첫 열 맨 위
            for col in cols:
                if "top" in col:
                    col.remove("top")
            cols = [col for col in cols if col] or [[]]
            cols[0].insert(0, "top")
        return cols, hidden

    def hidden_ids(self):
        user = self.user_layout()
        return set(user[1]) if user else set()

    def pack_ids(self, items, n):
        """masonry: [(id, 높이)] 를 순서대로 가장 짧은 열(동률이면 왼쪽)에 쌓는다. 반환: 열마다 id 목록."""
        cols, used = [[] for _ in range(n)], [0.0] * n
        for sid, hgt in items:
            if hgt <= 0:
                continue
            j = min(range(n), key=lambda cj: (used[cj], cj))
            used[j] += hgt + (GAP if cols[j] else 0)
            cols[j].append(sid)
        return cols

    def est_units(self, secs):
        """{id: 배율 1.0 기준 높이}: 배치·배율을 정할 때는 섹션마다 (접혔거나 작아도) 보통 최대 높이만큼 자리를 본다."""
        est = {}
        for sid, _f, _l, hgt, present in (sec[:5] for sec in secs):
            u = hgt / self.k
            est[sid] = max(u, RESERVE * NOMINAL_UNITS.get(sid, 60))  # 지금 작아도 나중에 커질 수 있으니 어느 정도는 잡아 둠
        return est

    def make_slots(self, secs, n):
        """고정할 배치: 사용자 배치가 있으면 그것, 없으면 지금 높이로 한 번 masonry (이후 잠금이 풀릴 때까지 그대로).
        숨기지 않은 모든 섹션이 들어간다 (지금 안 그려진 분석 섹션도 자리를 가짐)."""
        user = self.user_layout()
        if user:
            return [list(c) for c in user[0]]
        est = self.est_units(secs)
        hidden = self.hidden_ids()
        items = []
        for sid in SECTION_IDS:
            if sid in hidden:
                continue
            u = est.get(sid, NOMINAL_UNITS[sid])
            if sid == "tabs" and self.tab == "inputs":  # 입력칸이 열려 있는 높이로 정하면 안 됨
                u = NOMINAL_UNITS[sid]
            items.append((sid, u))
        return self.pack_ids(items, n)

    def arrange(self, secs):
        """고정된 slot_cols 를 secs 의 인덱스 목록으로. 처음 보는 id 는 가장 짧은 열 끝에 붙이고 그대로 고정."""
        pos = {sec[0]: i for i, sec in enumerate(secs)}
        known = {sid for col in self.slot_cols for sid in col}
        for sid in (sec[0] for sec in secs):
            if sid not in known:
                tall = [sum(secs[pos[i]][3] for i in col if i in pos) for col in self.slot_cols]
                self.slot_cols[tall.index(min(tall))].append(sid)
        return [[pos[i] for i in col if i in pos] for col in self.slot_cols]

    def apply_sticky(self, secs):
        """자리 높이 = 본 적 있는 가장 큰 높이 (탭마다). 반환: 높이가 고쳐진 새 목록 (5번째까지 같고, 6번째가 실제 높이)."""
        seen = self.sticky.setdefault(self.tab, {})
        out = []
        for sid, first, last, hgt, present in (sec[:5] for sec in secs):
            u = seen.get(sid, 0.0)
            seen[sid] = max(u, hgt / self.k)
            out.append((sid, first, last, max(hgt, int(round(u * self.k))), present, hgt))
        return out

    def col_extent(self, cols, secs):
        """(가장 긴 열의 높이 px, 열 폭 합 px) — 위아래 여백 포함."""
        gap = self.s(GAP)
        tall = 0
        for col in cols:
            if col:
                tall = max(tall, sum(secs[i][3] for i in col) + gap * (len(col) - 1))
        return tall + self.s(PAD - 8) + self.s(PAD // 2), len(cols) * self.s(self.col_w) + len(cols) - 1

    def pick_layout(self, secs, w, h):
        """(배율, 열 수, 폭 조절 비율): 섹션이 모두 들어가면서 배율이 가장 큰 열 수 (동률이면 열이 적은 쪽).
        사용자 배치가 있으면 그 열 수·배치 그대로. 배율은 0.02 단위로 내림.
        폭 조절 비율 = (가로 한계 배율 / 세로 한계 배율) — 1보다 작으면 가로가 먼저 차서 열을 좁혀야 함."""
        est = self.est_units(secs)
        order = [sec[0] for sec in secs]
        user = self.user_layout()
        best = None
        for n in ((len(user[0]),) if user else (1, 2, 3)):
            if user:
                groups = [[i for i in col if i in est] for col in user[0]]
            else:
                groups = self.pack_ids([(i, est[i]) for i in order], n)
            hu = max(sum(est[i] for i in g) + GAP * max(len(g) - 1, 0) for g in groups) + (PAD - 8) + PAD // 2
            wu = n * self.col_w + (n - 1)
            sw, sh = (w - 4) / (wu * self.dpi), h / (hu * self.dpi)
            scale = min(sw, sh, SCALE_MAX)
            if best is None or scale > best[0] + 1e-6:
                best = (scale, n, min(max(sw, 0.01) / max(sh, 0.01), 4.0))
        scale = max(FIT_MIN, math.floor(best[0] * 50 + 1e-6) / 50)
        return scale, best[1], best[2]

    def draw_sections(self, st):
        """섹션마다 (0, 0) 기준으로 그림 → [(id, 첫 id, 끝 id, 높이 px, 내용 있음)] (기본 순서). 내용이 없으면 한 줄로 접힌
        제목만 그림 (자리는 그대로). 놓을 자리는 place() 가 옮김."""
        c = self.canvas
        for i in c.find_all():
            if i != self.inputs_item:
                c.delete(i)
        self.W = self.col_w
        self.handle_local = {}
        hidden = self.hidden_ids()
        out = []
        for sid, draw, collapsed in self.section_plan(st):
            if sid in hidden:
                continue
            self._sid = sid
            first = c.create_line(0, 0, 0, 0, state="hidden")
            height = draw(c) if draw else 0
            present = bool(height)
            if not present:
                height = self.sec_collapsed(c, collapsed or SECTION_TITLES[sid])
                self.handle_local[sid] = (0, height)
            last = c.create_line(0, 0, 0, 0, state="hidden")
            out.append((sid, first, last, height, present))
        return out

    def section_plan(self, st):
        """[(id, 그리는 함수 또는 None, 접혔을 때 글)] 기본 순서. 그릴 게 없으면 None (접힌 한 줄)."""
        res = None if self.view.get("message") else self.result
        plan = [("top", self.sec_top, None), ("cards", self.sec_cards, None),
                ("equity", lambda c: self.sec_equity(c, st), None)]
        blocks = self.opp_blocks()  # 상대 예상: 상대마다 한 블록 (최대 3, 주 상대 먼저), 섹션은 하나
        plan.append(("opp", (lambda c: self.sec_opp_all(c, blocks)) if blocks else None, COLLAPSED_TEXT["opp"]))
        notes = []
        guide, guide_text = None, COLLAPSED_TEXT["guide"]
        if res and not res["board"]:
            guide = lambda c: self.sec_pre(c, res, st)
        elif res and res["board"]:
            if res.get("outs") is not None:
                guide = lambda c: self.sec_outs(c, res["outs"])
            else:
                guide_text = "아웃츠 — 플랍·턴에 완성할 드로우가 있으면 표시"
            sit = dict(pf.SITUATIONS).get(st["situation"], "")
            notes.append(f"프리플랍: {st['hero']} · {sit}" + (f" · {pf.VILLAIN_LABEL[st['situation']]} {st['villain']}"
                                                        if st["villains"] else ""))
        plan.append(("guide", guide, guide_text))
        plan.append(("tabs", self.sec_tabs, None))
        if self.tab != "inputs":
            info = self.analysis_info()
            for name in ANALYSIS_IDS:
                plan.append((name, (lambda c, nm=name, inf=info: self.sec_analysis(c, nm, inf))
                             if name in info["visible"] else None, COLLAPSED_TEXT[name]))
        plan.append(("notes", (lambda c: self.sec_notes(c, notes)) if notes else None, COLLAPSED_TEXT["notes"]))
        return plan

    def sec_collapsed(self, c, text):
        """비어 있는 섹션: 한 줄로 접힌 제목 (자리는 유지. 끌어서 옮기거나 우클릭으로 숨길 수 있음)."""
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        t = self.text(c, P, s(8), "▸ " + text, FS_NOTE, fill=TIE_C, width=W - 2 * P)
        return c.bbox(t)[3] + s(8)

    def place(self, secs, cols):
        c = self.canvas
        gap = self.s(GAP)
        cw = self.s(self.col_w)
        moves = {}
        self.col_bottoms = []
        self.handles, self.slot_rects = [], []
        for ci, col in enumerate(cols):
            x = ci * (cw + 1)
            y = self.s(PAD - 8)
            rects = []
            if ci:
                c.create_line(x - 1, self.s(PAD), x - 1, self.win_h - self.s(PAD), fill=LINE)
            for bi, idx in enumerate(col):
                name, first, last, hgt = secs[idx][:4]
                if bi:
                    c.create_line(x + self.s(PAD), y + gap / 2, x + cw - self.s(PAD), y + gap / 2, fill=LINE)
                    y += gap
                moves[name] = (x, y)
                ly0, ly1 = self.handle_local.get(name) or (self.s(2), self.s(38))
                self.handles.append((name, x, y + ly0, x + cw, y + min(ly1, hgt)))
                rects.append((name, y, y + hgt))
                y += hgt
            self.slot_rects.append(rects)
            self.col_bottoms.append(y)
        items = c.find_all()
        for name, first, last, *_ in secs:
            dx, dy = moves[name]
            for i in items:
                if first < i < last:
                    c.move(i, dx, dy)
            c.delete(first, last)
        if self.inputs_item is not None:
            if self.tab == "inputs" and self.placed_inputs is not None and "tabs" in moves:
                dx, dy = moves["tabs"]
                c.coords(self.inputs_item, self.placed_inputs[0] + dx, self.placed_inputs[1] + dy)
                c.itemconfigure(self.inputs_item, state="normal")
            else:
                c.itemconfigure(self.inputs_item, state="hidden")
        if self.toast:  # 알림은 배치에 영향 없이 창 아래쪽에 띄움
            s = self.s
            tw = min(self.measure(self.toast, FS_BODY, "bold"), self.win_w - s(80))
            t = self.text(c, self.win_w / 2, self.win_h - s(28), self.toast, FS_BODY, "bold", BG, anchor="s",
                          width=tw + 2, justify="center")
            b = c.bbox(t)
            bg = round_rect(c, b[0] - s(16), b[1] - s(8), b[2] + s(16), b[3] + s(8), s(10), fill=ACCENT, outline="")
            c.tag_raise(t, bg)

    # --- 섹션 옮기기: 제목줄 끌기 / 우클릭 메뉴 / 배치 초기화 ----------------------------------
    def handle_at(self, x, y):
        """(x, y) 에 있는 섹션 제목줄의 id (버튼·탭 같은 누르는 것 위면 None)."""
        sid = None
        for name, x0, y0, x1, y1 in self.handles:
            if x0 <= x < x1 and y0 <= y < y1:
                sid = name
                break
        if sid is None:
            return None
        c = self.canvas
        for i in c.find_overlapping(x, y, x, y):
            if any(t.startswith(NODRAG_TAGS) for t in c.gettags(i)):
                return None
        return sid

    def hover_handle(self, e):
        """제목줄 위에서는 손 모양 커서."""
        if self.drag is not None and self.drag.get("active"):
            return
        want = "hand2" if self.handle_at(e.x, e.y) else ""
        if want != self._cursor:
            self._cursor = want
            self.canvas.config(cursor=want)

    def drag_press(self, e):
        sid = self.handle_at(e.x, e.y)
        self.drag = {"sid": sid, "x": e.x, "y": e.y, "active": False, "target": None} if sid else None

    def drag_move(self, e):
        d = self.drag
        if d is None:
            return
        if not d["active"]:
            if abs(e.x - d["x"]) + abs(e.y - d["y"]) < 8:  # 눌렀다 살짝 움직인 정도는 끌기가 아님
                return
            d["active"] = True
            self.tip.hide()
            self.canvas.config(cursor="fleur")
        d["target"] = self.drop_target(e.x, e.y, d["sid"])
        self.draw_drag_fx(e.x, e.y)

    def drag_release(self, e):
        d, self.drag = self.drag, None
        if d is None or not d["active"]:
            return
        self.canvas.delete("dragfx")
        self.canvas.config(cursor="")
        self._cursor = ""
        target = self.drop_target(e.x, e.y, d["sid"])
        self.move_section(d["sid"], target[0], target[1])

    def drag_cancel(self, e=None):
        d, self.drag = self.drag, None
        if d is not None and d["active"]:
            self.canvas.delete("dragfx")
            self.canvas.config(cursor="")
            self._cursor = ""
            self.render(force=True)

    def drop_target(self, x, y, sid):
        """(열, 그 열의 (끌고 있는 것 빼고) 몇 번째 앞, 표시선 y). 열은 x 로, 자리는 y 가 섹션 칸의 위/아래 어디인지로."""
        n = len(self.slot_rects)
        cw = self.s(self.col_w) + 1
        ci = min(n - 1, max(0, int(x // cw)))
        rects = [r for r in self.slot_rects[ci] if r[0] != sid]
        idx = sum(1 for r in rects if y > (r[1] + r[2]) / 2)
        gap = self.s(GAP)
        if not rects:
            ly = self.s(PAD - 8)
        elif idx == 0:
            ly = rects[0][1] - gap / 4
        elif idx < len(rects):
            ly = (rects[idx - 1][2] + rects[idx][1]) / 2
        else:
            ly = rects[-1][2] + gap / 4
        return ci, idx, ly

    def draw_drag_fx(self, x, y):
        """끄는 동안: 놓일 자리에 굵은 선, 원래 자리에 점선 테두리, 포인터 옆에 섹션 이름표."""
        c, s, d = self.canvas, self.s, self.drag
        c.delete("dragfx")
        ci, idx, ly = d["target"]
        cw = s(self.col_w)
        x0 = ci * (cw + 1) + s(self.P)
        x1 = ci * (cw + 1) + cw - s(self.P)
        for col, rs in enumerate(self.slot_rects):
            for name, y0, y1 in rs:
                if name == d["sid"]:
                    cx = col * (cw + 1)
                    c.create_rectangle(cx + s(6), y0 + 1, cx + cw - s(6), y1 - 1, outline=MUTED, dash=(4, 4),
                                       tags="dragfx")
        c.create_line(x0, ly, x1, ly, fill=ACCENT, width=max(3, s(4)), capstyle="round", tags="dragfx")
        r = s(5)
        for ex in (x0, x1):
            c.create_oval(ex - r, ly - r, ex + r, ly + r, fill=ACCENT, outline="", tags="dragfx")
        label = SECTION_TITLES.get(d["sid"], d["sid"])
        tw = self.measure(label, FS_SMALL, "bold") + s(20)
        th = self.measure("가", FS_SMALL, "bold") + s(10)
        gx = min(max(x + s(14), 0), self.win_w - tw - s(4))
        gy = min(max(y + s(14), 0), self.win_h - th - s(4))
        bg = round_rect(c, gx, gy, gx + tw, gy + th, s(7), fill=ACCENT, outline="", tags="dragfx")
        t = self.text(c, gx + tw / 2, gy + th / 2, label, FS_SMALL, "bold", BG, anchor="center", tags="dragfx")
        c.tag_raise(t, bg)

    def edit_layout(self, cols, hidden):
        """배치를 저장하고 (빈 열은 없애고) 고정을 풀어 새 배치로 다시 그림."""
        cols = [list(col) for col in cols if col]
        # 맨 위 칸(제목·버튼·추천)은 항상 첫 열 맨 위에 고정
        for col in cols:
            if "top" in col:
                col.remove("top")
        cols = [col for col in cols if col]
        if not cols:
            cols = [[]]
        cols[0].insert(0, "top")
        old = self.cfg.get("section_layout")
        self.cfg["section_layout"] = {"cols": cols[:3], "hidden": list(hidden)}
        self.reset_lock()
        self.render(force=True)
        if self.scale <= FIT_MIN + 1e-6 and max(self.col_bottoms or [0]) > self.win_h + 1:  # 한 화면에 안 들어감 → 취소
            if old is None:
                self.cfg.pop("section_layout", None)
            else:
                self.cfg["section_layout"] = old
            self.reset_lock()
            self.render(force=True)
            self.show_toast("그 배치는 창에 다 들어가지 않아 취소했습니다 — 열을 늘리거나 섹션을 숨겨 보세요")
        save_config(self.cfg)

    def current_layout(self):
        """지금 고정된 배치 ([[id...]...], [숨긴 id...]) (사본)."""
        user = self.user_layout()
        hidden = list(user[1]) if user else []
        cols = [list(col) for col in (self.slot_cols or [])]
        return cols, hidden

    def move_section(self, sid, ci, idx):
        """sid 를 ci 열의 idx 번째(그 열에 보이는 섹션 기준)로. 보이지 않는(탭 때문에 안 그려진) 섹션은 그 자리에 둔다."""
        cols, hidden = self.current_layout()
        visible = [r[0] for r in self.slot_rects[ci] if r[0] != sid] if ci < len(self.slot_rects) else []
        for col in cols:
            if sid in col:
                col.remove(sid)
        while ci >= len(cols):
            cols.append([])
        dest = cols[ci]
        if idx < len(visible) and visible[idx] in dest:
            pos = dest.index(visible[idx])
        elif visible and visible[-1] in dest:
            pos = dest.index(visible[-1]) + 1
        else:
            pos = len(dest)
        dest.insert(pos, sid)
        if [col for col in cols if col] != self.current_layout()[0]:
            self.edit_layout(cols, hidden)

    def shift_section(self, sid, delta):
        """다음 열(+1) / 이전 열(-1)로. 마지막 열에서 +1 이면 (3열 미만일 때) 새 열."""
        cols, hidden = self.current_layout()
        ci = next((i for i, col in enumerate(cols) if sid in col), None)
        if ci is None:
            return
        ni = ci + delta
        if ni < 0 or ni > 2:
            return
        pos = cols[ci].index(sid)
        cols[ci].remove(sid)
        while ni >= len(cols):
            cols.append([])
        cols[ni].insert(min(pos, len(cols[ni])), sid)
        self.edit_layout(cols, hidden)

    def hide_section(self, sid):
        if sid in NO_HIDE:
            return
        cols, hidden = self.current_layout()
        for col in cols:
            if sid in col:
                col.remove(sid)
        if sid not in hidden:
            hidden.append(sid)
        self.edit_layout(cols, hidden)

    def show_section(self, sid):
        cols, hidden = self.current_layout()
        if sid in hidden:
            hidden.remove(sid)
        if cols and not any(sid in col for col in cols):
            min(cols, key=len).append(sid)
        self.edit_layout(cols, hidden)

    def reset_layout(self):
        """사용자 배치를 지우고 기본(자동) 배치로: 한 번 masonry 로 정한 뒤 다시 고정."""
        self.cfg.pop("section_layout", None)
        save_config(self.cfg)
        self.reset_lock()
        self.render(force=True)

    def popup_menu(self, entries, x, y):
        """entries: [(글자, 함수 또는 None=구분선, 사용 가능)] 을 (x, y) 에 띄움. 만든 메뉴를 돌려줌 (시험용)."""
        m = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT, activeforeground=BG,
                    disabledforeground=SLOT, bd=0, relief="flat", font=(F_UI, 10))
        for text, command, enabled in entries:
            if command is None:
                m.add_separator()
            else:
                m.add_command(label=text, command=command, state="normal" if enabled else "disabled")
        if x is not None:
            try:
                m.tk_popup(x, y)
            finally:
                m.grab_release()
        return m

    def section_entries(self, sid):
        cols, _ = self.current_layout()
        ci = next((i for i, col in enumerate(cols) if sid in col), 0)
        can_next = ci + 1 < len(cols) or (len(cols) < 3 and len(cols[ci]) > 1)
        return [("숨기기", lambda: self.hide_section(sid), sid not in NO_HIDE),
                ("다음 열로" + (" (새 열)" if ci + 1 == len(cols) else ""), lambda: self.shift_section(sid, 1), can_next),
                ("이전 열로", lambda: self.shift_section(sid, -1), ci > 0)]

    def section_menu(self, e):
        """제목줄 우클릭: 숨기기 / 다음 열로 / 이전 열로."""
        sid = self.handle_at(e.x, e.y)
        if sid is None or not self.slot_cols:
            return
        self.popup_menu(self.section_entries(sid), e.x_root, e.y_root)

    def layout_entries(self):
        entries = [("기본 배치로 초기화", self.reset_layout, True)]
        _, hidden = self.current_layout()
        if hidden:
            entries.append(("", None, False))
            entries += [(f"숨긴 섹션 보이기 — {SECTION_TITLES[sid]}", lambda sid=sid: self.show_section(sid), True)
                        for sid in hidden]
        return entries

    def layout_menu(self):
        """[배치 초기화] 버튼: 기본 배치로 되돌리기 + 숨긴 섹션 다시 보이기."""
        self.popup_menu(self.layout_entries(), self.root.winfo_pointerx(), self.root.winfo_pointery())

    # --- 섹션 ----------------------------------------------------------------
    def sec_top(self, c):
        self.handle_local["top"] = (0, self.s(8 + 30 + 4))  # 제목줄 = "Card Reader" 줄
        y = self.render_header(c, self.s(8))
        return self.render_advice(c, y) - self.s(12)

    def sec_cards(self, c):
        return self.render_cards(c, self.s(8))

    def sec_equity(self, c, st):
        y = self.s(8)
        msg = self.view.get("message")
        if msg:
            return self.render_message(c, y, *msg)
        if self.result:
            return self.render_result(c, y, self.result, st)
        return self.render_message(c, y, "계산 중…", "")

    def sec_pre(self, c, res, st):
        return self.render_preflop(c, self.s(8), res, st) - self.s(10)

    def sec_outs(self, c, outs):
        return self.render_outs(c, self.s(8), outs)

    def sec_tabs(self, c):
        """[분석]/[입력] 탭 + 입력값 요약 (입력 탭이면 입력칸). 탭은 이 섹션에만 있다."""
        y = self.draw_tabs(c, self.s(8))
        if self.tab == "inputs":
            frame = self.build_inputs_frame()
            self.placed_inputs = (self.s(PAD), y)
            frame.update_idletasks()
            return y + frame.winfo_reqheight()
        return self.draw_summary(c, y) - self.s(10)

    def opp_blocks(self):
        """[(이름, 포지션, 성향 한 줄, 요약 줄들)] 최대 3명, 주 상대(마지막 공격자) 먼저. 내 차례에 계산된 것만."""
        reads = self.reads_now()
        if not reads or self.view.get("message") or (self.table_state or {}).get("hero_folded"):
            return []
        names_m = _optional("names")
        names = self.track.get("names") or {}
        profs = self.track.get("profiles") or {}
        live = self.track.get("live") or {}
        out = []
        for k in reads["order"]:
            lines = reads["lines"].get(k)
            if not lines:
                continue
            nm = names.get(k)
            label = (names_m.display(*nm) if names_m and nm else None) or ("좌석 " + k.replace("seat_", ""))
            pos = (live.get(k) or {}).get("pos")
            p = profs.get(k) or {}
            pline = None
            if p.get("reliable"):
                pline = " / ".join(f"{n} {p[key]:.0f}" for n, key in (("VPIP", "vpip"), ("PFR", "pfr")) if p.get(key) is not None)
                pline = (pline + " · " if pline else "") + f"{p.get('hands', 0)}핸드"
            out.append((label, pos, pline, lines))
            if len(out) == 3:
                break
        return out

    def sec_opp_all(self, c, blocks):
        """상대 예상: 상대마다 한 블록 (최대 3명)을 한 섹션 안에 쌓는다 (자리는 하나)."""
        y = self.s(8)
        for idx, blk in enumerate(blocks):
            if idx:
                c.create_line(self.s(self.P), y + self.s(GAP) / 2, self.s(self.W) - self.s(self.P),
                              y + self.s(GAP) / 2, fill=LINE)
                y += self.s(GAP)
            y = self.sec_opp(c, idx, blk, y)
        return y

    def sec_opp(self, c, idx, blk, y):
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        label, pos, pline, lines = blk
        if idx == 0:  # 첫 블록에만 섹션 제목
            stale = self.reads_now() and tuple(self.reads_now()["board"]) != tuple(
                c_ for c_ in self.view.get("board", []) if ev.is_card(c_))
            y = self.section_title(c, y, W, P, "상대 예상", "opp", right="이전 계산" if stale else None,
                                   right_color=WARN)
        t = self.text(c, P, y, label, FS_BODY + 1, "bold", TEXT, width=W - 2 * P - s(70))
        b = c.bbox(t)
        if pos:
            w = self.measure(pos, FS_SMALL, "bold") + s(14)
            round_rect(c, b[2] + s(8), b[1] + s(2), b[2] + s(8) + w, b[3] - s(1), s(6), fill=CHIP, outline="")
            self.text(c, b[2] + s(8) + w / 2, (b[1] + b[3]) / 2 + s(0.5), pos, FS_SMALL, "bold", ACCENT, anchor="center")
        y = b[3] + s(2)
        if pline:
            t = self.text(c, P, y, pline, FS_NOTE, "bold", ACCENT, width=W - 2 * P)
            y = c.bbox(t)[3] + s(2)
        for ln in lines:
            t = self.text(c, P, y, ln, FS_NOTE, fill=MUTED, width=W - 2 * P)
            y = c.bbox(t)[3] + s(2)
        return y

    def sec_notes(self, c, notes):
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        y = self.s(8)
        for text in notes:
            t = self.text(c, P, y, text, FS_NOTE, fill=MUTED, width=W - 2 * P)
            y = c.bbox(t)[3] + s(4)
        return y

    def draw_tabs(self, c, y):
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        x = P
        th = self.measure("가", FS_H + 2, "bold")
        self.handle_local["tabs"] = (0, y + th + s(10))  # 제목줄 = 탭 줄 (탭 글자 자체는 눌러서 전환)
        for key, label in TABS:
            on = self.tab == key
            tag = self.new_tag("tab")
            tw = self.measure(label, FS_H + 2, "bold")
            c.create_rectangle(x - s(10), y - s(6), x + tw + s(10), y + th + s(10), fill=PANEL if on else BG,
                               outline="", tags=tag)
            self.text(c, x, y, label, FS_H + 2, "bold", ACCENT if on else MUTED, tags=tag)
            if on:
                c.create_rectangle(x - s(10), y + th + s(7), x + tw + s(10), y + th + s(10), fill=ACCENT, outline="")
            c.tag_bind(tag, "<Button-1>", lambda e, k=key: self.set_tab(k))
            c.tag_bind(tag, "<Enter>", lambda e: self.root.config(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: self.root.config(cursor=""))
            x += tw + s(30)
        c.create_line(P - s(10), y + th + s(10), W - P, y + th + s(10), fill=LINE)
        return y + th + s(22)

    def set_tab(self, key):
        if key != self.tab:
            self.tab = self.cfg["tab"] = key
            self.save_later()
            self.render(force=True)

    def draw_summary(self, c, y):
        """입력값 한 줄 요약 (크게). 누르면 [입력] 탭."""
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        bb_unit = any("bb" in self.inputs[k].get().lower() for k in ("pot", "bet", "my_stack", "opp_stack"))

        def fmt(v):
            return f"{round(v, 1):g}BB" if bb_unit else odds.format_amount(v)
        parts = [f"{label} {fmt(v)}" for label, v in (("팟", self.amount("pot")), ("콜", self.amount("bet")),
                                                       ("내 스택", self.amount("my_stack")),
                                                       ("상대", self.amount("opp_stack"))) if v]
        stacks = [v for v in (self.amount("my_stack"), self.amount("opp_stack")) if v]
        if len(stacks) == 2:
            parts.append(f"유효 {fmt(min(stacks))}")
        tag = self.new_tag("sum")
        pad = s(12)
        auto = bool(self.cfg.get("auto_numbers") and self.cfg.get("number_regions"))
        x = P + pad
        top = y
        if auto:
            bw = self.measure("자동", FS_SMALL, "bold") + s(14)
            bh = self.measure("가", FS_SMALL) + s(8)
            round_rect(c, x, y + pad, x + bw, y + pad + bh, s(6), fill="#123b37", outline="", tags=tag)
            self.text(c, x + bw / 2, y + pad + bh / 2, "자동", FS_SMALL, "bold", ACCENT, anchor="center", tags=tag)
            x += bw + s(8)
        t = self.text(c, x, y + pad, "  ·  ".join(parts) if parts else "입력값 없음 — 눌러서 입력", FS_BODY + 1,
                      "bold", TEXT if parts else MUTED, width=W - P - pad - x, tags=tag)
        bottom = c.bbox(t)[3] + pad
        bg = round_rect(c, P, top, W - P, bottom, s(10), fill=PANEL, outline="", tags=tag)
        c.tag_lower(bg)
        c.tag_bind(tag, "<Button-1>", lambda e: self.set_tab("inputs"))
        c.tag_bind(tag, "<Enter>", lambda e: self.root.config(cursor="hand2"))
        c.tag_bind(tag, "<Leave>", lambda e: self.root.config(cursor=""))
        return bottom + s(16)

    def text(self, c, x, y, text, pt, weight="normal", fill=TEXT, family=F_UI, **kw):
        kw.setdefault("anchor", "nw")
        return c.create_text(x, y, text=text, font=self.font(pt, weight, family), fill=fill, **kw)

    def header_button(self, c, key, text, right, y, h, primary=False):
        w = self.measure(text, FS_SMALL, "bold") + self.s(22)
        self.buttons[key].draw(c, right - w, y, right, y + h, text, primary=primary,
                               font=self.font(FS_SMALL, "bold"), r=self.s(7))
        return right - w - self.s(6)

    def render_header(self, c, y):
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        h = s(30)
        self.text(c, P, y + h / 2, "Card Reader", FS_TITLE, "bold", anchor="w")
        explain = self.cfg.get("explain", True)
        x = self.header_button(c, "explain", "설명 켬" if explain else "설명 끔", W - P, y, h, primary=explain)
        x -= s(8)
        x = self.header_button(c, "bigger", "A+", x, y, h)
        pw = self.measure("200%", FS_SMALL, "bold") + s(6)
        self.text(c, x - pw / 2, y + h / 2, f"{round(self.scale * 100)}%", FS_SMALL, "bold", MUTED, anchor="center")
        x = self.header_button(c, "smaller", "A−", x - pw - s(6), y, h)
        y += h + s(8)
        # 헤더 버튼: 카드 = 카드 영역 지정, 숫자 = 숫자 영역 지정, 학습 = 카드 학습
        x = P
        for bkey, text in (("auto", "자동 지정"), ("region", "카드"), ("numbers", "숫자"), ("learn", "학습")):
            w = self.measure(text, FS_SMALL) + s(22)
            self.buttons[bkey].draw(c, x, y, x + w, y + h, text, font=self.font(FS_SMALL), r=s(7),
                                    primary=bkey == "auto" and not self.cfg["regions"])
            x += w + s(6)
        # 기록 모드: 켜면 빨간 점 + 경과 시간 (검증용 캡처 저장)
        on = self.rec_on
        label = (f"{self.rec_elapsed() // 60:02d}:{self.rec_elapsed() % 60:02d}" if on else "기록")
        w = self.measure(label, FS_SMALL, "bold") + s(22) + s(26)
        self.buttons["rec"].draw(c, x, y, x + w, y + h, label, font=self.font(FS_SMALL, "bold"), r=s(7))
        c.create_oval(x + s(9), y + h / 2 - s(4), x + s(18), y + h / 2 + s(5), fill="#ef4444" if on else "",
                      outline="#ef4444" if on else MUTED)
        c.tag_bind("btn_rec", "<Enter>", lambda e: self.tip.show(
            "기록 모드: 게임 창을 1초마다 recordings 폴더(exe 옆)에 저장합니다. 액션 추적(핸드 기록)이 맞는지 나중에 "
            "검증하는 용도 · 평소엔 꺼 두세요", e.x_root, e.y_root), add="+")
        c.tag_bind("btn_rec", "<Leave>", lambda e: self.tip.hide(), add="+")
        x += w + s(6)
        # 셋째 줄 (항상 같은 높이): 배치 초기화(+ 숨긴 섹션 다시 보이기) · 분석 방식 (자동 / 클릭)
        row3 = y + h + s(6)
        x3 = P
        lw = self.measure("배치 초기화", FS_SMALL) + s(22)
        self.buttons["layout"].draw(c, x3, row3, x3 + lw, row3 + h, "배치 초기화", font=self.font(FS_SMALL), r=s(7))
        c.tag_bind("btn_layout", "<Enter>", lambda e: self.tip.show(
            "섹션 제목줄을 끌어서 옮기고, 우클릭으로 숨기거나 다른 열로 보냅니다 · 여기서 기본 배치로 되돌리거나 숨긴 섹션을 "
            "다시 보입니다", e.x_root, e.y_root), add="+")
        c.tag_bind("btn_layout", "<Leave>", lambda e: self.tip.hide(), add="+")
        x3 += lw + s(6)
        click = self.click_mode()
        label = "분석: 클릭" if click else "분석: 자동"
        mw = max(self.measure(t, FS_SMALL, "bold") for t in ("분석: 클릭", "분석: 자동")) + s(22)
        self.buttons["mode"].draw(c, x3, row3, x3 + mw, row3 + h, label, primary=click,
                                  font=self.font(FS_SMALL, "bold"), r=s(7))
        c.tag_bind("btn_mode", "<Enter>", lambda e: self.tip.show(
            "분석: 자동 = 내 차례에 에쿼티·추천·상대 예상을 자동 계산 · 분석: 클릭 = [지금 계산]을 누를 때만 계산 "
            "(카드 인식·팟 오즈 같은 가벼운 것은 항상 자동)", e.x_root, e.y_root), add="+")
        c.tag_bind("btn_mode", "<Leave>", lambda e: self.tip.hide(), add="+")
        x3 += mw + s(12)
        # 플레이 성향: 플레이 타이트 ◀ [값] ▶ 루즈  (자리가 모자라면 양끝 글자부터 뺀다. 줄 높이는 그대로)
        loose = self.looseness()
        vw = max(self.measure(self.looseness_text(t / 10), FS_SMALL, "bold") for t in (-10, -7, -4, 0, 4, 7, 10)) + s(8)
        bw_ = h
        need_core = self.measure("플레이", FS_SMALL) + s(6) + bw_ + vw + bw_
        ends = self.measure("타이트", FS_NOTE) + self.measure("루즈", FS_NOTE) + s(12)
        if x3 + need_core <= W - P:
            show_ends = x3 + need_core + ends <= W - P
            t = self.text(c, x3, row3 + h / 2, "플레이", FS_SMALL, "bold", MUTED, anchor="w")
            x3 = c.bbox(t)[2] + s(6)
            if show_ends:
                t = self.text(c, x3, row3 + h / 2, "타이트", FS_NOTE, fill=MUTED, anchor="w")
                x3 = c.bbox(t)[2] + s(4)
            self.buttons["loose_dn"].draw(c, x3, row3, x3 + bw_, row3 + h, "◀", font=self.font(FS_SMALL, "bold"), r=s(7))
            x3 += bw_
            self.text(c, x3 + vw / 2, row3 + h / 2, self.looseness_text(loose), FS_SMALL, "bold",
                      ACCENT if abs(loose) >= 0.05 else TEXT, anchor="center")
            x3 += vw
            self.buttons["loose_up"].draw(c, x3, row3, x3 + bw_, row3 + h, "▶", font=self.font(FS_SMALL, "bold"), r=s(7))
            x3 += bw_
            if show_ends:
                self.text(c, x3 + s(4), row3 + h / 2, "루즈", FS_NOTE, fill=MUTED, anchor="w")
            for bk in ("loose_dn", "loose_up"):
                c.tag_bind(f"btn_{bk}", "<Enter>", lambda e: self.tip.show(
                    "플레이 성향: ◀ 타이트(좁게) · ▶ 루즈(넓게, 공격적) · 0.0 = 솔버 근사 표준 · 0.1 단위. "
                    "추천과 프리플랍 가이드 격자에 바로 반영됩니다", e.x_root, e.y_root), add="+")
                c.tag_bind(f"btn_{bk}", "<Leave>", lambda e: self.tip.hide(), add="+")
        rows = h + s(6)
        # 상태 점 + 학습된 카드 수 (버튼 줄 오른쪽, 자리가 있을 때만)
        msg = self.view.get("message")
        dot = MUTED if not self.cfg["regions"] else (WARN if msg and msg[0] not in ("핸드 대기 중", "폴드") else WIN_C)
        txt = f"{len(self.matcher.labels)}/52"
        if x + self.measure(txt, FS_NOTE) + s(24) <= W - P:
            t = self.text(c, W - P, y + h / 2, txt, FS_NOTE, fill=MUTED, anchor="e")
            b = c.bbox(t)
            c.create_oval(b[0] - s(15), y + h / 2 - s(4), b[0] - s(6), y + h / 2 + s(5), fill=dot, outline="")
        return y + rows + h + s(16)

    def render_advice(self, c, y):
        """맨 위 추천 카드: 추천 행동(크게) + 이유 + 대응 계획 + 세부 + 상태 줄. 추천이 없으면 이유(대기 사유)를 보여준다."""
        adv, ts = self.advice, self.table_state
        status = None if adv else self.advice_status
        if not adv and not ts and not status and not self.err_note:
            return y
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        pad = s(14)
        x0, x1 = P, W - P
        tx, tw = x0 + pad, x1 - x0 - 2 * pad
        iy = y + pad
        outline = ""
        gate_closed = not self.heavy_allowed()
        click = self.click_mode()

        def pill(key, text, x, top, right_align=False, primary=False):
            """작은 버튼: (그린 폭, 높이)"""
            bw = self.measure(text, FS_SMALL, "bold") + s(18)
            bh = self.measure("가", FS_SMALL) + s(8)
            if right_align:
                x -= bw
            self.buttons[key].draw(c, x, top, x + bw, top + bh, text, primary=primary, font=self.font(FS_SMALL, "bold"),
                                   r=s(6))
            return bw, bh
        if adv:
            waiting = (ts.get("to_act") is False if ts else False) and not click  # 내 차례 아님 → 계획만, 흐리게
            stale = self.advice_stale
            color = ADVICE_COLORS.get(adv.get("color_key"), ACCENT)
            if waiting or stale:
                color = blend(color, PANEL, 0.45)
            else:
                outline = color
            tag = self.new_tag("tip")
            cap = self.text(c, tx, iy, "추천" + ("  ·  폴드 전 마지막 분석 (복기용)" if self.fold_view is not None else
                                                ("  ·  차례가 오면" if waiting else "") + ("  ·  이전 계산" if stale else "")),
                            FS_NOTE, "bold", WARN if stale else MUTED, tags=tag)
            self.bind_tip(c, tag, f"{EXPLAIN['advice'][0]}: {EXPLAIN['advice'][2]}")
            rx, cap_bottom = x1 - pad, c.bbox(cap)[3]
            if gate_closed:  # 내 차례가 아니라 계산을 미뤘다 → 직접 계산 버튼
                fresh = click and not stale  # 클릭 분석: 최신 결과면 '다시 계산', 아니면 눈에 띄게 '지금 계산'
                bw, bh = pill("calc", "다시 계산" if fresh else "지금 계산", rx, iy - s(2), right_align=True,
                              primary=click and not fresh)
                rx -= bw + s(10)
                cap_bottom = max(cap_bottom, iy - s(2) + bh)
            conf = "" if stale else CONFIDENCE.get(adv.get("confidence"), "")
            if conf:
                self.text(c, rx, iy, conf, FS_NOTE, fill=MUTED, anchor="ne")
            iy = cap_bottom + s(2)
            action = str(adv.get("action") or "")
            size = str(adv.get("size_text") or "")
            t = self.text(c, tx, iy, action, FS_ACTION * (0.8 if waiting else 1), "bold", color, width=tw)
            b = c.bbox(t)
            if size and size not in action:
                st = self.text(c, b[2] + s(12), b[3] - s(8), size, FS_H + 3, "bold", color if waiting else TEXT,
                               anchor="sw", width=max(s(60), x1 - pad - b[2] - s(12)))
                b = (b[0], b[1], b[2], max(b[3], c.bbox(st)[3]))
            iy = b[3] + s(4)
            reason = adv.get("reason")
            if reason:
                t = self.text(c, tx, iy, str(reason), FS_BODY, "bold", MUTED if waiting else TEXT, width=tw)
                iy = c.bbox(t)[3] + s(4)
            plan = adv.get("plan") or []
            if isinstance(plan, str):
                plan = [plan]
            if plan:  # 대응 계획: 눈에 잘 띄게 본문보다 한 단계 크게
                iy += s(2)
                for line in list(plan)[:3]:
                    t = self.text(c, tx, iy, f"\u25b8 {line}", FS_BODY + 1, "bold", TEXT, width=tw)
                    iy = c.bbox(t)[3] + s(3)
                iy += s(2)
            details = adv.get("detail") or []
            if isinstance(details, str):
                details = [details]
            for d in list(details)[:3]:
                t = self.text(c, tx, iy, f"· {d}", FS_NOTE, fill=MUTED, width=tw)
                iy = c.bbox(t)[3] + s(2)
        elif status:
            text, kind, buttons = status
            color = {"warn": WARN, "err": LOSE_C}.get(kind, MUTED)
            t = self.text(c, tx, iy, text, FS_BODY + 1, "bold", color, width=tw)
            b = c.bbox(t)
            line_h = self.measure("가", FS_BODY + 1, "bold")
            bx, by, bottom = b[2] + s(10), b[1], b[3]
            for key in buttons:
                bkey, label = {"learn": ("st_learn", "학습"), "auto": ("st_auto", "자동 지정"),
                               "calc": ("calc", "지금 계산")}[key]
                bw = self.measure(label, FS_SMALL, "bold") + s(18)
                if bx + bw > x1 - pad or b[3] - b[1] > line_h * 1.6:  # 한 줄에 안 들어가면 아래 줄로
                    bx, by = tx, bottom + s(6)
                bw, bh = pill(bkey, label, bx, by + s(1), primary=click and bkey == "calc")
                bx += bw + s(8)
                bottom = max(bottom, by + s(1) + bh)
            iy = bottom + s(2)
        if self.err_note:
            t = self.text(c, tx, iy + s(4), self.err_note, FS_NOTE, "bold", LOSE_C, width=tw)
            iy = c.bbox(t)[3] + s(2)
        if ts:
            if adv or status or self.err_note:
                iy += s(8)
                c.create_line(tx, iy, x1 - pad, iy, fill=LINE)
                iy += s(10)
            iy = self.render_strip(c, tx, iy, tw, ts)
        bottom = iy + pad - s(2)
        bg = round_rect(c, x0, y, x1, bottom, s(12), fill=PANEL, outline=outline, width=max(1, s(2)))
        c.tag_lower(bg)
        return bottom + s(16)

    def render_strip(self, c, x0, y, width, ts):
        """상태 칩 한 줄 (넘치면 줄바꿈)."""
        s = self.s
        board = [b for b in self.view.get("board", []) if ev.is_card(b)]
        chips = [(STREET_NAMES.get(len(board), "딜링 중"), TEXT)]
        if ts.get("hero_pos"):
            chips.append((str(ts["hero_pos"]), TEXT))
        players, in_hand = _number(ts.get("players")), _number(ts.get("in_hand"))
        if players:
            chips.append((f"{int(players)}명 중 {int(in_hand)}명 참여" if in_hand is not None else f"{int(players)}명",
                          TEXT))
        if ts.get("hero_folded"):
            chips.append(("폴드함", LOSE_C))
        elif ts.get("to_act") is True:
            chips.append(("내 차례", WARN))
        elif ts.get("to_act") is False:
            chips.append(("대기", MUTED))
        call = _number(ts.get("call"))
        if call:  # 테이블 상태 금액은 BB 단위
            chips.append((f"콜 {round(call, 1):g}BB", TEXT))
        elif ts.get("can_check") and ts.get("to_act"):
            chips.append(("체크 가능", TEXT))
        h = self.measure("가", FS_BODY) + s(12)
        x = x0
        for text, color in chips:
            w = self.measure(text, FS_BODY, "bold") + s(18)
            if x > x0 and x + w > x0 + width:
                x, y = x0, y + h + s(6)
            round_rect(c, x, y, x + w, y + h, s(8), fill="#232b35", outline="")
            self.text(c, x + w / 2, y + h / 2, text, FS_BODY, "bold", color, anchor="center")
            x += w + s(6)
        return y + h

    def render_cards(self, c, y):
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        hero = self.view.get("hero", [])
        board = self.view.get("board", [])
        gap = s(6)
        cw = min(s(50), int((W - 2 * P - 6 * gap - s(26)) / 7))  # 내 카드 2 + 보드 5 가 열 폭에 들어가게
        ch = int(cw * 1.4)
        self.text(c, P, y, "내 카드", FS_NOTE, "bold", MUTED)
        bx = P + 2 * cw + gap + s(26)
        t = self.text(c, bx, y, "보드", FS_NOTE, "bold", MUTED)
        y = c.bbox(t)[3] + s(6)
        self.handle_local["cards"] = (0, y)  # 제목줄 = 라벨 줄
        for i in range(2):
            draw_card(c, P + i * (cw + gap), y, cw, ch, hero[i] if i < len(hero) else None)
        c.create_line(bx - s(13), y + s(4), bx - s(13), y + ch - s(4), fill=LINE)
        for i in range(5):
            draw_card(c, bx + i * (cw + gap), y, cw, ch, board[i] if i < len(board) else None)
        return y + ch + s(4)

    def render_message(self, c, y, title, body):
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        t = self.text(c, P, y, title, FS_H + 1, "bold")
        y = c.bbox(t)[3] + s(6)
        if body:
            t = self.text(c, P, y, body, FS_NOTE, fill=MUTED, width=W - 2 * P)
            y = c.bbox(t)[3] + s(6)
        return y

    def badge(self, c, x, y, key, align="left"):
        """자동 채움 표시. '수동'을 누르면 다시 자동으로. 그린 폭을 반환 (테이블 상태가 없으면 0)."""
        if not self.table_state:
            return 0
        s = self.s
        manual = key in self.manual
        text = "수동 · 자동으로" if manual else "자동"
        w = self.measure(text, FS_SMALL, "bold") + s(14)
        if align == "right":
            x -= w
        h = self.measure("가", FS_SMALL) + s(8)
        tag = self.new_tag("badge")
        round_rect(c, x, y, x + w, y + h, s(6), fill=CHIP if manual else "#123b37", outline="", tags=tag)
        self.text(c, x + w / 2, y + h / 2, text, FS_SMALL, "bold", MUTED if manual else ACCENT, anchor="center",
                  tags=tag)
        if manual:
            c.tag_bind(tag, "<Button-1>", lambda e: self.resume_auto(key))
            c.tag_bind(tag, "<Enter>", lambda e: self.root.config(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: self.root.config(cursor=""))
        return w

    def section_title(self, c, y, W, P, title, key, right=None, right_color=MUTED, badge=None, extra=None,
                      right_pt=FS_NOTE, right_weight="normal"):
        """섹션 제목 (+ 마우스를 올리면 자세한 설명) + 설명 켬이면 한 줄 설명 (자리가 있으면 제목 옆). 다음 y."""
        s = self.s
        y_top = y
        tag = self.new_tag("tip")
        t = self.text(c, P, y, title, FS_H, "bold", tags=tag)
        self.bind_tip(c, tag, f"{EXPLAIN[key][0]}: {EXPLAIN[key][2]}")
        b = c.bbox(t)
        x = b[2] + s(10)
        if extra:  # (글자, 글자색, 배경)
            text, fg, bg = extra
            w = self.measure(text, FS_SMALL, "bold") + s(14)
            round_rect(c, x, b[1] + s(1), x + w, b[3] - s(1), s(6), fill=bg, outline="")
            self.text(c, x + w / 2, (b[1] + b[3]) / 2, text, FS_SMALL, "bold", fg, anchor="center")
            x += w + s(6)
        if badge:
            x += self.badge(c, x, b[1] + s(1), badge) + s(6)
        bottom = b[3]
        if right:
            rw = self.measure(right, right_pt, right_weight)
            if x + rw <= W - P:
                self.text(c, W - P, (b[1] + b[3]) / 2, right, right_pt, right_weight, right_color, anchor="e")
                x = W - P - rw - s(12)  # 설명이 들어갈 수 있는 오른쪽 끝
            else:  # 자리가 없으면 다음 줄
                t = self.text(c, P, bottom + s(2), right, right_pt, right_weight, right_color, width=W - 2 * P)
                bottom = c.bbox(t)[3]
                x = None
        else:
            x = (x, W - P)
        if self.cfg.get("explain", True):
            text = EXPLAIN[key][1]
            if isinstance(x, tuple) and x[0] + s(4) + self.measure(text, FS_NOTE) <= x[1]:
                self.text(c, x[0] + s(4), b[3], text, FS_NOTE, fill=EXPLAIN_C, anchor="sw")  # 제목 옆
            else:
                t = self.text(c, P, bottom + s(4), text, FS_NOTE, fill=EXPLAIN_C, width=W - 2 * P)
                bottom = c.bbox(t)[3]
        if self._sid is not None:  # 섹션 제목줄 (끌어서 옮기는 손잡이)
            self.handle_local[self._sid] = (max(0, y_top - s(6)), bottom + s(4))
        return bottom + s(10)

    def render_result(self, c, y, res, st):
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        eq = res["equity"]
        stale = self.computing or self.result_stale()

        y = self.section_title(c, y, W, P, "에쿼티 (승률)", "equity", right=res["hand"], right_color=TEXT,
                               right_pt=FS_BODY, right_weight="bold")
        if eq is None:  # 내 차례 전: 시뮬레이션은 미뤄 둠 (CPU 절약)
            t = self.text(c, P, y, (f"클릭해서 분석 (상대 {res['opponents']}명 기준)" if self.click_mode() else
                           f"내 차례가 되면 계산합니다 (상대 {res['opponents']}명 기준)"), FS_BODY, "bold", MUTED,
                          width=W - 2 * P)
            y = c.bbox(t)[3] + s(8)
            bw = self.measure("지금 계산", FS_SMALL, "bold") + s(18)
            bh = self.measure("가", FS_SMALL) + s(8)
            self.buttons["calc2"].draw(c, P, y, P + bw, y + bh, "지금 계산", primary=self.click_mode(),
                                       font=self.font(FS_SMALL, "bold"), r=s(6))
            return y + bh + s(4)

        # 에쿼티 + 상대 수 조절
        num = self.text(c, P - s(2), y - s(4), f"{eq['equity'] * 100:.1f}%", FS_EQ, "bold",
                        MUTED if stale else ACCENT, F_NUM)
        nb = c.bbox(num)
        bs, nw = s(38), s(42)
        cy = (nb[1] + nb[3]) / 2
        sx = W - P
        self.buttons["plus"].draw(c, sx - bs, cy - bs / 2, sx, cy + bs / 2, "+", font=self.font(FS_H + 2, "bold", F_NUM),
                                  r=s(7))
        self.text(c, sx - bs - nw / 2, cy, str(self.opponents()), FS_H + 3, "bold", TEXT, F_NUM, anchor="center")
        mx = sx - 2 * bs - nw
        self.buttons["minus"].draw(c, mx, cy - bs / 2, mx + bs, cy + bs / 2, "−",
                                   font=self.font(FS_H + 2, "bold", F_NUM), r=s(7))
        lab = self.text(c, sx - bs - nw / 2, cy + bs / 2 + s(4), "상대 수", FS_NOTE, fill=MUTED, anchor="n")
        self.badge(c, mx - s(10), cy - (self.measure("가", FS_SMALL) + s(8)) / 2, "opponents", align="right")
        method = "정확 계산" if eq["exact"] else f"시뮬레이션 {eq['samples']:,}회"
        n = res["opponents"]
        head = self.text(c, P, nb[3] + s(2), f"상대 {n}명 기준 승률" + ("  (다시 계산 중…)" if self.computing else "  (이전 계산)" if stale else ""),
                         FS_BODY, "bold", MUTED if stale else TEXT, width=mx - P - s(12))
        basis = ("범위 기준 (상대 예상 레인지)" if res.get("equity_basis") == "range" else "랜덤 기준 (랜덤 핸드 가정)")
        t = self.text(c, P, c.bbox(head)[3] + s(2), f"{basis} · {method}", FS_NOTE,
                      fill=ACCENT if res.get("equity_basis") == "range" else MUTED, width=mx - P - s(12))
        y = max(c.bbox(t)[3], c.bbox(lab)[3]) + s(14)

        # 승/무/패 막대
        bw = W - 2 * P
        x = P
        for value, color in ((eq["win"], WIN_C), (eq["tie"], TIE_C), (eq["lose"], LOSE_C)):
            w = bw * value
            if w >= 1:
                c.create_rectangle(x, y, x + w, y + s(10), fill=color, outline="")
            x += w
        y += s(18)
        x = P
        lh = self.measure("가", FS_BODY)
        for name, value, color in (("승", eq["win"], WIN_C), ("무", eq["tie"], TIE_C), ("패", eq["lose"], LOSE_C)):
            c.create_oval(x, y + lh / 2 - s(4), x + s(9), y + lh / 2 + s(5), fill=color, outline="")
            t = self.text(c, x + s(14), y, f"{name} {value * 100:.1f}%", FS_BODY)
            x = c.bbox(t)[2] + s(18)
        return c.bbox(t)[3] + s(4)

    def render_outs(self, c, y, outs):
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        y = self.section_title(c, y, W, P, f"아웃츠 {outs['count']}장", "outs",
                               extra=("콤보 드로우", ACCENT, "#123b37") if outs["combo"] else None)
        rows = [("리버까지 (2장)", outs["by_river"], 2)] if outs["left"] == 2 else []
        rows.append(("다음 카드 (1장)", outs["next"], 1))
        px = P + max(self.measure(n, FS_BODY) for n, _, _ in rows) + s(16)
        for name, prob, n in rows:
            t = self.text(c, P, y, name, FS_BODY, fill=MUTED)
            p = self.text(c, px, y - s(3), f"{prob * 100:.1f}%", FS_H + 3, "bold", ACCENT, F_NUM)
            self.text(c, W - P, y, f"4-2 법칙 ≈ {odds.rule_of_4_2(outs['count'], n)}%", FS_NOTE, fill=MUTED,
                      anchor="ne")
            y = max(c.bbox(t)[3], c.bbox(p)[3]) + s(6)
        y += s(6)
        if not outs["groups"]:
            t = self.text(c, P, y, "드로우가 없습니다", FS_BODY, fill=MUTED)
            y = c.bbox(t)[3] + s(10)
        mw, mh, mg = s(28), s(40), s(4)
        names = [f"{DRAW_SHORT.get(name, name)}  {len(cards)}" for name, cards in outs["groups"]]
        label_w = max([self.measure(n, FS_BODY) for n in names] + [s(60)]) + s(12)
        per_row = max(1, int((W - 2 * P - label_w + mg) // (mw + mg)))
        for text, (name, cards) in zip(names, outs["groups"]):
            self.text(c, P, y + mh / 2, text, FS_BODY, anchor="w")
            for i, card in enumerate(cards):
                row, col = divmod(i, per_row)
                draw_card(c, P + label_w + col * (mw + mg), y + row * (mh + mg), mw, mh, ev.card_str(card))
            y += ((len(cards) - 1) // per_row + 1) * (mh + mg) + s(8)
        return y

    # --- 프리플랍 가이드 ------------------------------------------------------
    def preflop_state(self):
        """저장된 선택값(+ 테이블 상태 자동값)을 현재 인원/상황에 맞게 보정해서 반환."""
        st = dict(DEFAULT_PREFLOP, **self.cfg.get("preflop", {}))
        auto = self.auto_values().get("preflop")
        if auto:
            st.update(auto)
        st["auto"] = bool(auto)
        try:
            st["players"] = max(6, min(10, int(st["players"])))
        except (TypeError, ValueError):
            st["players"] = DEFAULT_PREFLOP["players"]
        stacks = [v for v in (self.amount("my_stack"), self.amount("opp_stack")) if v] if self.inputs else []
        bb = self.amount("bb") if self.inputs else None
        st["stack_bb"] = min(stacks) / bb if stacks and bb else None
        # 추천(advice_ctx)과 격자가 같은 유효 스택을 쓰도록: 프리플랍이면 테이블 상태의 스택, 내 스택보다 크지 않게
        board_seen = [b for b in self.view.get("board", []) if ev.is_card(b)]
        ts_stack = None if board_seen else _number(((self.table_state or {}).get("preflop") or {}).get("stack_bb"))
        base = ts_stack or st["stack_bb"]
        mine = self.amount("my_stack") / bb if self.inputs and bb and self.amount("my_stack") else None
        st["stack_bb"] = min(base, mine) if base and mine else (base or mine)
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
        """알약을 직접 누름 → 지금 보이는 값을 저장하고 이번 핸드 동안은 수동."""
        st = self.preflop_state()
        st.update(changes)
        self.cfg["preflop"] = {k: st[k] for k in DEFAULT_PREFLOP}
        if self.table_state:
            self.manual.add("preflop")
        save_config(self.cfg)
        self.schedule_advice()
        self.render()

    def pills(self, c, y, label, options, selected, on_pick, x0):
        """라벨 + 알약 버튼 (자리가 모자라면 줄바꿈). options: [(값, 표시)]"""
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        h = self.measure("가", FS_SMALL) + s(12)
        self.text(c, P, y + h / 2, label, FS_NOTE, fill=MUTED, anchor="w")
        x, gap = x0, s(5)
        for value, text in options:
            on = value == selected
            w = max(s(34), self.measure(text, FS_SMALL, "bold" if on else "normal") + s(16))
            if x > x0 and x + w > W - P:
                x, y = x0, y + h + gap
            tag = self.new_tag("pill")
            round_rect(c, x, y, x + w, y + h, s(7), fill=ACCENT if on else CHIP, outline="", tags=tag)
            self.text(c, x + w / 2, y + h / 2, text, FS_SMALL, "bold" if on else "normal", BG if on else TEXT,
                      anchor="center", tags=tag)
            c.tag_bind(tag, "<Button-1>", lambda e, v=value: on_pick(v))
            c.tag_bind(tag, "<Enter>", lambda e: self.root.config(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: self.root.config(cursor=""))
            x += w + gap
        return y + h + s(7)

    def render_preflop(self, c, y, res, st):
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        stack = (f"유효 {st['stack_bb']:.1f}BB" if st["stack_bb"] is not None else "유효 스택 미입력 (딥스택 기준)")
        if st["short"]:
            stack += " · 숏스택 잼/폴드"
        y = self.section_title(c, y, W, P, "프리플랍 가이드", "preflop", right=stack,
                               right_color=WARN if st["short"] else MUTED, badge="preflop")

        situations = pf.SHORT_SITUATIONS if st["short"] else pf.SITUATIONS
        labels = ["인원", "상황", "내 포지션"] + ([pf.VILLAIN_LABEL[st["situation"]]] if st["villains"] else [])
        x0 = P + max(self.measure(t, FS_NOTE) for t in labels) + s(12)
        y = self.pills(c, y, "인원", [(n, f"{n}") for n in (6, 7, 8, 9, 10)], st["players"],
                       lambda v: self.set_preflop(players=v), x0)
        y = self.pills(c, y, "상황", situations, st["situation"], lambda v: self.set_preflop(situation=v), x0)
        heroes = pf.valid_heroes(st["situation"], st["players"])
        y = self.pills(c, y, "내 포지션", [(h, h) for h in heroes], st["hero"], lambda v: self.set_preflop(hero=v), x0)
        if st["villains"]:
            y = self.pills(c, y, pf.VILLAIN_LABEL[st["situation"]], [(v, v) for v in st["villains"]], st["villain"],
                           lambda v: self.set_preflop(villain=v), x0)
        y += s(6)

        acts = pf.ranges(st["situation"], st["hero"], st.get("villain"), st["players"],
                         st["stack_bb"] or 100.0, st["ante"], style=self.looseness())
        hand = pf.hand_code(res["hero"])
        action, color_key = pf.decide(hand, acts)
        color = ACTION_COLORS[color_key] if color_key != "fold" else "#9aa5b4"

        # 판정
        rank = pf.DATA["G"].index(hand) + 1
        bx = P + s(16)
        t1 = self.text(c, bx, y + s(10), hand, FS_HAND + 2, "bold", TEXT, F_NUM)
        t2 = self.text(c, bx, c.bbox(t1)[3] + s(2), f"강도 {rank}/169", FS_NOTE, fill=MUTED)
        left_w = max(c.bbox(t1)[2], c.bbox(t2)[2])
        mid = (y + s(10) + c.bbox(t2)[3]) / 2
        a = self.text(c, left_w + s(14), mid, "→", FS_H + 2, fill=MUTED, family=F_NUM, anchor="w")
        act = self.text(c, c.bbox(a)[2] + s(12), mid, action, FS_HAND + 4, "bold", color, anchor="w")
        bottom = max(c.bbox(t2)[3], c.bbox(act)[3]) + s(10)
        box = round_rect(c, P, y, W - P, bottom, s(10), fill=PANEL, outline="")
        c.tag_lower(box)
        y = bottom + s(12)

        # 격자 + 범례
        cell, gap = s(16), max(1, s(1))
        grid = pf.grid()
        colors = {}
        for _, hands, ck in reversed(acts):
            for h in hands:
                colors[h] = ACTION_COLORS[ck]
        mark = None
        for i, row in enumerate(grid):
            for j, h in enumerate(row):
                gx0, gy0 = P + j * (cell + gap), y + i * (cell + gap)
                c.create_rectangle(gx0, gy0, gx0 + cell, gy0 + cell, fill=colors.get(h, ACTION_COLORS["fold"]),
                                   outline="")
                if h == hand:
                    mark = (gx0 - 2, gy0 - 2, gx0 + cell + 2, gy0 + cell + 2)
        if mark:
            c.create_rectangle(*mark, outline="#ffffff", width=2)
        gx = P + 13 * (cell + gap) + s(16)
        ly = y
        total = 0.0
        lh = self.measure("가", FS_BODY)

        def legend(name, pct, swatch, fg, weight):
            nonlocal ly
            c.create_rectangle(gx, ly + lh / 2 - s(5), gx + s(11), ly + lh / 2 + s(6), fill=swatch, outline="")
            p = self.text(c, W - P, ly, f"{pct:.1f}%", FS_BODY, weight, fg, F_NUM, anchor="ne")
            t = self.text(c, gx + s(17), ly, name, FS_BODY, fill=fg, width=c.bbox(p)[0] - gx - s(22))
            ly = max(c.bbox(t)[3], c.bbox(p)[3]) + s(6)
        for name, hands, ck in acts:
            pct = pf.range_pct(hands)
            total += pct
            legend(name, pct, ACTION_COLORS[ck], TEXT, "bold")
        legend("폴드", max(0.0, 100 - total), "#3a434f", MUTED, "normal")
        t = self.text(c, gx, ly + s(8), self.preflop_note(st), FS_NOTE, fill=MUTED, width=W - P - gx)
        return max(y + 13 * (cell + gap), c.bbox(t)[3]) + s(16)

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

    def make_inputs(self):
        """입력값(StringVar)은 한 번만 만들고, 입력칸 화면은 배율이 바뀔 때마다 다시 만든다."""
        saved = self.cfg.get("inputs", {})
        self.inputs = {}
        for row in self.FIELDS:
            for key, _ in row:
                var = tk.StringVar(value=saved.get(key, ""))
                var.trace_add("write", lambda *_: self.on_input())
                self.inputs[key] = var
        # 상대 벳을 바꾸면 팟(= 화면 POT - 벳)도 다시 계산
        self.inputs["bet"].trace_add("write", lambda *_: self.fill_pot())
        self.build_inputs_frame()

    def build_inputs_frame(self):
        if self.inputs_frame is not None and self.inputs_k == self.k:
            return self.inputs_frame
        if self.inputs_frame is not None:
            self.inputs_frame.destroy()
        s = self.s
        self.inputs_k = self.k
        self.entries = {}
        box = self.inputs_frame = tk.Frame(self.canvas, bg=BG)
        head = tk.Frame(box, bg=BG)
        head.grid(row=0, column=0, columnspan=3, sticky="we", pady=(0, s(10)))
        tk.Label(head, text="상황 입력", bg=BG, fg=TEXT, font=self.font(FS_H, "bold")).pack(side="left")
        self.auto_btn = tk.Label(head, font=self.font(FS_SMALL, "bold"), padx=s(8), pady=s(2), cursor="hand2")
        self.auto_btn.pack(side="right", pady=(s(3), 0))
        self.auto_btn.bind("<Button-1>", lambda e: self.toggle_auto())
        tk.Label(box, text="단위 자유 (칩·BB) · 13만, 84만 7500, 21BB 형식 가능", bg=BG, fg=MUTED, justify="left",
                 font=self.font(FS_NOTE), wraplength=s(self.col_w - 2 * PAD)).grid(row=90, column=0, columnspan=3,
                                                                         sticky="w")
        for r, row in enumerate(self.FIELDS):
            for col, (key, label) in enumerate(row):
                f = tk.Frame(box, bg=BG)
                f.grid(row=1 + r * 2, column=col, sticky="w", padx=(0, s(10) if col < 2 else 0))
                tk.Label(f, text=label, bg=BG, fg=MUTED, font=self.font(FS_NOTE)).pack(anchor="w")
                entry = tk.Entry(f, textvariable=self.inputs[key], width=9, font=self.font(FS_BODY, "normal", F_NUM),
                                 bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", highlightthickness=1,
                                 highlightbackground=LINE, highlightcolor=ACCENT)
                entry.pack(anchor="w", ipady=s(3), pady=(s(2), 0))
                self.entries[key] = entry
                extra = tk.Frame(box, bg=BG)
                extra.grid(row=2 + r * 2, column=col, sticky="w", pady=(s(3), s(8)))
                if key == "pot":
                    self.pot_hint = tk.Label(extra, text="", bg=BG, fg=MUTED, font=self.font(FS_SMALL))
                    self.pot_hint.pack(side="left")
                if key in ("bet", "my_bet"):
                    for name, frac in (("⅓", 1 / 3), ("½", 0.5), ("팟", 1.0)):
                        b = tk.Label(extra, text=name, bg=CHIP, fg=TEXT, font=self.font(FS_SMALL),
                                     padx=s(7), pady=0, cursor="hand2")
                        b.pack(side="left", padx=(0, s(3)))
                        b.bind("<Button-1>", lambda e, k=key, fr=frac: self.apply_preset(k, fr))
                        b.bind("<Enter>", lambda e, w=b: w.config(bg=CHIP_HOVER))
                        b.bind("<Leave>", lambda e, w=b: w.config(bg=CHIP))
        self.villain_box = tk.Frame(box, bg=BG)
        self.villain_box.grid(row=95, column=0, columnspan=3, sticky="we", pady=(s(10), 0))
        if self.inputs_item is not None:
            self.canvas.delete(self.inputs_item)
        self.inputs_item = self.canvas.create_window(0, 0, window=box, anchor="nw", state="hidden")
        self.refresh_auto_toggle()
        return box

    def amount(self, key):
        return odds.parse_amount(self.inputs[key].get())

    def apply_preset(self, key, frac):
        pot = self.amount("pot")
        if pot:
            self.inputs[key].set(odds.format_amount(round(pot * frac, 2)))

    def on_input(self):
        self.cfg["inputs"] = {k: v.get() for k, v in self.inputs.items()}
        self.save_later()
        if hasattr(self, "canvas"):
            self.schedule_advice()
            self.render()

    def analysis_info(self):
        """분석 탭에 보여줄 섹션 판단: 값이 없는 섹션은 빼고(폴드 에쿼티·M-ratio 등), 핵심만 '입력하면 계산' 한 줄로."""
        values = {k: self.amount(k) for k in self.inputs}
        res = None if self.view.get("message") else self.result
        stacks = [v for v in (values["my_stack"], values["opp_stack"]) if v]
        info = {"values": values, "res": res, "eff": min(stacks) if stacks else None,
                "eq": res["equity"]["equity"] if res and res.get("equity") else None,
                "outs": res.get("outs") if res else None}
        pot, bet = values["pot"], values["bet"]
        hero = res["hero"] if res else []
        info["preflop"] = bool(res) and not res["board"]
        info["pocket_pair"] = len(hero) == 2 and hero[0] >> 2 == hero[1] >> 2
        sb, bb, ante = values["sb"] or 0, values["bb"] or 0, values["ante"] or 0
        vis, waiting = [], []
        if pot and bet:
            vis.append("pot")
        else:
            waiting.append("팟 오즈 (팟·상대 벳)")
        if (info["preflop"] and info["pocket_pair"] and bet and info["eff"]) or (info["outs"] and pot and bet):
            vis.append("implied")
        if info["eff"] and pot:
            vis.append("spr")
        elif "pot" not in vis:
            waiting.append("SPR (스택·팟)")
        if pot and values["my_bet"]:
            vis.append("fold")
        if values["my_stack"] and sb + bb + ante:
            vis.append("m")
        info["visible"], info["waiting"] = vis, waiting
        return info

    def sec_analysis(self, c, which, info):
        """팟 오즈 · 내재 배당 · SPR · 폴드 에쿼티 · M-ratio 중 하나."""
        values, res, eff, eq, outs = info["values"], info["res"], info["eff"], info["eq"], info["outs"]
        s, W, P = self.s, self.s(self.W), self.s(self.P)
        pot, bet, my_bet = values["pot"], values["bet"], values["my_bet"]
        y = s(8)

        def title(text, key):
            nonlocal y
            y = self.section_title(c, y, W, P, text, key)

        def line(label, value="", color=TEXT, note=None):
            nonlocal y
            vw = 0
            if value:
                v = self.text(c, W - P, y - s(2), value, FS_H, "bold", color, F_NUM, anchor="ne")
                vw = c.bbox(v)[2] - c.bbox(v)[0]
            t = self.text(c, P, y, label, FS_BODY, fill=MUTED, width=W - 2 * P - vw - s(14))
            y = max(c.bbox(t)[3], c.bbox(v)[3] if value else 0) + s(6)
            if note:
                t = self.text(c, P, y, note, FS_NOTE, fill=MUTED, width=W - 2 * P)
                y = c.bbox(t)[3] + s(8)

        def big(text, label, sub=None):
            nonlocal y
            t = self.text(c, P - s(1), y - s(6), text, FS_BIG, "bold", ACCENT, F_NUM)
            b = c.bbox(t)
            rw = W - P - b[2] - s(16)
            ry = y
            if sub:
                st = self.text(c, W - P, ry, sub, FS_BODY, "bold", TEXT, anchor="ne", width=rw, justify="right")
                ry = c.bbox(st)[3] + s(2)
            lt = self.text(c, W - P, ry, label, FS_NOTE, fill=MUTED, anchor="ne", width=rw, justify="right")
            y = max(b[3], c.bbox(lt)[3]) + s(10)

        def verdict(ok):
            return ("+EV", WIN_C) if ok else ("−EV", LOSE_C)

        if which == "pot":
            title("팟 오즈", "pot_odds")
            need = odds.pot_odds(pot, bet)
            big(f"{need * 100:.1f}%", f"콜 {odds.format_amount(bet)} → 최종 팟 {odds.format_amount(pot + 2 * bet)}",
                f"필요 승률  ·  팟의 {bet / pot * 100:.0f}% 벳")
            if eq is not None:
                tag, color = verdict(eq >= need)
                line(f"내 에쿼티 ({'범위' if res.get('equity_basis') == 'range' else '랜덤'} {res['opponents']}명)"
                     + ("  · 이전 계산" if self.result_stale() else ""),
                     f"{eq * 100:.1f}%  {tag}", color)
            if outs:
                prob = outs["by_river"] if outs["left"] == 2 else outs["next"]
                tag, color = verdict(prob >= need)
                line("드로우 완성 (리버까지)" if outs["left"] == 2 else "드로우 완성 (리버 1장)",
                     f"{prob * 100:.1f}%  {tag}", color,
                     note=f"턴 한 장 기준 {outs['next'] * 100:.1f}% — 턴에 또 벳이 나오면 이 값으로 판단"
                     if outs["left"] == 2 else None)
            if eq is None:
                line(("[지금 계산]을 누르면 승률과 비교합니다" if self.click_mode() else "내 차례가 되면 승률과 비교합니다") if res else "카드가 인식되면 승률과 비교합니다")
        elif which == "implied":
            title("내재 배당", "implied")
            if info["preflop"] and info["pocket_pair"] and bet and eff:
                ratio = odds.set_mining_ratio(eff, bet)
                ok = ratio >= 15
                line("셋마이닝  ·  유효 스택 ÷ 콜 금액", f"{ratio:.1f}배", WIN_C if ok else LOSE_C,
                     note="15~20배 이상이면 들어갈 가치가 있음" + ("" if ok else " — 지금은 부족"))
            else:
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
        elif which == "spr":
            title("SPR", "spr")
            value = odds.spr(eff, pot)
            zone, tip = odds.spr_zone(value)
            big(f"{value:.1f}", f"유효 스택 {odds.format_amount(eff)} ÷ 팟 {odds.format_amount(pot)}", zone)
            line(tip, note="SPR은 플랍이 열린 직후 팟 기준 수치입니다" if res and len(res["board"]) > 3 else None)
        elif which == "fold":
            title("폴드 에쿼티", "fold_eq")
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
        elif which == "m":
            title("M-ratio", "m")
            sb, bb, ante = values["sb"] or 0, values["bb"] or 0, values["ante"] or 0
            m = odds.m_ratio(values["my_stack"], sb, bb, ante)
            zone, tip = odds.m_zone(m)
            big(f"{m:.1f}", f"내 스택 ÷ (SB + BB + 앤티) = {odds.format_amount(sb + bb + ante)}", zone)
            line(tip)
        return y

    def close(self):
        self.closed = True
        self.rec_on = False
        try:
            if self.tracker is not None:
                self.tracker.flush()
            if self.recorder is not None and self.recorder.enabled:
                self.recorder.stop()
            if self.store is not None:
                self.store.close()
        except Exception:
            self.log_once("close")
        self.tip.hide()
        if self.window_ok():
            self.cfg["window"] = [self.root.winfo_x(), self.root.winfo_y(), self.win_w, self.win_h]
            save_config(self.cfg)
        self.sct.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
