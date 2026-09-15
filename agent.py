"""상권 생애주기 추론 에이전트 — Claude API 도구 호출 루프.

CLI 실행:  python agent.py [gemini|claude]
"""
from __future__ import annotations

import inspect
import json
import os
import sys
from typing import Iterator

import anthropic

import config as C
from core.tool_specs import TOOL_SPECS
from core.tools import TOOL_FUNCTIONS, ToolError, store

MAX_TOOL_ROUNDS = 12
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def build_system_prompt() -> str:
    meta = store().meta
    rec = meta["records"]["통합"]
    return f"""당신은 '대구 상권 생애주기 추론 에이전트'입니다. 대구에서 음식점·카페 창업을 준비하는 사람에게 그 자리와 동네의 과거를 공공데이터로 확인시켜 줍니다.

데이터: 행정안전부 지방행정 인허가 — 대구광역시 일반음식점·휴게음식점 {rec['total']:,}건(폐업 {rec['closed']:,} / 영업 중 {rec['active']:,}), 기준일 {meta['reference_date']}.

## 숫자 원칙
- 답변의 모든 수치(생존율, 기간, 건수, 공실률, 비율)는 이번 대화에서 Tool 이 반환한 값만 사용합니다. 기억·상식·추정으로 숫자를 만들지 않습니다.
- 필요한 수치가 Tool 결과에 없으면 Tool 을 더 호출하고, 그래도 없으면 "데이터로 확인되지 않음"이라고 말합니다.
- 수치를 쓸 때 표본 수(n)와 정의(예: 2010년 이후 개업 기준, Kaplan-Meier)를 짧게 함께 밝힙니다.

## 해석 원칙
- 인허가 데이터는 영업 신고 기록입니다. 매출·임대료·유동인구·폐업 사유는 알 수 없으므로 원인을 단정하지 않습니다.
- '공실'은 음식점 인허가 공백일 뿐 실제 공실 확정이 아닙니다(타 업종 전환 가능). 현장 확인을 권합니다.
- 다점포 건물(is_single_unit=false)의 이력은 한 자리의 교체 이력으로 해석하지 않습니다.
- 표본이 작거나(대략 n<100) 사이클이 '판정 보류'면 신뢰도가 낮다고 밝힙니다.
- 창업 여부를 대신 결정하거나 투자 조언을 하지 않습니다. 판단 근거와 추가로 확인할 점을 제시합니다.

## 대화 방식
- 한국어로 결론 → 근거 수치 → 주의점 순으로 간결하게 답하고, 이어서 확인해 볼 만한 질문을 1~2개 제안합니다.
- 후속 질문은 앞선 맥락(업태·지역·주소)을 이어받아 필요한 Tool 을 새로 호출합니다. 판단에 여러 관점이 필요하면 Tool 을 여러 개 호출해 교차 확인합니다.
- 여러 지역·업태를 비교할 때는 마크다운 표를 씁니다.
- 주소가 모호하면 normalize_address 로 후보를 확인하고, 후보가 여럿이면 사용자에게 고르게 합니다."""


def run_tool(name: str, args: dict) -> tuple[dict | None, str, bool]:
    """(원본 결과, tool_result 문자열, is_error)."""
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return None, f"알 수 없는 Tool: {name}", True
    params = inspect.signature(fn).parameters
    kwargs = {k: v for k, v in (args or {}).items() if k in params and v is not None}
    try:
        out = fn(**kwargs)
        return out, json.dumps(out, ensure_ascii=False), False
    except ToolError as e:
        return None, str(e), True
    except Exception as e:  # Tool 내부 버그도 대화를 끊지 않고 모델에 알린다
        return None, f"Tool 실행 중 오류: {type(e).__name__}: {e}", True


