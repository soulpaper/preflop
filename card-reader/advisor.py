"""액션 추천 (휴리스틱, 솔버 아님).

recommend(ctx) -> dict 순수 함수: I/O 없음, 난수 없음 (에쿼티는 ctx 로 받는다).
단위는 BB. ctx 의 어떤 키든 없거나 None 일 수 있다.

ctx 키
  hero, board          : ['Ah', 'Kd'] 형식 문자열 (또는 evaluator 정수)
  street               : 'preflop' / 'flop' / 'turn' / 'river' (없으면 보드 장수로)
  players              : 테이블 인원 (6~10 범위로 보정)
  opponents_in_hand    : 핸드에 남은 상대 수
  hero_pos             : preflop.py 포지션 이름 (UTG, U1.., LJ, HJ, MP, CO, BTN, SB, BB)
  situation            : open / vsopen / vs3bet / vs4bet / jam / jamcall (없으면 추정)
  villain_pos          : 상대 포지션
  stack_bb             : 유효 스택, my_stack: 내 스택
  pot                  : 상대 벳 전 팟, pot_total: 화면 POT (모든 벳 포함)
  call                 : 콜 금액 (0/None = 체크로 넘어옴)
  to_act, can_check    : bool
  ante                 : bool 또는 BB (숫자면 팟에 들어간 앤티 합계로 본다)
  equity               : 0~1 (랜덤 핸드 opponents_in_hand 명 대비), outs: reader.analyze 의 res['outs']
  hand_name            : 표시용 족보 이름
  facing_allin         : (선택) 상대가 올인했는지
  equity_basis         : (선택) 'range' 면 equity 가 상대 레인지 기준 (rangeq.equity_vs_ranges). 없으면 랜덤 핸드 기준
  villain_range(s)     : (선택) 상대 레인지 dict / 리스트 — 표시용 폭(%)만 쓴다 (에쿼티는 caller 가 계산)
  villain_profile(s)   : (선택) {vpip, pfr, threebet, fold_to_bet, af, hands, reliable} — 주 상대(벳한 사람/마지막 콜러)가
                         첫 번째. reliable(또는 hands>=30) 일 때만 반영
  villain_raised       : (선택) 상대가 벳이 아니라 레이즈로 들어왔는지
  closing_action       : (선택) 내가 이 베팅 라운드의 마지막 액션인지 (True 면 뒤에 레이즈할 사람이 없다)
  hero_ip              : (선택) 포스트플랍에서 내가 상대보다 늦게 행동하는지 (헤즈업 표 HU_GTO 의 'ip'/'oop' 열).
                         없으면 hero_pos/villain_pos 로 추정 (9인 기준: SB 가 먼저), 그것도 없으면 'unk' 열
  aggressor            : (선택) 직전 베팅 라운드의 마지막 공격자가 나인지 (플랍이면 프리플랍 레이저). None = 모름/체크로 돔
  flop_line            : (선택) 턴 결정일 때 플랍 액션 'x_x' / 'x_bet33_c' / 'x_bet75_c' / 'bet33_c' / 'bet75_c' (solverlib 턴 조회용)
  looseness            : (선택) -1.0 ~ +1.0 연속 다이얼.  -1 아주 타이트 / 0 솔버(GTO) 근사로 보정한 표준(기본) / +1 아주 루즈·공격적.
                         프리플랍 오픈·방어·3벳 폭 배수(preflop.style_params)와 포스트플랍 콜 여유·밸류/블러프 임계값(post_params)에 연속 반영.
                         L 이 커질수록 프리플랍 레인지는 줄지 않고 포스트플랍 콜은 늘어난다.  0 이 아니면 detail 에 '스타일' 표기.
  style                : (선택) 'tight' / 'standard' / 'aggressive' — looseness 별칭 (-0.6 / 0 / +0.6).  looseness 가 있으면 그쪽이 우선.
                         tight 는 앤티 보정 전(예전) 동작에 가깝고, aggressive 는 오픈/방어/3벳이 더 넓고 헤즈업 세미블러프가 많고 얇은 밸류 벳.
  스택 깊이            : stack_bb(유효 스택)에 따라 프리플랍 폭이 연속으로 변한다 (preflop.DEPTH_*).  PUSHFOLD_BB(15BB) 이하 푸시/폴드,
                         15~25BB 는 오픈이 조금 좁고 콜이 줄며(투기 핸드 먼저) 3벳이 곧 잼, 40~100BB+ 는 투기 핸드 콜이 늘어난다.
  tournament           : (선택) 토너먼트 ICM.  {players_left, entrants, paid, payouts (리스트 또는 'standard'/'flat'/'top_heavy'),
                         prize_pool, my_rank ('6/32' 문자열도 됨 — 뒤 숫자를 players_left 로), avg_stack_bb}.  지급 인원 = paid / len(payouts)
                         / entrants 의 ~15% (셋 다 없으면 단계 'unknown' → 보정 없음).  단계가 early(초반)이면 칩EV 그대로 (동작 동일).
  table_stacks_bb      : (선택) {seat: 스택BB} 내 테이블 (내 자리가 있으면 'hero' 키나 my_stack 과 같은 값 하나를 빼고 쓴다)
  villain_stack_bb     : (선택) 주 상대의 남은 스택 (벳 뒤, 올인이면 0).  없으면: 올인 상대는 0(+내 스택을 넘는 몫),
                         그 밖엔 유효 스택 < 내 스택이면 유효 스택, 아니면 테이블 스택 중앙값, 그것도 없으면 평균 스택
  ICM (tournament 가 있고 단계가 mid 이후): 필드 = 나 + 상대 + 테이블 나머지 + (남은 인원 - 테이블) × 나머지 평균 스택 (icm.py)
    올인 결정 (잼, 잼 콜, 포스트플랍 올인 콜, 낮은 SPR 커밋, 압박 콜): 칩EV 필요 승률 대신 ICM 필요 승률 (상한 85% — 너트는 안 접음).
      프리플랍 잼/잼콜 차트 폭은 (칩 필요/ICM 필요)^2 배 (잼은 상대 콜 쪽 ICM 압박만큼 넓힘, 0.4~1.35배).
      detail 'ICM: 버블 팩터 1.6 → 필요 승률 33%→44% (버블)'.  포스트플랍 표기 '필요 승률 33%+11%(ICM) …'
    올인 아닌 결정: 위험 프리미엄 rp = BF/(1+BF) - 0.5 (상대 기준) 로 콜 여유 + rp × 0.5 × 스택 관여, 프리플랍 오픈/방어/3벳 폭
      × (1 - 2rp) (최소 0.55; 덮는 상대에겐 rp 가 커서 더 좁다).  평균 1.3배 이상 + 상대를 덮는 빅스택은 오픈 폭을 (상대 rp - 내 rp) 만큼 넓힌다 (최대 1.2배).
      위험 프리미엄 3%p 이상이면 콤보 드로우 세미블러프 올인 안 함.

멀티웨이 (opponents_in_hand >= 2) 규칙 요약
  콜 판단: 보정 에쿼티 = 에쿼티 × (벳 크기 보정) × (1 - 상대 수 할인) × (1 - 원페어 취약), 단 이 합산 감소는 상대 2명 14% / 3명+ 18% 를 넘지 않는다 (standard; post_params)
           필요 승률 = 팟 오즈 + 뒤에 남은 사람의 레이즈 위험(추가 1명당 1.5%, 최대 3.5%; 마지막 액션이거나 BTN(뒤에 아무도 없음)이면 0)
  프리플랍 BB/SB 방어: 에쿼티가 상대 레인지 기준으로 안 들어와도(equity_basis != 'range') 오프너 레인지 대비 에쿼티를
           rangeq.equity_vs_ranges (2000회, 고정 시드) 로 직접 계산해 팟 오즈로 차트를 검증한다 (_defence_equity)
  밸류 벳: 원페어 계열(탑페어/오버페어)은 상대가 늘수록 작게·드물게, 투페어+ 는 계속 벳 (젖은 보드 3/4 팟)
  블러프: 2명 이상 상대엔 순수 블러프 없음, 세미블러프는 콤보/너트 드로우 vs 2명 이하만
  프리플랍: 림퍼/콜러 수는 팟에서 역산 (opponents_in_hand 는 아직 안 낸 사람 포함이라 쓰지 않음)

헤즈업 포스트플랍 (opponents_in_hand == 1) — HU_GTO 표 (솔버 데이터셋으로 맞춘 임계값, bench_gto.py 로 재현)
  벳에 콜   : 보정 에쿼티 >= 팟 오즈 + call[거리] + call_slope × (벳/팟 - 0.5)  (올인 콜은 allin_call[거리])
  레이즈    : (보정 에쿼티 - 팟 오즈) >= raise_edge[거리] 이고 등급이 raise_cls 안.  콜 후 SPR < jam_spr 이고 에지 >= jam_edge 면 올인
  체크 노드 : 에쿼티 >= bet[위치][거리] (위치 = 체크로 넘어옴 ip / 먼저 행동 oop / 모름 unk, 주도권 agg 보정) 이면 밸류 벳,
              플랍 IP + 주도권이면 cbet 이상 작은 c-벳, 세미블러프(semi)·리버 에어 블러프(bluff) 는 위치별 허용 여부
  위치·주도권은 ctx hero_ip / aggressor (없으면 hero_pos·villain_pos 로 위치만 추정, 그것도 없으면 'unk' 열)

상대 레인지 기준 (equity_basis == 'range')
  에쿼티가 이미 상대 레인지를 반영하므로 '랜덤 핸드 할인'(벳하는 상대 레인지 보정, 다자 에쿼티 할인)은 작은 잔여분만 남기고
  (벳 크기 보정 3%, 추가 상대 1명당 1%), 원페어 취약 할인은 절반, 등급 보정 기준도 레인지 에쿼티 눈높이로 낮춘다. detail 에 '상대 범위 기준' 표기.

상대 프로필 보정 (reliable 일 때만, detail 에 '상대 VPIP x / PFR y (n핸드)' 표기)
  벳 폴드율 >= 60 (헤즈업): 체크로 넘어온 에어/드로우로 블러프·세미블러프   <= 30: 블러프 금지(콤보 드로우 제외), 얇은 밸류 벳
  VPIP >= 40 (루즈): 밸류 벳 사이즈업 + 더 넓게 (미디엄 핸드도 벳)         <= 15 (타이트): 벳/레이즈 존중 → 필요 승률 가산
  AF >= 3 (공격적): 미디엄/원페어로 블러프캐치 확대   AF <= 1 + 레이즈: 강한 핸드로 보고 필요 승률 가산

GTO 라이브러리 (solverlib, 헤즈업 플랍/턴 SRP): 조회가 믿을 만하면 그 액션이 주 추천(규칙 기반 판단은 detail 로), 아니면 규칙 기반 유지 +
  detail 'GTO 참고(비슷한 플랍 K72r): …' 한 줄 + 신뢰도 low.  조건·상수는 GTO_* 와 _gto_primary 참고.  멀티웨이·프리플랍·3벳팟엔 쓰지 않는다.

반환
  action      : 폴드 / 체크 / 콜 / 벳 / 레이즈 / 올인
  size_bb     : float 또는 None
  size_text   : '2.5BB', '팟의 2/3 ≈ 4.2BB' ...
  reason      : 한 줄 이유
  detail      : 핵심 수치 최대 3줄
  confidence  : 'high' / 'medium' / 'low'
  color_key   : 'raise' / 'call' / 'fold' / 'jam' / 'check'
  plan        : 대응 계획 짧은 문장 최대 3줄 (없으면 []).  내 차례가 아니거나 콜 금액을 모를 때 채운다:
                포스트플랍 '체크로 오면: 벳 …' / '벳이 오면: 팟의 60%(≈3.6BB)까지 콜, 그 이상 폴드' (에쿼티·팟오즈·멀티웨이 할인·드로우 내재배당),
                프리플랍 '레이즈가 오면: xBB까지 콜 / 3벳 yBB'.  내 차례일 때는 '레이즈 당하면: 폴드' 같은 후속 대응.
  action '대기': 앞에 벳이 있는데 금액을 모를 때 (color_key 'check'). 금액이 보이면 plan 대신 콜/폴드가 나온다.
"""
import math
import random
import time
from functools import lru_cache

import evaluator as ev
import icm
import odds
import preflop as pf
import rangeq

FOLD, CHECK, CALL, BET, RAISE, ALLIN = "폴드", "체크", "콜", "벳", "레이즈", "올인"
WAIT = "대기"   # 앞에 뭔가 있는데 금액을 모를 때: 계획(plan)만 보여준다
COLOR = {FOLD: "fold", CHECK: "check", CALL: "call", BET: "raise", RAISE: "raise", ALLIN: "jam", WAIT: "check"}
STREETS = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}
DEEP_SITUATIONS = ("open", "vsopen", "vs3bet", "vs4bet")
SHORT_SITUATIONS = ("jam", "jamcall")
TO_SHORT = {"open": "jam", "vsopen": "jamcall", "vs3bet": "jamcall", "vs4bet": "jamcall"}
TO_DEEP = {"jam": "open", "jamcall": "vsopen"}
PRESSURE = 0.30       # 콜 금액이 유효 스택의 30% 를 넘으면 잼 or 폴드
COMMIT_SIZE = 0.45    # 레이즈/벳 사이즈가 스택의 45% 이상이면 그냥 올인
COMMIT_ANCHORS = ([15, 25, 40], [0.30, 0.36, 0.45])   # 프리플랍 3벳/4벳: 얕은 스택일수록 더 일찍 잼 (15~25BB 는 3벳잼)
EQ_GUESS = {"monster": 0.85, "strong": 0.70, "medium": 0.55, "weak": 0.40, "air": 0.22}
T_RANK, J_RANK, Q_RANK = 8, 9, 10  # evaluator 랭크 인덱스 (2=0 … A=12)
# 멀티웨이 할인/뒤 레이즈 위험 값은 post_params(느슨함) 에. 프리플랍 수학의 뒤 레이즈 위험은 아래 값 그대로.
MW_BEHIND_STEP = 0.02  # 프리플랍: 뒤에 남은 사람이 레이즈할 위험 (필요 승률 가산, 뒤 인원 1명당)
MW_BEHIND_CAP = 0.06
ONEPAIR_STEP_STRONG, ONEPAIR_STEP_WEAK = 0.10, 0.25  # 원페어 다자 벳 취약 할인 (상대 1명 추가당)
OFFSUIT_ISO_PCT = {0: 10, 1: 8, 2: 6}  # 림퍼 수별 오프수트 레이즈 상한 (상위 %), 3명 이상은 5.5
PREMIUM_OFFSUIT_PCT = 5.5              # AKo 까지
VALUE_4BET = {"AA", "KK", "QQ", "JJ", "AKs", "AKo"}  # 콜러가 있어도 4벳하는 밸류 핸드
RB_PEN_FRAC, RB_DISC_STEP, RB_DISC_CAP = 0.03, 0.01, 0.06   # 레인지 기준 에쿼티의 잔여 할인
FOLD_HIGH, FOLD_LOW = 60, 30      # 상대 벳 폴드율 (%)
VPIP_LOOSE, VPIP_TIGHT = 40, 15
AF_HIGH, AF_LOW = 3.0, 1.0
BET_LADDER = [(1 / 3, "1/3"), (1 / 2, "1/2"), (2 / 3, "2/3"), (3 / 4, "3/4"), (1.0, "1")]
# 포스트플랍 임계값은 느슨함 L 의 함수: 세 앵커(tight -0.6 / standard 0 / aggressive +0.6) 사이를 선형 보간하고 바깥은 같은 기울기로 외삽(post_params).
#   pen: 헤즈업 벳 상대 레인지 보정(랜덤 가정 대비, 팟 크기 벳 기준 에쿼티 감소율) / pen_mw: 멀티웨이 (상대 N명 랜덤 에쿼티는
#   이미 N명 몫으로 희석돼 있어서 헤즈업보다 훨씬 작다) growth: 3명+ 일 때 추가   disc_*: 추가 상대 1명당 에쿼티 할인 / 상한
#   cap2/cap3: (할인+레인지 보정+원페어 취약) 합산 감소 상한 (하프팟 벳 기준, 상대 2명 / 3명 이상). 벳이 팟 크기로 커지면 cap_slope 만큼
#   (팟 벳에서 +38%p) 상한이 늘고, 리버는 river_cap 만큼 더 풀리며 리버 멀티웨이의 할인·레인지 보정은
#   예전(tight) 값 × river_gen 을 쓴다 (리버 벳은 밸류 위주). tight 는 사실상 상한 없음 = 예전 동작.
#   standard 는 랜덤 핸드 플랍 하프팟 벳 폴드율을 헤즈업 ~22% / 2명 ~45% / 3명 ~64% 로 맞춘 값 (기존 14% / 58% / 75%).
#   behind: (1명당, 상한) 뒤에 남은 사람의 레이즈 위험
#   call_adj: 필요 승률 가감   value_adj: 리버 얇은 밸류 에쿼티 기준 가감   semi_outs: 강한 드로우로 보는 최소 아웃츠
#   hu_bluff: 헤즈업에서 아웃츠 4장+ 에어/약한 패로 세미블러프(L>=0.3)   math_margin: 프리플랍 차트→콜 뒤집기 여유 가감
POST_ANCHORS = [
    (-0.6, dict(pen=0.45, pen_mw=0.1875, growth=0.2, disc_step=0.05, disc_cap=0.25, cap2=0.9, cap3=0.9, cap_slope=0.0,
                river_cap=0.0, river_gen=1.0, behind=(0.02, 0.06), call_adj=0.02, value_adj=-0.04, semi_outs=8, math_margin=0.02)),
    (0.0, dict(pen=0.40, pen_mw=0.06, growth=0.10, disc_step=0.01, disc_cap=0.03, cap2=0.05, cap3=0.07, cap_slope=0.76,
               river_cap=0.30, river_gen=1.0, behind=(0.005, 0.01), call_adj=0.0, value_adj=0.0, semi_outs=8, math_margin=0.0)),
    (0.6, dict(pen=0.34, pen_mw=0.04, growth=0.10, disc_step=0.008, disc_cap=0.02, cap2=0.04, cap3=0.05, cap_slope=0.6,
               river_cap=0.30, river_gen=0.8, behind=(0.003, 0.006), call_adj=-0.02, value_adj=0.05, semi_outs=6, math_margin=-0.01)),
]
POST_FLOOR = {"pen": 0.1, "pen_mw": 0.0, "disc_step": 0.0, "disc_cap": 0.0, "cap2": 0.02, "cap3": 0.02, "cap_slope": 0.0,
              "river_cap": 0.0, "river_gen": 0.4, "semi_outs": 5, "growth": 0.0}
