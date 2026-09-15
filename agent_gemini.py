"""Gemini(무료 등급) 버전 에이전트 — Claude 버전과 같은 Tool 8종·시스템 프롬프트·이벤트 형식."""
from __future__ import annotations

from typing import Iterator

from google import genai
from google.genai import errors, types

import config as C
from agent import MAX_TOOL_ROUNDS, build_system_prompt, run_tool
from core.tool_specs import TOOL_SPECS

RETRY_NEXT_MODEL = (404, 429)  # 모델 없음 / 무료 한도 초과 → 다음 모델


def _tools() -> list[types.Tool]:
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=s["name"], description=s["description"],
                                  parameters_json_schema=s["input_schema"])
        for s in TOOL_SPECS
    ])]


class GeminiAgent:
    provider = "gemini"

    def __init__(self, api_key: str | None = None, client: genai.Client | None = None,
                 models: list[str] | None = None):
        # api_key 를 명시적으로 넘긴다 — 공개 서버에서 os.environ 에 넣으면 모든 방문자가 공유하게 됨
        self.client = client or (genai.Client(api_key=api_key) if api_key else genai.Client())
        self.models = list(models or C.GEMINI_MODELS)
        self.model_used: str | None = None
        self.config = types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            tools=_tools(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # 루프는 직접 돈다
            temperature=0.2,
        )
        self.contents: list[types.Content] = []

    def reset(self) -> None:
        self.contents = []

    def _generate(self) -> types.GenerateContentResponse:
        order = ([self.model_used] if self.model_used else []) + [m for m in self.models if m != self.model_used]
        last_error = None
        for model in order:
            try:
                response = self.client.models.generate_content(model=model, contents=self.contents, config=self.config)
                self.model_used = model
                return response
            except errors.ClientError as e:
                if e.code not in RETRY_NEXT_MODEL:
                    raise
                last_error = e
        raise last_error

    def ask(self, user_text: str) -> Iterator[dict]:
        """이벤트 스트림: text / tool_call / tool_result / done / error."""
        checkpoint = len(self.contents)
        self.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
        try:
            yield from self._loop()
        except errors.ClientError as e:
            if e.code == 429:
                msg = "Gemini 무료 사용 한도를 넘었습니다. 1분쯤 뒤에 다시 질문해 주세요."
            elif e.code in (400, 401, 403) and "key" in str(e).lower():
                msg = "Gemini API 키가 올바르지 않습니다."
            else:
                msg = f"Gemini API 오류 {e.code}: {e.message}"
            yield self._fail(checkpoint, msg)
        except errors.ServerError as e:
            yield self._fail(checkpoint, f"Gemini 서버 오류({e.code}) — 잠시 후 다시 시도하세요.")
        except errors.APIError as e:
            yield self._fail(checkpoint, f"Gemini API 오류: {e}")

    def _fail(self, checkpoint: int, message: str) -> dict:
        del self.contents[checkpoint:]  # 반쯤 진행된 턴은 버려 다음 질문이 깨진 이력으로 시작하지 않게
        return {"type": "error", "message": message}

    def _loop(self) -> Iterator[dict]:
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._generate()
            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None or not candidate.content.parts:
                reason = candidate.finish_reason if candidate else getattr(response.prompt_feedback, "block_reason", None)
                yield {"type": "error", "message": f"Gemini가 응답을 생성하지 않았습니다 (사유: {reason})."}
                return

            # thought_signature 가 담긴 원본 Content 를 그대로 이어 붙여야 다음 호출에서 추론이 이어진다
            self.contents.append(candidate.content)
            parts = candidate.content.parts
            text = "".join(p.text for p in parts if p.text and not p.thought)
            if text:
                yield {"type": "text", "text": text}

            calls = [p.function_call for p in parts if p.function_call]
            if not calls:
                yield {"type": "done", "model": self.model_used}
                return

            responses = []
            for i, fc in enumerate(calls):
                args = dict(fc.args or {})
                call_id = fc.id or f"{fc.name}-{i}"
                yield {"type": "tool_call", "id": call_id, "name": fc.name, "input": args}
                out, content, is_error = run_tool(fc.name, args)
                responses.append(types.Part(function_response=types.FunctionResponse(
                    id=fc.id, name=fc.name, response={"error": content} if is_error else {"result": out})))
                yield {"type": "tool_result", "id": call_id, "name": fc.name, "input": args,
                       "output": out, "error": content if is_error else None}
            # 병렬 호출 결과는 하나의 메시지로 함께 반환
            self.contents.append(types.Content(role="user", parts=responses))
            yield {"type": "text", "text": "\n\n"}

        yield {"type": "error", "message": f"Tool 호출이 {MAX_TOOL_ROUNDS}회를 넘어 중단했습니다."}
