"""상대 핸드 리딩 채점 — 쇼다운에서 공개된 상대 실제 카드로 '우리 예상이 맞았나'를 잰다.

    python score_reads.py [핸드기록 폴더] [--ok-only] [--json 경로] [--quiet]

  폴더: 인자 > 환경변수 CARD_READER_DATA > <이 파일 폴더>/dist/hands
        (YYYY-MM-DD.jsonl 핸드 기록 + decisions_YYYY-MM-DD.jsonl 결정 기록. 저장소에는 데이터를 두지 않는다)
  출력: 한국어 요약(표준출력) + JSON 요약(기본: 데이터 폴더/read_scores.json — 저장소 밖)

무엇을 재나 (공개된 상대 좌석 x 그 상대가 실제로 액션한 스트리트마다)
  범위를 다시 만든다: 기록된 액션을 handlog.to_handread → handread.estimate 로 재생 (지금 모델 기준).
    live    앱이 하는 그대로 (handlog.to_handread: 프리플랍 정리(같은 레이즈 두 번 읽음·최소 레이즈 미만 올인·올인에 콜 =
            올인콜·말이 안 되는 금액) + 올인/올인콜 상대는 실제 올인 금액을 스택으로 + 기록 금액으로 계산한 벳 크기(팟 대비))
    raw     예전 입력 (정리·크기·올인 스택 없음, 모든 벳/콜이 '보통 크기') — 지금 모델에서 입력 정리의 몫을 본다
    pre     프리플랍 액션만 반영 (포스트플랍 배율표가 도움이 되는지 보는 기준선)
    notype  live 에서 상대 유형(LAG/LP/TAG/TP) 보정만 뺀 것 — handread.STYLE_ADJ 가 도움이 되는지 본다
            (유형: 핸드 기록 seats[k].type > 결정 기록 프로필의 style. 둘 다 없으면 live 와 같다)
    uniform 남은 카드에서 무작위 (완전 무지 기준선)
  점수
    콤보 로그손실   -ln p(실제 콤보) (p 는 1% 를 균등과 섞어 0 방지). uniform 대비 '정보 이득(bit)' = 얼마나 좁혀 맞췄나
    PIT            범위에서 실제 콤보보다 가능성이 높은 콤보들의 확률 합(+같은 것 절반). 잘 맞으면 평균 0.5 근처,
                   낮으면 실제 패가 범위의 '핵심'에 있었음, 높으면 범위의 꼬리(과소평가)에 있었음
    분류           실제 핸드 분류(셋/투페어/탑페어+/드로우/노페어 …)에 준 확률, 예측 순위(1=최다), Brier(7분류)
    top5           실제 핸드코드가 앱이 보여주는 '가능성 높은 핸드' 5개 안에 있었나
    0확률          실제 콤보가 범위에서 아예 빠졌나 (가중치 컷)
    요약 표기      재생 결과의 표기 핸드(summary_codes)에 실제 핸드가 들었나 vs 화면 확률(summary_hit_pct, 실측 보정) /
                   모델 확률(summary_pct) → handread.SUMMARY_CAL 을 다시 잴 때 쓴다
  기록된 리딩(decisions_*.jsonl 의 reads: 결정 순간 사용자가 본 줄)도 따로 채점:
    표시된 상위 3분류 안에 실제 분류가 있었나 / 1순위였나 / 요약 표기에 실제 핸드가 들었나
    (예전 표기 'TT+, AJs+ 외 12%' 는 88% 를, 지금 표기 'TT+, AJs+ 중 하나일 확률 약 50%' 는 50% 를 주장 → 실제 포함률과 비교)

한계: 쇼다운까지 간 핸드만 보인다(폴드시킨 블러프는 안 보임 → 공격 액션의 '에어'는 실제보다 적게 보인다).
앤티는 금액을 몰라 팟에서 빠짐(벳 크기가 약간 크게 계산될 수 있음). 표본이 작다.
"""
import argparse
import glob
import json
import math
import os
import random
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from math import comb

import evaluator as ev
import handlog
import handread as hr

STREETS = ("preflop", "flop", "turn", "river")
STREET_KO = {"preflop": "프리플랍", "flop": "플랍", "turn": "턴", "river": "리버"}
NEED = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
EPS = 0.01                     # 범위 확률을 균등과 섞는 비율 (0 확률 → 무한 로그손실 방지)
VARIANTS = ("live", "raw", "pre", "notype", "uniform")
# 기여 분리 (순서 고정): 앞 변형에서 한 가지씩 켠다. 입력은 모두 live seats(정리·크기·스택) + 유형 프로필.
#   base 는 b538c3d 이전 MULT·차트 밖 0·유형 없음, +tail 은 차트 밖 꼬리(PRE_FLOOR), +mult 는 현재 MULT, +style/+style1 은 유형 보정 0.5/1.0
ABLATION = (
    ("base", {"mult_set": "prev", "pre_floor": 0, "style_strength": 0}),
    ("+tail", {"mult_set": "prev", "pre_floor": 0.07, "style_strength": 0}),
    ("+mult", {"mult_set": "current", "pre_floor": 0.07, "style_strength": 0}),
    ("+style", {"mult_set": "current", "pre_floor": 0.07, "style_strength": 0.5}),
    ("+style1", {"mult_set": "current", "pre_floor": 0.07, "style_strength": 1.0}),
)
ABL_NAMES = tuple(n for n, _ in ABLATION)
BOOT_SEED = 20261105
POST_WORDS = ("check", "bet", "call", "raise", "fold")
FAMILY = {"check": "체크", "call": "콜", "aggr": "벳/레이즈"}
OUT_NAME = "read_scores.json"


# --- 입력 ------------------------------------------------------------------------------------
def data_dir(arg=None):
    if arg:
        return arg
    env = os.environ.get("CARD_READER_DATA")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "hands")


def _read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    x = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(x, dict):
                    out.append(x)
    except OSError:
        pass
    return out


def load(folder):
    """(핸드 기록 리스트, 결정 기록 리스트)."""
    hands, decs = [], []
    for f in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
        name = os.path.basename(f)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", name):
            hands.extend(r for r in _read_jsonl(f) if r.get("actions") is not None and r.get("seats"))
        elif name.startswith("decisions_"):
            decs.extend(r for r in _read_jsonl(f) if r.get("type") == "decision")
    return hands, decs


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