POST_CEIL = {"cap2": 0.95, "cap3": 0.95, "river_gen": 1.0, "semi_outs": 9}
HU_BLUFF_MIN_L = 0.3


def _lerp_param(x, lo, hi, a, b):
    t = (x - lo) / (hi - lo)
    if isinstance(a, tuple):
        return tuple(u + t * (v - u) for u, v in zip(a, b))
    return a + t * (b - a)


@lru_cache(maxsize=64)
def _post_params(L):
    (x0, a), (x1, b), (x2, c) = POST_ANCHORS
    seg = ((x0, a), (x1, b)) if L < x1 else ((x1, b), (x2, c))
    (lo, pa), (hi, pb) = seg
    out = {k: _lerp_param(L, lo, hi, pa[k], pb[k]) for k in pa}
    for k, v in POST_FLOOR.items():
        out[k] = max(v, out[k])
    for k, v in POST_CEIL.items():
        out[k] = min(v, out[k])
    out["behind"] = tuple(max(0.0, x) for x in out["behind"])
    out["semi_outs"] = int(round(out["semi_outs"]))
    out["hu_bluff"] = L >= HU_BLUFF_MIN_L
    out["hu_bluff_outs"] = 6 if L < 0.6 else 4    # 느슨할수록 아웃츠 적은 핸드로도 세미블러프
    out["hu_bluff_frac"] = (1 / 2, "1/2") if L >= 0.85 else (1 / 3, "1/3")
    return out


def post_params(L):
    return _post_params(round(max(-1.0, min(1.0, L)), 3))


# --- 헤즈업 포스트플랍 솔버 보정 표 (HU_GTO) ------------------------------------------------
# 출처: Hugging Face 'jevonmao/postflop-solver-reasoning-traces-1m' (CC-BY-4.0, postflop-solver 엔진으로 푼 헤즈업 블라인드 대
#   블라인드 = SB 오픈 / BB 디펜스, 200BB 포스트플랍 결정 467,680개).  bench_gto.py 로 고정 시드 80/20 분할 후 학습 쪽 60k 표본에서만
#   좌표 하강(그리드)으로 맞췄다 (목표: share = 솔버가 그 노드에서 추천 범주를 고를 확률).  검증 20k 점수는 아래 HU_GTO_SCORE.
#   주의: 데이터는 200BB · BvB · 헤즈업 — 레이즈 전쟁으로 생긴 낮은 SPR 노드가 많다.  그래서 '낮은 SPR = 강한 레인지' 대신
#   스택과 무관한 '팟 크기(BB)' 로 레인지 강도를 본다 (pot_slope).  20~60BB 에서도 같은 의미.
# 적용 범위: 상대 1명(헤즈업) 포스트플랍만.  멀티웨이(opp >= 2)·프리플랍·스택 깊이 규칙은 이 표를 쓰지 않는다.
#   느슨함(looseness)·상대 프로필 보정은 이 표 위에 그대로 더해진다 (call_adj / value_adj / pen 변화분 / 프로필 가감).
# 열: 'range' = equity_basis 'range' (상대 레인지 기준 에쿼티), 'random' = 랜덤 핸드 기준 에쿼티 (또는 에쿼티 없음 → 족보 추정)
# 위치 열: 'ip' = 체크로 넘어옴(내가 IP), 'oop' = 내가 먼저 행동, 'unk' = 위치 모름 (ctx hero_ip / hero_pos+villain_pos 로 판단)
# 키 (거리 = flop/turn/river)
#   pen        : 벳 상대 레인지 보정 — 보정 에쿼티 = eq × (1 - pen × min(1, 벳/팟))
#   call       : 콜 여유 — 보정 에쿼티 >= 팟 오즈 + call[거리] + call_slope × (min(벳/팟, 1.5) - 0.5) + 팟 크기 가산
#   pot_ref / pot_slope : 팟(벳 포함, BB)이 pot_ref 보다 크면 콜 여유 += pot_slope[거리] × log2(팟/pot_ref)  (벳·레이즈가 많이 오간 팟)
#   allin_call / allin_eq : 올인(레이즈 불가) 콜 — 보정 에쿼티 >= 팟 오즈 + allin_call[거리] 이고 에쿼티 >= allin_eq[거리]
#   raise_edge : 밸류 레이즈 — 보정 에쿼티 - 팟 오즈 >= raise_edge[거리] 이고 등급이 raise_cls 안
#   jam_spr / jam_edge : 콜 후 SPR < jam_spr 이고 (보정 에쿼티 - 팟 오즈) >= jam_edge[거리] → 올인 (스택 커밋)
#   bet        : 체크로 넘어오거나 먼저 행동할 때 밸류 벳 최소 에쿼티 [위치][거리]
#   agg        : 직전 라운드 공격자(주도권) 보정 (내가 공격자면 bet 기준 -a, 상대가 공격자면 +b) [거리] = (a, b)
#   cbet       : 플랍 IP + 주도권이면 이 에쿼티 이상은 작은 c-벳 (0 = 레인지 벳)
#   semi       : 강한 드로우(8아웃츠+/콤보) 세미블러프 허용 [위치]
#   bluff      : 리버 에어 블러프 — 에쿼티 <= bluff[위치] 면 벳 (폴라라이즈. 음수 = 끔)
#   commit_spr / commit_eq : 체크 노드에서 SPR < commit_spr[위치] 이고 탑페어+ · 에쿼티 >= commit_eq 면 벳(=올인 커밋)
#   implied / implied_outs : 드로우 내재 배당 콜 — 완성 시 더 받아야 할 금액 <= 뒷스택 × implied (아웃츠 implied_outs 장 이상만)
# 튜닝 전(예전 규칙)과 달라진 핵심: 플랍·턴 콜 여유 +10~14%p(범위)/랜덤은 팟 크기 비례 가산, 랜덤 기준 리버 올인 콜은 에쿼티 90%+,
#   레이즈는 큰 에지에서만, 낮은 SPR 커밋은 콜 후 SPR < 1 에서만(예전 < 2~4), OOP 는 플랍에서 거의 리드 안 함,
#   IP + 주도권 플랍은 레인지 c-벳, 리버 IP 는 에어로 블러프(폴라라이즈), 내재 배당 드로우 콜 없음.
HU_GTO = {
    "range": {
        "pen": {"flop": 0.0, "turn": 0.06, "river": 0.0},
        "call": {"flop": 0.14, "turn": 0.10, "river": 0.0},
        "call_slope": 0.0,
        "pot_ref": 10.0,
        "pot_slope": {"flop": 0.0, "turn": 0.0, "river": 0.0},
        "allin_call": {"flop": -0.05, "turn": -0.02, "river": 0.0},
        "allin_eq": {"flop": 0.0, "turn": 0.0, "river": 0.0},
        "raise_edge": {"flop": 0.50, "turn": 0.45, "river": 0.50},
        "raise_cls": ("monster", "strong", "medium", "weak", "air"),
        "jam_spr": 1.0,
        "jam_edge": {"flop": 0.03, "turn": 0.06, "river": 0.15},
        "bet": {"ip": {"flop": 0.85, "turn": 0.80, "river": 0.78},
                "oop": {"flop": 0.95, "turn": 0.78, "river": 0.75},
                "unk": {"flop": 0.80, "turn": 0.80, "river": 0.78}},
        "agg": {"flop": (0.0, 0.0), "turn": (0.15, 0.15), "river": (0.05, 0.10)},
        "cbet": 0.0,
        "semi": {"ip": True, "oop": False, "unk": True},
        "bluff": {"ip": 0.18, "oop": -1.0, "unk": 0.03},
        "commit_spr": {"ip": 1.0, "oop": 1.0, "unk": 1.0},
        "commit_eq": 0.40,
        "implied": 0.0,
        "implied_outs": 8,
    },
    "random": {
        "pen": {"flop": 0.50, "turn": 0.50, "river": 0.50},
        "call": {"flop": 0.0, "turn": 0.0, "river": 0.03},
        "call_slope": 0.0,
        "pot_ref": 10.0,
        "pot_slope": {"flop": 0.04, "turn": 0.04, "river": 0.06},
        "allin_call": {"flop": 0.0, "turn": 0.08, "river": 0.12},
        "allin_eq": {"flop": 0.60, "turn": 0.85, "river": 0.85},   # 리버 0.85~0.90 은 학습 점수 동률 → 덜 극단적인 쪽
        "raise_edge": {"flop": 0.20, "turn": 0.25, "river": 0.15},
        "raise_cls": ("monster",),
        "jam_spr": 1.0,
        "jam_edge": {"flop": 0.06, "turn": 0.10, "river": 0.30},
        "bet": {"ip": {"flop": 0.85, "turn": 0.80, "river": 0.80},
                "oop": {"flop": 0.95, "turn": 0.85, "river": 0.90},
                "unk": {"flop": 0.85, "turn": 0.85, "river": 0.85}},
        "agg": {"flop": (0.0, 0.0), "turn": (0.0, 0.10), "river": (0.0, 0.10)},
        "cbet": 0.0,
        "semi": {"ip": True, "oop": False, "unk": True},
        "bluff": {"ip": 0.25, "oop": -1.0, "unk": 0.25},
        "commit_spr": {"ip": 1.0, "oop": 1.0, "unk": 1.0},
        "commit_eq": 0.70,
        "implied": 0.0,
        "implied_outs": 15,
    },
}
# 검증 20k (bench_gto.py --n 20000, 시드 20261029) 범주 일치율 agree / 솔버 빈도 점수 share, 튜닝 전 → 후.
#   'min' = hero_ip·aggressor 없이 (라이브 앱 현재 ctx 와 같은 'unk' 열),  'spr<4' = 낮은 SPR 노드 (검증의 68%)
HU_GTO_SCORE = {
    "range": {"agree": (0.628, 0.683), "share": (0.595, 0.653), "agree_min": (0.628, 0.663), "agree_spr<4": (0.651, 0.706)},
    "random": {"agree": (0.578, 0.665), "share": (0.551, 0.637), "agree_min": (0.578, 0.643), "agree_spr<4": (0.583, 0.692)},
}
NUT_K = 5.0            # _adj_equity 바닥: 보정 에쿼티 >= 1 - (1 - 에쿼티) × NUT_K  (에쿼티 0.9 이하는 영향 없음)
ALLIN_EQ_FULL_FRAC = 0.5   # 헤즈업 올인 콜 최소 에쿼티(allin_eq)는 팟의 절반 이상 올인에 전부, 그보다 작은 올인엔 비례해서만
HU_THIN = 0.25         # 상대 프로필(벳에 안 접음 / 루즈) 일 때 헤즈업 밸류 벳 기준을 이만큼 낮춘다 (얇은 밸류: 리버 0.85 → 0.60)
STANDARD_PEN = 0.40     # post_params 표준 pen — 느슨함에 따른 pen 변화분만 HU_GTO['random']['pen'] 위에 더한다


def _hu_table(c):
    return HU_GTO["range" if c.rb else "random"]


def _hu_node(c):
    return "unk" if c.ip is None else ("ip" if c.ip else "oop")


def _hu_margin(T, street, bet_frac, allin, pot_bb=None):
    """헤즈업 콜 필요 승률 가산 (거리·벳 크기·팟 크기별).
    팟이 클수록(벳/레이즈가 여러 번 오간 팟 = 상대 레인지가 강함) pot_slope × log2(팟 / pot_ref) 만큼 더 — 스택 깊이와 무관한 BB 기준."""
    grow = T["pot_slope"][street] * max(0.0, math.log2(pot_bb / T["pot_ref"])) if pot_bb else 0.0
    if allin:
        return T["allin_call"][street]
    return T["call"][street] + T["call_slope"] * (min(bet_frac, 1.5) - 0.5) + grow


STYLE_ALIAS = {"tight": -0.6, "standard": 0.0, "aggressive": 0.6}
STYLE_LABEL = {"tight": "타이트", "standard": "표준", "aggressive": "공격적"}
PUSHFOLD_BB = 15          # 이 이하 유효 스택은 푸시/폴드 (jam/jamcall).  그 위는 깊이에 따라 연속 보정된 일반 레인지
DEFENCE_ITER = 2000        # BB/SB 방어 인라인 에쿼티 반복 수 (내 차례에만 실행, 약 0.2~0.4초)
DEPTH_R = ([15, 20, 25, 35], [0.85, 0.90, 0.95, 1.0])   # 얕은 스택은 에쿼티 실현율이 낮다 (포스트플랍 플레이 여지 부족)
BLIND_R_BASE, BLIND_R_SLOPE, BLIND_R_FLOOR = 0.92, 0.0043, 0.45   # 블라인드 에쿼티 실현율 = 0.92 - 0.0043 × 강도순위 (하한 0.45)
SB_CALL_EXTRA = 0.05       # SB 는 BB 가 뒤에 있어 차트 폴드 → 콜 뒤집기에 여유를 더 둔다
# 프리플랍 차트 + 수학 (범위 기준 에쿼티가 있을 때)
MATH_CALL_MARGIN = 0.02    # 차트 폴드 → 콜 로 뒤집으려면 필요 승률 위로 이 만큼 더
MATH_FOLD_MARGIN = 0.04    # 차트 콜 → 폴드 로 뒤집으려면 필요 승률 아래로 이 만큼 더
MATH_BIG_CALL = 0.05       # 스택의 30% 넘는 콜은 추가 여유
IMPLIED = {"pair": (0.118, 0.22), "ace": (0.06, 0.18), "suited": (0.07, 0.22)}  # (완성 확률, 완성 시 따내는 스택 비율)
IMPLIED_DEPTH = 30         # 내재 배당으로 셈하는 뒷스택 상한 (콜 금액의 배수)
SETMINE_MIN, SPEC_MIN = 15, 20          # 셋마이닝 / 수딧 투기 핸드 콜의 최소 유효스택 배수


# --- 공통 유틸 --------------------------------------------------------------------
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN 제외


def _cards(v):
    out = []
    for c in v or []:
        try:
            out.append(c if isinstance(c, int) else ev.parse_card(str(c)))
        except ValueError:
            return None
    return out if len(set(out)) == len(out) else None


def _bb(x):
    x = round(x, 1)
    return f"{x:.0f}BB" if x == int(x) else f"{x:.1f}BB"


def _pct(x):
    return f"{x * 100:.0f}%"


def _out(action, size=None, size_text=None, reason="", detail=(), confidence="medium", color=None):
    if size is not None:
        size = round(size, 1)
        if size_text is None:
            size_text = _bb(size)
    return {
        "action": action,
        "size_bb": size,
        "size_text": size_text or "",
        "reason": reason,
        "detail": [d for d in detail if d][:3],
        "confidence": confidence,
        "color_key": color or COLOR[action],
        "plan": [],
    }


def _lower(conf):
    return {"high": "medium", "medium": "low", "low": "low"}[conf]


def _range_width(one, many):
    """표시용 상대 레인지 폭 (%) 문자열. 레인지가 없으면 None."""
    rs = [r for r in ([one] if one else []) + list(many or []) if isinstance(r, dict) and r]
    if not rs:
        return None
    return "/".join(f"{rangeq.width_pct(r):.0f}" for r in rs[:3]) + "%"


def _stat(p, key):
    try:
        v = float(p.get(key))
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _prof_note(c, why):
    """'상대 VPIP 42 / PFR 8 (64핸드) — 이유' 형식."""
    p = c.prof
    parts = []
    vpip, pfr = _stat(p, "vpip"), _stat(p, "pfr")
    if vpip is not None:
        parts.append(f"VPIP {vpip:.0f}")
    if pfr is not None:
        parts.append(f"PFR {pfr:.0f}")
    head = "상대 " + (" / ".join(parts) if parts else "성향")
    hands = _stat(p, "hands")
    if hands is not None:
        head += f" ({hands:.0f}핸드)"
    else:
        head += " (표본 충분)"
    return f"{head} — {why}"


