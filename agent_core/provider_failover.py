import asyncio
import logging
import time
from typing import Any, Callable, List, Optional
from contextlib import asynccontextmanager

from browser_use import ChatGoogle, ChatOpenAI, ChatAnthropic
from browser_use.agent.loop import WrapperModel
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelSettings, ModelRequestParameters, ModelResponse

from core_config.environment_loader import ConfigRegistry

failover_logger = logging.getLogger("web_agent.failover")


class ResilientModelBridge(WrapperModel):
    def __init__(
        self,
        primary_engine: Any,
        secondary_engine: Any,
        turnaround_limit: float = 14.0,
        status_broadcaster: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(primary_engine)
        self.primary_engine = primary_engine
        self.secondary_engine = secondary_engine
        self.turnaround_limit = turnaround_limit
        self.status_broadcaster = status_broadcaster
        self.active_tier = "gemini"

    def _broadcast(self, announcement: str):
        failover_logger.info(announcement)
        if self.status_broadcaster:
            try:
                self.status_broadcaster(announcement)
            except Exception:
                pass

    async def request(
        self,
        message_sequence: List[ModelMessage],
        tuning_params: Optional[ModelSettings],
        call_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._broadcast("[Failover] Invoking primary model engine...")
        primary_fault = None
        try:
            primary_verdict = await self.primary_engine.request(message_sequence, tuning_params, call_parameters)
            self.active_tier = "gemini"
            return primary_verdict
        except Exception as fault_gemini:
            primary_fault = fault_gemini
            self._broadcast(f"[Failover] Primary model fault: {fault_gemini}. Transferring to fallback...")

        try:
            self._broadcast(f"[Failover] Activating Groq model (limit={self.turnaround_limit}s)...")
            timer_start = time.monotonic()
            fallback_verdict = await asyncio.wait_for(
                self.secondary_engine.request(message_sequence, tuning_params, call_parameters),
                timeout=self.turnaround_limit,
            )
            elapsed_duration = time.monotonic() - timer_start
            self.active_tier = "groq"
            self._broadcast(f"[Failover] Fallback action succeeded in {elapsed_duration:.2f}s")
            return fallback_verdict
        except asyncio.TimeoutError:
            self._broadcast(f"[Failover] Fallback latency surpassed {self.turnaround_limit}s threshold.")
        except Exception as fault_groq:
            self._broadcast(f"[Failover] Fallback model fault: {fault_groq}.")

        self._broadcast("[Failover] Reverting to primary model engine...")
        self.active_tier = "gemini"
        try:
            return await self.primary_engine.request(message_sequence, tuning_params, call_parameters)
        except Exception as fault_recovery:
            raise RuntimeError(
                f"Multi-engine dispatch exhausted:\n"
                f"- Primary error: {primary_fault}\n"
                f"- Fallback error/timeout\n"
                f"- Retry error: {fault_recovery}"
            ) from fault_recovery

    @asynccontextmanager
    async def request_stream(
        self,
        message_sequence: List[ModelMessage],
        tuning_params: Optional[ModelSettings],
        call_parameters: ModelRequestParameters,
        execution_context: Any = None,
    ):
        try:
            async with self.primary_engine.request_stream(
                message_sequence, tuning_params, call_parameters, execution_context
            ) as stream_conduit:
                yield stream_conduit
                return
        except Exception as fault_stream:
            self._broadcast(f"[Failover] Stream transfer initiated: {fault_stream}")

        try:
            async with self.secondary_engine.request_stream(
                message_sequence, tuning_params, call_parameters, execution_context
            ) as stream_conduit:
                yield stream_conduit
                return
        except Exception:
            pass

        async with self.primary_engine.request_stream(
            message_sequence, tuning_params, call_parameters, execution_context
        ) as stream_conduit:
            yield stream_conduit


def construct_agent_llm(on_status_change: Optional[Callable[[str], None]] = None) -> Any:
    gemini_credential = ConfigRegistry.lookup_secret("GOOGLE_API_KEY", "GEMINI_API_KEY")
    groq_credential = ConfigRegistry.lookup_secret("GROQ_API_KEY")
    openrouter_credential = ConfigRegistry.lookup_secret("OPENROUTER_API_KEY")
    openai_credential = ConfigRegistry.lookup_secret("OPENAI_API_KEY")
    claude_credential = ConfigRegistry.lookup_secret("ANTHROPIC_API_KEY")
    latency_limit = ConfigRegistry.read_numeric_setting("AI_ENGINE_TIMEOUT", 14.0)

    if gemini_credential and groq_credential:
        gemini_wrapped = ChatGoogle("gemini-flash-lite-latest", api_key=gemini_credential).pydantic_model()
        groq_wrapped = ChatOpenAI(
            "qwen/qwen3.8-27b",
            base_url="https://api.groq.com/openai/v1",
            api_key=groq_credential,
        ).pydantic_model()
        return ResilientModelBridge(
            primary_engine=gemini_wrapped,
            secondary_engine=groq_wrapped,
            turnaround_limit=latency_limit,
            status_broadcaster=on_status_change,
        )
    elif gemini_credential:
        return ChatGoogle("gemini-flash-lite-latest", api_key=gemini_credential)
    elif groq_credential:
        return ChatOpenAI(
            "qwen/qwen3.8-27b",
            base_url="https://api.groq.com/openai/v1",
            api_key=groq_credential,
        )
    elif openrouter_credential:
        return ChatOpenAI(
            "openrouter/free",
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_credential,
        )
    elif openai_credential:
        return ChatOpenAI("gpt-4o", api_key=openai_credential)
    elif claude_credential:
        return ChatAnthropic("claude-3-5-sonnet-latest", api_key=claude_credential)

    return None


def analyze_provider_exception(failure_reason: Any) -> Optional[dict]:
    stringified_reason = str(failure_reason) if failure_reason is not None else ""
    lowered_text = stringified_reason.lower()

    quota_exhausted_flag = any(token_needle in lowered_text for token_needle in ["quota exceeded", "exceeded your current quota", "insufficient_quota", "check quota"])
    rate_boundary_flag = "429" in lowered_text or "too many requests" in lowered_text or any(token_needle in lowered_text for token_needle in ["rate limit", "ratelimit", "tpm", "rpm", "rpd"])
    resource_depleted_flag = "resource_exhausted" in lowered_text or "exhausted" in lowered_text
    payment_needed_flag = any(token_needle in lowered_text for token_needle in ["402", "payment required", "credit exhausted", "credits", "out of credits"])
    access_forbidden_flag = "403" in lowered_text and ("permission_denied" in lowered_text or "free tier" in lowered_text)

    if not (quota_exhausted_flag or rate_boundary_flag or resource_depleted_flag or payment_needed_flag or access_forbidden_flag):
        return None

    service_provider_title = "AI Service Provider"
    if "openrouter" in lowered_text or "sk-or-v1-" in lowered_text:
        service_provider_title = "OpenRouter"
    elif "gemini" in lowered_text or "google" in lowered_text or resource_depleted_flag:
        service_provider_title = "Google Gemini"
    elif "groq" in lowered_text or "gsk_" in lowered_text or "qwen" in lowered_text:
        service_provider_title = "Groq"
    elif "browser_use" in lowered_text or payment_needed_flag:
        service_provider_title = "Browser-Use Cloud"
    elif "openai" in lowered_text or "gpt" in lowered_text:
        service_provider_title = "OpenAI"
    elif "anthropic" in lowered_text or "claude" in lowered_text:
        service_provider_title = "Anthropic Claude"

    classification = "rate_limit"
    if payment_needed_flag:
        classification = "credits_exhausted"
    elif access_forbidden_flag:
        classification = "tier_restricted"
    elif quota_exhausted_flag:
        classification = "quota_exhausted"

    return {
        "is_limit": True,
        "provider_title": service_provider_title,
        "category": classification,
        "raw_detail": stringified_reason,
    }


async def execute_dialogue_query(
    prompt_query: str,
    past_conversation: list,
    context_brief: str = "",
    timeout_allowance: float = 12.0,
) -> str:
    openrouter_secret = ConfigRegistry.lookup_secret("OPENROUTER_API_KEY")
    google_secret = ConfigRegistry.lookup_secret("GOOGLE_API_KEY", "GEMINI_API_KEY")
    groq_secret = ConfigRegistry.lookup_secret("GROQ_API_KEY")

    if not openrouter_secret and not google_secret and not groq_secret:
        raise ValueError("No valid AI credentials located in configuration.")

    def _invoke_openrouter() -> str:
        import openai
        remote_client = openai.OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_secret,
            default_headers={"HTTP-Referer": "http://localhost:5000", "X-Title": "Web Agent"},
        )
        inbound_context = []
        if context_brief:
            inbound_context.append({
                "role": "system",
                "content": (
                    "You are an AI research consultant. The user performed automated web research.\n"
                    f"RESEARCH BRIEF:\n{context_brief}\n\n"
                    "Answer user questions accurately based on this brief."
                ),
            })
        for past_item in past_conversation:
            speaker_tag = "user" if past_item.get("role") == "user" else "assistant"
            inbound_context.append({"role": speaker_tag, "content": str(past_item.get("text", ""))})
        inbound_context.append({"role": "user", "content": prompt_query})

        candidate_models = [
            "openrouter/free",
            "nvidia/nemotron-3.5-lightning:free",
            "google/gemma-4-31b-it:free",
        ]
        router_fault = None
        for candidate_id in candidate_models:
            try:
                server_verdict = remote_client.chat.completions.create(
                    model=candidate_id,
                    messages=inbound_context,
                    max_tokens=1024,
                    temperature=0.7,
                )
                active_choice = server_verdict.choices[0]
                extracted_reply = active_choice.message.content
                if not extracted_reply or not str(extracted_reply).strip():
                    extracted_reply = getattr(active_choice.message, "reasoning", None) or getattr(
                        active_choice.message, "reasoning_content", None
                    )
                if extracted_reply and str(extracted_reply).strip():
                    return str(extracted_reply).strip()
            except Exception as candidate_fault:
                router_fault = candidate_fault
        if router_fault:
            raise router_fault
        raise RuntimeError("OpenRouter returned empty payload")

    def _invoke_gemini() -> str:
        from google import genai
        remote_client = genai.Client(api_key=google_secret)
        inbound_context = []
        if context_brief:
            inbound_context.append({
                "role": "user",
                "parts": [{
                    "text": (
                        "You are an AI research consultant. The user performed automated web research.\n"
                        f"RESEARCH BRIEF:\n{context_brief}\n\n"
                        "Answer user questions accurately based on this brief."
                    ),
                }],
            })
            inbound_context.append({
                "role": "model",
                "parts": [{"text": "Understood. Ready to consult based on the research brief."}],
            })
        for past_item in past_conversation:
            speaker_tag = "user" if past_item.get("role") == "user" else "model"
            inbound_context.append({"role": speaker_tag, "parts": [{"text": str(past_item.get("text", ""))}]})
        inbound_context.append({"role": "user", "parts": [{"text": prompt_query}]})

        for gemini_candidate in ["gemini-flash-lite-latest", "gemini-3.5-flash-lite"]:
            try:
                gemini_output = remote_client.models.generate_content(model=gemini_candidate, contents=inbound_context)
                return gemini_output.text
            except Exception:
                pass
        raise RuntimeError("Gemini model generation failed")

    def _invoke_groq() -> str:
        import openai
        remote_client = openai.OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_secret)
        inbound_context = []
        if context_brief:
            inbound_context.append({
                "role": "system",
                "content": (
                    "You are an AI research consultant. The user performed automated web research.\n"
                    f"RESEARCH BRIEF:\n{context_brief}\n\n"
                    "Answer user questions accurately based on this brief."
                ),
            })
        for past_item in past_conversation:
            speaker_tag = "user" if past_item.get("role") == "user" else "assistant"
            inbound_context.append({"role": speaker_tag, "content": str(past_item.get("text", ""))})
        inbound_context.append({"role": "user", "content": prompt_query})

        groq_output = remote_client.chat.completions.create(model="qwen/qwen3.8-27b", messages=inbound_context, temperature=0.7)
        return groq_output.choices[0].message.content

    if openrouter_secret:
        try:
            return await asyncio.to_thread(_invoke_openrouter)
        except Exception as fault_openrouter:
            failover_logger.warning(f"OpenRouter routing failed: {fault_openrouter}")

    if google_secret:
        try:
            return await asyncio.to_thread(_invoke_gemini)
        except Exception as fault_gemini:
            failover_logger.warning(f"Gemini routing failed: {fault_gemini}")

    if groq_secret:
        try:
            return await asyncio.wait_for(asyncio.to_thread(_invoke_groq), timeout=timeout_allowance)
        except Exception as fault_groq:
            failover_logger.warning(f"Groq routing failed: {fault_groq}")

    raise RuntimeError("All configured AI providers were unreachable or returned errors.")
