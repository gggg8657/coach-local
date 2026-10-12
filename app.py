#!/usr/bin/env python3
"""coach local — 쓰는 동안 고칠 곳을 제안하는 글쓰기 코치 (공문서·편지·논문·사설·메일 …). 로컬 LLM, 외부 의존성 없음(stdlib).

  python3 app.py                                    # http://localhost:8798
  LLM_API=openai LLM_BASE_URL=http://gpu:8000/v1 LLM_MODEL=Qwen3-32B python3 app.py
  python3 app.py --cli gongmun 초안.txt              # 문단별 제안을 터미널에

페르소나(글 종류 코치) = JSON 하나: name·audience·tone·rules·avoid·example. 기본은 personas.json, 사용자 것은 사용자 폴더 personas/.
제안은 LLM 이 돌려준 원문 조각이 그 문단에 글자 그대로 있을 때만 남긴다(자리 고정).
"""
import base64
import datetime
import io
import json
import os
import re
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
WS = os.environ.get("WORKSPACE") or os.path.join(ROOT, "_workspace")  # 포털이 AGENT_DATA/<도구> 로 모아 줌
LLM_API = os.environ.get("LLM_API", "ollama")            # ollama | openai
LLM_BASE = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1" if LLM_API == "openai" else "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
LLM_KEY = os.environ.get("LLM_API_KEY", "")
NUM_CTX = int(os.environ.get("NUM_CTX", "32768"))   # 포털은 모든 도구에 같은 값을 넘김 — 도구마다 다르면 Ollama 가 모델을 다시 올림
PORT = int(os.environ.get("PORT", "8798"))
KORDOC_CLI = os.environ.get("KORDOC_CLI") or os.path.join(os.path.dirname(ROOT), "kordoc-local", "node_modules", "kordoc", "dist", "cli.js")
CATS = {"맞춤법": "맞춤법·띄어쓰기", "문체": "문체·어조", "형식": "형식·관례", "명료": "명료성·간결", "논리": "논리·구조", "사실": "사실 확인 필요"}
DOC_RE = r"\d{8}-\d{6}-[0-9a-f]{4}"
PERSONA_RE = r"u-[0-9a-f]{6}"
PLACEHOLDER = re.compile(r"X{2}|x{2}|○○|20\dX|\(문서번호\)|\(날짜\)")


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


BUILTIN = json.loads(read(os.path.join(ROOT, "personas.json")))
for _p in BUILTIN:
    _p["builtin"] = True


# ── LLM (JSON 한 번에) ─────────────────────────────────────────────────────
class Cancelled(Exception):
    pass


def _json(out):
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    m = re.search(r"\{.*\}", out, flags=re.S)
    if not m:
        raise ValueError(f"JSON 아님: {out[:200]}")
    return json.loads(m.group(0))


