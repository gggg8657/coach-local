"""portal_auth — 도구 서버 앞에 붙는 공통 관문. 표준 라이브러리만.

누가 보냈는지(X-Agent-User)를 도구가 그냥 믿지 않게 한다. 포털이 서명한 요청만 받고, 서명 없는 요청은 막는다.

두 가지 동작(환경변수 PORTAL_TOOL_KEY 로 정해짐):
  - 서명 모드(PORTAL_TOOL_KEY 있음 — 포털이 도구를 띄울 때 넘김, 다른 컨테이너 도구는 env 로 직접):
      X-Agent-Auth: v2.<unix초>.<hex> 가 맞아야만 받음. hex = HMAC-SHA256(키, "v2|<초>|<받는 도구 dir>|<X-Agent-User>"), 앞뒤 5분 안.
      받는 도구 이름이 서명에 들어가므로 한 도구 로그에 남은 서명을 다른 도구에 다시 쓸 수 없음(예전 v1 은 V1_UNTIL 전까지만).
      틀리거나 없으면 401 JSON. 사용자 = 서명된 X-Agent-User(비어 있으면 거부).
      관리자 계정이면 X-Agent-Role: admin + X-Agent-Role-Auth: r2.<초>.<hex>(hex = HMAC-SHA256(키, "r2|<초>|<받는 도구 dir>|<X-Agent-User>|<역할>"))도 붙음.
      사용자 서명과 역할 서명이 둘 다 맞을 때만 역할을 인정하고, 아니면 역할 헤더를 지움(도구가 헤더를 직접 읽어도 위장 역할을 못 봄).
      관리자 권한 판정은 is_admin(h): owner(로컬 주인 토큰·단독 실행) 또는 서명된 admin 역할(관리자 계정 — 작업 공간은 자기 u/<id>).
      역할을 모르는 예전 사본은 역할 헤더를 안 읽으므로 그 도구에서 관리자 권한이 없을 뿐 새지 않음.
  - 단독 모드(PORTAL_TOOL_KEY 없음 — 포털 없이 띄운 도구·예전 포털):
      이 서버 안(127.0.0.1·::1)에서 온 요청만 받음(그 밖은 403). X-Agent-User 가 없으면 예전처럼 owner. 역할 헤더는 확인할 키가 없으므로 지움.
  - PORTAL_AUTH_ALLOW_UNSIGNED=<IP 또는 CIDR, 쉼표>: 서명 없이도 받을 주소(옮기는 동안만 — 예전 포털을 다른 컨테이너에 둘 때). 기본 없음.
누구나 받는 경로: GET /healthz → {"ok": true, "tool": …, "version": …} (사용자 정보·데이터 없음, 헬스체크용).

도구 간 호출(예: avatar → tts, writer → kordoc)은 같은 키로, 받는 도구 이름을 넣어 서명해서 보냄: headers.update(portal_auth.sign(user, "tts-local")).
  관리자 권한을 넘겨야 하면 portal_auth.sign(user, "tts-local", role=portal_auth.role(h)).
오류 응답에는 scrub(문장) — 서버 절대 경로·내부 주소를 감춤(예외 문장을 그대로 내보내지 않게).
키는 포털 비밀값에서 만든 값 하나를 모든 도구가 함께 씀(도구끼리 서로 부르므로). 로그·화면·응답에 찍지 말 것.
shortcut: 공용 키라 뚫린 도구 하나는 다른 도구 앞으로 서명을 만들 수 있음 — 도구별 키는 도구별 uid·컨테이너로 나눌 때(같은 uid 면 env 를 서로 읽음).

쓰는 법(도구 app.py 맨 아래 서버 줄):
  import portal_auth
  ThreadingHTTPServer((os.environ.get("BIND", "127.0.0.1"), PORT), portal_auth.install(H, "kordoc-local")).serve_forever()

Copyright (c) 2026 gggg8657 (DongJu Kim). 이 파일은 각 도구 저장소에 그대로 복사해 씀(원본: agent-page-portal/sdk/portal_auth.py)."""
import hashlib, hmac, ipaddress, json, os, re, subprocess, time