def _step_up(frac, label):
    """벳 사이즈를 한 단계 키운다."""
    for f, name in BET_LADDER:
        if f > frac + 1e-6:
            return f, name
    return frac, label


class _Ctx:
    """ctx 정규화."""

    def __init__(self, ctx):
        g = ctx.get
        self.hero = _cards(g("hero"))
        self.board = _cards(g("board")) or []
        n = len(self.board)
        self.street = STREETS.get(n) if n in STREETS else None
        if n == 0 and g("street") in ("flop", "turn", "river"):
            self.street = None  # 보드가 아직 안 읽힘
        players = _num(g("players"))
        self.players = int(min(10, max(6, players))) if players else None
        self.players_given = players
        opp = _num(g("opponents_in_hand"))
        self.opp = max(1, int(opp)) if opp else 1
        self.opp_known = opp is not None
        self.hero_pos = g("hero_pos") or None
        self.situation = g("situation") or None
        self.villain_pos = g("villain_pos") or None
        self.my_stack = _num(g("my_stack"))
        eff = _num(g("stack_bb"))
        self.eff = eff if eff else self.my_stack
        if self.eff and self.my_stack:
            self.eff = min(self.eff, self.my_stack)
        self.pot = _num(g("pot"))
        self.pot_total = _num(g("pot_total"))
        call = _num(g("call"))
        self.call = call if call and call > 0 else 0.0
        self.call_known = call is not None
        mine = self.my_stack or self.eff   # 내 뒷스택 (없으면 유효 스택)
        if mine and self.call > mine:
            # 콜 금액이 내 스택보다 크면 실제로 내는 건 스택까지 — 넘는 상대 벳은 돌려받으므로 팟(화면 POT)에서도 뺀다
            # (안 그러면 숏스택 올인 콜의 팟 오즈가 과대: 4BB 남았는데 44BB 벳 기준으로 필요 승률을 계산했다)
            excess = self.call - mine
            self.call = mine
            if self.pot_total:
                self.pot_total = max(self.call, self.pot_total - excess)
        self.can_check = g("can_check")
        if self.can_check is None and self.call_known:
            self.can_check = self.call == 0
        self.to_act = g("to_act")
        ante = g("ante")
        if isinstance(ante, bool) or ante is None:
            self.ante = bool(ante)
            self.ante_total = 1.0 if ante else 0.0
        else:
            a = _num(ante) or 0.0
            self.ante, self.ante_total = a > 0, a
        eq = _num(g("equity"))
        self.equity = min(1.0, max(0.0, eq)) if eq is not None else None
        self.outs = g("outs")
        self.hand_name = g("hand_name")
        self.facing_allin = g("facing_allin")
        self.closing = bool(g("closing_action"))
        self.rb = g("equity_basis") == "range" and self.equity is not None
        loose = _num(g("looseness"))
        style = g("style")
        if loose is None:
            loose = STYLE_ALIAS.get(style, 0.0)
        self.loose = max(-1.0, min(1.0, loose))
        self.style = next((k for k, v in STYLE_ALIAS.items() if abs(v - self.loose) < 1e-9), None)
        self.post = post_params(self.loose)
        self.closing_given = g("closing_action") is not None
        self.raised = bool(g("villain_raised"))
        self.range_width = _range_width(g("villain_range"), g("villain_ranges"))
        profiles = [g("villain_profile")] + list(g("villain_profiles") or [])
        self.prof = next((p for p in profiles if isinstance(p, dict) and rangeq.profile_reliable(p)), None)
        self.prof_why = None  # 프로필 보정이 실제로 적용되면 이유를 적는다
        hip = g("hero_ip")
        self.ip = bool(hip) if hip is not None else self._derive_ip()
        agg = g("aggressor")
        self.aggressor = None if agg is None else bool(agg)
        self.flop_line = g("flop_line") or None
        self.excess = excess if mine and call and call > mine else 0.0   # 내 스택을 넘는 상대 벳 (돌려받음)
        self.icm = None
        if isinstance(g("tournament"), dict):
            try:  # ICM 은 부가 보정 — 실패하면 칩EV 그대로
                self.icm = _Icm.build(self, g("tournament"), g("table_stacks_bb"), _num(g("villain_stack_bb")))
            except Exception:
                self.icm = None

    def _derive_ip(self):
        """포스트플랍에서 내가 상대보다 늦게 행동하는가 (헤즈업 판단용). 포지션을 모르면 None."""
        if not self.hero_pos or not self.villain_pos or self.hero_pos == self.villain_pos:
            return None
        n = int(self.players) if self.players else None
        for players in ([n] if n else []) + [9, 10, 8, 7, 6]:
            try:
                return pf.postflop_order(self.hero_pos, players) > pf.postflop_order(self.villain_pos, players)
            except (ValueError, KeyError):
                continue
        return None

    def total_pot(self):
        """화면 POT (벳 포함). 없으면 pot + call 로 근사."""
        if self.pot_total:
            return self.pot_total
        if self.pot is not None:
            return self.pot + self.call
        return None


# --- 토너먼트 ICM -------------------------------------------------------------------
ICM_ACTIVE = ("mid", "near_bubble", "bubble", "itm", "final")
ICM_NEED_CAP = 0.85        # ICM 이 올리는 필요 승률 상한 (NUT_K 바닥: 에쿼티 98% 의 보정 에쿼티 >= 90% → 너트는 ICM 때문에 접지 않는다)
ICM_CALL_MILD = 0.5        # 올인 아닌 콜: 위험 프리미엄 × 0.5 × 스택 관여 (0.4 + 1.2 × 콜/내 스택, 최대 1)
ICM_WIDTH_K = 2.0          # 프리플랍 오픈/방어/3벳 폭 배수 = 1 - 2 × 위험 프리미엄
ICM_WIDTH_MIN = 0.55
ICM_BIG = 1.3              # 평균 스택의 1.3배 이상 + 상대를 덮으면 '압박' — 오픈만 조금 넓게
ICM_PRESSURE_MAX = 1.2
ICM_JAM_RANGE = (0.4, 1.35)   # 잼 / 잼콜 차트 폭 배수 범위
ICM_SEMI_RP = 0.03         # 위험 프리미엄이 이 이상이면 콤보 드로우 세미블러프 올인을 안 한다


def _rank_pair(v):
    """'6/32' → (6, 32), 숫자 → (n, None)."""
    if isinstance(v, str) and "/" in v:
        a, b = v.split("/", 1)
        return _num(a.strip()), _num(b.strip())
    return _num(v), None


def _fmt_bf(bf):
    if bf == float("inf") or bf >= 10:
        return "10+"
    return f"{bf:.2f}".rstrip("0").rstrip(".")


