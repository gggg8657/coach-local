# 제3자 구성요소 고지 (Third-Party Notices)

이 문서는 coach-local 가 쓰는 라이브러리·모델·데이터·외부 서비스와 그 라이선스를 적은 것입니다(2026-10-12 조사, 실제 파일 기준).
라이선스는 각 1차 출처(패키지의 LICENSE·package.json, GitHub 저장소, HuggingFace 모델 카드)에서 확인했습니다. 법률 검토가 아니며, 상업 이용 전에는 각 원문을 직접 확인하세요.
이 저장소의 원저작물(코드·UI·프롬프트·문서·도구 설계)은 DongJu Kim(gggg8657)이 작성했습니다 — `NOTICE` 참고. 제3자 구성요소의 권리는 이 저장소의 라이선스로 바뀌지 않습니다.

쓰임: **동봉** = 이 저장소에 파일이 들어 있음 · **설치** = setup 이 사용자 PC 에 내려받음(저장소에 없음) · **가중치** = 내려받는 모델 가중치(저장소에 없음) · **외부** = 별도로 실행 중인 서비스/프로그램을 호출

| 이름 | 버전 | 쓰임 | 라이선스 | 링크 | 상업 이용 | 비고 |
|---|---|---|---|---|---|---|
| Python 표준 라이브러리 | 3.9+ | 런타임 | PSF-2.0 | https://docs.python.org/3/license.html | 가능 | pip 설치 없음 |
| Ollama 또는 OpenAI 호환 LLM 서버 | — | 외부 (setup 은 설치하지 않고 주소만 찾음) | Ollama: MIT | https://github.com/ollama/ollama | 가능 | |
| Google Gemma 4 (`gemma4:31b`) | — | 가중치 (권장 배포 모델 · 포털 환경 기본값) | Apache-2.0 (모델 카드 `google/gemma-4-31B-it`) | https://huggingface.co/google/gemma-4-31B-it · https://ai.google.dev/gemma/docs/gemma_4_license | 가능 | 저장소에 넣지 않음. 이전 세대 Gemma(1~3)는 별도의 Gemma Terms of Use — 다른 버전을 쓰면 그 조건 확인 |
| Qwen3 8B (`qwen3:8b`) | — | 가중치 (`LLM_MODEL` 을 안 줄 때 코드·setup 기본값) | Apache-2.0 (모델 카드 `Qwen/Qwen3-8B`) | https://huggingface.co/Qwen/Qwen3-8B | 가능 | 저장소에 넣지 않음 |
| kordoc (chrisryugj) | kordoc-local 에 설치된 것 | 외부 — hwp·hwpx·pdf 불러오기 때 옆 폴더 `kordoc-local/node_modules/kordoc` CLI 실행(`KORDOC_CLI`) | MIT | https://github.com/chrisryugj/kordoc | 가능 | 이 저장소에 동봉하지 않음. kordoc 의 의존성 라이선스는 kordoc-local 의 THIRD_PARTY_NOTICES.md |
| 행정업무운영 편람의 표기 관례 | — | 참고 — `personas.json` 공문서 페르소나의 일반 관례를 요약해 새로 씀 | 공공 자료 (원문 미포함) | https://www.mois.go.kr | 가능 | 문구를 그대로 옮기지 않음 |

## 확인 필요

지금 조사한 범위에서 상업 이용 전에 따로 확인해야 할 제3자 항목은 없습니다(LLM 은 저장소에 포함하지 않으며, 다른 모델을 쓰면 그 모델의 라이선스를 확인하세요).

## 이 저장소의 원저작물

위 표에 없는 파일(코드·UI·프롬프트·패턴·문서·설정 예시·selftest)은 DongJu Kim(gggg8657)이 작성했고 이 저장소의 `LICENSE`(PolyForm Noncommercial 1.0.0)를 따릅니다.