VERSION = "2"
SKEW = 300   # 서명 시각 허용 오차(초)
# 옮기는 동안만: 예전 v1 서명(받는 도구 이름 없음)도 이 날짜(UTC) 전까지 받음 — 도구를 먼저 바꾸고 포털을 나중에 바꿔도 끊기지 않게.
# 이 날짜가 지나면 v1 은 401. 모든 도구·포털이 v2 로 바뀐 뒤 이 줄과 _v1() 을 지울 것
V1_UNTIL = "2026-11-04"
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
METHODS = ("do_GET", "do_POST", "do_PUT", "do_DELETE", "do_PATCH", "do_HEAD", "do_OPTIONS")


def _key():
    k = os.environ.get("PORTAL_TOOL_KEY", "").strip()
    return k.encode() if k else None


def _mac(key, ts, tool, user):
    return hmac.new(key, f"v2|{ts}|{tool}|{user}".encode(), hashlib.sha256).hexdigest()


def _role_mac(key, ts, tool, user, role):
    return hmac.new(key, f"r2|{ts}|{tool}|{user}|{role}".encode(), hashlib.sha256).hexdigest()


def sign(user, tool, now=None, role=None):
    """도구 → 도구 호출에 붙일 헤더. tool = 받는 도구 dir(예: "tts-local") — 그 도구에서만 통함.
    키가 없으면(단독 모드) 사용자 헤더만 — 받는 쪽도 단독 모드면 127.0.0.1 이라 통과. role(예: "admin")은 키가 있을 때만 서명해서 붙임"""
    user = str(user or "")
    h = {"X-Agent-User": user} if user else {}
    key = _key()
    if key and user:
        ts = str(int(now if now is not None else time.time()))
        h["X-Agent-Auth"] = f"v2.{ts}.{_mac(key, ts, tool, user)}"
        if role:
            h["X-Agent-Role"] = role
            h["X-Agent-Role-Auth"] = f"r2.{ts}.{_role_mac(key, ts, tool, user, role)}"
    return h


def verify(user, auth, tool, now=None):
    """서명 확인(tool = 이 도구 dir) — 맞으면 True. 키가 없으면 False(서명 모드가 아님)"""
    key = _key()
    if not (key and user and auth):
        return False
    parts = auth.strip().split(".")
    now = now if now is not None else time.time()
    if len(parts) != 3 or not parts[1].isdigit() or abs(now - int(parts[1])) > SKEW:
        return False
    if parts[0] == "v1" and time.strftime("%Y-%m-%d", time.gmtime(now)) < V1_UNTIL:
        return hmac.compare_digest(parts[2], hmac.new(key, f"v1|{parts[1]}|{user}".encode(), hashlib.sha256).hexdigest())
    return parts[0] == "v2" and hmac.compare_digest(parts[2], _mac(key, parts[1], tool, user))


def verify_role(user, role, auth, tool, now=None):
    """역할 서명 확인(tool = 이 도구 dir) — 사용자·역할·받는 도구가 함께 서명돼 있어야 True. 키가 없으면 False"""
    key = _key()
    if not (key and user and role and auth):
        return False
    parts = auth.strip().split(".")
    now = now if now is not None else time.time()
    if len(parts) != 3 or parts[0] != "r2" or not parts[1].isdigit() or abs(now - int(parts[1])) > SKEW:
        return False
    return hmac.compare_digest(parts[2], _role_mac(key, parts[1], tool, user, role))


def _drop_role(h):
    for k in ("X-Agent-Role", "X-Agent-Role-Auth"):
        while k in h.headers:
            del h.headers[k]


def user(h):
    """요청의 사용자(관문을 지난 뒤). 헤더가 없으면(단독 실행) owner"""
    return (h.headers.get("X-Agent-User") or "owner").strip() or "owner"


def role(h):
    """관문이 확인한 역할(없으면 "") — check 가 확인 못 한 역할 헤더는 이미 지웠음"""
    return (h.headers.get("X-Agent-Role") or "").strip()


def is_admin(h):
    """관리자 권한: owner(로컬 주인 토큰·단독 실행) 또는 포털이 서명한 admin 역할(관리자 계정 — 작업 공간은 자기 u/<id>)"""
    return user(h) == "owner" or role(h) == "admin"


