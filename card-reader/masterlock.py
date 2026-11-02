"""실행 잠금: 마스터 비밀번호가 맞아야 프로그램이 시작된다.

솔트를 붙인 PBKDF2-HMAC-SHA256 해시만 쓰며, 코드에는 값이 없다 (소스 실행은 사용자 설정 파일, exe는 빌드 때 넣은 masterlock_build 모듈). 입력창은 글자를 가리고, 취소하거나 3회 틀리면 종료한다.
입력창을 띄울 수 없으면 (tkinter·화면 없음) 터미널에서 가린 입력을 받고, 터미널도 없으면 종료한다.

이 모듈을 _optional 로 불러오지 말 것: 빠졌을 때 잠금이 조용히 풀린다.
"""
import getpass
import hashlib
import hmac
import json
import os
import sys

MAX_ATTEMPTS = 3
MIN_ITERATIONS = 600000
MIN_SALT_BYTES = 16
HASH_BYTES = 32
MAX_FILE_BYTES = 4096
CONFIG_NAME = "masterlock.json"
FORMAT = 1
ALGO = "pbkdf2-sha256"


class LockConfigError(Exception):
    """잠금 값이 없거나 읽을 수 없거나 형식이 틀리다. 메시지에 값은 넣지 않는다."""


def config_path():
    """소스 실행 때 설정 파일 위치. Windows %APPDATA%, 그 밖에는 XDG_CONFIG_HOME 또는 ~/.config."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        if not base or not os.path.isabs(base):
            home = os.path.expanduser("~")
            base = os.path.join(home, ".config") if home and home != "~" else None
    if not base:
        raise LockConfigError("설정 폴더를 알 수 없음")
    return os.path.join(base, "card-reader", CONFIG_NAME)


def validate(fmt, algo, salt_hex, iterations, hash_hex):
    """형식이 맞으면 (salt, iterations, hash) 바이트·정수 튜플, 아니면 LockConfigError."""
    if fmt != FORMAT or algo != ALGO:
        raise LockConfigError("지원하지 않는 형식")
    if not isinstance(salt_hex, str) or not isinstance(hash_hex, str):
        raise LockConfigError("솔트·해시 형식 오류")
    try:
        salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except ValueError:
        raise LockConfigError("솔트·해시 형식 오류") from None
    if len(salt) < MIN_SALT_BYTES or len(digest) != HASH_BYTES:
        raise LockConfigError("솔트·해시 길이 오류")
    if type(iterations) is not int or iterations < MIN_ITERATIONS:
        raise LockConfigError("반복 수 부족")
    return salt, iterations, digest


def _from_file(path):
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise LockConfigError("설정 파일이 너무 큼")
        data = json.loads(raw.decode("utf-8"))
    except LockConfigError:
        raise
    except (OSError, ValueError):  # 없음, 권한, 깨진 JSON·UTF-8
        raise LockConfigError("설정 파일을 읽을 수 없음") from None
    if not isinstance(data, dict):
        raise LockConfigError("설정 파일 형식 오류")
    return validate(data.get("format"), data.get("algo"), data.get("salt"), data.get("iterations"), data.get("hash"))


def _from_build():
    try:
        import masterlock_build  # 빌드 때 masterlock_setup.py --write-build-module 로 만든다
        values = (masterlock_build.SALT, masterlock_build.ITERATIONS, masterlock_build.HASH)
    except Exception:
        raise LockConfigError("빌드에 잠금 값이 없음") from None
    return validate(FORMAT, ALGO, values[0], values[1], values[2])


def load_params():
    """(salt, iterations, hash). exe 는 주입된 모듈만, 소스 실행은 사용자 설정 파일만 읽는다."""
    if getattr(sys, "frozen", False):
        return _from_build()
    return _from_file(config_path())


def verify(password, params=None):
    if not isinstance(password, str):
        return False
    salt, iterations, digest = params if params is not None else load_params()
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, digest)


def _gui_prompt(attempt):
    """입력한 문자열, 취소면 None."""
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    try:
        note = "" if attempt == 1 else f"틀렸습니다. ({attempt}/{MAX_ATTEMPTS}회째)\n"
        return simpledialog.askstring("잠금", note + "마스터 비밀번호:", show="*", parent=root)
    finally:
        root.destroy()


def _console_prompt(attempt):
    """입력한 문자열, 취소(Ctrl+C, Ctrl+D)면 None. 터미널이 없으면 RuntimeError."""
    if sys.stdin is None or not sys.stdin.isatty():
        raise RuntimeError("no terminal")
    try:
        return getpass.getpass(f"마스터 비밀번호 ({attempt}/{MAX_ATTEMPTS}): ")
    except (EOFError, KeyboardInterrupt):
        return None


def _default_prompt(attempt):
    try:
        return _gui_prompt(attempt)
    except Exception:  # tkinter 없음, 화면 없음 (TclError)
        return _console_prompt(attempt)


def _refuse(msg):
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg + "\n")
    except Exception:
        pass
    raise SystemExit(1)


def require(prompt=None, params=None):
    """비밀번호가 맞으면 돌아오고, 그 밖의 모든 경우(값 없음·오류, 취소, 3회 실패, 입력 불가)에는 SystemExit(1).

    잠금 값을 못 읽으면 입력창을 띄우기 전에 거부한다. params 는 테스트용으로 값을 직접 넘길 때만 쓴다."""
    if params is None:
        try:
            params = load_params()
        except LockConfigError:
            _refuse("잠금 설정이 없거나 올바르지 않아 실행하지 않습니다. README의 마스터 잠금 설치 절차를 따르세요.")
        except Exception:
            _refuse("잠금 설정을 확인할 수 없어 실행하지 않습니다.")
    ask = prompt or _default_prompt
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            entered = ask(attempt)
        except Exception:
            raise SystemExit(1)
        if entered is None:
            raise SystemExit(1)
        try:
            ok = verify(entered, params)
        except Exception:
            raise SystemExit(1)
        if ok:
            return
    raise SystemExit(1)