# --- 핸드 기록 → 재생 입력 ---------------------------------------------------------------------
def phantom_streets(rec):
    """스트리트 시작 때 액션할 수 있는 사람(폴드·올인 아님)이 2명 미만 → 그 스트리트의 '체크'는 실제 선택이 아님."""
    acts = rec.get("actions") or {}
    seats = rec.get("seats") or {}
    in_hand = {a.get("seat") for a in acts.get("preflop") or []} | {k for k, s in seats.items() if not s.get("folded")}
    folded, allin, out = set(), set(), set()
    for si, st in enumerate(STREETS):
        if si:
            for k, s in seats.items():
                f = s.get("folded")
                if f in STREETS and STREETS.index(f) < si:
                    folded.add(k)
            live = in_hand - folded - allin
            if len(live) < 2 and acts.get(st):
                out.add(st)
        for a in acts.get(st) or []:
            if a.get("act") == "fold":
                folded.add(a.get("seat"))
            if a.get("allin"):
                allin.add(a.get("seat"))
    return out


def seat_positions(rec):
    return {k: s.get("pos") for k, s in (rec.get("seats") or {}).items()}


def replay_seats(rec):
    """앱과 같은 입력 (handlog.to_handread: 프리플랍 정리 + 올인 금액 스택 + 포스트플랍 벳 크기)."""
    return handlog.to_handread(rec.get("actions") or {}, seat_positions(rec))


def raw_seats(rec):
    """예전 앱 입력 (정리·크기·올인 스택 없음) — 입력 정리가 얼마나 도움이 되는지 보는 비교용."""
    return handlog.to_handread(rec.get("actions") or {}, seat_positions(rec), clean=False, sizes=False)


def input_fixes(rec):
    return handlog.clean_preflop((rec.get("actions") or {}).get("preflop"))[1]


def truncate(seat, street):
    """street 까지의 액션만 남긴 좌석 입력."""
    keep = STREETS.index(street)
    s = dict(seat)
    for st in STREETS[keep + 1:]:
        s[st] = []
    if "bet_role" in s:
        s["bet_role"] = {k: v for k, v in s["bet_role"].items() if STREETS.index(k) <= keep}
    return s


def pre_only(seat):
    return truncate(seat, "preflop")


# --- 분류 --------------------------------------------------------------------------------------
def combo_key(c1, c2):
    hi, lo = (c1, c2) if c1 > c2 else (c2, c1)
    return ev.card_str(hi) + ev.card_str(lo), hi, lo


def actual_class(board, hi, lo):
    """(분류 이름 목록, 실제 분류 인덱스)"""
    if len(board) >= 3:
        return hr.CLASSES_POST, hr._classify(hr._board_info(board), hi, lo)[1]
    return hr.CLASSES_PRE, hr._combo_class_pre(hr._combo_code(hi, lo))


_UNI_CACHE = {}


def uniform_classes(board, dead):
    """남은 카드 무작위일 때의 분류 분포 (합 1)."""
    key = (tuple(board), tuple(sorted(dead)))
    got = _UNI_CACHE.get(key)
    if got is not None:
        return got
    blocked = set(board) | set(dead)
    left = [c for c in range(52) if c not in blocked]
    acc = [0.0] * 7
    n = 0
    bi = hr._board_info(board) if len(board) >= 3 else None
    for i, a in enumerate(left):
        for b in left[i + 1:]:
            hi, lo = (a, b) if a > b else (b, a)
            acc[hr._classify(bi, hi, lo)[1] if bi else hr._combo_class_pre(hr._combo_code(hi, lo))] += 1
            n += 1
    out = [x / n for x in acc]
    _UNI_CACHE[key] = out
    return out


def _class_metrics(probs, y):
    """probs: 7분류 확률(합≈1), y: 실제 인덱스."""
    tot = sum(probs) or 1.0
    p = [x / tot for x in probs]
    py = p[y]
    rank = 1 + sum(1 for i, x in enumerate(p) if i != y and x > py + 1e-9)
    brier = sum((x - (1.0 if i == y else 0.0)) ** 2 for i, x in enumerate(p))
    ll = -math.log((1 - EPS) * py + EPS / len(p))
    return {"cls_p": py, "cls_rank": rank, "cls_top1": rank == 1, "brier": brier, "cls_ll": ll, "cls_probs": p}


def score_result(res, key, code, y, n_live):
    """handread 결과 하나 → 점수 dict."""
    rng = res.get("range") or {}
    p_raw = rng.get(key, 0.0)
    p = (1 - EPS) * p_raw + EPS / n_live
    higher = sum(v for v in rng.values() if v > p_raw + 1e-15)
    equal = sum(v for v in rng.values() if abs(v - p_raw) <= 1e-15)
    names = hr.CLASSES_POST if hr.CLASSES_POST[0] in (res.get("classes") or {}) else hr.CLASSES_PRE
    probs = [(res.get("classes") or {}).get(n, 0.0) for n in names]
    out = {"p": p_raw, "ll": -math.log(p), "pit": higher + 0.5 * equal if p_raw > 0 else 1.0,
           "zero": p_raw <= 0, "top5": code in [c for c, _ in res.get("top_hands") or []],
           "width": res.get("width_pct")}
    named = res.get("summary_codes")
    if named is None:   # 예전 결과: 표기 문자열에서
        named, model = parse_summary(res.get("summary"))
        shown = model
    else:
        model, shown = res.get("summary_pct"), res.get("summary_hit_pct")
    out["sum_in"] = code in named if named else None
    out["sum_claim"] = (shown / 100.0) if (named and shown is not None) else None    # 화면에 보인 확률
    out["sum_model"] = (model / 100.0) if (named and model is not None) else None   # 모델 확률(보정 전)
    out.update(_class_metrics(probs, y))
    return out


def score_uniform(board, dead, y, n_live):
    out = {"p": 1.0 / n_live, "ll": math.log(n_live), "pit": 0.5, "zero": False, "top5": False, "width": None,
           "sum_in": None, "sum_claim": None, "sum_model": None}
    out.update(_class_metrics(uniform_classes(board, dead), y))
    return out