class _Icm:
    """한 번의 recommend 동안 쓰는 ICM 모델 (필드 축소 모델 + 캐시된 icm 계산)."""

    @staticmethod
    def build(c, t, table, villain):
        rank, left2 = _rank_pair(t.get("my_rank"))
        left = _num(t.get("players_left")) or left2
        entrants = _num(t.get("entrants"))
        spec = t.get("payouts") or "standard"
        paid = _num(t.get("paid"))
        if isinstance(spec, (list, tuple)):
            spec = [x for x in (_num(v) for v in spec) if x is not None and x > 0]
            paid = len(spec) if spec else paid
            spec = spec or "standard"
        if not paid and entrants:
            paid = icm.paid_places(entrants)
        hero = c.my_stack or c.eff
        if not left or left < 2 or not paid or not hero or hero <= 0:
            return None
        stage = icm.stage_of(int(left), int(paid))
        if stage not in ICM_ACTIVE:
            return None
        pays = icm.as_payouts(spec, int(paid))
        if not pays:
            return None
        return _Icm(c, int(left), int(paid), pays, stage, rank, table, villain, _num(t.get("avg_stack_bb")))

    def __init__(self, c, left, paid, pays, stage, rank, table, villain, avg):
        self.c, self.left, self.paid, self.pays, self.stage, self.rank = c, left, paid, pays, stage, rank
        self.label = icm.STAGE_LABEL[stage]
        self.hero = float(c.my_stack or c.eff)
        hero_key = False
        if isinstance(table, dict):
            others = []
            for k, v in table.items():
                x = _num(v)
                if str(k).lower() in ("hero", "me", "mine"):
                    hero_key = True
                elif x and x > 0:
                    others.append(float(x))
        else:
            others = [float(_num(v)) for v in (table or []) if _num(v) and _num(v) > 0]
        if not hero_key and others:   # 내 자리가 섞여 있으면 my_stack 과 같은 값 하나를 뺀다
            j = min(range(len(others)), key=lambda i: abs(others[i] - self.hero))
            if abs(others[j] - self.hero) <= max(0.06, 0.005 * self.hero):
                others.pop(j)
        self.table = others[: max(0, left - 1)]
        known = [self.hero] + self.table
        self.avg = avg if avg and avg > 0 else sum(known) / len(known)
        self.villain_given = villain if villain is not None and villain >= 0 else None
        self.notes = []
        self._memo = {}

    def _rest(self, v, alt=None):
        """상대를 뺀 나머지 필드: 테이블 (상대 자리 = 남은 스택 alt 또는 전체 스택 v 와 같은 값 하나 제거) + 테이블 밖 평균 스택 그룹."""
        key = ("rest", icm.bucket(v), None if alt is None else icm.bucket(alt))
        if key in self._memo:
            return self._memo[key]
        tab = list(self.table)
        removed = False
        for x in ([alt] if alt else []) + [v]:
            if tab and x and x > 0:
                j = min(range(len(tab)), key=lambda i: abs(tab[i] - x))
                if abs(tab[j] - x) <= max(1.0, 0.05 * x):
                    tab.pop(j)
                    removed = True
                    break
        if not removed and tab and 2 + len(tab) > self.left:   # 인원이 넘치면 상대가 테이블에 있는 것 — 가장 가까운 값을 뺀다
            x = alt if alt else v
            tab.pop(min(range(len(tab)), key=lambda i: abs(tab[i] - x)))
        while tab and 2 + len(tab) > self.left:
            tab.pop()
        pot = self.c.total_pot() or 0.0
        rest = [(s, 1) for s in tab] + icm.rest_of_field(self.left, [self.hero, max(v, 0.01)] + tab, self.avg, pot)
        self._memo[key] = rest
        return rest

    def default_villain(self):
        c = self.c
        if self.villain_given is not None and self.villain_given > 0:
            return self.villain_given
        if c.eff and c.my_stack and c.eff < c.my_stack - 0.5:
            return c.eff
        if self.table:
            s = sorted(self.table)
            return s[len(s) // 2]
        return self.avg

    def allin_villain(self):
        """올인한 상대의 남은 스택 (벳 뒤): 주어진 값, 없으면 0 + 내 스택을 넘어 돌려받는 몫."""
        if self.villain_given is not None:
            return self.villain_given + (self.c.excess or 0.0)
        return self.c.excess or 0.0

    def bf(self, a=None, b=None):
        a = self.hero if a is None else a
        b = self.default_villain() if b is None else b
        key = ("bf", icm.bucket(a), icm.bucket(b))
        if key not in self._memo:
            rest = self._rest(b if a == self.hero else a)
            self._memo[key] = icm.bubble_factor(a, b, rest, self.pays)
        return self._memo[key]

    def rp(self, a=None, b=None):
        return icm.risk_premium(self.bf(a, b))

    def need(self, risk, reward, v_behind, hero=None, v_full=None):
        """올인 필요 승률 dict(icm, chip, bf, ...).  v_behind: 상대 남은 스택 (벳 뒤), v_full: 상대 전체 스택 (BF·필드 매칭용).
        hero 를 주면 그 사람(상대) 입장 — 내 스택이 상대 자리 (v_full = 내 스택)."""
        h = self.hero if hero is None else hero
        risk = min(risk, h)
        if risk <= 0 or reward < 0:
            return None
        vf = v_full if v_full is not None else v_behind + reward
        rest = self._rest(vf, alt=v_behind) if hero is None else self._rest(h)
        d = icm.required_equity_icm(risk, reward, h, v_behind, rest, self.pays, detail=True)
        # 칩EV 아래로는 내리지 않는다 (Harville 에서 1:1 대결은 오목 — 그 아래는 버킷 반올림 잡음), 위로는 ICM_NEED_CAP 까지
        d["icm"] = min(max(d["icm"], d["chip"]), max(ICM_NEED_CAP, d["chip"]))
        d["bf"] = icm.bubble_factor(h, vf, rest, self.pays)
        return d

    def line(self, d):
        return f"ICM: 버블 팩터 {_fmt_bf(d['bf'])} → 필요 승률 {_pct(d['chip'])}→{_pct(d['icm'])} ({self.label})"

    def note(self, text):
        if text and text not in self.notes:
            self.notes.append(text)

    def call_pad(self, call):
        """올인 아닌 콜의 위험 프리미엄 (필요 승률 가산)."""
        if not call or call <= 0:
            return 0.0
        inv = min(1.0, 0.4 + 1.2 * call / self.hero)
        return ICM_CALL_MILD * self.rp() * inv

    def width_mult(self, sit):
        """프리플랍 차트 폭 배수 (sit 별, 1 = 그대로).  (배수, 설명)"""
        key = ("w", sit)
        if key in self._memo:
            return self._memo[key]
        c = self.c
        out = (1.0, None)
        if sit in ("open", "vsopen", "vs3bet", "vs4bet"):
            v = self.default_villain()
            rp_h = self.rp(self.hero, v)
            m = max(ICM_WIDTH_MIN, 1 - ICM_WIDTH_K * rp_h)
            why = f"ICM: {self.label} — 레인지 ×{m:.2f} (버블 팩터 {_fmt_bf(self.bf(self.hero, v))})"
            if sit == "open" and self.hero >= v and self.hero >= ICM_BIG * self.avg:
                rp_v = self.rp(v, self.hero)
                m = min(ICM_PRESSURE_MAX, m + max(0.0, rp_v - rp_h))
                why = f"ICM: {self.label} 빅스택 압박 — 오픈 ×{m:.2f} (상대 버블 팩터 {_fmt_bf(self.bf(v, self.hero))})"
            out = (m, why)
        elif sit == "jam":
            eff = c.eff or self.hero
            v = self.default_villain()
            e = min(eff, self.hero, v)
            dead = c.total_pot() or (1.5 + c.ante_total)
            dh = self.need(e, e + dead, max(0.0, v - e), v_full=v)
            dv = self.need(e, e + dead, max(0.0, self.hero - e), hero=v, v_full=self.hero)
            if dh and dv:
                mh = (dh["chip"] / dh["icm"]) ** 2 if dh["icm"] > 0 else 1.0
                mv = (dv["chip"] / dv["icm"]) ** 2 if dv["icm"] > 0 else 1.0
                m = min(ICM_JAM_RANGE[1], max(ICM_JAM_RANGE[0], mh / math.sqrt(max(mv, 1e-6))))
                out = (m, f"ICM: 잼 폭 ×{m:.2f} (내 버블 팩터 {_fmt_bf(dh['bf'])} / 상대 콜 {_fmt_bf(dv['bf'])}, {self.label})")
        elif sit == "jamcall":
            d = self.facing_allin_need()
            if d:
                m = min(1.0, max(ICM_JAM_RANGE[0], (d["chip"] / d["icm"]) ** 2 if d["icm"] > 0 else 1.0))
                out = (m, self.line(d))
        self._memo[key] = out
        return out

    def facing_allin_need(self, call=None, pot=None):
        c = self.c
        call = c.call if call is None else call
        pot = c.total_pot() if pot is None else pot
        if not call or not pot:
            return None
        v = self.allin_villain()
        return self.need(call, pot, v, v_full=v + pot)


def _icm_facing(c, call, pot_with_bet, allin, base_need):
    """콜의 ICM 가산 (필요 승률). (가산, 표시 줄)  base_need 와 합쳐 ICM_NEED_CAP 을 넘기지 않는다 (원래 더 높으면 그대로)."""
    I = c.icm
    if I is None or not call or call <= 0:
        return 0.0, None
    line = None
    if allin:
        d = I.facing_allin_need(call, pot_with_bet) if pot_with_bet else None
        if not d:
            return 0.0, None
        pad, line = max(0.0, d["icm"] - d["chip"]), I.line(d)
    else:
        pad = I.call_pad(call)
    return min(pad, max(0.0, ICM_NEED_CAP - base_need)), line


def _icm_commit_pad(c, call, pot_with_bet, eff):
    """낮은 SPR 커밋(올인) 의 ICM 가산: 내 남은 스택 전부를 걸고 상대가 나머지를 맞춘다고 본다."""
    I = c.icm
    if I is None or not eff or eff <= 0 or not pot_with_bet:
        return 0.0
    v = I.default_villain()
    more = max(0.0, eff - call)
    d = I.need(eff, pot_with_bet + more, max(0.0, v - more), v_full=v)
    return max(0.0, d["icm"] - d["chip"]) if d else 0.0


def _scale_acts(acts, mult, grow=False):
    """프리플랍 액션 레인지 폭을 mult 배로: 줄일 때는 핸드 순위(G) 아래부터 빼고, grow 면 위 순위부터 첫 액션에 더한다."""
    if abs(mult - 1.0) < 0.01 or not acts:
        return acts
    union = set().union(*(a[1] for a in acts))
    if not union:
        return acts
    rank = {h: i for i, h in enumerate(pf.DATA["G"])}
    total = sum(pf.combos(h) for h in union)
    target = mult * total
    if mult < 1.0:
        keep, n = set(), 0
        for h in sorted(union, key=lambda x: rank.get(x, 999)):
            if keep and n >= target:
                break
            keep.add(h)
            n += pf.combos(h)
        return [(name, hands & keep, col) for name, hands, col in acts]
    if not grow:
        return acts
    add, n = set(), total
    for h in pf.DATA["G"]:
        if n >= target:
            break
        if h not in union:
            add.add(h)
            n += pf.combos(h)
    first = acts[0]
    return [(first[0], first[1] | add, first[2])] + list(acts[1:])


def _pf_ranges(c, sit, pos, villain, players, stack):
    """pf.ranges + ICM 폭 보정 (토너먼트가 아니거나 초반이면 그대로)."""
    acts = pf.ranges(sit, pos, villain, players, stack, c.ante, c.loose)
    if c.icm is None:
        return acts
    m, why = c.icm.width_mult(sit)
    if abs(m - 1.0) < 0.01:
        return acts
    c.icm.note(why)
    return _scale_acts(acts, m, grow=sit in ("open", "jam"))


# --- 진입점 -----------------------------------------------------------------------
def recommend(ctx):
    c = _Ctx(ctx or {})
    if not c.hero or len(c.hero) != 2:
        r = _out(CHECK if c.can_check else FOLD, reason="내 카드 인식 대기 — 판단 보류",
                 detail=["카드 2장이 읽히면 추천합니다"], confidence="low")
    elif c.street is None:
        r = _out(CHECK if c.can_check else FOLD, reason="보드 인식 대기 — 판단 보류",
                 detail=[f"보드 {len(c.board)}장 인식됨"], confidence="low")
    elif c.street == "preflop":
        r = _preflop(c)
    else:
        r = _postflop(c)
        if c.opp >= 2:  # 멀티웨이는 휴리스틱이 약하다 — 신뢰도 하향
            r["confidence"] = _lower(r["confidence"]) if c.opp >= 3 else _cap_medium(r["confidence"])
    if c.hero and len(c.hero) == 2 and c.street is not None:
        try:  # 계획은 부가 정보 — 실패해도 추천은 그대로
            plan = _preflop_plan(c, r) if c.street == "preflop" else r.pop("_plan", None)
            if plan is not None:
                r["plan"] = [p for p in plan if p][:3]
        except Exception:
            r["plan"] = []
    r.pop("_plan", None)
    gto_mode = None
    if c.street in ("flop", "turn") and c.hero and len(c.hero) == 2:
        try:  # GTO 라이브러리는 부가 정보 — 실패해도 규칙 기반 추천은 그대로
            r, gto_mode = _gto_overlay(c, ctx or {}, r)
        except Exception:
            gto_mode = None
    if c.to_act is False and r["action"] != WAIT:
        plan = r["action"] + (f" {r['size_text']}" if r["size_text"] else "")
        r["detail"] = ([f"차례가 오면: {plan} — {r['reason']}"] + r["detail"])[:3]
        r["reason"] = "내 차례 아님 — 대기"
        r["confidence"] = "low"
    if c.opp >= 2 and c.street in ("flop", "turn", "river") and c.hero and len(c.hero) == 2:
        _tag_detail(r, f"상대 {c.opp}명 기준", f"{c.opp}명")
    if c.prof_why and c.street in ("flop", "turn", "river") and gto_mode != "primary":
        _tag_detail(r, _prof_note(c, c.prof_why), "핸드)")
    if c.rb and c.street in ("flop", "turn", "river") and gto_mode != "primary" and             not any("상대 범위" in x for x in r["detail"]):
        _tag_detail(r, "상대 범위 기준", "상대 범위")
    if c.icm is not None and c.icm.notes and c.hero and len(c.hero) == 2 and c.street is not None:
        _tag_detail(r, c.icm.notes[0], "ICM")
    if abs(c.loose) > 1e-9 and c.hero and len(c.hero) == 2 and c.street is not None:
        name = f"{STYLE_LABEL[c.style]} " if c.style else ""
        tail = " — GTO 기준 유지" if gto_mode == "primary" else ""   # GTO 가 주 추천이면 느슨함은 표기만
        _tag_detail(r, f"스타일: {name}(느슨함 {c.loose:+.1f}){tail}", "스타일:")
    return r


# --- GTO 라이브러리 (solverlib) 연동 ------------------------------------------------------
# 헤즈업 플랍(턴 파일이 있으면 턴) 결정에서 미리 푼 솔루션을 조회한다.  라이브러리가 없거나 실패하면 규칙 기반 그대로.
#   주(primary)  : 조회 결과가 믿을 만할 때만 GTO 액션을 추천으로 쓰고, 규칙 기반 판단은 detail 로 내린다.
#   보조(secondary): 그 밖에는 규칙 기반 액션 유지 + detail 에 'GTO 참고' 한 줄, 신뢰도 low.
# 주 조건 (전부): 플랍이 같거나 거리 <= GTO_NEAR_DIST (solverlib 거리: 2 ≈ 하이카드 한 칸)  · 오프너 군(스팟)이 같음
#   · 스택이 풀린 값과 GTO_STACK_TOL BB 이내 · 내 콤보가 풀린 범위 안 · 오래된 레인지가 아님 · solverlib 신뢰도 low 아님
#   · (턴) 플랍 라인을 ctx 로 알고 있고 턴 카드 근사가 없음.   풀린 플랍이 적을 땐 근사 오차가 크니 보수적으로 유지.
GTO_ENABLED = True
GTO_NEAR_DIST = 2.0
GTO_STACK_TOL = 10.0
GTO_TTL = 30.0              # 초: 배치가 라이브러리를 갱신하므로 조회 결과 캐시는 이만큼만
_GTO_CACHE = {}
_GTO_KEYS = ("hero", "board", "street", "hero_pos", "villain_pos", "players", "opponents_in_hand", "situation",
             "stack_bb", "my_stack", "pot", "pot_total", "call", "can_check", "to_act", "villain_raised",
             "flop_line", "facing_allin")


def _hk(v):
    if isinstance(v, (list, tuple)):
        return tuple(_hk(x) for x in v)
    if isinstance(v, float):
        return round(v, 3)
    return v if isinstance(v, (str, int, bool)) or v is None else repr(v)


def gto_cache_clear():
    _GTO_CACHE.clear()


def _gto_lookup(ctx):
    """solverlib.gto_advice(ctx) 결과 (없거나 실패하면 None).  (함수, 핵심 ctx 값) 별로 GTO_TTL 초 캐시 — 조회는 매 호출 같은 입력이다."""
    try:
        import solverlib
        fn = solverlib.gto_advice
    except Exception:
        return None
    key = (fn,) + tuple(_hk(ctx.get(k)) for k in _GTO_KEYS)
    now = time.monotonic()
    hit = _GTO_CACHE.get(key)
    if hit and now - hit[0] < GTO_TTL:
        return hit[1]
    try:
        adv = fn(ctx)
        if not isinstance(adv, dict) or not adv.get("action"):
            adv = None
    except Exception:
        adv = None
    if len(_GTO_CACHE) > 256:
        _GTO_CACHE.clear()
    _GTO_CACHE[key] = (now, adv)
    return adv


def _flop_short(key):
    """'Ks7h2d' -> 'K72r' (r 레인보우 / tt 투톤 / m 모노톤)."""
    try:
        suits = len({key[1], key[3], key[5]})
        return key[0] + key[2] + key[4] + {3: "r", 2: "tt", 1: "m"}[suits]
    except (IndexError, KeyError, TypeError):
        return str(key)


def _gto_primary(c, adv, res):
    """주 추천으로 써도 되는가 (위 조건)."""
    dist = _num(res.get("distance"))
    if not (res.get("exact_flop") is True or (dist is not None and dist <= GTO_NEAR_DIST)):
        return False
    notes = [str(n) for n in res.get("notes") or []]
    if res.get("stale") or adv.get("confidence") == "low":
        return False
    if res.get("exact_spot") is not True and any(m in n for n in notes for m in ("오프너", "스팟", "SB 콜")):
        return False                                   # 다른 오프너 군 / 다른 스팟 파일로 대신 찾은 경우
    if res.get("combo_exact") is not True or any("랭크 근사 충돌" in n for n in notes):
        return False                                   # 내 핸드가 풀린 범위 안이 아님
    eff, used = c.eff, _num(res.get("stack_used"))
    if not eff or used is None:
        return False
    refs = [used] + ([_num(res.get("behind"))] if _num(res.get("behind")) is not None else [])   # 시작 스택 / 플랍 시작 뒷스택
    if min(abs(eff - x) for x in refs) > GTO_STACK_TOL:
        return False
    if c.street == "turn":
        if res.get("street") != "turn" or not c.flop_line or res.get("line") != c.flop_line:
            return False
        if any(m in n for n in notes for m in ("턴 카드 근사", "플랍 라인 추정")):
            return False
    return True


def _gto_overlay(c, ctx, r):
    """(r, 모드).  모드: 'primary' / 'secondary' / None (GTO 조회 없음 → r 그대로).  r 은 직접 고치지 않고 새로 만든다."""
    if not GTO_ENABLED or c.opp != 1 or not c.opp_known or c.to_act is False or r["action"] == WAIT:
        return r, None
    adv = _gto_lookup(ctx)
    if not adv or adv["action"] not in COLOR:
        return r, None
    res = adv.get("gto") if isinstance(adv.get("gto"), dict) else {}
    freq = str(adv.get("reason") or "").replace("GTO:", "", 1).strip()
    if _gto_primary(c, adv, res):
        gd = [d for d in adv.get("detail") or [] if d]
        rule = f"규칙 기반 판단: {r['action']}" + (f" {r['size_text']}" if r["size_text"] else "") + f" — {r['reason']}"
        more = next((d for d in reversed(gd[1:]) if d), None)
        out = _out(adv["action"], adv.get("size_bb"), adv.get("size_text") or "", adv.get("reason") or "GTO",
                   [gd[0] if gd else "솔버 라이브러리", rule, more],
                   adv.get("confidence") if adv.get("confidence") in ("high", "medium", "low") else "medium",
                   adv.get("color_key") or COLOR[adv["action"]])
        out["plan"] = list(r["plan"]) if r["action"] == adv["action"] else []
        return out, "primary"
    r = dict(r, detail=list(r["detail"]), confidence="low")
    exact = res.get("exact_flop") is True
    flop = _flop_short(res.get("mapped_flop"))
    where = f"같은 플랍 {flop}" if exact else f"비슷한 플랍 {flop}"
    if res.get("street") == "turn":
        where += ", 턴"
    _tag_detail(r, f"GTO 참고({where}): {freq}", "GTO 참고")
    return r, "secondary"


def _cap_medium(conf):
    return "medium" if conf == "high" else conf


def _tag_detail(r, text, marker):
    """detail 에 멀티웨이 표기가 없으면 넣는다 (3줄 제한 유지)."""
    d = r["detail"]
    if any(marker in x for x in d):
        return
    if len(d) < 3:
        d.append(text)
    elif d:
        d[-1] = f"{d[-1]} · {text}"
    else:
        d.append(text)


# --- 프리플랍 -----------------------------------------------------------------------
def _resolve_players(c):
    """포지션 이름이 있는 인원 수를 고른다 (6~10)."""
    order = [c.players] if c.players else []
    order += [n for n in (6, 7, 8, 9, 10) if n not in order]
    for n in order:
        if c.hero_pos in pf.seats(n):
            return n
    return None


def _commit_range(c, hand, players, villain, call, eff):
    """큰 금액을 걸어야 할 때 들어갈 범위 (숏스택 잼콜 표 기반). (포함 여부, 범위 %)"""
    depth = min(call, eff) if eff else call
    try:
        b = pf.behind(villain, players) if villain else 2
    except (ValueError, KeyError):
        b = 2
    b = min(9, max(1, b))
    pct = pf._interp(depth, pf.DATA["ts"], pf.DATA["xs"]) * pf.DATA["Bs"][str(b)]
    if depth > pf.SHORT_STACK_BB:
        pct *= pf.SHORT_STACK_BB / depth
    if c.ante:
        pct *= 1.15
    if c.opp > 1:  # 여러 명이 걸려 있으면 더 좁게
        pct *= 0.7 ** (min(c.opp, 3) - 1)
    pct = min(pct, 90)
    return hand in pf.top_pct(pct), pct


def _icm_commit_range(c, hand, ok, pct, call, eff, pot_total, allin_call):
    """큰 금액 승부 범위(_commit_range)를 ICM 필요 승률로 좁힌다: 폭 × (칩 필요/ICM 필요)^2.  (포함 여부, 범위 %, 표시 줄)"""
    I = c.icm
    if I is None or not pot_total or not call:
        return ok, pct, None
    if allin_call:
        d = I.facing_allin_need(min(call, I.hero), pot_total)
    else:   # 잼: 내 유효 스택 전부, 상대가 나머지를 맞춘다
        v = I.default_villain()
        more = max(0.0, eff - call)
        d = I.need(eff, pot_total + more, max(0.0, v - more), v_full=v)
    if not d or d["icm"] <= d["chip"] + 1e-9:
        return ok, pct, (I.line(d) if d else None)
    m = max(ICM_JAM_RANGE[0], (d["chip"] / d["icm"]) ** 2)
    pct2 = pct * m
    I.note(I.line(d))
    return hand in pf.top_pct(pct2), pct2, I.line(d)


def _preflop(c):
    state = {}
    r = _preflop_core(c, state)
    mw = state.get("mw")
    if mw:  # 림퍼/콜러가 있는 다자 팟: 신뢰도 하향 + 표기
        r["confidence"] = _lower(r["confidence"])
        d = r["detail"]
        if not any(mw in x for x in d):
            if len(d) < 3:
                d.append(mw)
            elif len(d) > 1:
                d[1] = f"{d[1]} · {mw}"
            else:
                d[-1] = f"{d[-1]} · {mw}"
    return r


def _offsuit(hand):
    return len(hand) == 3 and hand[2] == "o"


def _speculative(hand):
    """셋마이닝/드로우 가치가 있는 투기 핸드: 'pair' / 'suited' / None"""
    if len(hand) == 2:
        return "pair"
    if hand[2] != "s":
        return None
    i, j = pf.RANKS.index(hand[0]), pf.RANKS.index(hand[1])
    if hand[0] == "A" or (max(i, j) <= 9 and j - i <= 2):  # 수딧 에이스, 5x 이상 수딧 커넥터/한 칸
        return "suited"
    return None


def _offsuit_pct(limpers):
    return OFFSUIT_ISO_PCT.get(limpers, 5.5)


def _vs3bet_shape(pot_total, dead, call, pos):
    """3벳 상대에 콜러(콜드콜)가 끼었는지. (콜러 수, 3벳 금액)  내 오픈 크기를 2.5BB 근처로 보고 역산."""
    if not pot_total:
        return 0, None
    t0 = (pot_total - dead + call) / 2
    h0 = t0 - call
    if 1.9 <= h0 <= 4.5:
        return 0, t0
    h = 3.0 if pos == "SB" else 2.5
    t = h + call
    n = max(0, round((pot_total - dead - h - t) / t))
    if n:
        return n, t
    return 0, (t0 if h0 > 0 else None)


def _mw_adjust(state, sit, hand, name, pos, call, eff, limpers, callers):
    """preflop.ranges 결과(name)를 다자 팟 규칙으로 보정. (name, 메모, 투기콜 여부)"""
    pre = pf.top_pct(PREMIUM_OFFSUIT_PCT)
    spec = _speculative(hand)
    ratio = (eff / max(call, 1.0)) if eff else None
    if sit == "open" and limpers:
        state["mw"] = f"다자 팟: 림퍼 {limpers}명"
        if name == "오픈 레이즈" and _offsuit(hand) and hand not in pf.top_pct(_offsuit_pct(limpers)):
            return "폴드", f"림퍼 {limpers}명: 오프수트는 상위 {_offsuit_pct(limpers):g}% 만 레이즈", False
        if name == "폴드" and limpers >= 2 and spec and ratio and ratio >= SPEC_MIN:
            return "콜", f"림퍼 {limpers}명: 투기 핸드 림프 합류 (유효 {ratio:.0f}배)", True
    elif sit == "vsopen" and callers:
        state["mw"] = f"다자 팟: 오픈 + 콜러 {callers}명"
        off = _offsuit(hand) and hand not in pre
        if name == "3벳" and off:
            return "콜", f"콜러 {callers}명: 오프수트 3벳 → 콜로 하향", False
        if name == "콜" and off and pos != "BB":
            return "폴드", f"콜러 {callers}명: 오프수트 브로드웨이 가치 하락 → 폴드", False
        if name == "폴드" and spec and ratio:
            if spec == "pair" and ratio >= SETMINE_MIN:
                tail = "≥ 20배)" if ratio >= SPEC_MIN else "≥ 15배, 빠듯)"
                return "콜", f"콜러 {callers}명: 셋마이닝 (유효 {ratio:.0f}배 {tail}", True
            if spec == "suited" and callers >= 2 and ratio >= SPEC_MIN and pos != "SB":
                return "콜", f"콜러 {callers}명: 수딧 드로우/내재 배당 (유효 {ratio:.0f}배)", True
    elif sit == "vs3bet" and callers:
        state["mw"] = f"다자 팟: 3벳 + 콜드콜 {callers}명"
        if name == "4벳" and hand not in VALUE_4BET:
            if len(hand) == 2:
                return "콜", f"콜드콜러 {callers}명: 블러프 4벳 불가 → 포켓페어는 콜", False
            return "폴드", f"콜드콜러 {callers}명: 블러프 4벳은 폴드를 못 만듦 → 폴드", False
        if name == "콜" and _offsuit(hand) and hand not in pre:
            return "폴드", f"콜드콜러 {callers}명: 오프수트 콜 가치 하락 → 폴드", False
    return name, None, False


def _realization(hand, strength, pos, allin):
    """에쿼티 실현율: 올인이면 100%, 아니면 핸드 질/포지션에 따라 (포켓페어는 셋 못 맞히면 접게 되어 낮다)."""
    if allin:
        return 1.0
    if len(hand) == 2 and strength > 6:
        return 0.62
    if pos in ("SB", "BB") and len(hand) == 3:  # 블라인드(OOP)에서 약한 핸드는 에쿼티 실현이 훨씬 낮다 (강도에 따라 선형으로 하락)
        base = max(BLIND_R_FLOOR, BLIND_R_BASE - BLIND_R_SLOPE * strength)
        return min(base + (0.03 if hand.endswith("s") else 0.0), 0.97)
    base = 0.95 if strength <= 35 else 0.90 if strength <= 85 else 0.80
    if pos in ("SB", "BB"):
        base -= 0.07
    if hand.endswith("s"):
        base += 0.03
    return min(base, 0.97)


def _defence_key(c):
    p = c.prof
    return None if not p else tuple(p.get(k) for k in ("vpip", "pfr", "threebet", "hands", "reliable"))


@lru_cache(maxsize=512)
def _defence_equity_cached(hero, villain, players, ante, callers, pkey):
    prof = dict(zip(("vpip", "pfr", "threebet", "hands", "reliable"), pkey)) if pkey else None
    opener = rangeq.villain_range("open", villain, players, 100.0, prof, ante=ante)
    ranges = [opener] + [rangeq.villain_range("call_open", None, players, 100.0)] * min(2, callers)
    rng = random.Random(sum((x + 1) * (31 + 7 * i) for i, x in enumerate(sorted(hero))) + 12345)
    return rangeq.equity_vs_ranges(list(hero), [], ranges, DEFENCE_ITER, rng)["equity"]


def _defence_equity(c, villain, players, eff, callers):
    """BB/SB 방어용: 오프너(와 콜러) 레인지 대비 내 에쿼티를 직접 계산 (결정적: 고정 시드 + 캐시)."""
    try:
        return _defence_equity_cached(tuple(c.hero), villain, players, bool(c.ante), min(2, callers), _defence_key(c))
    except (ValueError, KeyError, IndexError):
        return None


def _preflop_math(c, hand, strength, name, pos, players, call, eff, pot_total, callers, eq=None):
    """차트 판단을 팟 오즈·범위 에쿼티·내재 배당·SPR 로 검증. dict(verdict, lines, ...) 반환.
    verdict: 'call' / 'fold' / None(차트 유지).  eq: 직접 계산한 상대 레인지 에쿼티 (없으면 c.equity)."""
    eq = c.equity if eq is None else eq
    call_margin = MATH_CALL_MARGIN + c.post["math_margin"] + (SB_CALL_EXTRA if pos == "SB" else 0.0)
    need = call / (pot_total + call)
    allin = bool(c.facing_allin) or (eff is not None and call >= 0.95 * eff) or         (c.my_stack is not None and call >= c.my_stack)
    spec = _speculative(hand)
    R = _realization(hand, strength, pos, allin)
    if not allin and eff:
        R *= pf._interp(eff, *DEPTH_R)
    pad = 0.0
    if not allin and not c.closing and pos not in (None, "BB"):
        pad = min(MW_BEHIND_CAP, MW_BEHIND_STEP * pf.behind(pos, players))
    imp, ratio = 0.0, (eff / call if eff and call else None)
    if spec and not allin and eff and eff > call:
        kind = "pair" if spec == "pair" else ("ace" if hand[0] == "A" else "suited")
        p, f = IMPLIED[kind]
        mult = min(2.0, 1 + 0.35 * callers)  # 상대가 많을수록 완성 시 팟이 커진다
        imp = p * f * mult * min(eff - call, IMPLIED_DEPTH * call) / (pot_total + call)
    mine = eq * R
    req = need + pad - imp
    icm_line, need_a = None, need   # need_a: 올인 필요 승률 (ICM 이면 ICM 필요 승률)
    if c.icm is not None:
        ipad, icm_line = _icm_facing(c, call, pot_total, allin, need if allin else req)
        if allin:
            need_a = need + ipad
        elif ipad > 0:
            req += ipad
            icm_line = f"ICM {c.icm.label}: 위험 프리미엄 +{ipad * 100:.0f}%p"
    big = bool(eff) and call > PRESSURE * eff and not allin
    verdict = None
    if allin:
        verdict = "call" if eq >= need_a + 0.005 else "fold"   # 올인은 차트가 아니라 수학으로만
    elif name == "폴드":
        if mine >= req + call_margin + (MATH_BIG_CALL if big else 0.0):
            verdict = "call"
    elif name == "콜":
        if mine < req - MATH_FOLD_MARGIN - c.post["math_margin"]:
            verdict = "fold"
    spr = (eff - call) / (pot_total + call) if eff and eff > call else None
    cmp_ = "<" if (eq >= need_a if allin else mine >= req) else ">"
    eqtxt = f"범위 대비 승률 {_pct(eq)}" + (f"→실현 {_pct(mine)}" if R < 1 else "")
    need_txt = f"필요 {_pct(need_a if allin else req)}"
    if verdict == "call":
        head_line = f"차트: {name} → {'올인도 수학으로: ' if allin else ''}팟 오즈로 콜 ({need_txt} {cmp_} {eqtxt})"
    elif verdict == "fold":
        head_line = f"차트: {name} → {'올인도 수학으로: ' if allin else ''}배당 부족 → 폴드 ({need_txt} {cmp_} {eqtxt})"
    else:
        ok = "✓ 일치" if (cmp_ == "<") == (name != "폴드") else "(근소 차이로 차트 유지)"
        head_line = f"차트: {name} {ok} ({need_txt} {cmp_} {eqtxt})"
    if spec == "pair" and ratio and not allin:
        second = f"셋마이닝 배당 {ratio:.0f}배 {'✓' if ratio >= SETMINE_MIN else '✗'} / 내재 배당 " +             (f"충분(+{imp * 100:.0f}%p)" if imp >= 0.02 else "부족")
    elif spec and ratio and not allin:
        second = f"내재 배당 {ratio:.0f}배 · " + (f"+{imp * 100:.0f}%p 반영" if imp >= 0.01 else "부족") +             (f" · 상대 {callers + 1}명" if callers else "")
    else:
        second = f"팟 오즈 {_pct(need)} (콜 {_bb(call)} / 최종 팟 {_bb(pot_total + call)})"
    if spr is not None and not allin:
        second += f" · 콜 후 SPR {spr:.1f}" + (" → 플랍 커밋 수준" if spr < 3 else "")
    if icm_line:
        second = icm_line if allin else f"{second} · {icm_line}"
    return {"verdict": verdict, "lines": [head_line, second], "allin": allin, "mine": mine, "req": req,
            "need_a": need_a, "margin": (eq - need_a) if allin else (mine - req)}


def _preflop_core(c, state):
    hand = pf.hand_code(c.hero)
    strength = pf.DATA["G"].index(hand) + 1
    players = _resolve_players(c) if c.hero_pos else None
    pos = c.hero_pos if players else None
    posted = {"SB": 0.5, "BB": 1.0}.get(pos, 0.0)
    call, eff = c.call, c.eff
    dead = 1.5 + c.ante_total
    pot_total = c.total_pot()
    conf = "high"
    head = f"{hand} (강도 {strength}/169)"

    unopened = call + posted <= 1.0 + 1e-6
    sit = c.situation if c.situation in DEEP_SITUATIONS + SHORT_SITUATIONS else None
    if sit is None:
        sit = "open" if unopened else ("vsopen" if not c.facing_allin else "jamcall")
        conf = "medium"
    elif sit in ("open", "jam") and not unopened and call + posted >= 2.0:
        sit, conf = "vsopen", "medium"  # 상황 정보가 늦게 갱신된 경우
    short = eff is not None and eff <= PUSHFOLD_BB
    if short and sit in TO_SHORT:
        sit = TO_SHORT[sit]
    elif not short and sit in TO_DEEP:
        sit = TO_DEEP[sit]
    if eff is None:
        conf = _lower(conf)

    # BB 에서 체크 가능 (림프 팟 / SB 컴플리트)
    if (pos == "BB" or (pos is None and c.can_check)) and unopened and (c.can_check or call == 0):
        limpers = _limpers(pot_total, dead)
        if limpers:
            state["mw"] = f"다자 팟: 림퍼 {limpers}명"
        strong_hand = hand in pf.top_pct(10) and not (_offsuit(hand) and hand not in pf.top_pct(_offsuit_pct(limpers)))
        if strong_hand:
            size = 3.0 + limpers
            if eff and size >= COMMIT_SIZE * eff:
                return _out(ALLIN, eff, _bb(eff), "림프 팟 + 강한 핸드 + 짧은 스택 → 올인",
                            [head, f"유효 {_bb(eff)}"], conf)
            return _out(RAISE, size, None, "림프 팟에서 강한 핸드 → 아이솔레이션 레이즈",
                        [head, f"3BB + 림퍼 {limpers}명 × 1BB" if limpers else "3BB 기준"], conf)
        return _out(CHECK, reason="BB 체크 옵션 — 공짜로 플랍 보기",
                    detail=[head, f"림퍼 {limpers}명: 오프수트는 상위 {_offsuit_pct(limpers):g}% 만 레이즈" if limpers
                            else "상위 10% 핸드만 레이즈"], confidence=conf)

    if pos is None or pos not in pf.valid_heroes(sit, players):
        return _preflop_generic(c, hand, strength, unopened, head)

    villain = None
    villains = pf.valid_villains(sit, pos, players)
    if villains:
        if c.villain_pos in villains:
            villain = c.villain_pos
        else:
            villain = villains[0] if sit == "vs3bet" else villains[len(villains) // 2]
            conf = _lower(conf)
    acts = _pf_ranges(c, sit, pos, villain, players, eff or 100.0)
    name, _ = pf.decide(hand, acts)
    vs = f" vs {villain}" if villain else ""
    where = f"{players}인 {pos}{vs}"
    limpers = _limpers(pot_total, dead) if sit == "open" else 0
    callers, three_to = 0, None
    if sit == "vsopen":
        callers = _callers(pot_total, dead, call + posted)
    elif sit == "vs3bet":
        callers, three_to = _vs3bet_shape(pot_total, dead, call, pos)
    name, mw_note, spec_call = _mw_adjust(state, sit, hand, name, pos, call, eff, limpers, callers)
    math = None
    eq_in = None
    if (not c.rb and sit == "vsopen" and pos in ("BB", "SB") and villain and name in ("콜", "폴드") and call > 0
            and pot_total and not unopened and not c.facing_allin and eff and call <= PRESSURE * eff):
        eq_in = _defence_equity(c, villain, players, eff, callers)   # 범위 에쿼티가 안 들어와도 BB/SB 방어는 팟 오즈로 검증
    if eq_in is not None or (c.rb and c.equity is not None and call > 0 and pot_total and not unopened
                             and sit in ("vsopen", "vs3bet", "vs4bet", "jamcall")):
        math = _preflop_math(c, hand, strength, name, pos, players, call, eff, pot_total, callers, eq_in)
        state["math"] = math
        v = math["verdict"]
        if v:
            conf2 = conf if abs(math["margin"]) >= 0.05 else _lower(conf)
            info = [head] + math["lines"]
            if v == "call":
                size = min(call, c.my_stack) if c.my_stack else call
                allin_txt = f"{_bb(size)} (올인 콜)" if math["allin"] else None
                why = "올인: 범위 승률 ≥ 필요 승률 → 콜" if math["allin"] else                     f"팟 오즈 충족 (필요 {_pct(math['req'])} < 승률 {_pct(math['mine'])}) → 콜"
                return _out(CALL, size, allin_txt, f"{where}: {why}", info, conf2)
            why = "올인: 범위 승률 < 필요 승률 → 폴드" if math["allin"] else                 f"배당 부족 (필요 {_pct(math['req'])} > 승률 {_pct(math['mine'])}) → 폴드"
            return _out(FOLD, reason=f"{where}: {why}", detail=info, confidence=conf2)
    if spec_call:
        info = [mw_note] + math["lines"] if math else [head, mw_note, _call_odds(call, pot_total)]
        return _out(CALL, call, None, f"{where}: 다자 팟 — 투기 핸드 콜", info, _lower(conf))

    # 큰 금액 압박: 콜 범위라도 잼 또는 폴드
    allin_call = bool(c.facing_allin) or (eff is not None and call >= 0.95 * eff)
    if sit in ("vsopen", "vs3bet", "vs4bet") and eff and call > PRESSURE * eff and name != "폴드":
        ok, pct = _commit_range(c, hand, players, villain, call, eff)
        need = call / ((pot_total or call) + call)
        ok, pct, icm_line = _icm_commit_range(c, hand, ok, pct, call, eff, pot_total, allin_call)
        info = [head, icm_line or f"콜 {_bb(call)} = 유효 스택의 {_pct(call / eff)}",
                f"상위 {pct:.0f}% 만 승부 · 필요 승률 {_pct(need)}" + (f" · 상대 {c.opp}명 기준" if c.opp > 1 else "")]
        if not ok:
            return _out(FOLD, reason=f"{where}: 큰 금액 — 승부 범위 밖이라 폴드", detail=info, confidence=conf)
        if allin_call:
            return _out(CALL, min(call, c.my_stack or call), f"{_bb(min(call, c.my_stack or call))} (올인 콜)",
                        f"{where}: 승부 범위 안 → 올인 콜", info, conf)
        return _out(ALLIN, eff, _bb(eff), f"{where}: 콜하면 스택이 묶임 → 잼", info, conf)

    if name == "폴드":
        if sit == "jamcall" or allin_call:
            need = call / ((pot_total or call) + call) if call else 0
            d = c.icm.facing_allin_need(call, pot_total) if c.icm is not None and call and pot_total else None
            detail = [head, (c.icm.line(d) if d else f"필요 승률 {_pct(need)}") if call else None]
        else:
            detail = [head, mw_note or f"{where} 레인지 밖"]
        why = "다자 팟 — 가치 하락" if mw_note else "레인지 밖"
        if c.can_check:
            return _out(CHECK, reason=f"{where}: {why} — 체크", detail=detail, confidence=conf)
        return _out(FOLD, reason=f"{where}: {why} → 폴드", detail=detail, confidence=conf)

    if sit == "open":
        base = 3.0 if pos == "SB" else (2.2 if c.ante and pos in ("CO", "BTN") else 2.5)
        size = base + limpers
        why = f"{_bb(base)} 기준" + (f" + 림퍼 {limpers}명 × 1BB" if limpers else "")
        return _raise_or_jam(size, eff, f"{where} 첫 진입 오픈 레인지 → 레이즈", [head, why], conf)
    if sit == "vsopen":
        if name == "콜":
            return _out(CALL, call, None, f"{where} 오픈 → 콜 레인지",
                        [head] + math["lines"] if math else [head, _call_odds(call, pot_total), _setmine(c, call)], conf)
        r_open = call + posted
        ip = pos not in ("SB", "BB")
        mult = 3.0 if ip else (4.0 if pos == "SB" else 3.5)
        size = r_open * mult + r_open * callers
        why = f"오픈 {_bb(r_open)} × {mult:g}" + (f" + 콜러 {callers}명" if callers else "") + (" (IP)" if ip else " (OOP)")
        return _raise_or_jam(size, eff, f"{where} 오픈 → 3벳 레인지", [head, why], conf, _commit_frac(eff))
    if sit == "vs3bet":
        if name == "콜":
            return _out(CALL, call, None, f"{where} 3벳 → 콜 레인지",
                        [head] + math["lines"] if math else [head, _call_odds(call, pot_total)], conf)
        three = three_to or call + (3.0 if pos == "SB" else 2.5)
        ip = pf.postflop_order(pos, players) > pf.postflop_order(villain, players)
        mult = 2.2 if ip else 2.5
        return _raise_or_jam(three * mult + three * callers, eff, f"{where} 3벳 → 4벳 레인지",
                             [head, f"3벳 {_bb(three)} × {mult:g}" + (f" + 콜드콜러 {callers}명" if callers else "")
                              + (" (IP)" if ip else " (OOP)")], conf, _commit_frac(eff))
    if sit == "vs4bet":
        if name == "콜":
            return _out(CALL, call, None, f"{where} 4벳 → 콜 레인지", [head, _call_odds(call, pot_total)], conf)
        size = eff or None
        return _out(ALLIN, size, _bb(size) if size else "스택 전부", "4벳에 5벳 잼 레인지 → 올인", [head], conf)
    if sit == "jam":
        size = eff or c.my_stack
        return _out(ALLIN, size, _bb(size) if size else "스택 전부",
                    f"숏스택 {where}: 첫 진입 잼 레인지 → 올인", [head, f"유효 {_bb(eff)} ≤ {pf.SHORT_STACK_BB}BB"], conf)
    # jamcall
    size = min(call, c.my_stack) if c.my_stack and call else call or None
    return _out(CALL, size, (f"{_bb(size)} (올인 콜)" if size else "올인 콜"), f"숏스택 {where}: 잼 콜 레인지 → 콜",
                [head, _call_odds(call, pot_total) if call else None], conf)


def _limpers(pot_total, dead):
    if not pot_total:
        return 0
    return max(0, int((pot_total - dead + 0.25) // 1.0))


def _callers(pot_total, dead, r_open):
    if not pot_total or r_open <= 0:
        return 0
    return max(0, int((pot_total - dead - r_open) / r_open + 0.3))


def _call_odds(call, pot_total):
    if not call or not pot_total:
        return None
    return f"필요 승률 {_pct(call / (pot_total + call))} (콜 {_bb(call)} / 최종 팟 {_bb(pot_total + call)})"


def _setmine(c, call):
    if c.hero[0] >> 2 != c.hero[1] >> 2 or not c.eff or not call:
        return None
    ratio = odds.set_mining_ratio(c.eff, call)
    return f"셋마이닝 {ratio:.0f}배 " + ("(15배 이상 OK)" if ratio >= 15 else "(15배 미만 — 부족)")


def _commit_frac(eff):
    return pf._interp(eff, *COMMIT_ANCHORS) if eff else COMMIT_SIZE


def _raise_or_jam(size, eff, reason, detail, conf, commit=COMMIT_SIZE):
    if eff and size >= commit * eff:
        return _out(ALLIN, eff, _bb(eff), reason.replace("레이즈", "올인") + " (사이즈가 스택의 큰 비중)",
                    detail + [f"레이즈 {_bb(size)} ≥ 스택 {_pct(commit)}"], conf, "jam")
    return _out(RAISE, size, None, reason, detail, conf)


def _preflop_generic(c, hand, strength, unopened, head):
    """포지션을 모를 때: 핸드 강도 순위만으로 대충 판단."""
    call, eff = c.call, c.eff
    pot_total = c.total_pot()
    detail = [head, "포지션 정보 없음 — 강도 순위 기준 추정"]
    if unopened:
        if hand in pf.top_pct(15):
            return _raise_or_jam(2.5, eff, "첫 진입 · 상위 15% 핸드 → 레이즈", detail, "low")
        if c.can_check:
            return _out(CHECK, reason="레인지 밖 — 체크", detail=detail, confidence="low")
        return _out(FOLD, reason="첫 진입 · 상위 15% 밖 → 폴드", detail=detail, confidence="low")
    if eff and call > PRESSURE * eff:
        ok, pct = _commit_range(c, hand, c.players or 9, None, call, eff)
        ok, pct, icm_line = _icm_commit_range(c, hand, ok, pct, call, eff, pot_total,
                                              bool(c.facing_allin) or call >= 0.95 * eff)
        detail.append(f"상위 {pct:.0f}% 만 승부 · 콜 {_bb(call)}" + (f" · {icm_line}" if icm_line else ""))
        if not ok:
            return _out(FOLD, reason="큰 금액 — 승부 범위 밖이라 폴드", detail=detail, confidence="low")
        if c.facing_allin or call >= 0.95 * eff:
            return _out(CALL, call, f"{_bb(call)} (올인 콜)", "승부 범위 안 → 올인 콜", detail, "low")
        return _out(ALLIN, eff, _bb(eff), "승부 범위 안 → 잼", detail, "low")
    if hand in pf.top_pct(4):
        r_open = call
        return _raise_or_jam(r_open * 3.5, eff, "상대 레이즈 · 상위 4% 핸드 → 레이즈", detail, "low")
    if hand in pf.top_pct(12):
        return _out(CALL, call, None, "상대 레이즈 · 상위 12% 핸드 → 콜", detail + [_call_odds(call, pot_total)], "low")
    return _out(FOLD, reason="상대 레이즈 · 상위 12% 밖 → 폴드", detail=detail, confidence="low")


# --- 포스트플랍 ----------------------------------------------------------------------
def _texture(board):
    ranks = sorted({c >> 2 for c in board})
    suits = [0] * 4
    for c in board:
        suits[c & 3] += 1
    max_suit = max(suits)
    span_ranks = set(ranks) | ({-1} if 12 in ranks else set())  # A 는 휠의 1로도
    most_in5 = max(sum(1 for r in span_ranks if lo <= r <= lo + 4) for lo in range(-1, 9))
    paired = len(ranks) < len(board)
    flush3 = max_suit >= 3
    straight3 = most_in5 >= 3
    connected = any(0 < b - a <= 2 for a, b in zip(ranks, ranks[1:]))
    scary = flush3 or straight3 or most_in5 >= 4
    river = len(board) == 5
    wet = scary or (not river and (max_suit == 2 or connected))
    return {"paired": paired, "flush3": flush3, "straight3": straight3, "scary": scary, "wet": wet,
            "four_flush": max_suit >= 4}


def _classify(hero, board):
    """(등급, 설명, 탑페어 이상 여부, 투페어 이상 여부)  등급: monster/strong/medium/weak/air"""
    score = ev.evaluate(hero + board)
    cat = score[0]
    bscore = ev.evaluate(board)
    bcat = bscore[0]
    tex = _texture(board)
    hr = sorted((c >> 2 for c in hero), reverse=True)
    br = sorted({c >> 2 for c in board}, reverse=True)
    if len(board) == 5 and score == bscore:
        return "weak", "보드 플레이", False, False
    if cat >= 7:
        return "monster", "포카드+", True, True
    if cat == 6:
        if bcat >= 6 or (bcat == 3 and max(hr) < Q_RANK and hr[0] != hr[1]):
            return "medium", "보드 풀하우스 계열", True, False
        return "monster", "풀하우스", True, True
    if cat == 5:
        suit = max(range(4), key=lambda s: sum(1 for c in board if c & 3 == s))
        mine = [c >> 2 for c in hero if c & 3 == suit]
        if max(mine, default=-1) >= 11 or len(mine) == 2 and not tex["four_flush"]:
            return ("strong" if tex["paired"] else "monster"), "플러시", True, True
        return ("medium" if tex["four_flush"] else "strong"), "낮은 플러시", True, True
    if cat == 4:
        if tex["flush3"] or tex["paired"]:
            return "strong", "스트레이트 (위험 보드)", True, True
        return "monster", "스트레이트", True, True
    if cat == 3:
        if hr[0] == hr[1]:
            return "monster", "셋", True, True
        if bcat == 3:
            return ("medium" if hr[0] >= 11 else "weak"), "보드 트리플", False, False
        return "strong", "트립스", True, True
    pair = hr[0] if hr[0] == hr[1] else max((r for r in hr if r in br), default=None)
    if cat == 2 and bcat < 1:
        top_two = set(hr) == set(br[:2])
        return ("monster" if top_two and not tex["scary"] else "strong"), "투페어", True, True
    if cat == 2 and bcat == 2:
        if hr[0] == hr[1] and hr[0] > br[-1]:
            return "medium", "투페어 (보드 페어)", False, False
        return "weak", "보드 투페어", False, False
    if pair is None:
        if hr[0] > br[0]:
            return "air", "오버카드", False, False
        return "air", "하이카드", False, False
    if hr[0] == hr[1]:
        if pair > br[0]:
            return ("strong" if pair >= T_RANK else "medium"), "오버페어", True, False
        if len(br) > 1 and pair > br[1]:
            return "medium", "포켓 세컨드 페어", False, False
        return "weak", "언더페어", False, False
    kicker = hr[0] if hr[1] == pair else hr[1]
    if pair == br[0]:
        good = kicker >= Q_RANK or (pair == 12 and kicker >= J_RANK)
        return ("strong" if good else "medium"), ("탑페어 좋은 키커" if good else "탑페어"), True, False
    if len(br) > 1 and pair == br[1]:
        return "weak" if len(board) == 5 else "medium", "세컨드 페어", False, False
    return "weak", "바텀 페어", False, False


def _outs(c):
    if c.outs:
        return c.outs
    if len(c.board) not in (3, 4):
        return None
    d = ev.draws(c.hero, c.board)
    n, left = len(d["outs"]), 5 - len(c.board)
    return {"groups": d["groups"], "count": n, "combo": d["combo"], "left": left,
            "next": ev.hit_probability(n, d["deck"], 1), "by_river": ev.hit_probability(n, d["deck"], left)}


def _nut_flush_draw(hero, board):
    """플러시 드로우이고 그 무늬의 보드에 없는 가장 높은 카드를 내가 들고 있는가."""
    cards = hero + board
    for s in range(4):
        if sum(1 for x in cards if x & 3 == s) == 4 and any(h & 3 == s for h in hero):
            used = {x >> 2 for x in board if x & 3 == s}
            top = next(r for r in range(12, -1, -1) if r not in used)
            return any(h & 3 == s and h >> 2 == top for h in hero)
    return False


def _mw_discount(c):
    """멀티웨이 에쿼티 할인율: 상대가 늘수록 랜덤 핸드 가정이 우리 에쿼티를 과대평가한다."""
    sp = c.post
    return min(sp["disc_cap"], sp["disc_step"] * max(0, c.opp - 1))


def _onepair_discount(c, cls, made, twopair_plus, river, bet_frac=1.0):
    """원페어 계열은 다자 팟 벳에 더 취약: 좋은 키커/오버페어 상대 1명 추가당 10%, 약한 원페어 25%.
    리버는 드로우가 없어 벳이 밸류 위주 → 2배. 최대 40%. (벳 크기에 비례: 팟의 3/4 이상이면 전부 적용)"""
    if c.opp < 2 or twopair_plus or cls not in ("strong", "medium", "weak") or "페어" not in made:
        return 0.0
    step = ONEPAIR_STEP_STRONG if cls == "strong" else ONEPAIR_STEP_WEAK
    return min(0.40, step * (c.opp - 1) * (2 if river else 1)) * min(1.0, bet_frac / 0.75)


def _behind_pad(c, allin):
    """뒤에 아직 안 한 사람이 레이즈할 위험만큼 필요 승률을 올린다.
    뒤에 사람이 실제로 있을 때만: 마지막 액션(closing_action)이거나 올인 콜이면 0.  closing_action 을 모르면 포지션으로 추정 —
    BTN 은 포스트플랍에서 마지막이라 뒤에 아무도 없다."""
    if c.opp <= 1 or allin or c.closing:
        return 0.0
    if not c.closing_given and c.hero_pos == "BTN":
        return 0.0
    step, cap = c.post["behind"]
    return min(cap, step * (c.opp - 1))


def _postflop(c):
    cls, made, tp_plus, twopair_plus = _classify(c.hero, c.board)
    tex = _texture(c.board)
    river = c.street == "river"
    name = c.hand_name or ev.hand_name(ev.evaluate(c.hero + c.board))
    outs = None if river else _outs(c)
    n_outs = outs.get("count", 0) if outs else 0
    combo = bool(outs and outs.get("combo"))
    strong_draw = combo or n_outs >= c.post["semi_outs"]
    nut_draw = (combo or (not river and _nut_flush_draw(c.hero, c.board))) and n_outs > 0
    overpair = made.startswith("오버페어")
    draw_names = [g[0].split(" (")[0] for g in (outs or {}).get("groups") or []
                  if "드로우" in g[0] or "것샷" in g[0]]
    if draw_names and cls in ("air", "weak"):
        made = f"{made} + {'/'.join(draw_names)}"
    # 랜덤 핸드 기준 에쿼티를 등급과 섞어 보정
    if c.equity is not None:
        eq = c.equity
        if c.rb:  # 상대 레인지 기준: 에쿼티가 이미 레인지 강도를 반영 → 등급 보정 눈높이를 낮춘다
            eq_src = f"에쿼티 {_pct(eq)} (상대 범위 기준" + (f" · 범위 {c.range_width}" if c.range_width else "") + ")"
            up, down = 0.75, max(0.20, 0.30 - 0.03 * (c.opp - 1))
        else:
            eq_src = f"에쿼티 {_pct(eq)} (" + (f"상대 {c.opp}명 랜덤 기준" if c.opp >= 2 else f"랜덤 {c.opp}명 기준 추정") + ")"
            up, down = 0.85, max(0.25, 0.40 - 0.04 * (c.opp - 1))
        if eq >= up and cls in ("medium", "weak"):
            cls = "strong"
        elif eq < down and cls == "medium":  # 상대가 많으면 에쿼티 자체가 낮다
            cls = "weak"
    else:
        eq = max(0.05, EQ_GUESS[cls] - 0.08 * (c.opp - 1))
        if strong_draw and eq < 0.35:
            eq = 0.35
        eq_src = f"에쿼티 미계산 — 족보로 대략 {_pct(eq)} 가정"
    base_conf = "medium" if c.equity is not None else "low"
    head = f"{name} · {made}"
    pot = c.total_pot()
    eff = c.eff
    facing = c.call > 0 or (not c.call_known and c.can_check is False)
    if facing:
        r = _facing_bet(c, cls, made, tp_plus, twopair_plus, tex, eq, eq_src, outs, strong_draw, combo, pot,
                        eff, head, base_conf, nut_draw, overpair)
    else:
        r = _checked_to(c, cls, tp_plus, twopair_plus, tex, eq, eq_src, outs, strong_draw, combo, pot, eff, head,
                        base_conf, river, nut_draw, overpair)
    pl = dict(cls=cls, made=made, tex=tex, twopair_plus=twopair_plus, eq=eq, outs=outs, strong_draw=strong_draw,
              nut_draw=nut_draw, river=river, facing=facing, eff=eff)
    r["_plan"] = _postflop_plan(c, r, pl)
    return r


def _adj_equity(c, eq, cls, made, twopair_plus, bet_frac):
    """벳에 콜할 때 쓰는 보정 에쿼티와 표기용 합산 할인. (콜 판단과 plan 의 '최대 콜 금액' 이 같은 식을 쓴다)"""
    sp = c.post
    disc = _mw_discount(c)
    # 벳하는 상대 레인지는 랜덤보다 강하고(상대가 많을수록 조금 더), 랜덤 가정은 상대가 많을수록 에쿼티를 더 과대평가한다
    range_pen = sp["pen"] if c.opp == 1 else sp["pen_mw"] * (1 + sp["growth"] * min(3, c.opp - 2))
    if c.street == "river" and c.opp >= 2:  # 리버 멀티웨이 벳은 밸류 위주 → 예전(엄격한) 보정 유지
        old = POST_ANCHORS[0][1]
        range_pen = old["pen"] * (1 + 0.25 * min(3, c.opp - 1)) * sp["river_gen"]
        disc = min(old["disc_cap"], old["disc_step"] * (c.opp - 1)) * sp["river_gen"]
    vuln = _onepair_discount(c, cls, made, twopair_plus, c.street == "river", bet_frac)
    if c.rb:  # 에쿼티가 이미 상대 레인지 기준 → 랜덤 가정 할인은 작은 잔여분만
        range_pen = RB_PEN_FRAC
        disc = min(RB_DISC_CAP, RB_DISC_STEP * (c.opp - 1))
        vuln *= 0.5
    if c.opp == 1:  # 헤즈업: 솔버 표의 거리별 보정 (랜덤 기준은 느슨함에 따른 pen 변화분을 더한다)
        range_pen = max(0.0, _hu_table(c)["pen"][c.street] + (0.0 if c.rb else sp["pen"] - STANDARD_PEN))
    red = 1 - (1 - range_pen * min(1.0, bet_frac)) * (1 - disc) * (1 - vuln)
    if c.opp >= 2:  # 할인·레인지 보정·원페어 취약이 겹쳐 쌓이지 않게 합산 감소에 상한 (과도한 멀티웨이 폴드 방지)
        # 상한은 벳이 클수록(팟 벳 = 강한 밸류 레인지) 넉넉해지고, 리버 벳은 밸류 위주라 사실상 상한을 풀어 준다
        cap = (sp["cap3"] if c.opp >= 3 else sp["cap2"]) + sp["cap_slope"] * max(0.0, bet_frac - 0.5)
        if c.street == "river":
            cap += sp["river_cap"]
        red = min(red, cap)
    mw_red = min(1 - (1 - disc) * (1 - vuln), red)
    # 바닥: 벳하는 레인지 안에서 나를 이기는 몫은 랜덤 대비 최대 NUT_K 배 — 에쿼티가 1 에 가까우면 보정 에쿼티도 1 에 가깝다.
    # (곱셈 보정만 쓰면 랜덤 기준 헤즈업에서 보정 에쿼티가 50% 에 묶여, 오버벳·올인에 리버 너트도 폴드했다)
    return max(eq * (1 - red), 1 - (1 - eq) * NUT_K), mw_red


def _prof_facing(c, cls, twopair_plus, bet_frac):
    """상대 성향에 따른 필요 승률 가감 (양수 = 더 타이트하게 콜). (가감, 이유)"""
    p = c.prof
    if not p or cls == "monster":
        return 0.0, None
    vpip, af = _stat(p, "vpip"), _stat(p, "af")
    pad, why = 0.0, []
    if vpip is not None and vpip <= VPIP_TIGHT:
        pad += (0.03 if c.rb else 0.05) + (0.04 if c.raised else 0.0) + (0.02 if bet_frac >= 0.66 else 0.0)
        why.append("타이트 상대의 " + ("레이즈" if c.raised else "벳") + " 존중")
    elif vpip is not None and vpip >= VPIP_LOOSE:
        pad -= 0.02
        why.append("루즈 상대의 벳은 약한 패 포함")
    if af is not None and af >= AF_HIGH and (cls == "medium" or (cls == "strong" and not twopair_plus)):
        pad -= 0.04
        why.append("공격적 상대(AF %.1f) → 블러프캐치 확대" % af)
    if af is not None and af <= AF_LOW and c.raised:
        pad += 0.06
        why.append("패시브 상대(AF %.1f)의 레이즈 = 강함" % af)
    return pad, " · ".join(why) if pad else None


def _facing_bet(c, cls, made, tp_plus, twopair_plus, tex, eq, eq_src, outs, strong_draw, combo, pot, eff, head,
                conf, nut_draw=False, overpair=False):
    call = c.call
    if not call:  # 앞에 벳이 있는 것 같은데 금액을 모름 → '콜' 이라고 하지 않고 계획만 (plan 은 _postflop 이 채움)
        return _out(WAIT, reason=f"{made} — 벳 금액 미확인: 금액이 보이면 콜/폴드를 알려줌",
                    detail=[head, eq_src], confidence="low", color="check")
    if pot is None:
        pot = call * 2.5  # 팟 정보가 없으면 대략 팟의 2/3 벳으로 가정
        conf = "low"
    # pot 은 화면 POT(상대 벳 + 먼저 콜한 사람들 돈까지 포함)가 우선이라 팟 오즈에 콜러 돈이 이미 들어 있다
    need = call / (pot + call)
    pot_before = c.pot if c.pot is not None else max(0.0, pot - call)
    bet_frac = call / pot_before if pot_before > 0 else 1.0
    mw = c.opp >= 2
    allin_call = bool(c.facing_allin) or (eff is not None and call >= 0.98 * eff) or         (c.my_stack is not None and call >= c.my_stack)
    pad = _behind_pad(c, allin_call)
    adj, disc = _adj_equity(c, eq, cls, made, twopair_plus, bet_frac)
    ppad, pwhy = _prof_facing(c, cls, twopair_plus, bet_frac)
    if ppad:
        c.prof_why = pwhy
    hu = c.opp == 1
    T = _hu_table(c)
    # 솔버 표의 실현 보정(거리·팟 크기별 콜 여유)은 멀티웨이에도 바닥으로 깐다 — 멀티웨이 할인은 그 위에 (같은 에쿼티면 다자 팟이 더 타이트)
    if hu:
        hu_pad = _hu_margin(T, c.street, bet_frac, allin_call, pot)
    else:
        hu_pad = 0.0 if allin_call else _hu_margin(T, c.street, bet_frac, False, pot)
    cadj = c.post["call_adj"]
    need_eff = need + pad + ppad + cadj + hu_pad
    icm_pad, icm_line = _icm_facing(c, call, pot, allin_call, need_eff)   # 토너먼트 ICM (없거나 초반이면 0)
    need_eff += icm_pad
    if icm_pad >= 0.005 and not icm_line:
        c.icm.note(f"ICM: {c.icm.label} 위험 프리미엄 +{icm_pad * 100:.0f}%p (버블 팩터 {_fmt_bf(c.icm.bf())})")
    # 표기에 필요 승률의 모든 가산을 보인다 (스타일 가감이 빠져 '필요 41% > 보정 에쿼티 44%' 처럼 숫자와 부등호가 어긋났다)
    odds_line = f"필요 승률 {_pct(need)}" + (f"+{_pct(pad)}(뒤 레이즈 위험)" if pad else "") +         (f"{'+' if ppad > 0 else '-'}{_pct(abs(ppad))}(상대 성향)" if ppad else "") + \
        (f"{'+' if cadj > 0 else '-'}{_pct(abs(cadj))}(스타일)" if abs(cadj) >= 0.005 else "") + \
        (f"{'+' if hu_pad > 0 else '-'}{_pct(abs(hu_pad))}(실현 보정)" if abs(hu_pad) >= 0.005 else "") + \
        (f"+{_pct(icm_pad)}(ICM)" if icm_pad >= 0.005 else "") + \
        f" {'<' if adj >= need_eff else '>'} 보정 에쿼티 {_pct(adj)}" + (f"(상대 {c.opp}명 -{_pct(disc)})" if mw else "")
    margin = adj - need_eff
    sure = "high" if abs(margin) >= 0.12 and conf != "low" else conf
    stack_after = (eff - call) if eff else None

    if allin_call:
        size = min(call, c.my_stack) if c.my_stack else call
        # 헤즈업 올인 최소 에쿼티: 솔버 데이터의 큰 올인 기준이라 팟 대비 작은 올인(좋은 팟 오즈)엔 비례해서만 적용
        floor = T["allin_eq"][c.street] * min(1.0, bet_frac / ALLIN_EQ_FULL_FRAC) if hu else 0.0
        third = icm_line or head
        if adj >= need_eff and eq >= floor:
            return _out(CALL, size, f"{_bb(size)} (올인 콜)", f"{made}: 에쿼티가 팟 오즈를 넘음 → 올인 콜",
                        [odds_line, eq_src, third], sure)
        if adj < need_eff:
            why = "올인에 에쿼티 부족" + (" (ICM)" if icm_pad and adj >= need_eff - icm_pad else "")
        else:
            why = f"큰 올인 콜은 에쿼티 {_pct(floor)} 이상만 (현재 {_pct(eq)})"
        return _out(FOLD, reason=f"{made}: {why} → 폴드", detail=[odds_line, eq_src, third], confidence=sure)

    spr_after = stack_after / (pot + call) if stack_after is not None and pot + call > 0 else None
    spr_line = f"SPR(콜 후) {spr_after:.1f}" if spr_after is not None else None

    if hu:  # 헤즈업: 솔버 표 — 낮은 SPR 커밋 / 에지 기준 밸류 레이즈
        edge = adj - need - ppad - c.post["call_adj"]
        st = c.street
        if spr_after is not None and spr_after < T["jam_spr"] and \
                edge >= T["jam_edge"][st] + _icm_commit_pad(c, call, pot, eff):
            return _out(ALLIN, eff, _bb(eff), f"{made} + 낮은 SPR → 커밋 (올인)", [spr_line, odds_line, eq_src],
                        conf)
        value = cls in T["raise_cls"] and edge >= T["raise_edge"][st]
    else:
        # 낮은 SPR + 탑페어 이상 → 커밋
        commit_ok = tp_plus and (cls == "monster" or (cls == "strong" and (twopair_plus or overpair)))
        if commit_ok and spr_after is not None and spr_after < 2 and \
                adj >= need_eff + max(0.0, _icm_commit_pad(c, call, pot, eff) - icm_pad):
            return _out(ALLIN, eff, _bb(eff), f"{made} + 낮은 SPR → 커밋 (올인)", [spr_line, odds_line, eq_src],
                        conf)
        # 밸류 레이즈 (멀티웨이: 원페어 계열은 레이즈 대신 콜, 투페어+ 만 레이즈)
        value = cls == "monster" or (cls == "strong" and twopair_plus and not tex["scary"] and c.opp <= 3)
        value = value and eq >= max(0.45, 0.6 - 0.04 * (c.opp - 1))
    if value:
        size = 3 * call
        if eff and size >= COMMIT_SIZE * eff:
            return _out(ALLIN, eff, _bb(eff), f"{made}: 밸류 레이즈 — 스택 대비 커서 올인",
                        [odds_line, eq_src, spr_line], conf)
        strong_txt = "강한 메이드" if cls in ("monster", "strong") else "상대 범위 대비 에쿼티 우위"
        return _out(RAISE, size, f"{_bb(size)} (벳의 3배)", f"{made}: {strong_txt} → 밸류 레이즈",
                    [odds_line, eq_src, spr_line], conf)
    # 콤보 드로우 + 낮은 SPR → 세미블러프 올인 (상대 2명 이하만)
    if combo and c.opp <= 2 and spr_after is not None and spr_after < 3 and \
            (c.icm is None or c.icm.rp() < ICM_SEMI_RP):   # 버블/파이널의 덮인 스택은 세미블러프 올인 자제
        return _out(ALLIN, eff, _bb(eff), "콤보 드로우 + 낮은 SPR → 세미블러프 올인",
                    [f"아웃츠 {outs['count']}장", odds_line, spr_line], "medium" if c.opp == 1 else "low")
    if adj >= need_eff:
        return _out(CALL, call, None, f"{made}: 에쿼티 ≥ 필요 승률 → 콜", [odds_line, eq_src, spr_line], sure)
    # 드로우 내재 배당 (팟 전체 = 화면 POT, 콜러 돈 포함). 멀티웨이 너트 드로우는 내재 배당 25% 가산
    has_draw = bool(outs) and any(k in g[0] for g in (outs.get("groups") or []) for k in ("드로우", "것샷", "양차", "포쁠"))
    if outs and outs.get("count") and (has_draw or strong_draw) and (not hu or outs["count"] >= T["implied_outs"]):
        hit = outs["next"]
        more = odds.implied_needed(max(0.0, pot - call), call, hit)
        if mw and nut_draw:
            more *= 0.75
        room = stack_after or 0.0
        line = f"아웃츠 {outs['count']}장 · 다음 카드 {_pct(hit)} · 완성 시 +{_bb(more)} 필요" + \
            (" (너트 드로우 다자 -25%)" if mw and nut_draw else "")
        imp_frac = T["implied"] if hu else (0.35 if mw and not nut_draw else 0.5)
        if more <= imp_frac * room and call <= 0.25 * (eff or 0):
            return _out(CALL, call, None, "드로우 — 내재 배당으로 콜 가능",
                        [line, odds_line, f"{eq_src} · 뒷스택 {_bb(room)}" + (f" · {spr_line}" if spr_line else "")],
                        "medium" if more <= 0.25 * room else "low")
        return _out(FOLD, reason="드로우지만 팟 오즈·내재 배당 부족 → 폴드",
                    detail=[line, odds_line, f"{eq_src}" + (f" · {spr_line}" if spr_line else "")],
                    confidence=conf)
    return _out(FOLD, reason=f"{made}: 에쿼티 < 필요 승률 → 폴드" + (" (다자 팟)" if mw else ""),
                detail=[odds_line, eq_src, spr_line or head], confidence=sure)


def _checked_to(c, cls, tp_plus, twopair_plus, tex, eq, eq_src, outs, strong_draw, combo, pot, eff, head, conf,
                river, nut_draw=False, overpair=False):
    made = head.split(" · ")[-1]
    opp = c.opp
    mw = opp >= 2
    if not pot:
        if cls in ("monster", "strong"):
            return _out(BET, None, "팟의 2/3", f"{made}: 체크로 넘어옴 → 밸류 벳", [head, eq_src, "팟 정보 없음"], "low")
        return _out(CHECK, reason=f"{made}: 체크", detail=[head, eq_src], confidence="low")
    spr = eff / pot if eff else None
    spr_line = f"SPR {spr:.1f} ({odds.spr_zone(spr)[0]})" if spr is not None else None
    n_outs_ = outs.get("count", 0) if outs else 0

    def bet(frac, label, reason, detail, confidence):
        size = pot * frac
        if eff and size >= COMMIT_SIZE * eff:
            return _out(ALLIN, eff, _bb(eff), reason + " — 스택이 짧아 올인", detail, confidence)
        return _out(BET, size, f"팟의 {label} ≈ {_bb(size)}", reason, detail, confidence)

    # 상대 성향 (reliable 프로필만)
    p = c.prof
    ftb = _stat(p, "fold_to_bet") if p else None
    vpip = _stat(p, "vpip") if p else None
    bluff_ok = ftb is not None and ftb >= FOLD_HIGH and opp == 1
    no_bluff = ftb is not None and ftb <= FOLD_LOW
    loose = vpip is not None and vpip >= VPIP_LOOSE
    thin = opp <= 2 and (no_bluff or loose)
    eq_hi = 0.57 if c.rb else 0.65

    def vbet(frac, label, reason, detail, confidence):
        """밸류 벳: 루즈 상대면 한 단계 크게."""
        if loose and cls in ("monster", "strong", "medium"):
            f2, l2 = _step_up(frac, label)
            if f2 != frac:
                frac, label = f2, l2
                c.prof_why = "루즈 상대 → 밸류 벳 사이즈업"
        return bet(frac, label, reason, detail, confidence)

    def no_bluff_check():
        c.prof_why = "벳에 안 접는 상대 → 블러프 금지"
        return _out(CHECK, reason=f"{made}: 벳에 안 접는 상대 → 블러프 금지, 체크",
                    detail=[head, eq_src, f"상대 폴드율 {ftb:.0f}% · 폴드 에쿼티 없음"], confidence=conf)

    def tail():
        """프로필/스타일 블러프 규칙, 없으면 체크."""
        if bluff_ok and cls in ("air", "weak") and (cls == "air" or (not river and n_outs_ >= 4)):
            c.prof_why = "벳에 자주 접는 상대 → 블러프 가치"
            if not river and n_outs_ >= 4:
                return bet(1 / 2, "1/2", f"{made}: 벳에 자주 접는 상대 → 세미블러프", [head, eq_src,
                           f"폴드율 {ftb:.0f}% > 본전 {_pct(odds.breakeven_fold(pot, pot / 2))} · 아웃츠 {n_outs_}장"], "low")
            frac, lab = (1 / 3, "1/3") if not river else (1 / 2, "1/2")
            return bet(frac, lab, f"{made}: 벳에 자주 접는 상대 → 블러프 가치", [head, eq_src,
                       f"폴드율 {ftb:.0f}% > 본전 {_pct(odds.breakeven_fold(pot, pot * frac))}"], "low")
        if (c.post["hu_bluff"] and opp == 1 and not river and not no_bluff and cls in ("air", "weak")
                and n_outs_ >= c.post["hu_bluff_outs"]):
            bf, bl = c.post["hu_bluff_frac"]
            return bet(bf, bl, f"{made}: 공격적 스타일 → 헤즈업 세미블러프 (아웃츠 {n_outs_}장)",
                       [head, eq_src, f"본전 폴드율 {_pct(odds.breakeven_fold(pot, pot * bf))} · 아웃츠 {n_outs_}장"], "low")
        if mw:
            return _out(CHECK, reason=f"{made}: 다자 팟 — 블러프 자제, 체크", detail=[head, eq_src, f"상대 {opp}명 기준"],
                        confidence=conf)
        why = "쇼다운 가치 — 체크" if cls in ("medium", "strong", "monster") else "약한 패 — 체크"
        return _out(CHECK, reason=f"{made}: {why}", detail=[head, eq_src, spr_line], confidence=conf)

    if opp == 1:  # 헤즈업: 솔버 표(HU_GTO) — 위치·주도권·거리별 에쿼티 기준
        T, node, st = _hu_table(c), _hu_node(c), c.street
        a_on, a_off = T["agg"][st]
        thr = T["bet"][node][st] - c.post["value_adj"] - (a_on if c.aggressor is True else 0.0) + \
            (a_off if c.aggressor is False else 0.0)
        where = {"ip": "체크로 넘어옴", "oop": "먼저 행동", "unk": "체크 가능"}[node]
        if tp_plus and spr is not None and spr < T["commit_spr"][node] and eq >= T["commit_eq"]:
            return bet(2 / 3, "2/3", f"{made} + 낮은 SPR → 벳으로 커밋",
                       [spr_line, eq_src, f"SPR<{T['commit_spr'][node]:g}: 탑페어+ 는 올인 승부 구간"],
                       "high" if cls in ("monster", "strong") else "medium")
        if eq >= thr or (thin and cls != "air" and eq >= thr - HU_THIN):
            thin_bet = eq < thr
            if thin_bet:
                c.prof_why = "벳에 안 접는 상대 → 얇은 밸류 벳" if no_bluff else "루즈 상대 → 얇은 밸류 벳"
            if cls in ("monster", "strong") and not thin_bet:
                frac, lab, why = 2 / 3, "2/3", "밸류 벳"
            elif river or not tex["wet"] or thin_bet:
                frac, lab, why = 1 / 3, "1/3", "얇은 밸류 벳"
            else:
                frac, lab, why = 1 / 2, "1/2", "보호 겸 밸류 벳"
            return vbet(frac, lab, f"{made}: {where} → {why}", [head, eq_src, spr_line],
                        "high" if cls == "monster" or (conf == "medium" and eq >= thr + 0.1) else conf)
        if st == "flop" and node == "ip" and c.aggressor is True and eq >= T["cbet"]:
            return bet(1 / 3, "1/3", f"{made}: IP + 주도권 → 작은 c-벳 (레인지 벳)", [head, eq_src, spr_line], "low")
        if not river and strong_draw and T["semi"][node]:
            if no_bluff and not combo:
                return no_bluff_check()
            kind = "콤보 드로우" if combo else "강한 드로우"
            return bet(1 / 2, "1/2", f"{kind} 헤즈업 → 세미블러프 벳",
                       [f"아웃츠 {outs['count']}장 · 리버까지 {_pct(outs['by_river'])}", eq_src,
                        f"본전 폴드율 {_pct(odds.breakeven_fold(pot, pot / 2))}"], "medium" if combo else "low")
        if river and cls == "air" and eq <= T["bluff"][node] and not no_bluff:
            return bet(2 / 3, "2/3", f"{made}: 리버 폴라라이즈 — 쇼다운 가치 없는 패로 블러프",
                       [head, eq_src, f"본전 폴드율 {_pct(odds.breakeven_fold(pot, pot * 2 / 3))}"], "low")
        return tail()

    # --- 멀티웨이 (상대 2명 이상) ---
    # 낮은 SPR 커밋: 상대가 늘수록 탑페어 약키커 등은 제외
    commit_ok = (cls in ("monster", "strong") if opp == 2 else
                 cls == "monster" or (cls == "strong" and (twopair_plus or overpair)))
    if tp_plus and spr is not None and spr < 4 and commit_ok:
        return bet(2 / 3, "2/3", f"{made} + 낮은 SPR → 벳으로 커밋", [spr_line, eq_src, "SPR<4: 탑페어+ 는 올인 승부 구간"],
                   "high" if cls in ("monster", "strong") else "medium")
    # 투페어 이상: 다자에서도 계속 밸류 벳. 젖은 보드는 보호를 위해 사이즈업
    if cls == "monster" or (cls == "strong" and twopair_plus):
        if mw and tex["wet"] and not river:
            return vbet(3 / 4, "3/4", f"{made}: 다자 + 젖은 보드 → 보호 겸 밸류 (사이즈업)",
                       [head, eq_src, f"상대 {opp}명 기준 · 드로우에 비싼 값 받기"], "high" if cls == "monster" else conf)
        if mw and river and tex["scary"]:
            return vbet(1 / 2, "1/2", f"{made}: 다자 리버 위험 보드 → 사이즈 축소 밸류",
                       [head, eq_src, f"상대 {opp}명 기준"], conf)
        return vbet(2 / 3, "2/3", f"{made}: 체크로 넘어옴 → 밸류 벳", [head, eq_src, spr_line],
                   "high" if cls == "monster" or (conf == "medium" and eq >= eq_hi) else conf)
    if cls == "strong":  # 원페어 계열 (탑페어 좋은 키커 / 오버페어)
        if opp == 2:
            if river:
                return vbet(1 / 3, "1/3", f"{made}: 3인 리버 → 얇은 밸류 벳", [head, eq_src, "상대 2명 기준 · 작게"], "low")
            return vbet(1 / 2, "1/2", f"{made}: 3인 팟 → 줄인 밸류/보호 벳", [head, eq_src, f"상대 2명 기준 · {spr_line}"], conf)
        if overpair and not river:
            if tex["wet"]:
                return vbet(1 / 2, "1/2", f"{made}: 다자 젖은 보드 → 보호 벳", [head, eq_src, f"상대 {opp}명 기준 · 공짜 카드 차단"],
                           _lower(conf))
            return vbet(1 / 3, "1/3", f"{made}: 다자 드라이 보드 → 작은 벳", [head, eq_src, f"상대 {opp}명 기준"], _lower(conf))
        if opp == 3 and not river and not tex["wet"]:
            return vbet(1 / 3, "1/3", f"{made}: 3명 상대 드라이 보드 → 작은 벳", [head, eq_src, "상대 3명 기준 · 키커 약세 위험"],
                       _lower(conf))
        return _out(CHECK, reason=f"{made}: 다자 팟 — 원페어는 체크 (벳은 더 좋은 패만 콜)",
                    detail=[head, eq_src, f"상대 {opp}명 기준 · 팟 컨트롤"], confidence=_lower(conf))
    if cls == "medium":
        if river:
            return _out(CHECK, reason=f"{made}: 쇼다운 가치 — 체크 (다자 팟)", detail=[head, eq_src], confidence=conf)
        if opp <= 2 and (not tex["wet"] or thin):
            if thin and tex["wet"]:
                c.prof_why = "벳에 안 접는 상대 → 얇은 밸류 벳" if no_bluff else "루즈 상대 → 얇은 밸류 벳"
            return vbet(1 / 3, "1/3", f"{made}: " + ("드라이 보드 → " if not tex["wet"] else "") + "작은 밸류 벳",
                        [head, eq_src, spr_line], conf)
        return _out(CHECK, reason=f"{made}: 멀티웨이 젖은 보드 — 팟 컨트롤 체크", detail=[head, eq_src, f"상대 {opp}명 기준"],
                    confidence=conf)
    if no_bluff and not combo and strong_draw and not river:
        return no_bluff_check()
    if not river and strong_draw:
        if opp == 2 and (nut_draw or c.post["hu_bluff"]):
            kind = "콤보 드로우" if combo else "너트 플러시 드로우"
            return bet(1 / 2, "1/2", f"{kind} 3인 팟 → 세미블러프 벳",
                       [f"아웃츠 {outs['count']}장 · 리버까지 {_pct(outs['by_river'])}", eq_src,
                        f"상대 2명 기준 · 본전 폴드율 {_pct(odds.breakeven_fold(pot, pot / 2))}"], "low")
        return _out(CHECK, reason="다자 팟: 블러프 자제 — 드로우는 체크로 공짜 카드",
                    detail=[head, f"아웃츠 {outs['count']}장 · 리버까지 {_pct(outs['by_river'])}", f"상대 {opp}명 기준 · 세미블러프는 너트/콤보 + 2명 이하만"],
                    confidence=conf)
    return tail()


# --- 대응 계획 (plan) ---------------------------------------------------------------
PLAN_MAX_F = 1.5          # 팟의 150% 이상 벳도 콜이면 '큰 벳도 콜'
PLAN_IMPLIED_MAX_F = 1.0  # 내재 배당으로 늘리는 한도는 팟 크기까지만
PLAN_MIN_F = 0.15         # 팟의 15% 도 못 받으면 '폴드'
PLAN_STEP = 0.05          # 최대 콜 비율은 5% 단위로 내림 (보수적으로)


def _plan_gain(g, hi):
    """g(f) >= 0 인 최대 f (g 는 f 에 대해 감소). g(hi) >= 0 이면 hi, g(0.02) < 0 이면 0."""
    if g(0.02) < 0:
        return 0.0
    if g(hi) >= 0:
        return hi
    lo, up = 0.02, hi
    for _ in range(30):
        mid = (lo + up) / 2
        if g(mid) >= 0:
            lo = mid
        else:
            up = mid
    return lo


def _max_call(c, pl, p0):
    """상대가 팟의 f 배를 벳했을 때 콜할 수 있는 최대 f.  (f, 내재 배당으로 늘었는지)
    콜 조건은 _facing_bet 과 같다: 보정 에쿼티 >= f/(1+2f) + 뒤 레이즈 위험 + 상대 성향 가감.
    (f/(1+2f) = 팟 오즈: 콜 f / (팟 1 + 벳 f + 콜 f).  헤즈업 에쿼티 e 면 f_max = e/(1-2e))"""
    cls, made, tw, eq = pl["cls"], pl["made"], pl["twopair_plus"], pl["eq"]
    pad = _behind_pad(c, False)

    T = _hu_table(c)

    def g(f):
        adj, _ = _adj_equity(c, eq, cls, made, tw, f)
        ppad, _ = _prof_facing(c, cls, tw, f)
        hu_pad = _hu_margin(T, c.street, f, False, p0 * (1 + f) if p0 else None)
        base = f / (1 + 2 * f) + pad + ppad + c.post["call_adj"] + hu_pad
        if c.icm is not None and p0:   # _facing_bet 과 같은 ICM 가산 (올인 크기면 ICM 올인 필요 승률)
            allin = bool(pl["eff"]) and f * p0 >= 0.98 * pl["eff"]
            base += _icm_facing(c, f * p0, p0 * (1 + f), allin, base)[0]
        return adj - base
    f_pure = _plan_gain(g, PLAN_MAX_F)
    outs, eff = pl["outs"], pl["eff"]
    if not (outs and outs.get("count") and not pl["river"] and p0 and eff):
        return f_pure, False
    has_draw = any(k in grp[0] for grp in (outs.get("groups") or []) for k in ("드로우", "것샷", "양차", "포쁠"))
    if not (has_draw or pl["strong_draw"]):
        return f_pure, False
    mw = c.opp >= 2
    thr = 0.35 if mw and not pl["nut_draw"] else 0.5
    if c.opp == 1:  # 헤즈업: _facing_bet 과 같은 내재 배당 기준
        if outs.get("count", 0) < T["implied_outs"]:
            return f_pure, False
        thr = T["implied"]
    hit = outs.get("next") or 0.0

    def gi(f):  # 내재 배당으로 콜 가능하면 >= 0
        x = f * p0
        more = odds.implied_needed(p0, x, hit) * (0.75 if mw and pl["nut_draw"] else 1.0)
        room = eff - x
        if x > 0.25 * eff or room <= 0:
            return -1.0
        return thr * room - more
    f_imp = _plan_gain(gi, PLAN_IMPLIED_MAX_F)
    return (f_imp, True) if f_imp > f_pure + 0.02 else (f_pure, False)


def _is_value_raise(c, pl):
    """벳에 레이즈할 만한 강한 패인가 (_facing_bet 의 밸류 레이즈 조건과 같다)."""
    cls = pl["cls"]
    if c.opp == 1:  # 헤즈업: 팟의 2/3 벳 기준 에지 (_facing_bet 의 HU_GTO 레이즈 조건)
        T, f = _hu_table(c), 2 / 3
        adj, _ = _adj_equity(c, pl["eq"], cls, pl["made"], pl["twopair_plus"], f)
        return cls in T["raise_cls"] and adj - f / (1 + 2 * f) - c.post["call_adj"] >= T["raise_edge"][c.street]
    value = cls == "monster" or (cls == "strong" and pl["twopair_plus"] and not pl["tex"]["scary"] and c.opp <= 3)
    return value and pl["eq"] >= max(0.45, 0.6 - 0.04 * (c.opp - 1))


def _bet_lines(c, pl, p0, prefix="벳이 오면"):
    """상대가 벳하면 어떻게 할지: 레이즈 / 팟의 몇 %까지 콜 / 폴드."""
    if _is_value_raise(c, pl):
        return [f"{prefix}: 레이즈 3배 (밸류)"]
    f, implied = _max_call(c, pl, p0)
    eff = pl["eff"]
    stack_cap = bool(p0 and eff and f * p0 >= 0.98 * eff)
    if f >= PLAN_MAX_F - 1e-9 or stack_cap:
        return [f"{prefix}: " + ("올인까지 콜 (에쿼티 충분)" if stack_cap else "큰 벳(팟의 150%+)도 콜 (에쿼티 충분)")]
    if f < PLAN_MIN_F:
        return [f"{prefix}: 폴드 (체크로 무료 카드)"]
    pct = int(f / PLAN_STEP + 1e-9) * 5
    size = f"(≈{_bb(pct / 100 * p0)})" if p0 else ""
    out = [f"{prefix}: 팟의 {pct}%{size}까지 콜, 그 이상 폴드"]
    if implied:
        n = (pl["outs"] or {}).get("count", 0)
        out.append(f"드로우 {n}장: 내재 배당까지 넣은 한도")
    return out


def _followup_lines(c, r, pl):
    """내 차례에 벳/콜/레이즈를 한 뒤 레이즈 당하면."""
    act, cls = r["action"], pl["cls"]
    if act == BET:
        if cls == "monster" or (cls == "strong" and pl["twopair_plus"]):
            return ["레이즈 당하면: 콜 (올인이어도 콜 가능)"]
        if cls == "strong":
            return ["레이즈 당하면: 폴드 고려 (원페어)"]
        return ["레이즈 당하면: 폴드" + (" (드로우는 오즈 맞으면 콜)" if pl["strong_draw"] else "")]
    if act == CALL:
        if cls == "monster":
            return ["레이즈 당하면: 올인까지 콜"]
        if cls == "strong" and pl["twopair_plus"]:
            return ["레이즈 당하면: 콜 (스택 크면 재검토)"]
        return ["레이즈 당하면: 폴드"]
    if act == RAISE:
        return ["리레이즈 당하면: 올인 콜" if cls == "monster" else "리레이즈 당하면: 폴드"]
    return []


def _postflop_plan(c, r, pl):
    act = r["action"]
    p0 = c.pot if c.pot is not None else (c.pot_total or None)
    if p0 is not None and p0 <= 0:
        p0 = None
    to_act = c.to_act is True
    if act == WAIT:
        return _bet_lines(c, pl, p0)
    if not pl["facing"]:  # 체크로 넘어온 경우의 액션이 r
        if not to_act:
            first = f"체크로 오면: {act}" + (f" {r['size_text']}" if r["size_text"] else "")
            return [first] + _bet_lines(c, pl, p0)
        if act == CHECK:
            return _bet_lines(c, pl, p0, "체크 후 벳이 오면")
    return _followup_lines(c, r, pl)


# 프리플랍 계획
def _pf_pos(c):
    players = _resolve_players(c) if c.hero_pos else None
    return (c.hero_pos if players else None), players


def _max_call_raise(hand, acts, eff):
    """차트의 콜/3벳 범위에 드는 핸드가 최대 얼마의 오픈까지 콜할지 (BB).
    표준 오픈 2.5BB 에서의 범위 폭 W 와 이 핸드의 순위 q 로: 레이즈가 2배가 되면 콜 범위는 절반 → 최대 = 2.5 × W / q.
    스택의 30% 를 넘는 콜은 '잼 or 폴드' 구간이라 그 위로는 올리지 않는다."""
    union = set()
    for _, hands, _ in acts:
        union |= set(hands)
    idx = pf.DATA["G"].index(hand)
    q = max(pf.range_pct(pf.DATA["G"][:idx + 1]), 0.5)
    x = 2.5 * pf.range_pct(union) / q
    cap = PRESSURE * eff if eff else 15.0
    return int(max(2.5, min(x, cap)) * 2) / 2     # 0.5BB 단위로 내림


def _preflop_plan(c, r):
    """프리플랍 대응 계획. 내 차례가 아니면 '레이즈가 오면', 내 차례면 레이즈한 뒤 '3벳/4벳 당하면'."""
    pos, players = _pf_pos(c)
    if pos is None:
        return []
    eff = c.eff
    if eff is not None and eff <= PUSHFOLD_BB:
        return []
    hand = pf.hand_code(c.hero)
    posted = {"SB": 0.5, "BB": 1.0}.get(pos, 0.0)
    unopened = c.call + posted <= 1.0 + 1e-6
    sit = c.situation if c.situation in DEEP_SITUATIONS else ("open" if unopened else "vsopen")
    if sit == "open" and not unopened and c.call + posted >= 2.0:
        sit = "vsopen"
    act = r["action"]
    stack = eff or 100.0
    if sit == "open":
        if c.to_act is True:  # 내가 오픈 레이즈 → 3벳 당하면
            if act != RAISE or pos not in pf.valid_heroes("vs3bet", players):
                return []
            vl = pf.valid_villains("vs3bet", pos, players)
            acts = _pf_ranges(c, "vs3bet", pos, vl[0] if vl else None, players, stack)
            name, _ = pf.decide(hand, acts)
            if name == "4벳":
                return ["3벳 당하면: 4벳 (또는 상황 보고 콜)"]
            if name == "콜":
                return ["3벳 당하면: 콜 (OOP면 신중하게)"]
            return ["3벳 당하면: 폴드"]
        # 아직 내 차례가 아님 — 내 앞에서 레이즈가 나오면
        if pos not in pf.valid_heroes("vsopen", players):
            return []
        vl = pf.valid_villains("vsopen", pos, players)
        villain = vl[len(vl) // 2] if vl else None
        acts = _pf_ranges(c, "vsopen", pos, villain, players, stack)
        name, _ = pf.decide(hand, acts)
        if name == "3벳":
            ip = pos not in ("SB", "BB")
            size = 2.5 * (3.0 if ip else (4.0 if pos == "SB" else 3.5))
            if eff and size >= COMMIT_SIZE * eff:
                return [f"레이즈가 오면: 올인 ({_bb(eff)})"]
            out = [f"레이즈가 오면: 3벳 ≈ {_bb(size)} (2.5BB 오픈 기준)"]
            if hand in VALUE_4BET:
                out.append("4벳이 오면: 올인" if hand in ("AA", "KK") else "4벳이 오면: 콜/올인 (상대 보고)")
            else:
                out.append("4벳이 오면: 폴드 (블러프성 3벳)")
            return out
        if name == "콜":
            return [f"레이즈가 오면: {_bb(_max_call_raise(hand, acts, eff))}까지 콜, 그 이상 폴드"]
        return ["레이즈가 오면: 폴드"]
    if sit in ("vsopen", "vs3bet") and act == RAISE:  # 내가 3벳/4벳 → 다음 레이즈 당하면
        acts = _pf_ranges(c, "vs4bet", pos, None, players, stack)
        name, _ = pf.decide(hand, acts)
        nxt = "4벳" if sit == "vsopen" else "5벳(올인)"
        if name == "5벳 잼":
            return [f"{nxt} 당하면: 올인"]
        return [f"{nxt} 당하면: " + ("콜" if name == "콜" else "폴드")]
    return []
