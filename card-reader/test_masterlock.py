"""python test_masterlock.py
임시 비밀번호와 임시 폴더만 쓴다 (XDG_CONFIG_HOME 을 임시로 바꾼다).
실제 설정 파일 확인(읽기만)은 환경변수 MASTERLOCK_CHECK 에 비밀번호를 넣고 실행한다."""
import ast
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import masterlock as ml

HERE = os.path.dirname(os.path.abspath(__file__))
results = []


def test(name, ok, info=""):
    results.append(("PASS" if ok else "FAIL", name, info))


def source(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def exits(fn):
    try:
        fn()
    except SystemExit as e:
        return e.code
    return None


# --- 실제 설정 파일 확인 (선택, 읽기만). 임시 XDG 로 바꾸기 전에 한다
env_pw = os.environ.get("MASTERLOCK_CHECK")
if env_pw is None:
    results.append(("SKIP", "실제 설정 파일: 비밀번호와 맞음", "MASTERLOCK_CHECK 없음"))
else:
    try:
        test("실제 설정 파일: 비밀번호와 맞음", ml.verify(env_pw))
    except ml.LockConfigError:
        test("실제 설정 파일: 비밀번호와 맞음", False, "설정 파일 없음 또는 형식 오류")

TMP = tempfile.mkdtemp(prefix="masterlock-test-")
XDG = os.path.join(TMP, "xdg")
os.makedirs(XDG)
saved_env = dict(os.environ)
os.environ["XDG_CONFIG_HOME"] = XDG
os.environ.pop("MASTERLOCK_CHECK", None)
CFG = os.path.join(XDG, "card-reader", "masterlock.json")
PW = "right-한글"
SALT = bytes(range(16))
ITER = ml.MIN_ITERATIONS
DIGEST = hashlib.pbkdf2_hmac("sha256", PW.encode(), SALT, ITER)
GOOD = {"format": 1, "algo": "pbkdf2-sha256", "salt": SALT.hex(), "iterations": ITER, "hash": DIGEST.hex()}


def put(data, path=CFG, raw=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw if raw is not None else json.dumps(data).encode())


def drop():
    shutil.rmtree(os.path.join(XDG, "card-reader"), ignore_errors=True)


def script(*answers):
    calls = []

    def ask(attempt):
        calls.append(attempt)
        return answers[len(calls) - 1]

    return ask, calls


def refuses_without_prompt(label):
    ask, calls = script(PW)
    real_err, sys.stderr = sys.stderr, io.StringIO()
    try:
        rc = exits(lambda: ml.require(ask))
    finally:
        sys.stderr = real_err
    test(label, rc == 1 and calls == [], f"rc={rc} calls={calls}")


# --- 경로
test("경로: XDG_CONFIG_HOME 존중", ml.config_path() == CFG)
del os.environ["XDG_CONFIG_HOME"]
os.environ["HOME"] = os.path.join(TMP, "home")
test("경로: XDG 없으면 ~/.config", ml.config_path() == os.path.join(TMP, "home", ".config", "card-reader", "masterlock.json"))
os.environ["XDG_CONFIG_HOME"] = XDG

# --- fail closed: 입력창 호출 없이 종료
drop()
refuses_without_prompt("값 없음: 입력 없이 종료")
put(None, raw=b"{not json")
refuses_without_prompt("손상된 JSON: 입력 없이 종료")
put(None, raw=b"\xff\xfe\x00")
refuses_without_prompt("깨진 인코딩: 입력 없이 종료")
put(None, raw=b"[1,2]")
refuses_without_prompt("JSON 이 객체가 아님: 입력 없이 종료")
for field in GOOD:
    d = dict(GOOD)
    del d[field]
    put(d)
    refuses_without_prompt(f"필드 누락({field}): 입력 없이 종료")
put(dict(GOOD, iterations=ITER - 1))
refuses_without_prompt("반복 수 60만 미만: 입력 없이 종료")
put(dict(GOOD, iterations=str(ITER)))
refuses_without_prompt("반복 수가 문자열: 입력 없이 종료")
put(dict(GOOD, salt="zz"))
refuses_without_prompt("솔트가 hex 아님: 입력 없이 종료")
put(dict(GOOD, salt="00" * 8))
refuses_without_prompt("솔트 16바이트 미만: 입력 없이 종료")
put(dict(GOOD, hash="00" * 16))
refuses_without_prompt("해시 길이 틀림: 입력 없이 종료")
put(dict(GOOD, algo="md5"))
refuses_without_prompt("알고리즘 틀림: 입력 없이 종료")
put(dict(GOOD, format=2))
refuses_without_prompt("형식 버전 틀림: 입력 없이 종료")
drop()
os.makedirs(CFG)  # 파일 자리에 폴더
refuses_without_prompt("읽기 실패(폴더): 입력 없이 종료")
drop()

# --- 정상 값
put(GOOD)
ask, calls = script(PW)
test("정상 값: 첫 시도에 통과", exits(lambda: ml.require(ask)) is None and calls == [1])
ask, calls = script("x", "y", PW)
test("정상 값: 세 번째에 통과", exits(lambda: ml.require(ask)) is None and calls == [1, 2, 3])
ask, calls = script("x", "y", "z", PW)
test("3회 틀리면 종료, 4번째는 묻지 않음", exits(lambda: ml.require(ask)) == 1 and calls == [1, 2, 3])
ask, calls = script(None)
test("취소하면 바로 종료", exits(lambda: ml.require(ask)) == 1 and calls == [1])
test("verify: 맞음/틀림/빈 값/None", ml.verify(PW) and not ml.verify("wrong") and not ml.verify("") and not ml.verify(None))
params = ml.load_params()
test("params 인자로 직접 값을 넘길 수 있음", ml.verify(PW, params) and not ml.verify("x", params))


def broken(_):
    raise RuntimeError("no input")


test("입력을 받을 수 없으면 종료", exits(lambda: ml.require(broken)) == 1)
gui, console = ml._gui_prompt, ml._console_prompt
ml._gui_prompt = broken
ml._console_prompt = lambda attempt: PW
test("입력창 실패 → 터미널 입력으로 대체", exits(ml.require) is None)
ml._console_prompt = broken
test("입력창·터미널 모두 실패 → 종료", exits(ml.require) == 1)
ml._gui_prompt, ml._console_prompt = gui, console

# --- frozen: 설정 파일 무시, 주입 모듈만
OTHER_PW = "build-pw"
OTHER = hashlib.pbkdf2_hmac("sha256", OTHER_PW.encode(), b"B" * 16, ITER)


class FakeBuild:
    SALT, ITERATIONS, HASH = (b"B" * 16).hex(), ITER, OTHER.hex()


had_frozen = hasattr(sys, "frozen")
sys.frozen = True
old_mod = sys.modules.get("masterlock_build", "absent")
try:
    sys.modules["masterlock_build"] = FakeBuild
    ask, calls = script(OTHER_PW)
    test("frozen: 주입 모듈의 비밀번호로 통과", exits(lambda: ml.require(ask)) is None)
    ask, calls = script(PW, PW, PW)
    test("frozen: 설정 파일의 비밀번호는 통과하지 못함", exits(lambda: ml.require(ask)) == 1 and calls == [1, 2, 3])
    drop()
    ask, calls = script(OTHER_PW)
    test("frozen: 설정 파일이 없어도 주입 모듈로 통과", exits(lambda: ml.require(ask)) is None)
    put(GOOD)
    sys.modules["masterlock_build"] = None  # import 실패
    refuses_without_prompt("frozen: 주입 모듈 없으면 설정 파일이 있어도 입력 없이 거부")

    class WeakBuild(FakeBuild):
        ITERATIONS = ITER - 1

    sys.modules["masterlock_build"] = WeakBuild
    refuses_without_prompt("frozen: 주입 모듈 반복 수 부족이면 거부")

    class PartialBuild:
        SALT = FakeBuild.SALT

    sys.modules["masterlock_build"] = PartialBuild
    refuses_without_prompt("frozen: 주입 모듈 값 누락이면 거부")
finally:
    if old_mod == "absent":
        sys.modules.pop("masterlock_build", None)
    else:
        sys.modules["masterlock_build"] = old_mod
    if had_frozen:
        sys.frozen = False
    else:
        del sys.frozen
drop()

# --- setup 도구
SETUP = os.path.join(HERE, "masterlock_setup.py")
cfg2 = os.path.join(TMP, "tool", "cfg", "masterlock.json")
build2 = os.path.join(TMP, "tool", "masterlock_build.py")
sub_env = {k: v for k, v in saved_env.items() if k != "MASTERLOCK_CHECK"}
sub_env["XDG_CONFIG_HOME"] = XDG


def run_setup(*args, stdin=""):
    return subprocess.run([sys.executable, SETUP, *args], input=stdin.encode(), capture_output=True,
                          env=sub_env, timeout=120)


def leaks(p, *secrets_):
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    return [s for s in secrets_ if s and s in out]


p = run_setup("--stdin", "--config-path", cfg2, stdin="pw-one\n")
data = json.load(open(cfg2)) if os.path.exists(cfg2) else {}
test("setup --stdin: 설정 파일 생성", p.returncode == 0 and data.get("format") == 1 and data.get("iterations") == ml.MIN_ITERATIONS
     and len(bytes.fromhex(data.get("salt", ""))) == 16, f"rc={p.returncode}")
if os.name == "posix":
    test("setup: 파일 권한 600", stat.S_IMODE(os.stat(cfg2).st_mode) == 0o600)
    test("setup: 새로 만든 폴더 권한 700", stat.S_IMODE(os.stat(os.path.dirname(cfg2)).st_mode) == 0o700)
test("setup: 출력에 평문·해시·솔트 없음", not leaks(p, "pw-one", data.get("hash"), data.get("salt")))
test("setup: 만든 값이 masterlock 검증과 일치", ml.verify("pw-one", ml._from_file(cfg2)) and not ml.verify("pw-two", ml._from_file(cfg2)))
before = open(cfg2, "rb").read()
p = run_setup("--stdin", "--config-path", cfg2, stdin="pw-two\n")
test("setup: 이미 있으면 --force 없이 거부, 내용 그대로", p.returncode != 0 and open(cfg2, "rb").read() == before)
p = run_setup("--stdin", "--force", "--config-path", cfg2, stdin="pw-two\n")
test("setup --force: 교체됨", p.returncode == 0 and ml.verify("pw-two", ml._from_file(cfg2)))
if os.name == "posix":
    test("setup --force: 권한 600 유지", stat.S_IMODE(os.stat(cfg2).st_mode) == 0o600)
test("setup: 임시 파일이 남지 않음", os.listdir(os.path.dirname(cfg2)) == ["masterlock.json"])
p = run_setup("--stdin", "--config-path", os.path.join(TMP, "e", "m.json"), stdin="\n")
test("setup --stdin: 빈 비밀번호 거부", p.returncode != 0 and not os.path.exists(os.path.join(TMP, "e", "m.json")))
p = run_setup("--stdin", "--config-path", os.path.join(TMP, "e", "m.json"), "--password", "x")
test("setup: 비밀번호 명령행 인자는 없음", p.returncode != 0)

# 빌드 모듈
cur = json.load(open(cfg2))
p = run_setup("--write-build-module", "--config-path", cfg2, "--build-path", build2)
spec = {}
if os.path.exists(build2):
    exec(compile(open(build2, encoding="utf-8").read(), "build", "exec"), spec)
test("setup --write-build-module: 설정 파일 값을 옮김",
     p.returncode == 0 and (spec.get("SALT"), spec.get("ITERATIONS"), spec.get("HASH")) == (cur["salt"], cur["iterations"], cur["hash"]))
if os.name == "posix":
    test("빌드 모듈 권한 600", stat.S_IMODE(os.stat(build2).st_mode) == 0o600)
test("빌드 모듈 출력에 평문·해시·솔트 없음", not leaks(p, "pw-two", cur["hash"], cur["salt"]))
test("빌드 모듈 파일에 평문 없음", "pw-two" not in open(build2, encoding="utf-8").read())
p = run_setup("--write-build-module", "--config-path", cfg2, "--build-path", build2)
test("빌드 모듈: 이미 있으면 --force 없이 거부", p.returncode != 0)
os.remove(build2)
none_cfg = os.path.join(TMP, "none", "m.json")
p = run_setup("--write-build-module", "--stdin", "--config-path", none_cfg, "--build-path", build2, stdin="pw-three\n")
spec = {}
if os.path.exists(build2):
    exec(compile(open(build2, encoding="utf-8").read(), "build", "exec"), spec)
test("빌드 모듈: 설정 파일이 없으면 새로 만들고 설정 파일은 만들지 않음",
     p.returncode == 0 and not os.path.exists(none_cfg) and spec.get("ITERATIONS") == ml.MIN_ITERATIONS
     and ml.verify("pw-three", ml.validate(1, "pbkdf2-sha256", spec["SALT"], spec["ITERATIONS"], spec["HASH"])))
test("setup 가 실제 사용자 설정 위치를 건드리지 않음", not os.path.exists(CFG))

# --- 정적 검사
ml_tree = ast.parse(source("masterlock.py"))
consts = [n.value for n in ast.walk(ml_tree) if isinstance(n, ast.Constant) and isinstance(n.value, (str, bytes))]
test("masterlock.py: hex 상수 없음", not any(isinstance(c, str) and re.fullmatch(r"[0-9a-fA-F]{16,}", c) for c in consts))
assigned = {t.id for n in ml_tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
test("masterlock.py: SALT·HASH 상수 없음", not ({"SALT", "HASH"} & assigned))
test("비교는 hmac.compare_digest", "hmac.compare_digest" in source("masterlock.py"))
test("masterlock.py: 경로·잠금 해제용 환경변수 없음",
     set(re.findall(r"environ(?:\.get)?[\[(]\s*[\"'](\w+)", source("masterlock.py"))) <= {"XDG_CONFIG_HOME", "APPDATA"})
test("setup: 표준 라이브러리만",
     all(n.names[0].name.split(".")[0] in sys.stdlib_module_names or n.names[0].name == "masterlock"
         for n in ast.walk(ast.parse(source("masterlock_setup.py"))) if isinstance(n, ast.Import)))

reader_tree = ast.parse(source("reader.py"))
main_if = [n for n in reader_tree.body if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)][-1]
test("reader: 시작 전 require 호출", "masterlock" in ast.dump(main_if.body[0]) and "require" in ast.dump(main_if.body[0]))
top = [a.name for n in reader_tree.body if isinstance(n, ast.Import) for a in n.names]
test("reader: 일반 import (try 안이 아님)", "masterlock" in top)
test("reader: _optional 로 불러오지 않음", '_optional("masterlock")' not in source("reader.py"))
app_tree = ast.parse(source("review_app.py"))
top = [a.name for n in app_tree.body if isinstance(n, ast.Import) for a in n.names]
test("review_app: 일반 import", "masterlock" in top)
app_main = next(n for n in app_tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
first = next(n for n in app_main.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)))
test("review_app: main 첫 동작이 require", "masterlock" in ast.dump(first) and "require" in ast.dump(first))
rd = source("README.md")
test("README: 두 빌드 명령 모두 masterlock·masterlock_build hidden-import",
     rd.count("--hidden-import masterlock ") == 2 and rd.count("--hidden-import masterlock_build ") == 2)