# --- 프로필 ------------------------------------------------------------------------------------
def profile_type(p):
    if not isinstance(p, dict) or not p:
        return "프로필 없음"
    hands = _num(p.get("hands")) or 0
    vpip, pfr = _num(p.get("vpip")), _num(p.get("pfr"))
    if hands < 10 or vpip is None:
        return "표본<10핸드"
    tag = "~" if hands < 30 else ""
    if vpip >= 40:
        style = "루즈패시브" if (pfr or 0) < 15 else "루즈공격"
    elif vpip < 20:
        style = "타이트"
    else:
        style = "보통"
    return tag + style


def seat_type(rec_seat, prof):
    """상대 유형 키(LAG/LP/TAG/TP) 또는 None: 핸드 기록 seats[k].type (예전 기록엔 없음) > 결정 기록 프로필의 style."""
    t = hr._style_key((rec_seat or {}).get("type"))
    if t:
        return t
    return hr._style_key(prof.get("style")) if isinstance(prof, dict) else None


def _with_style(prof, style):
    """프로필 사본에 유형을 넣거나(style) 빼서(None) estimate 의 profiles 인자로."""
    p = {k: v for k, v in (prof or {}).items() if k != "style"}
    if style:
        p["style"] = style
    return p or None


# --- 결정 기록 (사용자가 본 리딩) --------------------------------------------------------------
_CLS_RE = re.compile(r"^(.*\S)\s+(\d+(?:\.\d+)?)%$")


def _expand_token(tok):
    """'TT+' 'AJs+' 'A9o-A3o' '99-33' 'KQs' → 핸드코드 집합 (handread._tokens 의 역)."""
    R = hr._R
    tok = tok.strip()
    out = set()
    try:
        if tok.endswith("+"):
            t = tok[:-1]
            if len(t) == 2 and t[0] == t[1]:
                for i in range(0, R.index(t[0]) + 1):
                    out.add(R[i] * 2)
            elif len(t) == 3:
                hi, lo = R.index(t[0]), R.index(t[1])
                for i in range(hi + 1, lo + 1):
                    out.add(R[hi] + R[i] + t[2])
        elif "-" in tok:
            a, b = tok.split("-", 1)
            if len(a) == 2 and a[0] == a[1]:
                for i in range(R.index(a[0]), R.index(b[0]) + 1):
                    out.add(R[i] * 2)
            elif len(a) == 3:
                hi = R.index(a[0])
                for i in range(R.index(a[1]), R.index(b[1]) + 1):
                    out.add(R[hi] + R[i] + a[2])
        elif len(tok) == 2 and tok[0] == tok[1] and tok[0] in R:
            out.add(tok)
        elif len(tok) == 3 and tok[0] in R and tok[1] in R and tok[2] in "so":
            out.add(tok)
    except ValueError:
        return set()
    return out


def parse_summary(body):
    """요약 표기 → (핸드코드 집합, 표기가 덮는다고 주장하는 비중 %).
    예전 'TT+, AJs+ 외 12%' (= 88% 주장) / 지금 화면 'TT+, AJs+ 중 하나일 확률 약 50%'."""
    body = (body or "").strip()
    hit = re.search(r"\s중 하나일 확률 약 (\d+(?:\.\d+)?)%$", body)
    rest = None if hit else re.search(r"\s외 (\d+(?:\.\d+)?)%$", body)
    covered = float(hit.group(1)) if hit else 100.0 - float(rest.group(1)) if rest else 100.0
    if hit or rest:
        body = body[:(hit or rest).start()]
    codes = set()
    for t in body.split(","):
        codes |= _expand_token(t)
    return codes, covered


def parse_logged(line):
    """'BTN 오픈: 예상 상위 7% (66, A9s-A7s 외 8%) | 보드 기준: 탑페어+ 40% · 드로우 30%' →
    {codes, covered(%), classes{이름: %}, board(bool), truncated}."""
    out = {"codes": set(), "covered": None, "classes": {}, "board": None, "truncated": False}
    if not line:
        return out
    head, _, tail = line.partition(" | ")
    m = re.search(r"예상 상위 [\d.]+% \((.*)\)\s*$", head)
    if m:
        out["codes"], out["covered"] = parse_summary(m.group(1))
    elif "예상 상위" in head:
        out["truncated"] = True
    if "기준:" in tail:
        kind, _, cls = tail.partition("기준:")
        out["board"] = kind.strip().startswith("보드")
        for part in cls.split("·"):
            mm = _CLS_RE.match(part.strip())
            if mm:
                out["classes"][mm.group(1)] = float(mm.group(2))
    return out


def logged_reads(decs):
    """{(hand_id, street, seat): 마지막 결정의 read line}."""
    out = {}
    for d in sorted(decs, key=lambda d: (d.get("t") or 0)):
        for k, r in (d.get("reads") or {}).items():
            if isinstance(r, dict) and r.get("line"):
                out[(d.get("hand_id"), d.get("street"), k)] = r["line"]
    return out


# --- 채점 본체 ---------------------------------------------------------------------------------
def _kind_label(sized_seat, street):
    """그 스트리트 마지막 액션의 종류 (크기·주도권 포함; 크기 모르면 '_?')."""
    if street == "preflop":
        return "pre_" + str(hr._pre_action(hr._norm_pre(sized_seat.get("preflop"))) or "?")
    kinds = hr._norm_post(sized_seat.get(street), (sized_seat.get("bet_role") or {}).get(street))
    if not kinds:
        return None
    kind, size = kinds[-1]
    if size is None and kind.rsplit("_", 1)[-1] in ("small", "med", "big"):
        kind = kind.rsplit("_", 1)[0] + "_?"
    return kind


def _family(kind):
    if kind is None or kind.startswith("pre_"):
        return None
    if kind == "check":
        return "check"
    if kind.startswith("call"):
        return "call"
    return "aggr"


