"""게임 자체의 '유저 정보' 팝업 수동 읽기 (사용자가 직접 연 팝업만 읽는다. 클릭/자동 조작 없음).

    userinfo.detect(img)          -> rect (x, y, w, h) | None    게임 창 전체 스크린샷 안에서 팝업 찾기 (없으면 ~3-6ms)
    userinfo.read(img, rect)      -> dict | None                 OCR: name, style, vpip, pfr, ats, threebet, tab, prize_text, ...
    userinfo.PopupWatcher().update(img) -> dict | None           연속 2프레임 같은 결과일 때 한 번만 내보냄

찾는 방법: 팝업 맨 아래 '색 고르기' 동그라미 줄(11색, 고정)을 1/8 크기 회색 영상에서 여러 배율로 템플릿 매칭
(후보 생성), 후보 둘레에서 제목/탭/X/VPIP.. 라벨/연필 등 고정 요소 12개를 절반 크기로 다시 매칭해 위치들이
한 장의 (배율, 이동) 으로 설명될 때만 팝업으로 인정. 세로 간격은 팝업마다 몇 px 다르므로(제목/탭은 위쪽,
라벨/색줄은 아래쪽 그룹) 좌표는 그룹별로 따로 맞춘다. rect 는 패널 사각형이며 화면 밖으로 조금 나갈 수 있다
(배율 = rect.w / PANEL_W).
"""
import base64
import re
import threading
import time
from collections import Counter

import cv2
import numpy as np

import ocr

PANEL_W = 880.0
PANEL_H = 706.0
PANEL_L, PANEL_T = 4, 5            # 기준 샘플(타이트-패시브)에서 패널 왼쪽/위 모서리
DOTS_X, DOTS_Y = 288, 628          # 기준 샘플에서 색 줄 템플릿의 왼쪽 위
STAGE1_SCALE = 1 / 8
SCALES = [0.65 * 1.065 ** i for i in range(14)]    # 팝업 배율 0.65 .. 1.5
STAGE1_MIN = 0.55