_PATH = re.compile(r"(?<![\w.])/(?:home|root|app|data|srv|opt|tmp|var|usr|mnt|models|shared|workspace)(?:/[^\s'\"),;]*)?")
_HOST = re.compile(r"\bhttps?://[^\s'\"/]+")


def scrub(msg):
    """오류 문장에서 서버 절대 경로·내부 주소(http://호스트:포트)를 감춤 — 응답·화면에 내보내기 전에"""
    return _HOST.sub("<내부주소>", _PATH.sub("<경로>", str(msg)))


def _nets():
    out = []
    for x in os.environ.get("PORTAL_AUTH_ALLOW_UNSIGNED", "").split(","):
        x = x.strip()
        if x:
            try:
                out.append(ipaddress.ip_network(x, strict=False))
            except ValueError:
                pass
    return out


def _allowed_unsigned(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in _nets())


def _version(root):
    v = os.environ.get("TOOL_VERSION")
    if v:
        return v
    try:
        return subprocess.run(["git", "-C", root, "describe", "--tags", "--always", "--dirty"], capture_output=True, text=True, timeout=3).stdout.strip() or None
    except Exception:
        return None


def _send(h, code, obj):
    b = json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(b)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(b)


def check(h, tool):
    """요청 하나를 받을지 판정. 받으면 True(도구 처리로 넘어감), 아니면 응답을 보내고 False"""
    path = h.path.split("?", 1)[0]
    if path == "/healthz" and h.command in ("GET", "HEAD"):
        _send(h, 200, {"ok": True, "tool": tool, "version": _VER.get(tool), "auth": "signed" if _key() else "standalone"})
        return False
    ip = (h.client_address or ("",))[0]
    user, auth = h.headers.get("X-Agent-User"), h.headers.get("X-Agent-Auth")
    if _key():
        if verify(user, auth, tool):
            r = h.headers.get("X-Agent-Role")
            if r and not verify_role(user, r.strip(), h.headers.get("X-Agent-Role-Auth"), tool):
                _drop_role(h)
            return True
        if not auth and _allowed_unsigned(ip):
            _drop_role(h)
            return True
        _send(h, 401, {"error": "포털을 거쳐서 요청해 주세요(서명 없음 또는 틀림)", "code": "unauthenticated"})
        return False
    if ip in LOOPBACK or _allowed_unsigned(ip):
        _drop_role(h)
        return True
    _send(h, 403, {"error": "이 도구는 포털을 거쳐서만 쓸 수 있습니다", "code": "forbidden"})
    return False


_VER = {}


def _tune_server():
    """도구 서버(http.server ThreadingHTTPServer) 대기열: 기본 listen backlog 5 는 몰릴 때 SYN 이 버려져 1~3초씩 다시 기다림
    (부하 시험 2026-10-20: 동시 64 이상 p99 1~4 s). TOOL_LISTEN_BACKLOG(기본 128)로 늘림. install() 은 서버를 만들기 전에 불리므로
    (ThreadingHTTPServer((주소), install(H, …))) 여기서 바꾸면 그 서버부터 적용. daemon_threads 는 ThreadingHTTPServer 기본값(True) 그대로"""
    import http.server
    try:
        n = int(os.environ.get("TOOL_LISTEN_BACKLOG") or 128)
    except ValueError:
        n = 128
    if n > http.server.ThreadingHTTPServer.request_queue_size:
        http.server.ThreadingHTTPServer.request_queue_size = n


def install(H, tool, root=None):
    """핸들러 클래스 H 의 do_* 앞에 관문을 붙여 돌려줌(같은 클래스). 두 번 불러도 한 번만 붙음. 서버 대기열도 늘림(_tune_server)"""
    _tune_server()
    if getattr(H, "_portal_auth", None):
        return H
    _VER[tool] = _version(root or os.path.dirname(os.path.abspath(__import__("sys").modules[H.__module__].__file__)))
    for name in METHODS:
        fn = getattr(H, name, None)
        if fn is None and name != "do_GET":
            continue

        def make(fn, name):
            def gated(self):
                if not check(self, tool):
                    return
                if fn is None:   # do_GET 이 없는 도구도 /healthz 는 되게
                    return _send(self, 404, {"error": "not found", "code": "not_found"})
                return fn(self)
            gated.__name__ = name
            return gated

        setattr(H, name, make(fn, name))
    H._portal_auth = tool
    return H
