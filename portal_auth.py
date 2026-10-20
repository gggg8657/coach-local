"""portal_auth — 도구 서버 앞에 붙는 공통 관문. 표준 라이브러리만.

누가 보냈는지(X-Agent-User)를 도구가 그냥 믿지 않게 한다. 포털이 서명한 요청만 받고, 서명 없는 요청은 막는다.

두 가지 동작(환경변수 PORTAL_TOOL_KEY 로 정해짐):
  - 서명 모드(PORTAL_TOOL_KEY 있음 — 포털이 도구를 띄울 때 넘김, 다른 컨테이너 도구는 env 로 직접):
      X-Agent-Auth: v1.<unix초>.<hex> 가 맞아야만 받음. hex = HMAC-SHA256(키, "v1|<초>|<X-Agent-User>"), 앞뒤 5분 안.
      틀리거나 없으면 401 JSON. 사용자 = 서명된 X-Agent-User(비어 있으면 거부).
  - 단독 모드(PORTAL_TOOL_KEY 없음 — 포털 없이 띄운 도구·예전 포털):
      이 서버 안(127.0.0.1·::1)에서 온 요청만 받음(그 밖은 403). X-Agent-User 가 없으면 예전처럼 owner.
  - PORTAL_AUTH_ALLOW_UNSIGNED=<IP 또는 CIDR, 쉼표>: 서명 없이도 받을 주소(옮기는 동안만 — 예전 포털을 다른 컨테이너에 둘 때). 기본 없음.
누구나 받는 경로: GET /healthz → {"ok": true, "tool": …, "version": …} (사용자 정보·데이터 없음, 헬스체크용).

도구 간 호출(예: avatar → tts, writer → kordoc)은 같은 키로 서명해서 보냄: headers.update(portal_auth.sign(user)).
오류 응답에는 scrub(문장) — 서버 절대 경로·내부 주소를 감춤(예외 문장을 그대로 내보내지 않게).
키는 포털 비밀값에서 만든 값 하나를 모든 도구가 함께 씀(도구끼리 서로 부르므로). 로그·화면·응답에 찍지 말 것.

쓰는 법(도구 app.py 맨 아래 서버 줄):
  import portal_auth
  ThreadingHTTPServer((os.environ.get("BIND", "127.0.0.1"), PORT), portal_auth.install(H, "kordoc-local")).serve_forever()

Copyright (c) 2026 gggg8657 (DongJu Kim). 이 파일은 각 도구 저장소에 그대로 복사해 씀(원본: agent-page-portal/sdk/portal_auth.py)."""
import hashlib, hmac, ipaddress, json, os, re, subprocess, time

VERSION = "1"
SKEW = 300   # 서명 시각 허용 오차(초)
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
METHODS = ("do_GET", "do_POST", "do_PUT", "do_DELETE", "do_PATCH", "do_HEAD", "do_OPTIONS")


def _key():
    k = os.environ.get("PORTAL_TOOL_KEY", "").strip()
    return k.encode() if k else None


def _mac(key, ts, user):
    return hmac.new(key, f"v1|{ts}|{user}".encode(), hashlib.sha256).hexdigest()


def sign(user, now=None):
    """도구 → 도구 호출에 붙일 헤더. 키가 없으면(단독 모드) 사용자 헤더만 — 받는 쪽도 단독 모드면 127.0.0.1 이라 통과"""
    user = str(user or "")
    h = {"X-Agent-User": user} if user else {}
    key = _key()
    if key and user:
        ts = str(int(now if now is not None else time.time()))
        h["X-Agent-Auth"] = f"v1.{ts}.{_mac(key, ts, user)}"
    return h


def verify(user, auth, now=None):
    """서명 확인 — 맞으면 True. 키가 없으면 False(서명 모드가 아님)"""
    key = _key()
    if not (key and user and auth):
        return False
    parts = auth.strip().split(".")
    if len(parts) != 3 or parts[0] != "v1" or not parts[1].isdigit():
        return False
    if abs((now if now is not None else time.time()) - int(parts[1])) > SKEW:
        return False
    return hmac.compare_digest(parts[2], _mac(key, parts[1], user))


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
        if verify(user, auth):
            return True
        if not auth and _allowed_unsigned(ip):
            return True
        _send(h, 401, {"error": "포털을 거쳐서 요청해 주세요(서명 없음 또는 틀림)", "code": "unauthenticated"})
        return False
    if ip in LOOPBACK or _allowed_unsigned(ip):
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