# 이름: (기준 x, y, 그룹(t=위쪽 t, b=아래쪽), 절반 크기 회색 PNG base64)
_TPL_SRC = {
    "title": (42, 56, "t", "iVBORw0KGgoAAAANSUhEUgAAAC4AAAAOCAAAAAB1fnHdAAAB9klEQVQoFZXB3UtTcRzA4c/3bNZOWpqbFqVTEIyfQtmiF60EMzCCoruV4Y1Z05gEEkRvauCN1jYQR+LpoovAXkgoJbroRtyMsoggggoKETNSc6nDbPOc1p/g84hiNUQBmoZpApqAZaZtnfsN6QJY8Vx9XGxJUjRZAVGUnHLrsvxjYLS8QcSyhoeMYBQCeRorsxe9+xrLWi5PAr78ayAqs/fbk0VLrz7kSxRmttyZmJ0xAlEo1rzOsHOxqsTnufnABKt0jR9EFd1t+gBsMdre4Dauv7Zn9wYjgN7quuq+4pjweW69I8W14AdR2X1jg3EcFSebvpJvtFpN4m6LQL6/aDrnUfTIbp+n4+wk0LzND6IoP70JtF+Pn0Nlx71npXp9VwRbaCEcqz7cfmJv487QJwsk62cziErXbesz5M9c8u/8hu5xT/tblxGIgHu5IM+xEkubfrV5u03buLSUXHgJouqO2zERSY511R3z1xdfWNsXjEBOl31q2cwqHO0E1vUMDPGfqNys87H7HDzQGbfCfYOu7tF+IxiBXTfOfQdqztTaLZsj3P8CK5FEFIRmO6itaUhUHW1dYn/J09uBKBSEP76Pa86KmUshp5A7l8AyhhEF9YsPqSzrMUmPk5LhHfkMFO1x69b8l2hshyZCijk1hShWQxSr8Q8TBbhNjdOV4gAAAABJRU5ErkJggg=="),
    "xbtn": (834, 12, "t", "iVBORw0KGgoAAAANSUhEUgAAABUAAAAUCAAAAABHICnvAAAA80lEQVQYGV3BPUrEUBSA0e++Hyfz7AUbsb0LMJXZh9t0G6YztYiNNgYFIYkk7z6TgWEGzxHlPx/eRDmwvADBO8BFUVbldzE2LuwEnCiQp8yRrzyIgo2ZE5ccopRx4VzYiyjzVLi4ec1AvH0xpIqijDP4O9pMbPrnAiGJ2mCAr0vrmr4zwCXRPBRWvva7z85YSRLNQ2Gzf/h5nDm4FM1DYRWb78razEqSqA0GxKbvpC5tBlwSZVgg3H91hq/tqUBIosxTIV59GOCv3w2poihlXDgX9iIKNhgnLjlEgTxljnzlQZSVzbOxcTE6QJQDy7kg3js2f4AsZS3ETlk+AAAAAElFTkSuQmCC"),
    "tab_jack": (274, 100, "t", "iVBORw0KGgoAAAANSUhEUgAAAEcAAAAXCAAAAAB8VQ00AAACB0lEQVQ4EaXBAU/aQBgG4Pe7a2unuImoKG6ohXae//+vjKDRiZPIpAh0BQvYu29FpUtclkx4HlJ4wYz3I8IrUpgzGkshIfCMFABOsTyLkCEFGI1VSAGAFIzGaqQASHGKVVkEUinjPxDj38gCnaZY2P8oTIJ4zcz2BBP3i50RMseDCJZ/vl6huOPfVDtPRUImDZGziAKDBcc+aaWVyNGDjUprd/pQb8fIBOEAdtAM7hLv7vOVdzsrAAxr7xtyQpLPyK2py5HnWN1uodr4kvTOzE0ClIt2e2AH5/65OZztheVWjDm31mAskEV15JxatP1jp++aYaHcLk2HJz8jTQeFa/soCoPm16tprV8Z7FzHQHGSuLUGI2dTHbnjUW+jzAPXTEsEcLzTjiEOOyns9cegWdo2fB1cee0Y8MOhW2swcjbVkZNFCRJM4whAtffod0aYWzu+kNUWf5rMjPru3Y4APxy6XgN/2OQzctsSYGyOuwCdtSNiPNs8upwAtt80qHTL/QTww6Fcj5EjiwKDNw5019kqafnUizWAjX0n3pzejx3/gpExHwRXho/EOsGCkHSa4o2y/uWNHhJZ2J20ga39+8jIrd2btG4IAHddBxnicQ8LFpFKGX8hxnuQBVKcYlUWgRSMxmqkAEgB2mAVQgIgBcBoLE8KZEhhThssR0g8I4UXzHg/Irz6DV1638dUeTy6AAAAAElFTkSuQmCC"),
    "tab_omaha": (420, 100, "t", "iVBORw0KGgoAAAANSUhEUgAAADgAAAAXCAAAAAB7F9MhAAABoklEQVQ4EZXBa1PTQACG0efdTUKFaorIRS0fxMG48P//SlGc1hkahgo6DpcUabNZKZdEPgjlHDnuhcDjhKjJcSP4wNOMEXfkmPEV8zGWW3JA8IF5yYoZOaAMzE8RM3LgK57DWK7JEUoeZUPFA5EAOXxFTYsty2Q8YXlBIfzsxCPYnI54udanZiwgx5TGh9bvqZbSfpGdTbTxLU1GH4mCL4+7ezRiQK7y1JJPX0pgfeEgywt2BmkyjJMWl9PWZo+GNSBXeWpxtj8B3kbDLC/Y7aeLR2brgnY/7vavqFkDcpWn8T4dT/UiHvzJ8oKdQbp21C5OWE9Ot08OqFkDcpXnHwut9uRyXJHlBbv9NBl+Pjzn1cao26NhDchVngdcfg5kecHOIE2G2z/O6Kwcb/ZoRAI5ptRsBFujMVxl45I3+504X+0c0v016e7RiAE5ysC95dfIegLf27GoTleS3KymnJ4svftKTREgRyj5HyPPDWNKapEAOSgDz6GIa3JAGZifImbkgOAD85IVM3LMlIH5KOKWHDeCDzxNVtyR414IPE6i8Res/JrHBdDLmQAAAABJRU5ErkJggg=="),
    "tab_aof": (542, 100, "t", "iVBORw0KGgoAAAANSUhEUgAAADQAAAAXCAAAAABhKzOvAAABL0lEQVQ4EZXBQU8TURiG0eeduU1lAcYmYogIdiGXz///WwhtF8SFEhYwY0pQO/fOZxu6YKYJTM+RseXO64R4JmPDs/O2ohAbMtZyyzBFyZoMPDtDqRTIIDnDKYCM3LKPokTmif0EyXJLz7Hu2Jocgv/MvFSUsoaeEJk3PJs+/YHHlo6gmOn58NHvHygOx6tlnlY1OwrFTM+3ez+ecTau3rc30+USX9GjmOkaxblfzpLNmzLOzg6cpx/0KGa6Jp9/cXL7++IKvi9Oq5pdipmui2bFaLy4nGXZ/EtVs6OQNXQEu24YxevzVB0dLL7WNTuCLDkvjSZ3wKcqT979e0hHq7/0Kcg8sZ8gGcnZhwIySM5wCiADz85QKgUy1pIzjAJrMjY8O29TKTZkbLnzOomt/y1/c7C7TXeGAAAAAElFTkSuQmCC"),
    "tab_aofo": (653, 100, "t", "iVBORw0KGgoAAAANSUhEUgAAAEgAAAAXCAAAAACNXla5AAACE0lEQVQ4EaXB6VIaQRSA0e9ODwGGVQgaNzSSDGmovP+bIBplsSJYMZSIUGzTdEBhzF/kHNGsWcvWBGFNNCvWWD7EcYRXolkyCz7MUayIBmssOxAlgGgILDsRFxCNWbAjR4FoG7A1heF/riDaLAg5ueGEN5JDbPAM4sXcRTCakY2Jtb1UrAvHtkPy4JYNRyF6zrv0Rb/FG6fag2kPzrynuSQyjaE/nMqXm6R3XxKFMQ/FGqEIUja8+zrdqxsk6kyNo68CliL+9RwoJFp+94VKM+m1I5E4k1m0WCOkHCkbQkr/urgfOBfuzGuOK/WAJffH9Qw4iLf8zpBKM5nqSGlIsqFOb6dsKEfKhlDmsF5INdJHVzaXv/05E9vtw2FuNBcv0hj7nSHVZuKwG5/8oZDofe+12VCOlA2h0qQfObnci/0mfn5VubHWGCAaTc3GY4PfGVJtJL22/zAgdXx/WiOkHCkbNtzKFOJNlW6ROLus1gPWyt0B4HdfqDaSXrvU65PZ7xZrhFwRPWcjc3QJhXTnW8McjR8qVwFLyoXzxxFM/cmcwnXSu8vnf9vTp3GxRiiC6MCytr/4C9HTm719Rt3Fyf2CpWweUQZLM/FJWAyy3p3zOcvzo3dSZ0NcRNuAbThieCUqYMMVRBNYdiQuiIbAshNxAdFgjWUHogQQzVJg+TBxWRHNijWWDxElvBLNmrVsTYSNf91h18dWGVbIAAAAAElFTkSuQmCC"),
    "lbl_vpip": (78, 547, "b", "iVBORw0KGgoAAAANSUhEUgAAABYAAAAMCAAAAABDkhIaAAAA4ElEQVQYGW3BPUtCYRyH4fsnhERDJNTQ8l9CKFrD4ZjQ1BThki6JHyACGwQ/ghI0NdkiDiU0RKOLuCS0NPQKJkVOJZZTL9TpiUPruS4ZYWSEkfHPAQIHCGTkH9rjhUY66nfaheZC3PWPP5Cx/XK0nKoVa7PeXqmyeTnMVG+RkZ2o7zTf0+X59UZut1gdlg4ekeEtnqf2vcTrTOtr6XSrNzk49JExt/p9drXxc/32vKbuSuuz74OM6fyg7uc6XcjcTMVOCMiIevdPY8mLESTvbNQjICMg/UacnIs4OQfICPMH0XtNgcU5138AAAAASUVORK5CYII="),
    "lbl_pfr": (180, 547, "b", "iVBORw0KGgoAAAANSUhEUgAAABQAAAAMCAAAAABHZ8InAAAAyUlEQVQIHWXBMUsCcRzH4c83AgvEUAILod8YDrcIUgS523I01eIg3RL0KnoDQauDIoJL2GTREojSFORWU1sQJBlHEtF5/4amuOeRkSQjSQYOhAPEHxkbNb7774EIb32i4R3IKO1dVlI39Y6br5euveJphAw/19zPPntdPg9mFztbZyCjvjJb7ewW45/z2lKU7j2CbOH4YfIaBuMnF5/0y29XgCxz1JqyHPReyB82CtX2B8hy3mhOZnv4xdrmYLFyH4JMcor5R0bSL/DTPYHrmXWVAAAAAElFTkSuQmCC"),
    "lbl_ats": (78, 646, "b", "iVBORw0KGgoAAAANSUhEUgAAABYAAAAMCAAAAABDkhIaAAAA0klEQVQYGW3BvUsCARjA4d+bp+An5iBo0XuDS9CQQ0vhKLjc1JRDQ9nUHLQ3NfYHHOJWq0YRBA1NjkKTJXINES5Scp7k2WX7PY8oYUQJI8pSvvBqmAHyOS0Fby4gytJp+SKxv7r47hyMk44NiAJ6Mr3tBedP3Z3apRFxAVGg4ZjDx9SZ/b7eGD2/BIAobBw+bHnXxaOrCdnt3ZsBIAr12Dz229rcs1fK/Z/juyEgyprVnJnVZiXTzlnJxce9D4gSTY2JG5O07xHJ+l/8EyWMKGH+ACqxPYHPl79bAAAAAElFTkSuQmCC"),
    "lbl_3bet": (167, 646, "b", "iVBORw0KGgoAAAANSUhEUgAAACAAAAAMCAAAAAAmf+BlAAABB0lEQVQYGX3BwStDARzA8e/vbWyjxCKW1e8iSxyJpHZxWi5quUnj4OC0w/6A5ezgL1CGkpMdOCgXaYWT46Q17ILsaZu2Pd7z3j/wPh9R/IniTxQIJYynFp7YCK1KIAGYUQfKHUShZycy+HiMJzPZHDup5l/FrE3F35r7NqLgjNZXe4/wZG/usg9mevcX4rn8lwGigLEZPRhegu/zXI3w4eJcjcv36dSeDYgCwfF08ScmTqO+fRFaOE0aVfu+uxwr4BLFmDE/toovuCaShUjmLFUq02Xt8xqXKKzoX+XKxjU/2wmXSxttOqfW+u0zLlEIDFmm4BkIOlYj3Cc4daO/beESxZ8o/v4BQg1Xgf2pEwkAAAAASUVORK5CYII="),
    "dots": (288, 628, "b", "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"),
    "pencil": (806, 578, "b", "iVBORw0KGgoAAAANSUhEUgAAABQAAAATCAAAAAC153JpAAABAElEQVQYGU3BT0oCYRzH4c93XmeUGCdFlJ/gvrpC7jpAtG4rHaqWQV2goA7Rqk20DyRFxnQEHf9M84oLn0fGETlHSYanKAp2eV4QVAQySnHiKG1nGQqFDIJWjYPlZKcIGbRreL2Lj5TlmCCUETfxwkGyuV+QZkQydR1ev8/iYc12WDhZtYOXDELeP4HRKpDVG3jX5/w+FsB0LtlpQql3C88/lGZ/yOoNQJd9vl7wpnPJqh3g5Ob77HWGN1oFMnUdNO/WTyO87bBwMuIm2NVbyl6aEcmgXSPcFOwtx7iKDIJWjYPlZKdQMkpx4ihtZxkKhQxPUaQizwuCikDGETlH6R9nKEk6Q4oJBQAAAABJRU5ErkJggg=="),
}