def llm(system, user, model=MODEL, stop=lambda: False):
    """JSON 응답 하나. stop() 이 참이 되면 LLM 연결을 끊어 생성을 멈춘다(오래된 요청 취소)."""
    if LLM_API == "openai":
        body = {"model": model, "temperature": 0.2, "stream": True, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        hdr = {"Content-Type": "application/json", **({"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})}
        url, pick = LLM_BASE + "/chat/completions", lambda j: (j.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
    else:
        body = {"model": model, "stream": True, "think": False, "format": "json", "keep_alive": "30m",
                "options": {"temperature": 0.2, "num_ctx": NUM_CTX},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        hdr = {"Content-Type": "application/json"}
        url, pick = LLM_BASE + "/api/chat", lambda j: j.get("message", {}).get("content", "")
    for attempt in (0, 1):
        buf = []
        try:
            with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), hdr), timeout=600) as r:
                for line in r:
                    if stop():
                        raise Cancelled()
                    line = line.decode().strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        continue
                    j = json.loads(line)
                    if "error" in j:
                        raise RuntimeError(j["error"])
                    buf.append(pick(j))
                    if j.get("done"):
                        break
            return _json("".join(buf))
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if attempt == 0 and LLM_API != "openai" and "think" in msg:
                body.pop("think")
                continue
            if attempt == 0 and LLM_API == "openai" and "response_format" in msg:
                body.pop("response_format")
                continue
            raise RuntimeError(f"LLM HTTP {e.code}: {msg[:300]}")
        except (ValueError, json.JSONDecodeError):
            if attempt == 0:  # 깨진 JSON — 한 번만 다시
                continue
            raise
    raise RuntimeError("LLM 응답 없음")


def models():
    if LLM_API == "openai":
        req = urllib.request.Request(LLM_BASE + "/models", headers={"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return [m["id"] for m in json.load(r)["data"]]
    with urllib.request.urlopen(LLM_BASE + "/api/tags", timeout=10) as r:
        return [m["name"] for m in json.load(r)["models"]]


# ── 페르소나 ────────────────────────────────────────────────────────────────
def _lines(v, n=20, w=200):
    v = v.splitlines() if isinstance(v, str) else (v or [])
    return [str(x).strip()[:w] for x in v if str(x).strip()][:n]


def clean_persona(p):
    """화면·JSON 편집에서 온 페르소나를 정해진 모양으로. 이름 없으면 ValueError."""
    if not isinstance(p, dict):
        raise ValueError("페르소나는 JSON 객체여야 합니다")
    name = str(p.get("name") or "").strip()[:30]
    if not name:
        raise ValueError("페르소나 이름이 필요합니다")
    return {"id": str(p.get("id") or ""), "name": name, "audience": str(p.get("audience") or "").strip()[:200],
            "tone": str(p.get("tone") or "").strip()[:300], "rules": _lines(p.get("rules")), "avoid": _lines(p.get("avoid")),
            "example": str(p.get("example") or "").strip()[:2000]}


def persona_text(p):
    p = clean_persona(p)
    s = f"{p['name']}"
    if p["audience"]: s += f" / 독자: {p['audience']}"
    if p["tone"]: s += f"\n어조: {p['tone']}"
    if p["rules"]: s += "\n관례: " + "; ".join(p["rules"])
    if p["avoid"]: s += "\n피할 것: " + "; ".join(p["avoid"])
    if p["example"]: s += f"\n예시:\n{p['example'][:600]}"
    return s


def personas(ws):
    d, out = os.path.join(ws, "personas"), [dict(p) for p in BUILTIN]
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            try:
                out.append(json.loads(read(os.path.join(d, fn))))
            except Exception:
                pass
    return out


def save_persona(p, ws):
    p = clean_persona(p)
    if not re.fullmatch(PERSONA_RE, p["id"]):
        p["id"] = "u-" + secrets.token_hex(3)
    d = os.path.join(ws, "personas")
    os.makedirs(d, exist_ok=True)
    p["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    write(os.path.join(d, p["id"] + ".json"), json.dumps(p, ensure_ascii=False, indent=1))
    return p


def _inside(base, name):
    p = os.path.join(base, name)
    if not os.path.realpath(p).startswith(os.path.realpath(base) + os.sep):
        raise FileNotFoundError(name)
    return p


def delete_persona(pid, ws):
    if not re.fullmatch(PERSONA_RE, str(pid)):
        raise FileNotFoundError(pid)
    os.remove(_inside(os.path.join(ws, "personas"), pid + ".json"))


# ── 문단 검사 ───────────────────────────────────────────────────────────────
CHECK_SYS = """너는 한국어 글쓰기 코치다. [글 종류]의 관례와 [목적]에 맞게, 사용자가 보낸 문단에서 고칠 곳만 찾는다.
[글 종류] {persona}
[목적] {purpose}
[글 파악] {profile}

문단마다 위치와 앞뒤 문단이 함께 온다. 고칠 곳(o)은 ▶ 문단에서만 고르고, 앞뒤 문단·[글 파악]은 문맥을 보는 데 쓴다. 문맥 문제도 짚는다:
- 앞 문장과 맞지 않는 접속어(그래서·따라서·하지만 등) → 논리, n 에 맞는 접속어
- 앞 문장과 같은 내용·같은 말의 되풀이 → 명료, r 에 '앞 문장과 반복'
- ▶ 문단의 숫자·날짜·금액·이름이 앞 문단이나 [글 파악]의 같은 대상 값과 다르면 반드시 사실로, n 에 앞의 값
- 접속어는 앞 문장과의 관계(인과·역접·첨가)가 분명히 어긋날 때만(다만·또한·한편처럼 무난한 것은 둔다)
- 이 글의 주된 문체·용어 표기와 다름 → 문체·형식
- 위치에 맞는 글 종류 관례(도입: 용건·결론 먼저 / 맺음: 요청·끝맺음) → 형식
- [목적]과 어긋나는 요청·결론 → 논리

출력 JSON: {{"s":[{{"p":"문단번호","o":"원문 일부","n":"고친 말","r":"이유","c":"분류"}}]}}
- 맞춤법·띄어쓰기는 다른 검사가 따로 하니 내지 않는다.
- o: 그 문단의 글자를 그대로 복사한 연속 구간(띄어쓰기·문장부호까지 같게). 고칠 곳만 담은 가장 짧은 구간(보통 한두 어절).
  문장을 통째로 바꾸는 제안은 문체·논리에서 꼭 필요할 때만, 문단당 1개.
- n: o 자리에 그대로 넣을 말. 뜻·숫자·고유명사는 바꾸지 않는다. 글에 없는 내용(관련 근거·날짜·문서번호 등)을 지어 넣거나 ○○·XX 자리표시를 쓰지 않는다.
- 글이 [글 종류]와 많이 달라 보이면(예: 공문서인데 메신저 말투) 문장 구조를 갈아엎지 말고 꼭 필요한 어휘·표기만 짚는다.
- r: 이유, 15자 이내.
- c: 문체(어조·높임·종결) | 형식(이 글 종류의 관례·표기) | 명료(모호·중복·장황) | 논리(연결·순서·근거) | 사실
- 사실: 숫자·날짜·이름·인용처럼 확인할 수 있는 구체적 내용이 틀려 보이거나 서로 맞지 않을 때만(없는 날짜, 요일 불일치, 합계·인원 불일치 등). n 은 맞는 값(모르면 o 그대로), r 에 확인할 점. 막연한 의심은 내지 않는다.
- 문단당 최대 3개, 중요한 것부터, 확실한 것만. 없으면 {{"s":[]}}."""

SPELL_SYS = """한국어 맞춤법·띄어쓰기 교정기. 문단마다 틀린 어절만 찾는다. 어조·어휘·문장 구조는 건드리지 않는다.
출력 JSON: {{"s":[{{"p":"문단번호","o":"틀린 어절(문단 그대로)","n":"고친 어절","r":"15자 이내 이유"}}]}}
- o 는 한 어절(띄어쓰기 오류면 두 어절)만. 문장 통째 금지. 틀린 곳이 없으면 {{"s":[]}}.
- 글 종류: {kind}. 공문서·논문·보고서면 합성어 띄어쓰기 원칙도 적용(고속카메라→고속 카메라).{terms}
자주 틀리는 예: 시일내에→시일 내에 / 할때→할 때 / 되요→돼요 / 몇일→며칠 / 않되요→안 돼요 / 어의없다→어이없다 / 학생으로써(자격)→학생으로서 / 대화로서(수단)→대화로써"""

FULL_OK = ("문체", "논리")   # 문장을 크게 바꾸는 제안은 이 분류에서만


def _sentence_len(text, at, end):
    """o 가 걸친 문장(들)의 길이 — 문장 끝(. ? ! 줄바꿈) 기준."""
    a = max(text.rfind(ch, 0, at) for ch in ".?!\n") + 1
    ends = [i for i in (text.find(ch, end) for ch in ".?!\n") if i >= 0]
    return (min(ends) + 1 if ends else len(text)) - a


def _split(o, n, merge=True):
    """글자 단위로 비교해 바뀐 곳을 어절 경계까지 넓힌 조각들 → [(o 안 위치, 원문 조각, 고친 조각)]. 통째로 바뀌었으면 []."""
    from difflib import SequenceMatcher
    sm, ps = SequenceMatcher(None, o, n, autojunk=False), []
    if sm.ratio() < 0.5:  # 거의 다 바뀜 = 다시 쓰기 → 나누지 않음
        return []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a, b = i1, i2
        while a > 0 and not o[a - 1].isspace():
            a -= 1
        while b < len(o) and not o[b].isspace():
            b += 1
        if ps and (a <= ps[-1][1] or merge and not o[ps[-1][1]:a].strip()):  # 같은 어절은 늘, 이웃한 어절은 구절로 합침(맞춤법은 어절마다 따로)
            ps[-1][1], ps[-1][3] = b, j2 + (b - i2)
        else:
            ps.append([a, b, j1 - (i1 - a), j2 + (b - i2)])
    if not ps or (len(ps) == 1 and ps[0][0] == 0 and ps[0][1] == len(o)):
        return []
    return [(a, o[a:b], n[x:y].strip()) for a, b, x, y in ps]


def _accept(raw, by, cat_default, dropped, minimal=False):
    """LLM 이 낸 제안 하나 → 자리 확인·정리된 제안 목록(0개 이상)."""
    if not isinstance(raw, dict):
        dropped.append("형식")
        return []
    pid, o, n = str(raw.get("p", "")).strip("[] "), str(raw.get("o") or ""), str(raw.get("n") or "")
    if pid not in by and "p" + pid in by:  # "0" 처럼 p 를 빼고 답하는 경우
        pid = "p" + pid
    if pid not in by and len(by) == 1:
        pid = next(iter(by))
    c = cat_default or next((k for k in CATS if k in str(raw.get("c", ""))), "명료")
    r, text = str(raw.get("r") or "")[:80], by.get(pid)
    if not cat_default and c == "맞춤법":  # 문체 검사는 맞춤법을 내지 않는다(맞춤법 검사 몫)
        c = None
    if not c or not text or not o or o not in text or (o == n and c != "사실") or PLACEHOLDER.search(n) or len(n) > 2 * len(o) + 20:
        dropped.append(o)
        return []
    at = text.index(o)
    big = len(o) >= 15 and len(o) > 0.6 * _sentence_len(text, at, at + len(o)) and c != "사실"
    if big and minimal:  # 글 종류가 어긋나 보일 때: 문장 갈아엎기는 내지 않고 최소 조각만
        parts = [x for x in _split(o, n) if x[1] != x[2]]
        if not parts or len(parts) > 3:
            dropped.append(o)
            return []
        return [{"p": pid, "o": po, "n": pn, "r": r, "c": c, "at": at + off} for off, po, pn in parts]
    if (big and c not in FULL_OK) or (cat_default == "맞춤법" and len(o.split()) > 2):  # 크게 바꾸는 제안 → 최소 조각으로
        parts = _split(o, n, merge=cat_default != "맞춤법")
        if not parts or len(parts) > 3:
            dropped.append(o)
            return []
        return [{"p": pid, "o": po, "n": pn, "r": r, "c": c, "at": at + off} for off, po, pn in parts if po != pn]
    if big and not r:
        dropped.append(o)
        return []
    return [{"p": pid, "o": o, "n": n, "r": r, "c": c, "at": at}]


def _dedupe(items):
    """같은 문단에서 겹치면 규칙 검사·맞춤법, 그다음 짧은 쪽을 남긴다. 단, 문체·논리의 문장 다시 쓰기는 겹쳐도 카드로 남긴다(밑줄은 짧은 쪽)."""
    out, seen = [], set()
    def rank(s):  # 규칙 검사의 수정 → 맞춤법 → 그 밖의 수정 → 확인 메모(n == o), 그다음 짧은 것
        return (0 if s.get("src") == "rule" and s["o"] != s["n"] else 1 if s["c"] == "맞춤법" else 2 if s["o"] != s["n"] else 3, len(s["o"]))
    for s in sorted(items, key=rank):
        if (s["p"], s["o"], s["n"]) in seen:
            continue
        clash = any(t["p"] == s["p"] and s["at"] < t["at"] + len(t["o"]) and t["at"] < s["at"] + len(s["o"]) for t in out)
        if clash and not (s["c"] in FULL_OK and len(s["o"]) >= 15 or s.get("src") == "rule"):   # 규칙 검사의 메모는 겹쳐도 카드로
            continue
        seen.add((s["p"], s["o"], s["n"]))
        out.append(s)
    return sorted(out, key=lambda s: (s["p"], s["at"]))


def check(units, persona, purpose="", model=MODEL, stop=lambda: False, passes=("spell", "style"), minimal=False, prof="", terms=(), head=""):
    """units = [{"id": "p3", "text": "..."}] → 자리가 확인된 제안 목록 + 버린 개수.
    맞춤법 검사(작은 프롬프트, 어절 단위)와 문체·형식 검사를 따로 묻는다. 화면은 맞춤법 → 문체 순으로 따로 불러 먼저 온 것부터 보이고,
    둘 다 부르면(CLI·기본) 동시에 돌려 합친다."""
    units = [{"id": str(u["id"])[:20], "text": str(u["text"])[:3000], "ctx": str(u.get("ctx") or "")[:1500]} for u in units if str(u.get("text", "")).strip()][:20]
    if not units:
        return {"s": [], "dropped": 0}
    p = clean_persona(persona)
    plain = "\n".join(f"[{u['id']}] {u['text']}" for u in units)
    ctx = head + "\n\n".join(f"[{u['id']}] {u['ctx']}\n▶ {u['text']}" if u.get("ctx") else f"[{u['id']}] ▶ {u['text']}" for u in units)
    jobs = {"style": (CHECK_SYS.format(persona=persona_text(p), purpose=(purpose or "").strip()[:200] or "(없음)", profile=prof or "(아직 없음)"), None, ctx),
            "spell": (SPELL_SYS.format(kind=p["name"], terms=("\n- 이 글이 정한 용어 표기(바꾸지 말 것): " + ", ".join(terms)) if terms else ""), "맞춤법", plain)}
    jobs = {k: v for k, v in jobs.items() if k in passes}
    res = {}

    def run(k):
        try:
            res[k] = llm(jobs[k][0], jobs[k][2], model, stop).get("s") or []
        except Exception as e:  # noqa: BLE001 — 한쪽이 실패해도 다른 쪽 결과는 살린다
            res[k] = e
    ts = [threading.Thread(target=run, args=(k,)) for k in jobs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    errs = [v for v in res.values() if isinstance(v, Exception)]
    if any(isinstance(e, Cancelled) for e in errs):
        raise Cancelled()
    if len(errs) == len(jobs):
        raise errs[0]
    by, dropped, items = {u["id"]: u["text"] for u in units}, [], []
    for k, got in res.items():
        if isinstance(got, list):
            for raw in got:
                items += _accept(raw, by, jobs[k][1], dropped, minimal)
    out = _dedupe(items)
    return {"s": out, "dropped": len(dropped) + len(items) - len(out), **({"partial": str(errs[0])[:120]} if errs else {})}



# ── 글 파악(문맥) ─────────────────────────────────────────────────────────────
# 문단 하나만 보면 놓치는 것(앞 문장과의 연결·용어·숫자·문체)을 잡으려고, 글 전체에서 싸게 뽑은 특징(규칙)과
# 가끔 LLM 으로 고친 요약(문체·용어·인물·요청·지금까지)을 합쳐 '글 파악'으로 들고 다닌다. 사용자가 고친 문체·용어는 제약이 된다.
ENDS = [("개조식", r"(함|음|임|됨|바람|요망|것|끝)\.?$"), ("합니다체", r"(니다|니까)[.?!]?$"), ("해요체", r"(요|죠)[.?!~]*$"), ("~다체", r"다\.?$")]
STYLE_HINT = {"개조식": "개조식('~함', '~바람')", "합니다체": "합니다체('~합니다')", "해요체": "해요체('~해요')", "~다체": "'~다' 평서체"}
PARTICLE = re.compile(r"(으로서|으로써|에서는|에게서|으로|에서|에게|까지|부터|처럼|보다|이나|은|는|이|가|을|를|의|에|와|과|도|만|로)$")
ABBR_OK = {"AI", "IT", "PC", "OK", "TV", "ID", "URL", "PDF", "SNS", "CEO", "GPU", "CPU"}
NUM_RE = re.compile(r"(\d{4}\s*[.년]\s*)?\d{1,2}\s*[.월]\s*\d{1,2}\s*[.일]?(?:\s*\([월화수목금토일]\))?|\d[\d,.]*\s*(?:만\s*)?(?:원|명|대|건|개|%|부|시간|일|주|개월|년)")
DAYS = "월화수목금토일"


def sentences(text):
    return [x.strip() for x in re.split(r"(?<=[.?!])\s+|\n+", text) if len(x.strip()) >= 4]


def ending(snt):
    snt = re.sub(r"[\s\"')\]」』~]+$", "", snt)
    return next((k for k, rx in ENDS if re.search(rx, snt)), None)


def heur_profile(text):
    """규칙으로 뽑는 글 특징 — 매번(밀리초) 계산."""
    ks = [k for k in map(ending, sentences(text)) if k]
    cnt = {k: ks.count(k) for k, _ in ENDS if ks.count(k)}
    dom = max(cnt, key=cnt.get) if cnt else ""
    share = cnt.get(dom, 0) / len(ks) if ks else 0
    abbr, seen = [], set()
    for m in re.finditer(r"(?<![A-Za-z])[A-Z][A-Z0-9]{1,9}(?![A-Za-z])", text):
        a = m.group(0)
        if a in seen or a in ABBR_OK:
            continue
        seen.add(a)
        inside = text.rfind("(", 0, m.start()) > text.rfind(")", 0, m.start())   # (풀이, LSTM) 안
        abbr.append({"a": a, "def": inside or text[m.end():m.end() + 1] == "(", "at": m.start()})
    nums = []
    for m in NUM_RE.finditer(text):
        lab = re.findall(r"[가-힣A-Za-z]+", text[max(0, m.start() - 12):m.start()])
        nums.append((lab[-1] + " " if lab else "") + m.group(0).strip())
    return {"style": dom if len(ks) >= 3 and share >= 0.6 else ("섞임" if len(cnt) > 1 and len(ks) >= 3 else dom), "styles": cnt,
            "abbr": abbr, "numbers": nums[:12], "variants": term_variants(text)}


def term_variants(text):
    """띄어 쓴 꼴과 붙여 쓴 꼴이 섞인 용어 — [{"spaced": "고속 카메라", "joined": "고속카메라", "first": 먼저 쓴 꼴}]"""
    toks = list(re.finditer(r"[가-힣A-Za-z0-9]+", text))
    words = {t.group(0) for t in toks}
    out, seen = [], set()
    for a, b in zip(toks, toks[1:]):
        if text[a.end():b.start()] != " " or len(a.group(0)) < 2 or len(b.group(0)) < 2 or PARTICLE.search(a.group(0)) and len(a.group(0)) > 2:
            continue
        stem = PARTICLE.sub("", b.group(0)) or b.group(0)
        joined = a.group(0) + stem
        if len(stem) < 2 or joined in seen or not any(w.startswith(joined) for w in words):
            continue
        seen.add(joined)
        js = re.search(re.escape(joined), text).start()
        out.append({"spaced": a.group(0) + " " + stem, "joined": joined, "first": "spaced" if a.start() < js else "joined"})
    return out[:8]


def canon_terms(prof):
    """용어 표기 결정: 사용자가 정한 것 > 먼저 쓴 꼴."""
    user = [t.strip() for t in re.split(r"[,\n]", (prof.get("user") or {}).get("terms") or "") if t.strip()]
    out = []
    for v in prof.get("variants") or []:
        c = next((u for u in user if u in (v["spaced"], v["joined"])), v[v["first"]])
        out.append((c, v["joined"] if c == v["spaced"] else v["spaced"], c in user))
    return out, user


def profile_text(prof):
    """프롬프트에 넣는 글 파악(짧게). 바뀌는 일이 드문 순서대로."""
    user = prof.get("user") or {}
    llmp = prof.get("llm") or {}
    style = user.get("style") or (prof.get("style") if prof.get("style") in STYLE_HINT else "") or llmp.get("style") or prof.get("style") or ""   # 문장 끝 세기가 뚜렷하면 그것 먼저
    canon, uterms = canon_terms(prof)
    parts = []
    if style and style != "섞임":
        parts.append(f"문체: {STYLE_HINT.get(style, style)}" + (" (사용자 지정 — 다른 종결은 맞춰 고칠 것)" if user.get("style") else " 위주"))
    terms = list(dict.fromkeys(uterms + [c for c, _, _ in canon] + (llmp.get("terms") or [])))[:8]
    if terms: parts.append("용어 표기: " + ", ".join(terms))
    if llmp.get("entities"): parts.append("인물·기관: " + ", ".join(llmp["entities"][:6]))
    if llmp.get("ask"): parts.append("핵심 요청·주장: " + llmp["ask"])
    if llmp.get("summary"): parts.append("지금까지: " + llmp["summary"])
    return " / ".join(parts)[:900]


PROFILE_SYS = """글을 읽고 이 글을 파악한 결과를 JSON 으로만 낸다.
{"style":"합니다체|해요체|개조식|~다체|섞임","terms":["핵심 용어(글에 쓴 표기 그대로, 최대 6)"],"entities":["사람·기관·직함(최대 6)"],"ask":"이 글의 핵심 요청·주장 한 줄","summary":"지금까지 한 말 1~2줄"}"""


def llm_profile(text, model=MODEL, stop=lambda: False):
    j = llm(PROFILE_SYS, text[:6000], model, stop)
    lst = lambda v, n: [str(x)[:40] for x in (v if isinstance(v, list) else [])][:n]
    st = str(j.get("style") or "")
    return {"style": st if st in STYLE_HINT or st == "섞임" else "", "terms": [t for t in lst(j.get("terms"), 6) if t in text],
            "entities": lst(j.get("entities"), 6), "ask": str(j.get("ask") or "")[:120], "summary": str(j.get("summary") or "")[:200]}


def josa(word, a, b):
    """받침 있으면 a(이·으로), 없으면 b(가·로). ㄹ 받침 + 으로/로 → 로."""
    ch = word[-1] if word else ""
    if not ("가" <= ch <= "힣"):
        return a
    jong = (ord(ch) - 0xAC00) % 28
    return b if jong == 0 or (a == "으로" and jong == 8) else a


def _note(i, line, o, n, r, c, at=None):
    return {"i": i, "o": o, "n": n, "r": r, "c": c, "at": line.index(o) if at is None else at, "src": "rule"}


def _date_checks(text, lines, idxs):
    """날짜·요일: 없는 날짜, 요일 틀림(앞에서 나온 연·월을 이어 받음). 맞는 날짜 자리는 verified 로 돌려 LLM 의 헛된 '사실'을 거른다."""
    out, ok = [], []
    year, month, off = datetime.date.today().year, None, 0
    pat = re.compile(r"(?:(\d{4})\s*(?:년|\.)\s*)?(?:(\d{1,2})\s*(?:월|\.)\s*)?(\d{1,2})\s*(?:일|\.)\s*(?:\(\s*([월화수목금토일])\s*\)|([월화수목금토일])요일)?")
    for i, line in enumerate(lines):
        for m in pat.finditer(line):
            y, mo, d, w1, w2 = m.groups()
            if not mo and not (w1 or w2) or (not mo and month is None):   # 월 없이 '15일' 만이면 요일이 붙고 앞에 월이 있을 때만
                continue
            if y: year = int(y)
            if mo: month = int(mo)
            if not (1 <= month <= 12):
                continue
            w, wat = (w1, m.start(4)) if w1 else (w2, m.start(5)) if w2 else (None, None)
            try:
                dt = datetime.date(year, month, int(d))
            except ValueError:
                if i in idxs and mo:
                    out.append(_note(i, line, m.group(0).strip(), m.group(0).strip(), f"{month}월에는 {d}일이 없음 — 날짜 확인", "사실", m.start()))
                continue
            if not w:
                continue
            right = DAYS[dt.weekday()]
            if w == right:
                ok.append((i, m.start(), m.end()))
            elif i in idxs:
                o = w + ("요일" if w2 else "")
                out.append(_note(i, line, o, right + ("요일" if w2 else ""), f"{dt.year}. {dt.month}. {dt.day}.는 {right}요일", "사실", wat))
    return out, ok


def rule_checks(text, idxs, persona, prof):
    """LLM 없이 글 전체를 보고 바로 아는 문맥 문제(밀리초). 고칠 문단(idxs)에 걸린 것만."""
    lines, idxs, pid = text.split("\n"), set(idxs), clean_persona(persona)
    formal = persona.get("id") in ("gongmun", "paper", "report", "press", "editorial")
    out, verified = _date_checks(text, lines, idxs)
    starts = [0]
    for l in lines:
        starts.append(starts[-1] + len(l) + 1)
    line_of = lambda pos: max(i for i, s in enumerate(starts[:-1]) if s <= pos)
    # 용어 표기 섞임
    canon, _ = canon_terms(prof)
    for c, other, fixed in canon:
        why = f"고정 표기 '{c}'{josa(c, '으로', '로')} 통일" if fixed else f"앞에서 '{c}'{josa(c, '으로', '로')} 씀 — 용어 통일"
        for i in idxs:
            line = lines[i] if i < len(lines) else ""
            for m in re.finditer(re.escape(other), line):
                if other in c and line[m.start():].startswith(c):   # '고속 카메라' 안의 '고속' 같은 겹침 방지
                    continue
                out.append(_note(i, line, other, c, why, "형식", m.start()))
    # 약어 첫 등장에 풀이 없음
    if formal:
        for a in prof.get("abbr") or []:
            i = line_of(a["at"])
            if not a["def"] and i in idxs:
                out.append(_note(i, lines[i], a["a"], a["a"], f"약어 {a['a']} 첫 등장 — 풀어 쓴 말을 함께", "형식", a["at"] - starts[i]))
    # 가리킬 대상 없는 지시어
    body = [i for i, l in enumerate(lines) if l.strip()]
    for i in idxs:
        if i >= len(lines): continue
        line, before = lines[i], "\n".join(lines[:i])
        substantive = [l for l in lines[:i] if len(l.strip()) >= 25 or re.search(r"[.?!]$", l.strip())]
        for m in re.finditer(r"위와 같이|위의 |상기 |앞서 (?:말한|언급한|밝힌)|전술한", line):
            if not substantive:
                out.append(_note(i, line, m.group(0).strip(), m.group(0).strip(), "앞에 가리킬 내용이 없음", "논리", m.start()))
        for m in re.finditer(r"해당 ([가-힣A-Za-z]+)", line):
            stem = PARTICLE.sub("", m.group(1)) or m.group(1)
            if len(stem) >= 2 and stem not in ("사항", "없음", "기간", "분야", "내용", "부서", "기관") and stem not in before:
                out.append(_note(i, line, m.group(0), m.group(0), f"'해당 {stem}'{josa(stem, '이', '가')} 가리킬 대상이 앞에 없음", "논리", m.start()))
    # 문체 섞임: 주된 문체(사용자 지정 또는 개조식·~다체가 60% 이상)와 다른 종결
    style = (prof.get("user") or {}).get("style") or (prof.get("style") if prof.get("style") in ("개조식", "~다체") else "")
    if style:
        for i in idxs:
            line = lines[i] if i < len(lines) else ""
            for snt in sentences(line):
                k = ending(snt)
                if k and k != style and not (style == "개조식" and len(snt) < 12):
                    last = snt.split()[-1]
                    out.append(_note(i, line, last, last, f"이 글은 {STYLE_HINT[style]} — 종결 맞추기", "문체", line.rindex(last)))
    # 합계 금액이 앞에 나온 금액(들)의 합과 다름
    def won(t):
        return [(m.start(), float(m.group(1).replace(",", "")) * {"억": 1e8, "만": 1e4}.get(m.group(2) or "", 1))
                for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(억|만)?\s*원", t)]
    TOTAL = re.compile(r"(총|합계|전체|모두)\s*(?:금액|예산|비용)?\s*:?\s*(?:금\s*)?$")
    for i in idxs:
        line = lines[i] if i < len(lines) else ""
        earlier = [v for j in range(i) for at, v in won(lines[j]) if not TOTAL.search(lines[j][:at])]
        for at, v in won(line):
            if earlier and TOTAL.search(line[:at]) and abs(v - sum(earlier)) > 0.5:
                m = re.match(r"[\d,.]+\s*(?:억|만)?\s*원", line[at:])
                fmt = lambda x: f"{x / 1e4:,.0f}만 원" if x >= 1e4 and x % 1e4 == 0 else f"{x:,.0f}원"
                out.append(_note(i, line, m.group(0), m.group(0), f"앞의 금액 합({fmt(sum(earlier))})과 다름 — 확인", "사실", at))
    # 앞 문단과 거의 같은 문장(되풀이)
    stems = lambda t: {PARTICLE.sub("", w) or w for w in re.findall(r"[가-힣A-Za-z0-9,]+", t)}
    for i in idxs:
        k = body.index(i) if i in body else 0
        if k == 0: continue
        prev = [stems(x) for x in sentences(lines[body[k - 1]])]
        for snt in sentences(lines[i]):
            st = stems(snt)
            if len(st) >= 3 and any(len(st & p) / len(st | p) >= 0.6 for p in prev):
                out.append(_note(i, lines[i], snt, snt, "앞 문장과 거의 같은 말 — 하나로 줄이기", "명료"))
    # 공문 맺음 '끝.' — 마지막 줄이 '붙임'일 때만(쓰는 중인 마지막 줄마다 끝. 을 조르지 않게; 나머지는 전체 검토가 본다)
    if pid["name"] == "공문서" and len(body) >= 3 and body[-1] in idxs and "끝." not in text and lines[body[-1]].strip().startswith("붙임"):
        line = lines[body[-1]].rstrip()
        last = line.split()[-1]
        out.append(_note(body[-1], line, last, last + ("  끝." if last.endswith(".") else ".  끝."), "공문은 본문·붙임 끝에 '끝.'", "형식", line.rindex(last)))
    return out, verified


def position(i, body):
    k = body.index(i)
    if k == 0 and len(body) > 1:
        return "도입(첫 문단·제목)"
    return f"{'맺음' if k == len(body) - 1 else '본문'} {k + 1}/{len(body)}"


def check_doc(text, idxs, persona, purpose="", title="", prof_in=None, model=MODEL, stop=lambda: False, passes=("spell", "style", "rules"), minimal=False):
    """글 전체 + 고칠 줄 번호들 → 그 줄들의 제안(i = 줄 번호). 문체 검사에는 위치·앞 두 문단·뒤 한 문단·글 파악을 함께 보낸다.
    passes 의 "rules" = 규칙 검사(용어·약어·지시어·날짜·합계·되풀이·문체 섞임·맺음, LLM 없이 수십 ms). 화면은 글이 바뀔 때마다 이것만 따로 부른다."""
    text = str(text or "")[:200000]
    lines = text.split("\n")
    body = [i for i, l in enumerate(lines) if l.strip()]
    idxs = [i for i in dict.fromkeys(int(x) for x in idxs) if 0 <= i < len(lines) and lines[i].strip()][:20]
    prof = heur_profile(text) | {"llm": (prof_in or {}).get("llm") or {}, "user": (prof_in or {}).get("user") or {}}
    persona = clean_persona(persona) | {"id": (persona or {}).get("id", "")}
    units = []
    for i in idxs:
        k = body.index(i)
        cut = lambda t, n: t if len(t) <= n else t[:n] + "…"
        prev = [cut(lines[j], 220) for j in body[max(0, k - 2):k]]
        nxt = [cut(lines[j], 160) for j in body[k + 1:k + 2]]
        ctx = f"위치: {position(i, body)}" + (f"\n앞: " + " / ".join(f"«{p}»" for p in prev) if prev else "\n앞: (없음 — 글의 시작)") \
            + (f"\n뒤: «{nxt[0]}»" if nxt else "")
        units.append({"id": f"p{len(units)}", "text": lines[i], "ctx": ctx, "i": i})
    canon, uterms = canon_terms(prof)
    titled = (f"제목: {title.strip()[:80]} / " if (title or "").strip() else "") + profile_text(prof)
    llm_passes = tuple(p for p in passes if p in ("spell", "style"))
    head = ("[글에 나온 숫자·날짜] " + "; ".join(prof["numbers"][:8]) + "\n\n") if prof.get("numbers") else ""
    r = check(units, persona, purpose, model, stop, llm_passes, minimal, titled, uterms + [c for c, _, _ in canon], head) if units and llm_passes else {"s": [], "dropped": 0}
    ids = {u["id"]: u["i"] for u in units}
    items = [s | {"i": ids[s["p"]]} for s in r["s"] if s["p"] in ids]
    rules, verified = rule_checks(text, idxs, persona, prof)
    rules = rules if "rules" in passes else []
    keep = []
    for s in items:   # 거르기: 정한 용어를 다른 꼴로 바꾸는 제안, 이미 맞다고 확인된 날짜에 대한 '사실'
        if any(c in s["o"] and o2 in s["n"] and c not in s["n"] for c, o2, _ in canon):
            continue
        if s["c"] == "사실" and any(v[0] == s["i"] and s["at"] < v[2] and v[1] < s["at"] + len(s["o"]) for v in verified):
            continue
        keep.append(s)
    out = _dedupe([x | {"p": f"L{x['i']}"} for x in keep + rules])
    return {"s": [{k: v for k, v in x.items() if k != "p"} for x in out], "dropped": r["dropped"] + len(items) - len(keep) + len(keep) + len(rules) - len(out),
            "profile": {k: prof[k] for k in ("style", "styles", "abbr", "numbers", "variants")}}


REVIEW_SYS = """너는 한국어 글쓰기 코치다. 글 전체를 [글 종류]의 관례와 [목적]에 비추어 검토한다.
[글 종류] {persona}
[목적] {purpose}

문장 단위 맞춤법은 빼고, 구조·흐름·빠진 요소·목적 부합을 본다(예: 공문의 '끝.'·붙임 누락, 메일의 기한 누락, 논문의 근거 없는 주장).
출력 JSON: {{"kind":"이 글에 가장 맞는 글 종류 — {kinds} 중 하나","summary":"총평 2문장 이내","good":["잘된 점(최대 3)"],"issues":[{{"q":"관련 원문 일부(그대로 복사, 글 전체 문제면 빈 문자열)","t":"문제","f":"고칠 방향 또는 예시 문장","c":"형식|논리|명료|문체|사실"}}]}}
issues 는 중요한 순서로 최대 7개."""


def review(text, persona, purpose="", model=MODEL):
    text = (text or "").strip()[:12000]
    if not text:
        raise ValueError("검토할 글이 없습니다")
    kinds = [p["name"] for p in BUILTIN]
    j = llm(REVIEW_SYS.format(persona=persona_text(persona), purpose=(purpose or "").strip()[:200] or "(없음)", kinds="·".join(kinds)), text, model)
    issues = []
    for i in j.get("issues") or []:
        if isinstance(i, dict) and i.get("t"):
            q = str(i.get("q") or "")
            issues.append({"q": q if q and q in text else "", "t": str(i["t"])[:200], "f": str(i.get("f") or "")[:400],
                           "c": next((k for k in CATS if k in str(i.get("c", ""))), "논리")})
    return {"kind": next((k for k in kinds if k == str(j.get("kind") or "").strip()), ""), "summary": str(j.get("summary") or "")[:400], "good": [str(x)[:200] for x in (j.get("good") or [])][:3], "issues": issues[:7]}


# ── 문서 저장·기록 ──────────────────────────────────────────────────────────
def doc_path(did, ws):
    if not re.fullmatch(DOC_RE, str(did)):
        raise FileNotFoundError(did)
    return _inside(os.path.join(ws, "docs"), did + ".json")


def save_doc(req, ws):
    """같은 id 면 덮어쓰고, 내용이 바뀌었고 snapshot 이거나 마지막 판이 10분 넘었으면 판(version)을 남긴다(최대 30)."""
    now = datetime.datetime.now()
    did = str(req.get("id") or "")
    d = os.path.join(ws, "docs")
    os.makedirs(d, exist_ok=True)
    try:
        doc = json.loads(read(doc_path(did, ws)))
    except (FileNotFoundError, ValueError):
        did = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        doc = {"id": did, "created": now.isoformat(timespec="seconds"), "versions": []}
    text = str(req.get("text") or "")[:200000]
    vs = doc["versions"]
    last = datetime.datetime.fromisoformat(vs[-1]["ts"]) if vs else None
    if text.strip() and (not vs or vs[-1]["text"] != text) and (req.get("snapshot") or not last or (now - last).total_seconds() > 600):
        vs.append({"ts": now.isoformat(timespec="seconds"), "text": text})
        del vs[:-30]
    doc.update({"title": str(req.get("title") or "").strip()[:100] or (text.strip().splitlines() or ["제목 없음"])[0][:40],
                "text": text, "persona": str(req.get("persona") or "")[:20], "purpose": str(req.get("purpose") or "")[:200],
                "profile_user": {k: str((req.get("profile_user") or {}).get(k) or "")[:300] for k in ("style", "terms")},
                "updated": now.isoformat(timespec="seconds")})
    write(doc_path(did, ws), json.dumps(doc, ensure_ascii=False, indent=1))
    return {k: doc[k] for k in ("id", "title", "updated")} | {"versions": len(vs)}


def list_docs(ws):
    d, out = os.path.join(ws, "docs"), []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d), reverse=True)[:200]:
        try:
            j = json.loads(read(os.path.join(d, fn)))
            out.append({k: j.get(k) for k in ("id", "title", "persona", "updated")} | {"chars": len(j.get("text", "")), "versions": len(j.get("versions", []))})
        except Exception:
            pass
    return sorted(out, key=lambda x: x["updated"] or "", reverse=True)


# ── 가져오기·내보내기 ───────────────────────────────────────────────────────
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
    return "\n".join("".join(t.text or "" for t in p.iter(W + "t")) for p in root.iter(W + "p"))


def kordoc_ok():
    return os.path.exists(KORDOC_CLI)


def import_file(name, data, ws):
    ext = os.path.splitext(name)[1].lower()
    if ext in (".txt", ".md", ""):
        for enc in ("utf-8-sig", "cp949"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                pass
        return data.decode("utf-8", "replace")
    if ext == ".docx" and not kordoc_ok():
        return docx_text(data)
    if ext not in (".docx", ".hwp", ".hwpx", ".pdf", ".hml"):
        raise ValueError(f"지원하지 않는 형식: {ext} (txt·md·docx" + (", hwp·hwpx·pdf" if kordoc_ok() else "") + ")")
    if not kordoc_ok():
        raise ValueError("hwp·hwpx·pdf 는 kordoc-local 이 같은 폴더에 설치돼 있어야 읽을 수 있습니다")
    d = os.path.join(ws, "_upload", secrets.token_hex(4))
    os.makedirs(d)
    try:
        src, out = os.path.join(d, "in" + ext), os.path.join(d, "out.md")
        with open(src, "wb") as f:
            f.write(data)
        r = subprocess.run(["node", KORDOC_CLI, "--silent", src, "-o", out], capture_output=True, text=True, timeout=180)
        if not os.path.exists(out):
            raise RuntimeError("kordoc 변환 실패: " + (r.stdout + r.stderr).strip()[-300:])
        return read(out)
    finally:
        for fn in os.listdir(d):
            os.remove(os.path.join(d, fn))
        os.rmdir(d)


def docx_bytes(title, text):
    """python-docx 없이 최소 구성 .docx (맑은 고딕, 문단 = 줄)."""
    def para(s, bold=False, size=22):
        rpr = f'<w:rPr><w:rFonts w:ascii="Malgun Gothic" w:hAnsi="Malgun Gothic" w:eastAsia="맑은 고딕"/>{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr>'
        return f'<w:p><w:r>{rpr}<w:t xml:space="preserve">{escape(s)}</w:t></w:r></w:p>'
    body = (para(title, True, 32) if title else "") + "".join(para(line) for line in text.split("\n"))
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                               '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                             f'<w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>',
    }
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


# ── HTTP ───────────────────────────────────────────────────────────────────
HTML = read(os.path.join(ROOT, "ui.html")) if os.path.exists(os.path.join(ROOT, "ui.html")) else "ui.html 없음"
CANCEL, CANCEL_LOCK = {}, threading.Lock()   # (사용자, 요청 id) → 취소 시각. 포털 프록시는 연결 끊김을 전하지 않으므로 따로 받는다


def user_ws(h):
    """포털이 넘긴 사용자별 작업 폴더. owner(서버 주인)·헤더 없음(단독 실행) = 예전 WORKSPACE 그대로"""
    u = (h.headers.get("X-Agent-User") or "owner").strip()
    if u == "owner":
        return WS
    d = os.path.join(WS, "u", re.sub(r"[^A-Za-z0-9_.-]", "_", u)[:64] or "_")
    os.makedirs(d, exist_ok=True)
    return d


def _gone(sock):
    """브라우저가 요청을 버렸는지(단독 실행 때) — 읽을 게 있는데 0바이트면 끊긴 것."""
    try:
        r, _, _ = select.select([sock], [], [], 0)
        return bool(r) and sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


# ── 저작권 표기 (LICENSE·NOTICE 참고) ─────────────────────────────────────
_SIG = __import__("base64").b64decode("wqkgMjAyNiBnZ2dnODY1NyDCtyBkb25nanVraW0uZGV2QGdtYWlsLmNvbQ==").decode()
_SIG_A = __import__("base64").b64decode("Z2dnZzg2NTcgPGRvbmdqdWtpbS5kZXZAZ21haWwuY29tPg==").decode()


def signed(html):
    """화면에 저작권 표기를 붙인다. ui.html 에서 지워져도 서버가 내보낼 때 다시 붙는다."""
    name, mail = _SIG.split(" · ")
    if 'name="author"' not in html:
        meta = f'<meta name="author" content="{name[7:]} <{mail}>">'
        html = html.replace("<head>", "<head>" + meta, 1) if "<head>" in html else meta + html
    if "data-sig" not in html:
        tag = (f'<!-- {_SIG} --><div data-sig title="{mail}" style="text-align:center;font-size:11px;color:#9aa0a6;'
               f'opacity:.55;margin:28px 0 8px">{name}</div>')
        html = html.replace("</body>", tag + "</body>", 1) if "</body>" in html else html + tag
    return html


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        if "/api/review" in (a[0] if a else ""):
            super().log_message(fmt, *a)

    def _send(self, body, ctype="application/json", code=200, name=None):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("X-Author", _SIG_A)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        if name:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        ws = user_ws(self)
        try:
            if self.path == "/api/health":
                return self._send({"ok": True, "model": MODEL})
            if self.path == "/api/models":
                return self._send(models())
            if self.path == "/api/personas":
                return self._send(personas(ws))
            if self.path == "/api/docs":
                return self._send(list_docs(ws))
            if self.path == "/api/import_ok":
                return self._send({"kordoc": kordoc_ok()})
            m = re.fullmatch(rf"/api/docs/({DOC_RE})", self.path)
            if m:
                return self._send(read(doc_path(m.group(1), ws)).encode())
            self._send(signed(HTML.replace("%MODEL%", json.dumps(MODEL)).replace("%CATS%", json.dumps(CATS, ensure_ascii=False))).encode(),
                       "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send({"error": "없음"}, code=404)
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, code=500)

    def do_POST(self):
        ws, who = user_ws(self), self.headers.get("X-Agent-User") or "owner"
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/check":
                rid, t0 = (who, str(req.get("rid") or "")), time.time()
                stop = lambda: rid in CANCEL or _gone(self.connection)
                try:
                    ps = (req["pass"],) if req.get("pass") in ("spell", "style", "rules") else ("spell", "style", "rules")
                    if "text" in req:   # 화면: 글 전체 + 고칠 줄 번호 → 문맥을 붙여 검사
                        r = check_doc(req["text"], req.get("idx") or [], req.get("persona") or BUILTIN[0], req.get("purpose") or "", req.get("title") or "",
                                      req.get("profile") or {}, req.get("model") or MODEL, stop, ps, bool(req.get("minimal")))
                    else:
                        r = check(req.get("units") or [], req.get("persona") or BUILTIN[0], req.get("purpose") or "", req.get("model") or MODEL, stop, ps, bool(req.get("minimal")))
                except Cancelled:
                    return self._send({"cancelled": True}, code=499)
                finally:
                    with CANCEL_LOCK:
                        CANCEL.pop(rid, None)
                return self._send(r | {"ms": int((time.time() - t0) * 1000)})
            if self.path == "/api/profile":   # 글 파악: 규칙(늘) + LLM(llm:true 일 때만)
                t0, text = time.time(), str(req.get("text") or "")[:200000]
                out = heur_profile(text)
                if req.get("llm") and len(text.strip()) >= 40:
                    rid = (who, str(req.get("rid") or ""))
                    try:
                        out["llm"] = llm_profile(text, req.get("model") or MODEL, lambda: rid in CANCEL or _gone(self.connection))
                    except Cancelled:
                        return self._send({"cancelled": True}, code=499)
                    finally:
                        with CANCEL_LOCK:
                            CANCEL.pop(rid, None)
                return self._send(out | {"ms": int((time.time() - t0) * 1000)})
            if self.path == "/api/cancel":
                with CANCEL_LOCK:
                    now = time.time()
                    for k in [k for k, v in CANCEL.items() if now - v > 300]:
                        del CANCEL[k]
                    for r in (req.get("rids") or [])[:50]:
                        CANCEL[(who, str(r))] = now
                return self._send({"ok": True})
            if self.path == "/api/review":
                t0 = time.time()
                return self._send(review(req.get("text"), req.get("persona") or BUILTIN[0], req.get("purpose") or "", req.get("model") or MODEL)
                                  | {"ms": int((time.time() - t0) * 1000)})
            if self.path == "/api/personas":
                return self._send(save_persona(req, ws))
            if self.path == "/api/personas/delete":
                delete_persona(req.get("id"), ws)
                return self._send({"ok": True})
            if self.path == "/api/docs":
                return self._send(save_doc(req, ws))
            if self.path == "/api/docs/delete":
                os.remove(doc_path(req.get("id"), ws))
                return self._send({"ok": True})
            if self.path == "/api/import":
                name = os.path.basename(str(req.get("file_name") or "upload.txt"))
                return self._send({"text": import_file(name, base64.b64decode(req.get("file_b64") or ""), ws), "name": name})
            if self.path == "/api/export":
                title = str(req.get("title") or "").strip() or "글"
                fn = re.sub(r'[\\/:*?"<>|\n]', "_", title)[:60] + ".docx"
                return self._send(docx_bytes(title, str(req.get("text") or "")),
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document", name=fn)
            return self._send({"error": "not found"}, code=404)
        except FileNotFoundError:
            self._send({"error": "없음"}, code=404)
        except ValueError as e:
            self._send({"error": str(e)}, code=400)
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, code=500)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        pid = sys.argv[2] if len(sys.argv) > 2 else "gongmun"
        p = next((x for x in BUILTIN if x["id"] == pid), None) or sys.exit(f"페르소나 없음: {pid} (있는 것: {', '.join(x['id'] for x in BUILTIN)})")
        text = read(sys.argv[3]) if len(sys.argv) > 3 else sys.stdin.read()
        paras = [t for t in text.split("\n") if t.strip()]
        r = check([{"id": f"p{i}", "text": t} for i, t in enumerate(paras)], p)
        for s in r["s"]:
            print(f"[{CATS[s['c']]}] {s['o']} → {s['n']}   ({s['r']})")
        print(f"— 제안 {len(r['s'])}개, 자리 못 찾아 버린 것 {r['dropped']}개", file=sys.stderr)
        sys.exit(0)
    print(f"coach local → http://localhost:{PORT}  (model={MODEL}, llm={LLM_API} {LLM_BASE}, num_ctx={NUM_CTX}, personas={len(BUILTIN)})  {_SIG}")
    ThreadingHTTPServer((os.environ.get("BIND", ""), PORT), H).serve_forever()