def _context(rec, decs_by_hand, sess_defaults):
    ds = decs_by_hand.get(rec.get("id")) or []
    last = ds[-1] if ds else None
    sess = str(rec.get("id") or "").rsplit("-", 1)[0]
    dflt = sess_defaults.get(sess) or {}
    stack = _num((last or {}).get("stack_bb")) or dflt.get("stack_bb") or 100
    ante = bool((_num((last or {}).get("ante")) or 0) > 0) if last else bool(dflt.get("ante"))
    profiles = {}
    for d in ds:
        for k, s in (d.get("seats") or {}).items():
            if isinstance(s, dict) and isinstance(s.get("profile"), dict):
                profiles[k] = s["profile"]
    return stack, ante, profiles


def _session_defaults(decs):
    acc = defaultdict(list)
    for d in decs:
        acc[str(d.get("hand_id") or "").rsplit("-", 1)[0]].append(d)
    out = {}
    for s, ds in acc.items():
        stacks = [_num(d.get("stack_bb")) for d in ds if _num(d.get("stack_bb"))]
        antes = [(_num(d.get("ante")) or 0) > 0 for d in ds]
        out[s] = {"stack_bb": sorted(stacks)[len(stacks) // 2] if stacks else None,
                  "ante": antes.count(True) > antes.count(False)}
    return out


def score_hands(hands, decs, ok_only=False, ablation=False):
    """→ (rows, logged_rows, skipped Counter). ablation=True 면 행마다 row["abl"][변형] = 점수 (ABLATION 5개)."""
    skipped = Counter()
    decs_by_hand = defaultdict(list)
    for d in sorted(decs, key=lambda d: d.get("t") or 0):
        decs_by_hand[d.get("hand_id")].append(d)
    sess_defaults = _session_defaults(decs)
    logged = logged_reads(decs)
    rows, lrows, seen = [], [], set()
    order = sorted(hands, key=lambda r: (r.get("confidence") != "ok", r.get("start") or 0))
    for rec in order:
        seats_rec = rec.get("seats") or {}
        shown = {k: s.get("shown") for k, s in seats_rec.items() if s.get("shown") and not s.get("hero")}
        if not shown:
            continue
        if ok_only and rec.get("confidence") != "ok":
            skipped["low_confidence"] += len(shown)
            continue
        if not (rec.get("actions") or {}).get("preflop"):
            skipped["no_preflop(mid_hand_start)"] += len(shown)
            continue
        board = hr._board_cards(rec.get("board") or [])
        dead = [c for c in hr._cards(rec.get("hero_cards") or []) if c not in board]
        live_seats = replay_seats(rec)
        raw = raw_seats(rec)
        nfix = input_fixes(rec)
        phantom = phantom_streets(rec)
        stack, ante, profiles = _context(rec, decs_by_hand, sess_defaults)
        players = int(_num(rec.get("pos_players")) or _num(rec.get("players")) or 9)
        for k, cards in shown.items():
            cs = hr._cards(cards)
            if len(cs) != 2 or set(cs) & (set(board) | set(dead)):
                skipped["bad_shown_cards"] += 1
                continue
            dkey = (tuple(board), tuple(sorted(cs)))
            if dkey in seen:
                skipped["duplicate"] += 1
                continue
            seen.add(dkey)
            if k not in live_seats or not live_seats[k].get("preflop") or k not in raw:
                skipped["no_actions"] += 1
                continue
            key, hi, lo = combo_key(*cs)
            code = hr._combo_code(hi, lo)
            prof = profiles.get(k)
            ptype = profile_type(prof)
            styp = seat_type(seats_rec.get(k), prof)
            p_live, p_notype = _with_style(prof, styp), _with_style(prof, None)
            prof_arg = {k: p_live} if p_live else None
            prof_notype = {k: p_notype} if p_notype else None
            for st in STREETS:
                n = NEED[st]
                if st != "preflop":
                    if len(board) < n or not live_seats[k].get(st):
                        continue
                    if st in phantom:
                        skipped["phantom_street(올인 뒤 체크)"] += 1
                        continue
                    if all(w == "fold" for w in live_seats[k].get(st)):
                        continue
                b = board[:n]
                n_live = comb(52 - len(b) - len(dead), 2)
                names, y = actual_class(b, hi, lo)
                ests = {}
                for var, seat, stk in (("live", truncate(live_seats[k], st), stack),
                                       ("raw", truncate(raw[k], st), stack),
                                       ("pre", pre_only(live_seats[k]), stack)):
                    ests[var] = hr.estimate({k: seat}, b, players, dead=dead, profiles=prof_arg, stack_bb=stk,
                                            ante=ante)[k]
                if styp:
                    ests["notype"] = hr.estimate({k: truncate(live_seats[k], st)}, b, players, dead=dead,
                                                 profiles=prof_notype, stack_bb=stack, ante=ante)[k]
                else:
                    ests["notype"] = ests["live"]
                kind = _kind_label(live_seats[k], st)
                row = OrderedDict(hand=rec.get("id"), seat=k, street=st, actual=code, actual_class=names[y],
                                  kind=kind, kind_raw=_kind_label(raw[k], st),
                                  family=_family(kind), profile=ptype, type=styp or "없음",
                                  conf=rec.get("confidence"),
                                  label=ests["live"].get("label"), label_raw=ests["raw"].get("label"),
                                  stack=stack, stack_jam=live_seats[k].get("stack_bb"), input_fixes=nfix)
                for var in ("live", "raw", "pre", "notype"):
                    row[var] = score_result(ests[var], key, code, y, n_live)
                row["uniform"] = score_uniform(b, dead, y, n_live)
                if ablation:
                    row["abl"] = {}
                    for name, cfg in ABLATION:
                        with hr.switches(**cfg):
                            res = hr.estimate({k: truncate(live_seats[k], st)}, b, players, dead=dead,
                                              profiles=prof_arg, stack_bb=stack, ante=ante)[k]
                        row["abl"][name] = score_result(res, key, code, y, n_live)
                row["class_names"] = names
                rows.append(row)
                line = logged.get((rec.get("id"), st, k))
                if line:
                    lrows.append(score_logged(line, rec.get("id"), k, st, code, names[y], row))
    return rows, lrows, skipped


def score_logged(line, hand, seat, street, code, cls, row):
    p = parse_logged(line)
    shown = p["classes"]
    top = max(shown.items(), key=lambda kv: kv[1])[0] if shown else None
    return OrderedDict(hand=hand, seat=seat, street=street, actual=code, actual_class=cls, kind=row["kind"],
                       family=row["family"], in_top3=cls in shown, top1=(top == cls), shown_p=shown.get(cls, 0.0) / 100,
                       in_summary=(code in p["codes"]) if p["covered"] is not None else None,
                       covered=p["covered"], replay_cls_p=row["live"]["cls_p"])


# --- 집계 --------------------------------------------------------------------------------------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def agg(rows):
    """행 묶음 → 변형별 평균 점수."""
    out = {"n": len(rows)}
    for var in VARIANTS:
        rs = [r[var] for r in rows]
        out[var] = {
            "ll": _mean([r["ll"] for r in rs]),
            "pit": _mean([r["pit"] for r in rs]),
            "cls_p": _mean([r["cls_p"] for r in rs]),
            "cls_top1": _mean([float(r["cls_top1"]) for r in rs]),
            "cls_rank": _mean([r["cls_rank"] for r in rs]),
            "brier": _mean([r["brier"] for r in rs]),
            "cls_ll": _mean([r["cls_ll"] for r in rs]),
            "top5": _mean([float(r["top5"]) for r in rs]),
            "zero": _mean([float(r["zero"]) for r in rs]),
            "sum_in": _mean([None if r.get("sum_in") is None else float(r["sum_in"]) for r in rs]),
            "sum_claim": _mean([r.get("sum_claim") if r.get("sum_in") is not None else None for r in rs]),
            "sum_model": _mean([r.get("sum_model") if r.get("sum_in") is not None else None for r in rs]),
        }
    if rows:
        u = out["uniform"]["ll"]
        for var in ("live", "raw", "pre", "notype"):
            out[var]["bits_vs_uniform"] = (u - out[var]["ll"]) / math.log(2)
    return out


def calibration(rows, var="live"):
    """분류별 평균 예측 확률 vs 실제 빈도."""
    if not rows:
        return {}
    names = rows[0]["class_names"]
    pred = [_mean([r[var]["cls_probs"][i] for r in rows]) for i in range(7)]
    act = [sum(1 for r in rows if r["actual_class"] == n) / len(rows) for n in names]
    return OrderedDict((n, {"pred": round(p, 3), "actual": round(a, 3)}) for n, p, a in zip(names, pred, act))


def group(rows, keyfn):
    g = OrderedDict()
    for r in rows:
        k = keyfn(r)
        if k is None:
            continue
        g.setdefault(k, []).append(r)
    return g


def summarize(rows, lrows, skipped, bet_fold=None):
    post = [r for r in rows if r["street"] != "preflop"]
    final = {}
    for r in rows:
        final[(r["hand"], r["seat"])] = r
    s = OrderedDict()
    s["n_seats"] = len({(r["hand"], r["seat"]) for r in rows})
    s["n_rows"] = len(rows)
    s["skipped"] = dict(skipped)
    s["by_street"] = OrderedDict((k, agg(v)) for k, v in group(rows, lambda r: r["street"]).items())
    s["final_street"] = agg(list(final.values()))
    s["by_kind"] = OrderedDict((k, agg(v)) for k, v in sorted(group(post, lambda r: r["kind"]).items()))
    s["by_family"] = OrderedDict((f"{r0}/{k}", agg(v)) for (r0, k), v in sorted(
        group(post, lambda r: (r["street"], r["family"])).items()))
    s["by_preflop_action"] = OrderedDict((k, agg(v)) for k, v in sorted(
        group([r for r in rows if r["street"] == "preflop"], lambda r: r["kind"]).items()))
    s["by_preflop_action_raw"] = OrderedDict((k, agg(v)) for k, v in sorted(
        group([r for r in rows if r["street"] == "preflop"], lambda r: r["kind_raw"]).items()))
    s["by_profile"] = OrderedDict((k, agg(v)) for k, v in sorted(group(rows, lambda r: r["profile"]).items()))
    # 상대 유형별 (팝업 LAG/LP/TAG/TP): live vs notype(유형 보정 없음) — STYLE_ADJ 를 데이터로 다시 맞출 때 본다
    typed = [r for r in post if r.get("type", "없음") != "없음"]
    s["by_type"] = OrderedDict((k, agg(v)) for k, v in sorted(group(rows, lambda r: r.get("type", "없음")).items()))
    s["by_type_family"] = OrderedDict(
        (f"{t}/{fam}", agg(v)) for (t, fam), v in sorted(group(typed, lambda r: (r["type"], r["family"])).items()))
    s["calibration_by_type_family"] = OrderedDict(
        (k, {"n": len(v), "classes": calibration(v), "classes_notype": calibration(v, "notype")})
        for k, v in sorted(group(typed, lambda r: f"{r['type']}/{r['family']}").items()))
    s["by_confidence"] = OrderedDict((k, agg(v)) for k, v in sorted(group(rows, lambda r: r["conf"]).items()))
    s["calibration_by_family"] = OrderedDict(
        (f"{st}/{fam}", {"n": len(v), "classes": calibration(v)})
        for (st, fam), v in sorted(group(post, lambda r: (r["street"], r["family"])).items()))
    s["calibration_postflop_all"] = {"n": len(post), "classes": calibration(post)}
    s["calibration_preflop"] = {"n": sum(1 for r in rows if r["street"] == "preflop"),
                                "classes": calibration([r for r in rows if r["street"] == "preflop"])}
    if bet_fold:
        s["bet_fold_by_type"] = bet_fold
    if lrows:
        lg = OrderedDict()
        for k, v in group(lrows, lambda r: r["street"]).items():
            summ = [x for x in v if x["in_summary"] is not None]
            lg[k] = {"n": len(v), "in_top3": _mean([float(x["in_top3"]) for x in v]),
                     "top1": _mean([float(x["top1"]) for x in v]), "shown_p": _mean([x["shown_p"] for x in v]),
                     "replay_cls_p": _mean([x["replay_cls_p"] for x in v]),
                     "summary_n": len(summ), "in_summary": _mean([float(x["in_summary"]) for x in summ]),
                     "summary_claims": _mean([x["covered"] / 100 for x in summ])}
        s["logged"] = lg
    return s


# --- 기여 분리: 부트스트랩 -------------------------------------------------------------------------
def _pctile(sorted_xs, q):
    return sorted_xs[int(round(q * (len(sorted_xs) - 1)))]


def bootstrap_ablation(rows, B=1000, seed=BOOT_SEED, subset=None):
    """행의 abl 로 변형별 정보이득(bit, 무작위 대비)과 연속 변형 간 Δ(bit, 뒤 - 앞)의 95% 신뢰구간 (짝지은 부트스트랩).
    묶음 단위 = (hand, seat) (그 좌석의 모든 스트리트 행을 함께 뽑는다 — 같은 상대 패의 행은 독립이 아니다).
    subset: 행 -> bool (예: 포스트플랍만). STYLE Δ(+style-+mult, +style1-+style)는 유형이 있는 묶음에서만.
    → {"n_bundles", "n_typed", "bits": {변형: (추정, 하한, 상한)}, "delta": {"+tail-base": (...), ...}}"""
    bun = OrderedDict()
    for r in rows:
        if not r.get("abl") or (subset is not None and not subset(r)):
            continue
        v = bun.setdefault((r["hand"], r["seat"]), [0, 0.0] + [0.0] * len(ABL_NAMES) + [False])
        v[0] += 1
        v[1] += r["uniform"]["ll"]
        for i, n in enumerate(ABL_NAMES):
            v[2 + i] += r["abl"][n]["ll"]
        v[-1] = v[-1] or r.get("type", "없음") != "없음"
    vecs = list(bun.values())
    typed = [v for v in vecs if v[-1]]
    pairs = [(ABL_NAMES[i + 1] + "-" + ABL_NAMES[i], i) for i in range(len(ABL_NAMES) - 1)]
    ln2 = math.log(2)

    def stats(vs, which):
        """which: 'bits' -> {변형: bit}, 'delta' -> {쌍: bit}. 총합 기준 (행 수로 가중한 평균)."""
        n = sum(v[0] for v in vs)
        if not n:
            return None
        tot = [sum(v[j] for v in vs) for j in range(len(ABL_NAMES) + 2)]
        if which == "bits":
            return {nm: (tot[1] - tot[2 + i]) / n / ln2 for i, nm in enumerate(ABL_NAMES)}
        return {nm: (tot[2 + i] - tot[3 + i]) / n / ln2 for nm, i in pairs}

    def is_typed_pair(nm):
        return "style" in nm.split("-")[0]

    out = {"n_bundles": len(vecs), "n_typed": len(typed), "bits": {}, "delta": {}}
    est_b = stats(vecs, "bits")
    est_d_all, est_d_typed = stats(vecs, "delta"), stats(typed, "delta")
    draws = {"bits": {nm: [] for nm in ABL_NAMES}, "delta": {nm: [] for nm, _ in pairs}}
    rng = random.Random(seed)
    if vecs and B > 0:
        for _ in range(B):
            samp = [vecs[rng.randrange(len(vecs))] for _ in vecs]
            sb, sd = stats(samp, "bits"), stats(samp, "delta")
            for nm in ABL_NAMES:
                draws["bits"][nm].append(sb[nm])
            samp_t = [typed[rng.randrange(len(typed))] for _ in typed] if typed else []
            sdt = stats(samp_t, "delta") if samp_t else None
            for nm, _ in pairs:
                x = sdt[nm] if (is_typed_pair(nm) and sdt) else None if is_typed_pair(nm) else sd[nm]
                if x is not None:
                    draws["delta"][nm].append(x)

    def pack(est, xs):
        if est is None:
            return None
        if not xs:
            return (est, est, est)
        xs = sorted(xs)
        return (est, _pctile(xs, 0.025), _pctile(xs, 0.975))

    for nm in ABL_NAMES:
        out["bits"][nm] = pack(est_b and est_b[nm], draws["bits"][nm])
    for nm, _ in pairs:
        est = (est_d_typed if is_typed_pair(nm) else est_d_all)
        out["delta"][nm] = pack(est and est[nm], draws["delta"][nm])
    return out


def decide(boot):
    """부트스트랩 결과 → 권고. MULT 먼저, 그다음 STYLE. Δ 신뢰구간 하한 > 0 이면 유지, 상한 < 0 이면 되돌림(MULT→prev, STYLE→0),
    그 외는 근거 부족 → 되돌림 권고(보류). +style1 - +style 의 하한 > 0 이면 STYLE_STRENGTH 1.0 제안."""
    def verdict(key, undo):
        d = (boot.get("delta") or {}).get(key)
        if d is None:
            return {"verdict": "보류", "delta": None, "action": f"표본 없음 → {undo} 권고"}
        _, lo, hi = d
        if lo > 0:
            return {"verdict": "유지", "delta": d, "action": "현재 값 유지"}
        if hi < 0:
            return {"verdict": "되돌림", "delta": d, "action": f"{undo}"}
        return {"verdict": "보류", "delta": d, "action": f"근거 부족 → {undo} 권고 (보류)"}
    out = {"mult": verdict("+mult-+tail", "MULT_SET=prev 로 되돌림"),
           "style": verdict("+style-+mult", "STYLE_STRENGTH=0 으로 되돌림")}
    d1 = (boot.get("delta") or {}).get("+style1-+style")
    out["style_strength_suggest"] = 1.0 if d1 is not None and d1[1] > 0 else None
    return out


# --- 벳 후 폴드율 (유형별) ------------------------------------------------------------------------
def wilson(k, n, z=1.96):
    """이항 비율의 Wilson 95% 구간 (하한, 상한)."""
    if not n:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def bet_fold_rates(hands, decs):
    """유형(LAG/LP/TAG/TP/없음) x 스트리트별: 상대(hero 아님)의 포스트플랍 벳/레이즈 중 '벳하고 다 폴드시킨' 비율.
    confidence=ok 기록만. 분자 = 쇼다운 없음 + 핸드의 마지막 벳/레이즈 + 이후 다른 좌석의 콜/벳/레이즈 없음 +
    다른 좌석이 모두 폴드 + 이 액션 뒤 다른 좌석 폴드 1번 이상. 쇼다운 편향 없이 '벳 = 밸류 vs 블러프' 비율을 가늠하는 데 쓴다.
    → {유형: {스트리트|'all': {n, k, rate, lo, hi}}}"""
    profs = defaultdict(dict)
    for d in decs:
        for k, s in (d.get("seats") or {}).items():
            if isinstance(s, dict) and isinstance(s.get("profile"), dict):
                profs[d.get("hand_id")][k] = s["profile"]
    cnt = defaultdict(lambda: [0, 0])      # (유형, 스트리트) -> [n, k]
    for rec in hands:
        if rec.get("confidence") != "ok":
            continue
        acts = rec.get("actions") or {}
        seats = rec.get("seats") or {}
        seq = [(st, a) for st in STREETS[1:] for a in acts.get(st) or [] if isinstance(a, dict)]
        last_aggr = max((i for i, (_, a) in enumerate(seq) if a.get("act") in ("bet", "raise")), default=None)
        for i, (st, a) in enumerate(seq):
            k = a.get("seat")
            if a.get("act") not in ("bet", "raise") or (seats.get(k) or {}).get("hero"):
                continue
            typ = seat_type(seats.get(k), profs.get(rec.get("id"), {}).get(k)) or "없음"
            win = (rec.get("showdown") is None and i == last_aggr
                   and not any(b.get("seat") != k and b.get("act") in ("call", "bet", "raise") for _, b in seq[i + 1:])
                   and all(s.get("folded") for kk, s in seats.items() if kk != k)
                   and any(b.get("seat") != k and b.get("act") == "fold" for _, b in seq[i + 1:]))
            for key in ((typ, st), (typ, "all")):
                cnt[key][0] += 1
                cnt[key][1] += int(win)
    out = OrderedDict()
    for t in ("LAG", "LP", "TAG", "TP", "없음"):
        for st in STREETS[1:] + ("all",):
            n, k = cnt.get((t, st), (0, 0))
            if n:
                lo, hi = wilson(k, n)
                out.setdefault(t, OrderedDict())[st] = {"n": n, "k": k, "rate": k / n, "lo": lo, "hi": hi}
    return out


# --- 보고 --------------------------------------------------------------------------------------
def _pct(x, d=0):
    return "-" if x is None else f"{x * 100:.{d}f}%"


def _f(x, d=2):
    return "-" if x is None else f"{x:.{d}f}"


def _line(name, a):
    lv, fx, pr, un = a["live"], a["raw"], a["pre"], a["uniform"]
    return (f"  {name:<16} n={a['n']:>3} | 정보이득 live {_f(lv.get('bits_vs_uniform'), 1)}bit"
            f" (raw {_f(fx.get('bits_vs_uniform'), 1)}, pre {_f(pr.get('bits_vs_uniform'), 1)})"
            f" | PIT {_f(lv['pit'])} | 실제분류 확률 {_pct(lv['cls_p'])} (pre {_pct(pr['cls_p'])}, 무작위 {_pct(un['cls_p'])})"
            f" 1순위 {_pct(lv['cls_top1'])} | Brier {_f(lv['brier'])}/{_f(pr['brier'])}/{_f(un['brier'])}"
            f" | top5 {_pct(lv['top5'])} | 0확률 {_pct(lv['zero'])}"
            f" | 요약 표기에 실제 핸드 {_pct(lv.get('sum_in'))} (화면 {_pct(lv.get('sum_claim'))},"
            f" 모델 {_pct(lv.get('sum_model'))})")


def _type_line(name, a):
    lv, nt = a["live"], a["notype"]
    return (f"  {name:<16} n={a['n']:>3} | 정보이득 live {_f(lv.get('bits_vs_uniform'), 2)}bit"
            f" (notype {_f(nt.get('bits_vs_uniform'), 2)}) | 실제분류 확률 {_pct(lv['cls_p'], 1)}"
            f" (notype {_pct(nt['cls_p'], 1)}) | Brier {_f(lv['brier'], 3)}/{_f(nt['brier'], 3)} | PIT {_f(lv['pit'])}")


def report(s, out=None):
    w = (out or sys.stdout).write
    w(f"== 상대 리딩 채점: 공개된 상대 {s['n_seats']}명, 채점 지점 {s['n_rows']}개 ==\n")
    if s["skipped"]:
        w("제외: " + ", ".join(f"{k} {v}" for k, v in s["skipped"].items()) + "\n")
    w("지표: 정보이득 = 무작위 대비 실제 콤보를 몇 bit 더 잘 맞혔나(높을수록 좋음) · PIT 0.5 근처가 보정 양호"
      "(낮음=실제 패가 범위 중심, 높음=범위 꼬리) · Brier live/pre/무작위 (낮을수록 좋음)\n")
    w("\n[스트리트별]\n")
    for k, a in s["by_street"].items():
        w(_line(STREET_KO.get(k, k), a) + "\n")
    w(_line("마지막 스트리트", s["final_street"]) + "\n")
    w("\n[프리플랍 액션별]\n")
    for k, a in s["by_preflop_action"].items():
        w(_line(k, a) + "\n")
    w("\n[프리플랍 액션별 — 정리 전 입력(raw) 분류 기준]\n")
    for k, a in s["by_preflop_action_raw"].items():
        w(_line(k, a) + "\n")
    w("\n[포스트플랍 마지막 액션별 (크기=기록 금액 기준, _?=금액 모름 → 보통 크기로 계산)]\n")
    for k, a in s["by_kind"].items():
        w(_line(str(k), a) + "\n")
    w("\n[프로필별]\n")
    for k, a in s["by_profile"].items():
        w(_line(k, a) + "\n")
    if any(k != "없음" for k in s.get("by_type") or {}):
        w("\n[상대 유형별 (유저 정보 팝업) — live vs 유형 보정 없음(notype)]\n")
        for k, a in list(s["by_type"].items()) + list(s["by_type_family"].items()):
            w(_type_line(k, a) + "\n")
        for k, c in s["calibration_by_type_family"].items():
            parts = [f"{n} {_pct(v['pred'])}/{_pct(c['classes_notype'][n]['pred'])}→{_pct(v['actual'])}"
                     for n, v in c["classes"].items() if v["pred"] >= 0.03 or v["actual"] > 0]
            w(f"  보정 {k} (n={c['n']}, 예측 live/notype→실제): " + " · ".join(parts) + "\n")
    if s.get("bet_fold_by_type"):
        w("\n[상대 벳/레이즈 후 전원 폴드 비율 (유형별, confidence=ok, Wilson 95%)]\n")
        for t, by in s["bet_fold_by_type"].items():
            for st, v in by.items():
                w(f"  {t:<4} {STREET_KO.get(st, '전체'):<6} n={v['n']:>3} 폴드로 끝 {v['k']:>3} = {_pct(v['rate'], 1)}"
                  f" (95% {_pct(v['lo'], 1)} ~ {_pct(v['hi'], 1)})\n")
    if s.get("ablation"):
        _report_ablation(s["ablation"], w)
    w("\n[기록 신뢰도별]\n")
    for k, a in s["by_confidence"].items():
        w(_line(str(k), a) + "\n")
    w("\n[분류 보정: 예측 평균 → 실제 빈도 (live)]\n")
    for name, c in [("프리플랍", s["calibration_preflop"]), ("포스트플랍 전체", s["calibration_postflop_all"])] + \
            [(k, v) for k, v in s["calibration_by_family"].items()]:
        if not c["n"]:
            continue
        parts = [f"{n} {_pct(v['pred'])}→{_pct(v['actual'])}" for n, v in c["classes"].items()
                 if v["pred"] >= 0.03 or v["actual"] > 0]
        w(f"  {name} (n={c['n']}): " + " · ".join(parts) + "\n")
    if s.get("logged"):
        w("\n[결정 순간 화면에 보인 리딩 (decisions 기록)]\n")
        for k, v in s["logged"].items():
            w(f"  {STREET_KO.get(k, k):<6} n={v['n']:>3} | 실제 분류가 표시 상위3 안 {_pct(v['in_top3'])}, 1순위 {_pct(v['top1'])},"
              f" 표시 확률 {_pct(v['shown_p'])} (재생 {_pct(v['replay_cls_p'])})"
              f" | 요약 표기에 실제 핸드 포함 {_pct(v['in_summary'])} (표기가 주장한 비중 {_pct(v['summary_claims'])},"
              f" n={v['summary_n']})\n")
    w("\n주의: 쇼다운까지 간 핸드만 보임 → 폴드시킨 블러프가 빠져 공격 액션의 에어 비중은 실제보다 낮게 관측된다. 표본이 작다.\n")


def _ci(x, d=3):
    return "-" if x is None else f"{x[0]:+.{d}f} [{x[1]:+.{d}f}, {x[2]:+.{d}f}]"


def _report_ablation(ab, w):
    w("\n[기여 분리: 무작위 대비 정보이득(bit) 추정 [95% 구간], (hand, seat) 묶음 부트스트랩]\n")
    for label, key in (("전체", "all"), ("포스트플랍", "post")):
        b = ab.get(key)
        if not b:
            continue
        w(f"  {label} (묶음 {b['n_bundles']}, 유형 있는 묶음 {b['n_typed']}):\n")
        for nm in ABL_NAMES:
            w(f"    {nm:<8} {_ci(b['bits'].get(nm))}\n")
        for nm, d in b["delta"].items():
            w(f"    Δ {nm:<14} {_ci(d)}\n")
    dec = ab.get("decision")
    if dec:
        w("  [판정 (포스트플랍 기준; 구간 하한>0 유지 / 상한<0 되돌림 / 그 외 근거 부족=되돌림 권고)]\n")
        for nm, ko in (("mult", "MULT 행 변경(b538c3d)"), ("style", "STYLE_ADJ 유형 보정(0.5)")):
            v = dec[nm]
            w(f"    {ko}: {v['verdict']} — {v['action']} (Δ {_ci(v['delta'])})\n")
        if dec.get("style_strength_suggest"):
            w(f"    STYLE_STRENGTH {dec['style_strength_suggest']} 이 0.5 보다 낫다 (구간 하한>0) → 1.0 제안\n")


def _jsonable(rows):
    out = []
    for r in rows:
        r2 = OrderedDict((k, v) for k, v in r.items() if k != "class_names")
        for var in VARIANTS:
            if var in r2:
                r2[var] = {k: (round(v, 4) if isinstance(v, float) else
                               [round(x, 4) for x in v] if isinstance(v, list) else v) for k, v in r2[var].items()}
        if "abl" in r2:
            r2["abl"] = OrderedDict((n, {k: (round(v, 4) if isinstance(v, float) else
                                             [round(x, 4) for x in v] if isinstance(v, list) else v)
                                         for k, v in sc.items()}) for n, sc in r2["abl"].items())
        out.append(r2)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="쇼다운 공개 카드로 상대 리딩 채점")
    ap.add_argument("folder", nargs="?", help="핸드 기록 폴더 (기본: CARD_READER_DATA 또는 dist/hands)")
    ap.add_argument("--ok-only", action="store_true", help="confidence=ok 기록만")
    ap.add_argument("--json", help=f"JSON 요약 경로 (기본: 데이터 폴더/{OUT_NAME})")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--boot", type=int, default=1000, help="기여 분리 부트스트랩 횟수 (기본 1000, 0 이면 생략)")
    ap.add_argument("--no-ablation", action="store_true", help="기여 분리(변형별 재생) 건너뜀")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    folder = data_dir(args.folder)
    hands, decs = load(folder)
    abl = not args.no_ablation
    rows, lrows, skipped = score_hands(hands, decs, ok_only=args.ok_only, ablation=abl)
    s = summarize(rows, lrows, skipped, bet_fold=bet_fold_rates(hands, decs))
    if abl and args.boot > 0 and rows:
        post = bootstrap_ablation(rows, args.boot, subset=lambda r: r["street"] != "preflop")
        s["ablation"] = {"all": bootstrap_ablation(rows, args.boot), "post": post, "decision": decide(post)}
    if not args.quiet:
        report(s)
    path = args.json or os.path.join(folder, OUT_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"summary": s, "rows": _jsonable(rows), "logged": lrows}, f, ensure_ascii=False, indent=1,
                      default=lambda o: sorted(o) if isinstance(o, set) else str(o))
        if not args.quiet:
            print(f"\nJSON: {path}")
    except OSError as e:
        print(f"JSON 저장 실패: {e}", file=sys.stderr)
    return s


if __name__ == "__main__":
    main()