class ClaudeAgent:
    provider = "claude"

    def __init__(self, api_key: str | None = None, client: anthropic.Anthropic | None = None,
                 model: str = C.CLAUDE_MODEL):
        # api_key 를 명시적으로 넘긴다 — 공개 서버에서 os.environ 에 넣으면 모든 방문자가 공유하게 됨
        self.client = client or (anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic())
        self.model = model
        self.system = build_system_prompt()
        self.messages: list[dict] = []

    def reset(self) -> None:
        self.messages = []

    def ask(self, user_text: str) -> Iterator[dict]:
        """이벤트 스트림: text / tool_call / tool_result / done / error."""
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        try:
            yield from self._loop()
        except anthropic.AuthenticationError:
            yield self._fail(checkpoint, "Claude API 인증 실패 — API 키를 확인하세요.")
        except anthropic.RateLimitError:
            yield self._fail(checkpoint, "Claude API 요청 한도 초과 — 잠시 후 다시 시도하세요.")
        except anthropic.APIConnectionError:
            yield self._fail(checkpoint, "Claude API 네트워크 연결 실패.")
        except anthropic.APIStatusError as e:
            yield self._fail(checkpoint, f"Claude API 오류 {e.status_code}: {e.message}")

    def _fail(self, checkpoint: int, message: str) -> dict:
        del self.messages[checkpoint:]  # 반쯤 진행된 턴은 버려 다음 질문이 깨진 이력으로 시작하지 않게
        return {"type": "error", "message": message}

    def _loop(self) -> Iterator[dict]:
        for _ in range(MAX_TOOL_ROUNDS):
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=16000,
                system=self.system,
                tools=TOOL_SPECS,
                messages=self.messages,
                thinking={"type": "adaptive"},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield {"type": "text", "text": event.delta.text}
                response = stream.get_final_message()

            # 사고·도구 호출 블록까지 원형 그대로 이어 붙인다
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                yield {"type": "error", "message": "요청이 안전 정책에 의해 거절되었습니다."}
                return
            if response.stop_reason == "max_tokens":
                yield {"type": "error", "message": "응답이 최대 길이에서 잘렸습니다. 질문을 나눠 주세요."}
                return

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                yield {"type": "done", "model": self.model}
                return

            results = []
            for tu in tool_uses:
                yield {"type": "tool_call", "id": tu.id, "name": tu.name, "input": tu.input}
                out, content, is_error = run_tool(tu.name, tu.input)
                block = {"type": "tool_result", "tool_use_id": tu.id, "content": content}
                if is_error:
                    block["is_error"] = True
                results.append(block)
                yield {"type": "tool_result", "id": tu.id, "name": tu.name, "input": tu.input,
                       "output": out, "error": content if is_error else None}
            # 병렬 호출 결과는 하나의 user 메시지로 함께 반환
            self.messages.append({"role": "user", "content": results})
            yield {"type": "text", "text": "\n\n"}

        yield {"type": "error", "message": f"Tool 호출이 {MAX_TOOL_ROUNDS}회를 넘어 중단했습니다."}


LifecycleAgent = ClaudeAgent  # 이전 이름 호환

API_KEY_ENV = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}


def create_agent(provider: str = C.LLM_PROVIDER, api_key: str | None = None):
    provider = (provider or C.LLM_PROVIDER).lower()
    if provider == "gemini":
        from agent_gemini import GeminiAgent
        return GeminiAgent(api_key=api_key)
    if provider == "claude":
        return ClaudeAgent(api_key=api_key)
    raise ValueError(f"지원하지 않는 LLM_PROVIDER: {provider} (gemini | claude)")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    provider = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LLM_PROVIDER", C.LLM_PROVIDER)).lower()
    agent = create_agent(provider, os.environ.get(API_KEY_ENV.get(provider, "")))
    print(f"대구 상권 생애주기 에이전트 [{provider}] (종료: exit, 초기화: reset)\n")
    while True:
        try:
            q = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q in ("exit", "quit"):
            break
        if q == "reset":
            agent.reset()
            continue
        if not q:
            continue
        for ev in agent.ask(q):
            if ev["type"] == "text":
                print(ev["text"], end="", flush=True)
            elif ev["type"] == "tool_call":
                print(f"\n  🔧 {ev['name']}({json.dumps(ev['input'], ensure_ascii=False)})", flush=True)
            elif ev["type"] == "tool_result" and ev["error"]:
                print(f"  ⚠️ {ev['error']}", flush=True)
            elif ev["type"] == "error":
                print(f"\n[오류] {ev['message']}")
        print()


if __name__ == "__main__":
    main()