# --- git check-ignore (저장소 밖이면 건너뜀)
if shutil.which("git") and subprocess.run(["git", "-C", HERE, "rev-parse"], capture_output=True).returncode == 0:
    p = subprocess.run(["git", "-C", HERE, "check-ignore", "masterlock.json", "masterlock_build.py"], capture_output=True)
    test("git: masterlock.json, masterlock_build.py 무시됨", p.returncode == 0 and len(p.stdout.split()) == 2)
else:
    results.append(("SKIP", "git check-ignore", "git 저장소 아님"))

# --- 실제 실행: 임시 XDG 로, 입력창도 터미널도 없으면 작업을 시작하지 않는다
env = {k: v for k, v in sub_env.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
run = lambda: subprocess.run([sys.executable, "-I", os.path.join(HERE, "review_app.py"), "check-gto", HERE],
                             stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=60)
drop()
p = run()
test("review_app: 설정 없으면 종료 코드 1, 안내만 stderr", p.returncode == 1 and not p.stdout and p.stderr, f"rc={p.returncode}")
put(GOOD)
p = run()
test("review_app: 설정 있어도 입력 불가면 종료 코드 1, 출력 없음", p.returncode == 1 and not p.stdout, f"rc={p.returncode}")
drop()

os.environ.clear()
os.environ.update(saved_env)
shutil.rmtree(TMP, ignore_errors=True)

failed = 0
for st, name, info in results:
    failed += st == "FAIL"
    print(f"[{st}] {name}" + (f"  — {info}" if info else ""))
print(f"\n{sum(1 for s, *_ in results if s == 'PASS')}/{len(results)} passed, {failed} failed")
assert failed == 0, f"{failed} failed"