_TPL = {}
_S1 = {}
_LOCK = threading.Lock()


def _templates():
    with _LOCK:
        if not _TPL:
            for k, (x, y, g, b) in _TPL_SRC.items():
                im = cv2.imdecode(np.frombuffer(base64.b64decode(b), np.uint8), cv2.IMREAD_GRAYSCALE)
                _TPL[k] = (x, y, g, im)
            dots = _TPL["dots"][3]
            for s in SCALES:
                f = STAGE1_SCALE * s / 0.5
                t = cv2.resize(dots, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
                if t.shape[0] >= 3:
                    _S1[s] = t
    return _TPL


def _gray(img):
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _stage1(gray):
    """1/8 크기 회색에서 색 줄 템플릿을 배율별로 찾아 가장 닮은 (점수, 배율, (x, y) 원본 좌표) 반환."""
    _templates()
    small = cv2.resize(gray, None, fx=STAGE1_SCALE, fy=STAGE1_SCALE, interpolation=cv2.INTER_AREA)
    best = (-1.0, None, None)
    for s, t in _S1.items():
        if t.shape[0] > small.shape[0] or t.shape[1] > small.shape[1]:
            continue
        _, m, _, loc = cv2.minMaxLoc(cv2.matchTemplate(small, t, cv2.TM_CCOEFF_NORMED))
        if m > best[0]:
            best = (m, s, (loc[0] / STAGE1_SCALE, loc[1] / STAGE1_SCALE))
    return best


XREL = (-330, 620)       # 색 줄 템플릿 왼쪽 위 기준 정규화 영역 (기준 크기 px)
YREL = (-620, 90)
WIN_X, WIN_Y = 14, {"t": 22, "b": 10}   # 절반 크기 영상에서의 탐색 반경
MIN_SCORE = 0.5
MIN_INLIERS = 6


class _Fit:
    """기준 좌표(샘플 X, Y) → 화면 좌표: x = s*X + offx, y = s*Y + offy[그룹]."""
    __slots__ = ("s", "offx", "offy", "score", "n", "names")

    def x(self, X):
        return self.s * X + self.offx

    def y(self, Y, grp="t"):
        return self.s * Y + self.offy[grp]


def _fit(gray, s0, dx, dy):
    """(배율 s0, 색 줄 위치 dx, dy) 추정 둘레에서 고정 요소를 다시 찾아 한 장의 (배율, 이동) 으로 맞춤. 실패 None."""
    tpl = _templates()
    H, W = gray.shape[:2]
    n = 0.5 / s0
    x0, y0 = int(round(dx + XREL[0] * s0)), int(round(dy + YREL[0] * s0))
    x1, y1 = int(round(dx + XREL[1] * s0)), int(round(dy + YREL[1] * s0))
    crop = gray[max(0, y0):min(H, y1), max(0, x0):min(W, x1)]
    if crop.size == 0:
        return None
    pl, pt, pr, pb = max(0, -x0), max(0, -y0), max(0, x1 - W), max(0, y1 - H)
    if pl or pt or pr or pb:
        crop = cv2.copyMakeBorder(crop, pt, pb, pl, pr, cv2.BORDER_CONSTANT, value=0)
    N = cv2.resize(crop, None, fx=n, fy=n, interpolation=cv2.INTER_AREA)
    NH, NW = N.shape[:2]
    pts = []
    for name, (X, Y, grp, t) in tpl.items():
        th, tw = t.shape
        ex = ((dx + s0 * (X - DOTS_X)) - x0) * n
        ey = ((dy + s0 * (Y - DOTS_Y)) - y0) * n
        wy = WIN_Y[grp]
        xa, ya = int(max(0, ex - WIN_X)), int(max(0, ey - wy))
        xb, yb = int(min(NW, ex + tw + WIN_X)), int(min(NH, ey + th + wy))
        if xb - xa < tw or yb - ya < th:
            continue
        r = cv2.matchTemplate(N[ya:yb, xa:xb], t, cv2.TM_CCOEFF_NORMED)
        _, m, _, loc = cv2.minMaxLoc(r)
        if m >= MIN_SCORE:
            pts.append((name, 0.5 * X, 0.5 * Y, xa + loc[0], ya + loc[1], float(m), grp))
    if len(pts) < MIN_INLIERS:
        return None
    # x: 한 직선 (배율, 이동) 으로 설명되지 않는 것은 버림
    xh = np.array([p[1] for p in pts])
    fx = np.array([p[3] for p in pts], float)
    keep = np.abs(fx - xh - np.median(fx - xh)) <= 5
    slope, b = 1.0, float(np.median(fx - xh))
    for _ in range(6):
        if keep.sum() < MIN_INLIERS:
            return None
        slope, b = np.polyfit(xh[keep], fx[keep], 1)
        res = np.abs(fx - (slope * xh + b))
        new = res <= 2.5
        worst = np.where(keep, res, -1).argmax()
        if (keep & ~new).any():
            keep = keep & new if (keep & new).sum() >= MIN_INLIERS else keep.copy()
            keep[worst] = False if res[worst] > 2.5 else keep[worst]
        else:
            break
    if not 0.88 <= slope <= 1.12 or keep.sum() < MIN_INLIERS:
        return None
    # y: 그룹별 같은 이동이어야 함
    offs = {}
    for grp in ("t", "b"):
        idx = [i for i in range(len(pts)) if keep[i] and pts[i][6] == grp]
        if not idx:
            return None
        d = np.array([pts[i][4] - slope * pts[i][2] for i in idx])
        med = float(np.median(d))
        for i, v in zip(idx, d):
            if abs(v - med) > 3.0:
                keep[i] = False
        good = [v for i, v in zip(idx, d) if keep[i]]
        if not good:
            return None
        offs[grp] = float(np.median(good))
    if keep.sum() < MIN_INLIERS:
        return None
    score = float(np.mean([pts[i][5] for i in range(len(pts)) if keep[i]]))
    f = _Fit()
    f.s = s0 * float(slope)
    f.offx = x0 + b / n
    f.offy = {g: y0 + o / n for g, o in offs.items()}
    f.score, f.n = score, int(keep.sum())
    f.names = [pts[i][0] for i in range(len(pts)) if keep[i]]
    return f


def _edges(gray, f):
    """패널 위/아래 가장자리를 회색(41) 영역 경계로 찾음. 못 찾으면 기준 크기로 추정."""
    H, W = gray.shape[:2]
    xa, xb = int(max(0, f.x(150))), int(min(W, f.x(250)))
    top, bottom = f.y(PANEL_T, "t"), f.y(PANEL_T + PANEL_H, "b")
    if xb - xa >= 4:
        med = np.median(gray[:, xa:xb], axis=1)
        ok = lambda v: abs(v - 41) <= 5
        y = int(f.y(40, "t"))
        if 0 <= y < H and ok(med[y]):
            while y > 0 and ok(med[y - 1]):
                y -= 1
            if y > 0 and 44 <= med[y - 1] <= 58:
                y -= 1
            t2 = y
        else:
            t2 = None
        y = int(f.y(PANEL_T + PANEL_H - 22, "b"))
        if 0 <= y < H and ok(med[y]):
            while y < H - 1 and ok(med[y + 1]):
                y += 1
            if y < H - 1 and 44 <= med[y + 1] <= 58:
                y += 1
            b2 = y + 1
        else:
            b2 = None
        if t2 is not None and b2 is not None and 0.9 * PANEL_H * f.s <= b2 - t2 <= 1.1 * PANEL_H * f.s:
            top, bottom = t2, b2
        elif t2 is not None and abs(t2 - top) < 20 * f.s:
            top = t2
        elif b2 is not None and abs(b2 - bottom) < 20 * f.s:
            bottom = b2
    return top, bottom


def _detect(img):
    """(rect, fit) 또는 (None, None)."""
    if img is None or img.ndim < 2 or img.shape[0] < 120 or img.shape[1] < 160:
        return None, None
    gray = _gray(img)
    score, s, pos = _stage1(gray)
    if pos is None or score < STAGE1_MIN:
        return None, None
    f = _fit(gray, s, pos[0], pos[1])
    if f is None:
        return None, None
    if abs(f.s / s - 1) > 0.02:   # 배율 추정이 어긋났으면 한 번 더
        dx, dy = f.x(DOTS_X), f.y(DOTS_Y, "b")
        f2 = _fit(gray, f.s, dx, dy)
        if f2 is not None and f2.score >= f.score - 0.02:
            f = f2
    if f.score < 0.6:
        return None, None
    top, bottom = _edges(gray, f)
    left, right = f.x(PANEL_L), f.x(PANEL_L + PANEL_W)
    rect = (int(round(left)), int(round(top)), int(round(right - left)), int(round(bottom - top)))
    return rect, f


def detect(img):
    """게임 창 전체 이미지(BGR) 안에서 '유저 정보' 팝업 패널 사각형 (x, y, w, h) 또는 None.
    패널 일부가 화면 밖이면 x/y 가 음수이거나 x+w 가 이미지보다 클 수 있다. 배율 = w / PANEL_W."""
    try:
        return _detect(img)[0]
    except Exception:
        return None


# --- 필드 위치 (기준 샘플 좌표 X0, Y0, X1, Y1, 그룹) -------------------------------------------------
BOX_NAME = (282, 188, 516, 222, "t")
BOX_STYLE = (56, 198, 240, 230, "t")
BOX_PRIZE = (690, 236, 842, 272, "t")
BOX_RING = (110, 236, 190, 252, "t")        # 위쪽 강조 구간 (색으로 스타일 구분)
STAT_CENTERS = {"vpip": (99, 516), "pfr": (199, 516), "ats": (99, 614), "threebet": (199, 614)}
STAT_HALF = (20, 9)
BOX_H2H = {"h2h_hands": (288, 297, 332, 325, "t"), "h2h_wins": (288, 361, 332, 389, "t")}
TABS = [("토너먼트", 35, 155), ("홀덤", 164, 265), ("잭팟 싯앤고", 274, 415), ("오마하", 421, 532),
        ("AoF", 543, 644), ("AoF 오마하", 654, 795)]
TAB_Y = (102, 145)


def _crop(img, f, box, margin=ocr.JITTER_MARGIN):
    """기준 크기(배율 1)로 정규화한 필드 이미지. 사방 margin px 더 넓게 (ocr.read_jittered 용).
    소수 픽셀 오프셋을 보존하려고 warpAffine 으로 뽑고, 축소(배율>1)일 때는 먼저 가볍게 흐려 계단 현상을 줄임."""
    X0, Y0, X1, Y1, grp = box
    X0, Y0, X1, Y1 = X0 - margin, Y0 - margin, X1 + margin, Y1 + margin
    w, h = int(round(X1 - X0)), int(round(Y1 - Y0))
    s = f.s
    sx, sy = s * X0 + f.offx, s * Y0 + f.offy[grp]
    H, W = img.shape[:2]
    pad = 6
    xa, ya = int(np.floor(sx)) - pad, int(np.floor(sy)) - pad
    xb, yb = int(np.ceil(sx + w * s)) + pad, int(np.ceil(sy + h * s)) + pad
    part = img[max(0, ya):min(H, yb), max(0, xa):min(W, xb)]
    if part.size == 0:
        return None
    part = cv2.copyMakeBorder(part, max(0, -ya), max(0, yb - H), max(0, -xa), max(0, xb - W), cv2.BORDER_REPLICATE)
    if s > 1.03:
        part = cv2.GaussianBlur(part, (0, 0), 0.45 * s)
    m = np.array([[s, 0, sx - xa], [0, s, sy - ya]], np.float64)
    interp = cv2.INTER_LANCZOS4 if s < 0.97 else cv2.INTER_CUBIC
    return cv2.warpAffine(part, m, (w, h), flags=cv2.WARP_INVERSE_MAP | interp, borderMode=cv2.BORDER_REPLICATE)


# --- 문자열 해석 -------------------------------------------------------------------------------
STYLES = {"LAG": "루즈-어그레시브", "LP": "루즈-패시브", "TAG": "타이트-어그레시브", "TP": "타이트-패시브"}
NAME_OK = re.compile(r"^[0-9A-Za-z_가-힣ㄱ-ㅣ]+$")
PCT_SUFFIX = ("%", "0%", "96", "9%", "0/0", "%0")


def _jamo(ch):
    o = ord(ch) - 0xAC00
    return ("H", o // 588, (o % 588) // 28, o % 28) if 0 <= o < 11172 else ("C", ch)


def _unjamo(p):
    return chr(0xAC00 + p[1] * 588 + p[2] * 28 + p[3]) if p[0] == "H" else p[1]


def vote_name(texts):
    """OCR 문자열들 → (이름, 일치 비율). 글자 수가 가장 많이 나온 후보끼리 자리마다 다수결 (한글은 초/중/종성 단위)."""
    cands = [t.replace(" ", "") for t in texts]
    cands = [t for t in cands if t and NAME_OK.match(t)]
    if not cands:
        return None, 0.0
    length = Counter(len(t) for t in cands).most_common(1)[0][0]
    same = [t for t in cands if len(t) == length]
    whole, cnt = Counter(same).most_common(1)[0]
    if cnt * 5 >= len(same) * 2:   # 가장 많이 나온 통문자열이 40% 이상이면 그대로
        name = whole
    else:
        out = []
        for i in range(length):
            parts = [_jamo(t[i]) for t in same]
            kind = Counter(p[0] for p in parts).most_common(1)[0][0]
            parts = [p for p in parts if p[0] == kind]
            if kind == "C":
                out.append(Counter(p[1] for p in parts).most_common(1)[0][0])
            else:
                out.append(_unjamo(("H",) + tuple(Counter(p[j] for p in parts).most_common(1)[0][0] for j in (1, 2, 3))))
        name = "".join(out)
    # 신뢰도 = (유효 후보 비율) x (자리별 평균 일치율). 이름이 틀리게 읽힌 자리는 후보들이 갈라져 낮게 나옴
    agree = np.mean([sum(1 for t in same if t[i] == name[i]) / len(same) for i in range(len(name))])
    return name, round(float(agree) * len(cands) / max(1, len(texts)), 3)


def parse_percent(text):
    """'54%', '540%', '5396' (% 를 0%/96 으로 읽은 경우) → (정수 0..100, 가중치) 또는 None. 정확히 '%' 로 끝나면 가중치 2, 그 외 1."""
    t = re.sub(r"^\D+", "", text.replace(" ", ""))
    for n in (3, 2, 1):
        d, suffix = t[:n], t[n:]
        if d.isdigit() and len(d) == n and suffix in PCT_SUFFIX and int(d) <= 100:
            return int(d), 2 if suffix == "%" else 1
    return None


def arc_percent(crop):
    """동그라미 통계 둘레의 파란 호 길이 → 퍼센트 추정 (호 길이가 값에 비례: 120개 샘플 중 약 1.2 x 퍼센트).
    crop = 동그라미 중심 둘레 80x80 (기준 크기). 숫자 오독(1% 를 10% 로 등)을 걸러내는 교차 검증용."""
    b, r = crop[:, :, 0].astype(np.int16), crop[:, :, 2].astype(np.int16)
    blue = (b - r > 50) & (b > 120)
    c = crop.shape[0] // 2
    best = 0
    for rad in (27, 28, 29, 30):
        n = 0
        for a in range(0, 360, 3):
            t = np.deg2rad(a)
            n += bool(blue[int(round(c - rad * np.cos(t))), int(round(c + rad * np.sin(t)))])
        best = max(best, n)
    return best / 1.2


def vote_percent(texts, arc=None):
    """OCR 문자열들 → (값, 득표 비율) 또는 (None, 0). 유효 득표 3 이상 + 1등이 유효 득표의 60% 이상이어야 함.
    arc (호 길이로 잰 퍼센트) 가 있으면 그와 4+6% 넘게 다른 후보는 버리고, 여분의 0 (6% -> 60%, 1% -> 10%) 오독은
    호와 맞는 쪽으로 되돌린다. 없으면 '6%' 가 '60%' 와 같거나 더 많이 읽힐 때 6 으로 합친다."""
    votes = Counter()
    for t in texts:
        p = parse_percent(t)
        if p:
            votes[p[0]] += p[1]
    if arc is not None:
        tol = 4 + 0.06 * arc
        keep = Counter()
        for v, w in votes.items():
            for u, k in ((v, 1.0), (v // 10, 0.9)):
                if (u == v or (v >= 10 and v % 10 == 0)) and abs(u - arc) <= tol:
                    keep[u] += w * k
        votes = keep
        need = 2
    else:
        for v in range(10, 101, 10):
            if votes.get(v) and votes.get(v // 10, 0) >= votes[v]:
                votes[v // 10] += votes.pop(v)
        need = 3
    n = sum(votes.values())
    if not votes:
        return None, 0.0
    val, w = votes.most_common(1)[0]
    return (val, w / n) if w >= need and w >= 0.6 * n else (None, 0.0)


def parse_prize(text):
    """'634만 5080' → ('634만 5080', 6345080). 억/만/나머지 숫자. 해석 불가면 (None, None)."""
    t = text.replace(" ", "").replace(",", "")
    m = re.fullmatch(r"(?:(\d+)억)?(?:(\d+)만)?(\d+)?", t)
    if not m or not any(m.groups()):
        return None, None
    eok, man, rest = (int(g) if g else 0 for g in m.groups())
    if rest > 9999 or (rest and not man and not eok and len(t) > 4):
        return None, None
    val = eok * 10 ** 8 + man * 10 ** 4 + rest
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man}만")
    if rest or not parts:
        parts.append(str(rest))
    return " ".join(parts), val


def _hue_style(crop):
    """강조된 라벨 글자 색 → 스타일 코드 (금=LAG, 초록=TP, 분홍=TAG, 파랑=LP). 색이 뚜렷하지 않으면 None."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    m = (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 110)
    if m.sum() < 25:
        return None
    h = int(np.median(hsv[:, :, 0][m]))
    if 8 <= h < 40:
        return "LAG"
    if 40 <= h < 95:
        return "TP"
    if 95 <= h < 140:
        return "LP"
    return "TAG"


def _text_style(texts):
    """OCR 라벨 → 스타일 코드 (다수결). 어그레시브/패시브는 '매시보' 처럼 틀리게 읽혀도 어느 쪽인지 판별."""
    votes = Counter()
    for t in texts:
        t = t.replace(" ", "")
        loose = bool(re.search(r"[루즈]", t))
        tight = bool(re.search(r"[타이트]", t)) and not loose
        aggr = bool(re.search(r"[어그레]", t))
        passive = bool(re.search(r"[패매]|시[보브]", t)) and not aggr
        if (loose or tight) and (aggr or passive):
            votes[("L" if loose else "T") + ("AG" if aggr else "P")] += 1
    if not votes:
        return None
    return {"LAG": "LAG", "LP": "LP", "TAG": "TAG", "TP": "TP"}[votes.most_common(1)[0][0]]


def _gray3(img):
    """따뜻한 색(금/분홍 글자)을 ocr 의 warm 억제가 지우지 못하게 밝기만 남김."""
    return cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)


def _prize_crop(img, f):
    """금색 동전 아이콘 오른쪽의 숫자 영역만 (동전의 'G' 가 숫자로 읽히지 않게)."""
    crop = _crop(img, f, BOX_PRIZE)
    if crop is None:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gold = (hsv[:, :, 0] >= 8) & (hsv[:, :, 0] < 40) & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 120)
    cols = np.where(gold.any(0))[0]
    if len(cols) == 0:
        return None
    runs, start = [], cols[0]
    for a, b in zip(cols[:-1], cols[1:]):
        if b - a > 4:
            runs.append((start, a))
            start = b
    runs.append((start, cols[-1]))
    if len(runs) < 2 or runs[0][1] - runs[0][0] < 14:
        return None
    x0 = max(0, runs[1][0] - 2 - ocr.JITTER_MARGIN)
    return _gray3(crop[:, x0:])


def _tab(img, f):
    """흰 테두리가 있는 탭 알약 → (탭 이름, 신뢰도) 또는 (None, 0)."""
    X0, Y0, X1, Y1 = 28, 92, 802, 156
    crop = _crop(img, f, (X0, Y0, X1, Y1, "t"), margin=0)
    if crop is None:
        return None, 0.0
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    scores = []
    for name, a, b in TABS:
        ring = np.zeros_like(g, bool)
        ya, yb, xa, xb = TAB_Y[0] - Y0, TAB_Y[1] - Y0, a - X0, b - X0
        ring[max(0, ya - 3):yb + 3, max(0, xa - 3):xb + 3] = True
        ring[ya + 3:yb - 3, xa + 3:xb - 3] = False
        ring[:, :xa + 14] &= False
        ring[:, xb - 14:] &= False   # 둥근 양끝은 제외
        ring[:, :] &= True
        scores.append(float((g[ring] > 170).mean()) if ring.any() else 0.0)
    order = np.argsort(scores)[::-1]
    best, second = scores[order[0]], scores[order[1]]
    if best >= 0.12 and best >= 3 * second:
        return TABS[order[0]][0], best
    return None, best


MIN_NAME_CONF = 0.05          # 이 아래는 읽힌 게 아님. 이름을 DB 키로 쓰려면 NAME_RELIABLE 이상만
NAME_RELIABLE = 0.5


def validate(vpip, pfr, ats, threebet):
    """퍼센트 범위/대략적 일관성: 0..100, pfr <= vpip (+2 오차), 3벳이 pfr 보다 15 넘게 크면 오독으로 봄."""
    vals = (vpip, pfr, ats, threebet)
    if any(v is None or not 0 <= v <= 100 for v in vals):
        return False
    return pfr <= vpip + 2 and threebet <= pfr + 15


def read(img, rect):
    """detect() 가 준 rect 의 팝업을 OCR 로 읽음. 확신이 없으면 None.
    -> {name, name_conf, name_reliable, style('LAG'|'LP'|'TAG'|'TP'), style_ko, style_text, vpip, pfr, ats, threebet,
        tab, prize_text, prize, h2h_hands, h2h_wins, scale, rect}"""
    try:
        return _read(img, rect)
    except Exception:
        return None


def _read(img, rect):
    if rect is None or img is None or not ocr.available():
        return None
    x, y, w, h = rect
    s0 = w / PANEL_W
    gray = _gray(img)
    f = _fit(gray, s0, x + s0 * (DOTS_X - PANEL_L), y + h - s0 * (PANEL_T + PANEL_H - DOTS_Y))
    if f is None:
        return None
    crops = {"name": _crop(img, f, BOX_NAME), "style": _gray3(_crop(img, f, BOX_STYLE)),
             "ring": _crop(img, f, BOX_RING)}
    arcs = {}
    for k, (cx, cy) in STAT_CENTERS.items():
        crops[k] = _crop(img, f, (cx - STAT_HALF[0], cy - STAT_HALF[1], cx + STAT_HALF[0], cy + STAT_HALF[1], "b"))
        ring = _crop(img, f, (cx - 40, cy - 40, cx + 40, cy + 40, "b"), margin=0)
        arcs[k] = arc_percent(ring) if ring is not None else None
    tab, tab_conf = _tab(img, f)
    prize_img = _prize_crop(img, f) if tab in (None, "토너먼트") else None
    if prize_img is not None:
        crops["prize"] = prize_img
    if tab not in (None, "토너먼트"):
        for k, box in BOX_H2H.items():
            crops[k] = _gray3(_crop(img, f, box))
    if any(c is None or c.size == 0 for c in crops.values()):
        return None
    texts = ocr.read_jittered({k: c for k, c in crops.items() if k != "ring"})
    name, name_conf = vote_name(texts["name"])
    if not name or name_conf < MIN_NAME_CONF:
        return None
    stats, confs = {}, {}
    for k in STAT_CENTERS:
        stats[k], confs[k] = vote_percent(texts[k], arcs[k])
    if not validate(stats["vpip"], stats["pfr"], stats["ats"], stats["threebet"]):
        return None
    hue = _hue_style(_crop(img, f, BOX_STYLE))
    by_text = _text_style(texts["style"])
    style = hue or by_text
    out = {"name": name, "name_conf": round(name_conf, 2), "name_reliable": name_conf >= NAME_RELIABLE, "style": style, "style_ko": STYLES.get(style),
           "style_text": Counter(t for t in texts["style"] if t).most_common(1)[0][0] if any(texts["style"]) else None,
           **stats, "tab": tab, "tab_conf": round(tab_conf, 2), "prize_text": None, "prize": None,
           "h2h_hands": None, "h2h_wins": None, "scale": round(f.s, 3), "rect": tuple(rect)}
    if "prize" in texts:
        votes = Counter(p for p in (parse_prize(t) for t in texts["prize"]) if p[0])
        if votes:
            (out["prize_text"], out["prize"]), _ = votes.most_common(1)[0]
    for k in BOX_H2H:
        if k in texts:
            vals = Counter(int(m.group(0)) for m in (re.search(r"\d{1,4}", t.replace("O", "0").replace(" ", ""))
                                                    for t in texts[k]) if m)
            if vals:
                out[k] = vals.most_common(1)[0][0]
    return out


# --- 안정화 ----------------------------------------------------------------------------------
def result_key(res):
    """같은 팝업인지 비교하는 키: (이름, 탭, 통계 4개)."""
    if not res:
        return None
    return (res["name"], res["tab"], res["vpip"], res["pfr"], res["ats"], res["threebet"])


class PopupWatcher:
    """프레임마다 update(img). 같은 해석이 연속 confirm 프레임 나오면 한 번 내보냄 (이미 내보낸 (이름, 탭, 통계)는 다시 안 냄).
    화면이 그대로면(팝업 픽셀 서명이 같으면) OCR 을 다시 하지 않고 지난 결과를 재사용한다 (프레임당 OCR ~0.5s 방지).
    팝업이 사라지면 연속 기록만 초기화 (내보낸 목록은 reset() 전까지 유지)."""

    SIG_SIZE = (96, 76)
    SIG_TOL = 2.0

    def __init__(self, confirm=2):
        self.confirm = confirm
        self.emitted = set()
        self.reset_streak()
        self.reads = 0          # 실제 OCR 횟수 (테스트/진단용)

    def reset_streak(self):
        self.pending, self.streak, self.sig, self.last = None, 0, None, None
        self.absent = 0

    def reset(self):
        self.emitted.clear()
        self.reset_streak()

    def _sig(self, img, rect):
        x, y, w, h = rect
        H, W = img.shape[:2]
        part = _gray(img)[max(0, y):min(H, y + h), max(0, x):min(W, x + w)]
        if part.size == 0:
            return None
        return cv2.resize(part, self.SIG_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32)

    def update(self, img):
        rect = detect(img)
        if rect is None:
            self.absent += 1
            if self.absent >= 2:
                self.reset_streak()
                self.absent = 2
            return None
        self.absent = 0
        sig = self._sig(img, rect)
        if sig is not None and self.sig is not None and float(np.abs(sig - self.sig).mean()) < self.SIG_TOL:
            res = self.last
        else:
            res = read(img, rect)
            self.reads += 1
            self.sig, self.last = sig, res
        key = result_key(res)
        if key is None:
            self.pending, self.streak = None, 0
            return None
        if key == self.pending:
            self.streak += 1
        else:
            self.pending, self.streak = key, 1
        if self.streak >= self.confirm and key not in self.emitted:
            self.emitted.add(key)
            return res
        return None


if __name__ == "__main__":
    import sys
    import json
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for p in sys.argv[1:]:
        im = cv2.imread(p)
        r = detect(im)
        print(p, r)
        if r:
            print(json.dumps(read(im, r), ensure_ascii=False, indent=1))
