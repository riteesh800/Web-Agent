import asyncio
from typing import Any, Optional, Tuple

from browser_use import Agent, Browser
from browser_use.agent.loop import last_text

from .provider_failover import construct_agent_llm, analyze_provider_exception

BROWSER_SPEED_DIRECTIVES = (
    "HIGH-SPEED BROWSING & RESEARCH INSTRUCTIONS:\n"
    "1. DIRECT NAVIGATION:\n"
    "   - If target store/site is specified (Amazon, YouTube, Google, Wikipedia, Reddit, etc.), "
    "navigate directly to target URL rather than using generic search engines.\n"
    "   - Use search engine only when domain is unknown.\n\n"
    "2. INSTANT FILTERING & SORTING:\n"
    "   - Append sort query arguments directly to URL for 0-step sorting (e.g. Amazon: `&s=price-asc-rank`).\n\n"
    "3. RAPID DOM EXTRACTION:\n"
    "   - Extract structured cards or listings using concise JavaScript evaluation rather than parsing massive accessibility trees.\n\n"
    "4. CAPTCHA HANDLING:\n"
    "   - Pause for human intervention if interactive challenges or bot-detection screens appear.\n\n"
    "5. STRUCTURED REPORTING:\n"
    "   - Conclude with a clean, bulleted Markdown synthesis including product names, links, and pricing."
)


class BrowserSessionManager:
    _singleton_handle: Optional["BrowserSessionManager"] = None

    def __init__(self):
        self.active_session: Optional[Browser] = None

    @classmethod
    def obtain_manager(cls) -> "BrowserSessionManager":
        if cls._singleton_handle is None:
            cls._singleton_handle = BrowserSessionManager()
        return cls._singleton_handle

    async def acquire_session(self, background_operation: bool) -> Browser:
        if self.active_session is not None:
            session_headless = getattr(self.active_session, "headless", None)
            if session_headless != background_operation:
                try:
                    await self.active_session.stop()
                except Exception:
                    pass
                self.active_session = None

        if (
            self.active_session is None
            or getattr(self.active_session, "stopped", False)
            or (
                getattr(self.active_session, "_local", None)
                and self.active_session._local.proc
                and self.active_session._local.proc.poll() is not None
            )
        ):
            self.active_session = Browser("edge", headless=background_operation, keep_alive=True)

        return self.active_session

    async def terminate_session(self):
        if self.active_session is not None:
            try:
                await self.active_session.stop()
            except Exception:
                pass
            self.active_session = None

    @staticmethod
    def extract_task_conclusion(execution_output: Any) -> Tuple[str, bool]:
        extracted_summary = ""
        if execution_output.final_result() and str(execution_output.final_result()).strip():
            extracted_summary = str(execution_output.final_result()).strip()
        elif getattr(execution_output, "text", None) and str(execution_output.text).strip():
            extracted_summary = str(execution_output.text).strip()
        elif getattr(execution_output, "output", None) and str(execution_output.output).strip() != "None":
            extracted_summary = str(execution_output.output).strip()
        elif getattr(execution_output, "messages", None):
            tail_msg = last_text(execution_output.messages)
            if tail_msg and tail_msg.strip():
                extracted_summary = tail_msg.strip()

        is_valid_run = execution_output.status == "completed" and not execution_output.has_errors() and bool(extracted_summary)

        if not extracted_summary:
            fault_memo = getattr(execution_output, "error", "") or ""
            diagnostic_details = analyze_provider_exception(fault_memo) if fault_memo else None
            if diagnostic_details:
                extracted_summary = (
                    f"⚠️ **{diagnostic_details['provider_title']} Limit Reached ({diagnostic_details['category'].replace('_', ' ').title()})**\n\n"
                    f"Operation paused because {diagnostic_details['provider_title']} reached request boundaries."
                )
            elif "402" in str(fault_memo) or "quota" in str(fault_memo).lower() or "credits" in str(fault_memo).lower():
                extracted_summary = "⚠️ **Browser credits exhausted (402 Quota)**\nPlease provide a free alternative token in configuration."
            elif execution_output.status == "error":
                extracted_summary = f"❌ **Automation Error:**\n\n{fault_memo or 'Execution stopped unexpectedly.'}"
            elif execution_output.status == "timeout":
                extracted_summary = "⏱️ **Operation Timed Out**"
            elif execution_output.status == "max_steps":
                extracted_summary = f"⚠️ **Step Limit Reached:** Completed {execution_output.steps} iterations without final yield."
            else:
                extracted_summary = f"Notice: Process finished in {execution_output.steps} steps without returning text."
            is_valid_run = False

        return extracted_summary, is_valid_run
