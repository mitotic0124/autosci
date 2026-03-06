import json
import logging

from openai import OpenAI

from app.core.config import Settings

logger = logging.getLogger(__name__)


class LLMService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None

        provider = (settings.llm_provider or "openai_compatible").strip().lower()
        if provider == "disabled":
            return

        api_key = settings.llm_api_key or settings.openai_api_key
        if not api_key:
            return

        kwargs = {"api_key": api_key}
        base_url = self._normalize_base_url(settings.llm_api_base_url)
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    @staticmethod
    def _normalize_base_url(url: str | None) -> str | None:
        if not url:
            return None
        normalized = url.strip().rstrip("/")
        for suffix in ("/chat/completions", "/responses", "/completions"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        return normalized

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def generate(self, system_prompt: str, user_prompt: str) -> str | None:
        if not self._client:
            return None
        text = self._generate_with_responses(system_prompt, user_prompt)
        if text:
            return text
        return self._generate_with_chat(system_prompt, user_prompt)

    def _generate_with_responses(self, system_prompt: str, user_prompt: str) -> str | None:
        try:
            response = self._client.responses.create(
                model=self._settings.llm_model or self._settings.openai_model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
            )
            return response.output_text.strip()
        except Exception as exc:
            logger.warning("LLM responses API failed: %s", exc)
            return None

    def _generate_with_chat(self, system_prompt: str, user_prompt: str) -> str | None:
        try:
            response = self._client.chat.completions.create(
                model=self._settings.llm_model or self._settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
            )
            if not response.choices:
                return None
            msg = response.choices[0].message
            if not msg:
                return None
            content = msg.content
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                chunks: list[str] = []
                for item in content:
                    text = getattr(item, "text", None) if not isinstance(item, dict) else item.get("text")
                    if text:
                        chunks.append(str(text))
                return "\n".join(chunks).strip() if chunks else None
            return None
        except Exception as exc:
            logger.warning("LLM chat API failed: %s", exc)
            return None

    def score_papers(self, paper_payload: list[dict]) -> dict[str, float]:
        if not self._client or not paper_payload:
            return {}

        user_prompt = (
            "请对以下论文按“适合日频及以上量化金融研究”打分（0-100）。"
            "只返回 JSON 对象，键为 arxiv_id，值为分数。\n"
            f"论文列表:\n{json.dumps(paper_payload, ensure_ascii=False)}"
        )
        text = self.generate(
            system_prompt=self._settings.llm_score_system_prompt,
            user_prompt=user_prompt,
        )
        if not text:
            return {}

        try:
            data = json.loads(text)
            return {str(k): float(v) for k, v in data.items()}
        except Exception as exc:
            logger.warning("LLM scoring parse failed: %s | output=%s", exc, text)
            return {}
