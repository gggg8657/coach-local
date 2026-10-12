#!/usr/bin/env python3
"""LLM 없이 검증 (가짜 LLM): 페르소나 정리·프롬프트 → 문단 검사(자리 확인·없는 조각 버림·분류 정규화) → 취소
→ 전체 검토 → 문서 저장·판 기록 → 사용자 분리(HTTP, X-Agent-User) → 페르소나 저장·삭제 → docx 쓰고 읽기 → ui.html.
WORKSPACE 는 임시 폴더로 바꿔 실데이터 폴더에 흔적을 남기지 않는다.   python3 selftest.py"""
import base64, json, os, shutil, sys, tempfile, threading, urllib.error, urllib.request

TMP = tempfile.mkdtemp(prefix="coach-selftest-")
os.environ["WORKSPACE"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

SEEN = []


def fake(system, user, model=None, stop=lambda: False):
    SEEN.append((system, user))
    if stop():
        raise app.Cancelled()
    if "글 전체를" in system:
        return {"kind": "메시지·메일", "summary": "요청이 뒤에 있습니다.", "good": ["날짜 표기 정확"], "issues": [
            {"q": "참석 바랍니다요", "t": "요청이 끝에 묻힘", "f": "첫 항목으로", "c": "논리"},
            {"q": "본문에 없는 말", "t": "붙임 누락", "f": "붙임 표기", "c": "형식 관례"}]}
    if "교정기" in system:  # 맞춤법 검사: 어절 단위
        return {"s": [{"p": "p0", "o": "월요일 에", "n": "월요일에", "r": "조사는 붙여 씀"},
                      {"p": "p0", "o": "다음주 월요일 에 할", "n": "다음 주 월요일에 할", "r": "띄어쓰기"},   # 3어절 → 조각으로 나눔
                      {"p": "p0", "o": "없는 조각", "n": "x"}]}                                            # 문단에 없음 → 버림
    return {"s": [
        {"p": "p0", "o": "월요일 에", "n": "월요일에", "r": "맞춤법은 맞춤법 검사 몫", "c": "맞춤법"},   # 문체 검사의 맞춤법 → 버림
        {"p": "[p0]", "o": "바랍니다요", "n": "바람", "r": "공문 종결", "c": "문체·어조"},
        {"p": "p0", "o": "회의는 다음주 월요일 에 할 예정임.", "n": "회의는 다음 주 월요일에 열 예정임.", "r": "x", "c": "형식"},  # 문장 대부분 → 조각, 겹치면 짧은 쪽
        {"p": "p1", "o": "12,000,000원", "n": "12,000,000원", "r": "견적서 금액과 대조", "c": "사실"},
        {"p": "p1", "o": "견적", "n": "견적", "r": "같은 말", "c": "명료"},          # 바뀐 것 없음 → 버림
        {"p": "p9", "o": "x", "n": "y", "c": "맞춤법"}]}


app.llm = fake
P = app.BUILTIN[0]

# 1) 기본 페르소나 8종, 필수 필드
assert len(app.BUILTIN) >= 8 and {"공문서", "편지", "논문", "사설·칼럼", "메시지·메일", "보고서", "보도자료", "자기소개서"} <= {p["name"] for p in app.BUILTIN}
for p in app.BUILTIN:
    assert p["rules"] and p["avoid"] and p["tone"] and p["audience"], p["name"]
t = app.persona_text(P)
assert "공문서" in t and "끝." in t and "피할 것" in t
try:
    app.clean_persona({"name": " "}); assert False
except ValueError:
    pass
assert app.clean_persona({"name": "x", "rules": "a\n\n b "})["rules"] == ["a", "b"]

# 2) 문단 검사: 원문에 있는 조각만, 분류 정규화, 위치
units = [{"id": "p0", "text": "회의는 다음주 월요일 에 할 예정임. 참석 바랍니다요."}, {"id": "p1", "text": "금액: 12,000,000원(견적 기준)"}]
r = app.check(units, P, "부서장 승인 요청")
got = [(s["p"], s["o"], s["n"], s["c"]) for s in r["s"]]
assert got == [("p0", "다음주", "다음 주", "맞춤법"), ("p0", "월요일 에", "월요일에", "맞춤법"),   # 형식 다시 쓰기는 구절로 줄였지만 맞춤법과 겹쳐 빠짐
               ("p0", "바랍니다요", "바람", "문체"), ("p1", "12,000,000원", "12,000,000원", "사실")], got
assert r["dropped"] == 6, r["dropped"]
assert [s["c"] for s in app.check(units, P, passes=("spell",))["s"]] == ["맞춤법"] * 2
assert app._accept({"p": "1", "o": "가", "n": "카"}, {"p0": "x", "p1": "가 나"}, "맞춤법", [])[0]["p"] == "p1"   # "1" → p1
_big = {"p": "p0", "o": "회의는 다음주 월요일 에 할 예정임.", "n": "회의는 다음 주 월요일에 할 예정임.", "r": "x", "c": "문체"}
assert len(app._accept(_big, {"p0": _big["o"]}, None, [])) == 1                                        # 문체 다시 쓰기는 통째로
assert [x["o"] for x in app._accept(_big, {"p0": _big["o"]}, None, [], minimal=True)] == ["다음주 월요일 에"]  # 글 종류 어긋남 → 조각만
assert app._split("가 나 다", "가 라 다") == [(2, "나", "라")] and app._split("가 나", "라 마") == []
assert app._split("1. 상기 건에 대하여 협조", "1. 위 호와 관련하여 협조") == [(3, "상기 건에 대하여", "위 호와 관련하여")]       # 이웃 어절은 구절로
assert app._split("다음주 월요일 에", "다음 주 월요일에", merge=False) == [(0, "다음주", "다음 주"), (4, "월요일 에", "월요일에")]
assert all(units[int(s["p"][1])]["text"][s["at"]:].startswith(s["o"]) for s in r["s"])
sty = [x for x in SEEN if "교정기" not in x[0]][-1]
assert "부서장 승인 요청" in sty[0] and "[p1] ▶ 금액" in sty[1] and any("교정기" in x[0] and "공문서" in x[0] for x in SEEN)
assert app.check([{"id": "p0", "text": "  "}], P) == {"s": [], "dropped": 0}
try:
    app.check(units, P, stop=lambda: True); assert False
except app.Cancelled:
    pass

# 2-1) 글 파악·규칙 검사·문맥 붙여 검사 (가짜 LLM)
DOC = "장비 구매 요청\n1. 위와 같이 고속 카메라 구매를 요청함.\n  가. 금액: 1,200만 원\n2. 고속카메라는 LSTM 분석에 씀.\n3. 총 1,500만 원 집행을 승인 바람.\n4. 회의는 2026. 10. 15.(수)임.\n붙임  견적서 1부."
hp = app.heur_profile(DOC)
assert hp["style"] == "개조식" and hp["variants"][0]["first"] == "spaced" and hp["abbr"][0]["a"] == "LSTM" and not hp["abbr"][0]["def"]
rules, ver = app.rule_checks(DOC, range(8), P, hp)
rr = {(x["i"], x["o"], x["n"]) for x in rules}
assert (1, "위와 같이", "위와 같이") in rr and (3, "고속카메라", "고속 카메라") in rr and (3, "LSTM", "LSTM") in rr, rr
assert (4, "1,500만 원", "1,500만 원") in rr and (5, "수", "목") in rr and (6, "1부.", "1부.  끝.") in rr, rr
assert app.rule_checks(DOC.replace("(수)", "(목)"), [5], P, hp)[1]                       # 맞는 요일은 verified
ut = app.rule_checks(DOC, [1], P, app.heur_profile(DOC) | {"user": {"terms": "고속카메라"}})[0]
assert any(x["o"] == "고속 카메라" and x["n"] == "고속카메라" for x in ut)                 # 사용자가 정한 표기가 기준
assert app.josa("장비", "이", "가") == "가" and app.josa("카메라", "으로", "로") == "로" and app.josa("결과", "이", "가") == "가" and app.josa("용어", "으로", "로") == "로"
SEEN.clear()
cd = app.check_doc(DOC, [3], P, "승인 요청", "제목", {"llm": {"ask": "카메라 구매 승인"}, "user": {"style": "개조식"}})
sty = [x for x in SEEN if "교정기" not in x[0]][-1]
assert "위치: 본문 4/7" in sty[1] and "앞: «1. 위와 같이" in sty[1] and "뒤: «3. 총" in sty[1] and "▶ 2. 고속카메라는" in sty[1], sty[1]
assert "핵심 요청·주장: 카메라 구매 승인" in sty[0] and "사용자 지정" in sty[0] and "제목: 제목" in sty[0]
assert any("고속 카메라" in x[0] for x in SEEN if "교정기" in x[0])                       # 맞춤법 검사에도 정한 용어 표기
assert all(x["i"] == 3 for x in cd["s"]) and any(x.get("src") == "rule" for x in cd["s"])

# 3) 전체 검토: 본문에 없는 인용은 비움
rv = app.review("회의는 월요일. 참석 바랍니다요.", P)
assert rv["kind"] == "메시지·메일" and "메시지·메일" in SEEN[-1][0]
assert rv["issues"][0]["q"] == "참석 바랍니다요" and rv["issues"][1]["q"] == "" and rv["issues"][1]["c"] == "형식"

# 4) 문서 저장·판 기록
d = app.save_doc({"text": "첫 판", "persona": "gongmun", "purpose": "p"}, TMP)
d2 = app.save_doc({"id": d["id"], "text": "둘째 판", "snapshot": True}, TMP)
assert d2["id"] == d["id"] and d2["versions"] == 2 and d2["title"] == "둘째 판"
assert app.save_doc({"id": d["id"], "text": "셋째"}, TMP)["versions"] == 2   # 10분 안 자동 저장은 판을 늘리지 않음
assert [x["id"] for x in app.list_docs(TMP)] == [d["id"]]
try:
    app.doc_path("../../etc/passwd", TMP); assert False
except FileNotFoundError:
    pass

# 5) docx 쓰고 다시 읽기
b = app.docx_bytes("제목", "첫 줄 <&>\n둘째 줄")
assert app.docx_text(b) == "제목\n첫 줄 <&>\n둘째 줄"
assert app.import_file("a.txt", "안녕".encode("cp949"), TMP) == "안녕"

# 6) HTTP + 사용자 분리
srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"


def call(path, body=None, user=None):
    h = {"Content-Type": "application/json", **({"X-Agent-User": user} if user else {})}
    req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


c, b = call("/api/check", {"rid": "r1", "units": units, "persona": P, "purpose": ""})
assert c == 200 and len(json.loads(b)["s"]) == 4 and "ms" in json.loads(b)
c, b = call("/api/check", {"rid": "r3", "pass": "rules", "text": DOC, "idx": [5], "persona": P})
assert c == 200 and any(x["o"] == "수" and x["n"] == "목" and x["i"] == 5 for x in json.loads(b)["s"])
c, b = call("/api/profile", {"text": DOC, "llm": False})
assert c == 200 and json.loads(b)["style"] == "개조식" and "llm" not in json.loads(b)
c, b = call("/api/check", {"rid": "r2", "pass": "style", "units": units, "persona": P})
assert c == 200 and "맞춤법" not in [s["c"] for s in json.loads(b)["s"]]
c, b = call("/api/docs", {"text": "앨리스 글"}, "alice")
aid = json.loads(b)["id"]
assert os.path.exists(os.path.join(TMP, "u", "alice", "docs", aid + ".json"))
assert json.loads(call("/api/docs", user="bob")[1]) == []
assert call(f"/api/docs/{aid}", user="bob")[0] == 404
assert call("/api/docs/delete", {"id": aid}, "bob")[0] == 404
assert call(f"/api/docs/{aid}", user="alice")[0] == 200
assert [x["id"] for x in json.loads(call("/api/docs")[1])] == [d["id"]]   # owner 목록에 u/ 안 섞임
c, b = call("/api/personas", {"name": "내 공문", "rules": ["끝."]}, "alice")
pid = json.loads(b)["id"]
assert pid.startswith("u-") and any(p["id"] == pid for p in json.loads(call("/api/personas", user="alice")[1]))
assert not any(p["id"] == pid for p in json.loads(call("/api/personas", user="bob")[1]))
assert call("/api/personas/delete", {"id": pid}, "bob")[0] == 404
assert call("/api/personas/delete", {"id": pid}, "alice")[0] == 200
assert call("/api/personas", {"name": ""})[0] == 400
c, b = call("/api/export", {"title": "제목", "text": "본문"})
assert c == 200 and b[:2] == b"PK"
c, b = call("/api/import", {"file_name": "x.md", "file_b64": base64.b64encode("# 제목\n본문".encode()).decode()})
assert json.loads(b)["text"] == "# 제목\n본문"
assert call("/api/cancel", {"rids": ["zz"]})[0] == 200
c, b = call("/")
assert c == 200 and b"data-sig" in b and "글쓰기 코치".encode() in b and b"%CATS%" not in b
srv.shutdown()

# 저작권 표기: 서버가 화면에 붙이는 코드가 있어야 한다 (LICENSE·NOTICE)
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
assert "wqkgMjAyNiBnZ2dnODY1NyDCtyBkb25nanVraW0uZGV2QGdtYWlsLmNvbQ==" in _src and "signed(" in _src and "X-Author" in _src, "저작권 표기 누락"

shutil.rmtree(TMP)
print("selftest OK —", len(app.BUILTIN), "personas, check/review/docs/user-split/docx")
