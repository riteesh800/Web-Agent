import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from browser_use import Agent, Browser, Controller
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
    "4. CAPTCHA & HUMAN VERIFICATION HANDLING:\n"
    "   - When ANY security check, CAPTCHA, reCAPTCHA, hCaptcha, Cloudflare turnstile, or 'verify you are human' prompt appears:\n"
    "     DO NOT attempt to solve, guess, or bypass it automatically.\n"
    "     Immediately call `pause_for_human_verification` with a brief description of the challenge.\n"
    "     The frontend UI will alert the user to complete verification in the visible Microsoft Edge window.\n"
    "     Once resumed, re-inspect the page DOM and proceed with the task.\n\n"
    "5. STRUCTURED REPORTING:\n"
    "   - Conclude with a clean, bulleted Markdown synthesis including product names, links, and pricing."
)


class HumanVerificationCoordinator:
    _instance: Optional["HumanVerificationCoordinator"] = None

    def __init__(self):
        self.active_gate: Optional[asyncio.Event] = None
        self.is_waiting: bool = False
        self.last_reason: str = ""
        self._listener: Optional[Callable[[Dict[str, Any]], None]] = None

    @classmethod
    def get_coordinator(cls) -> "HumanVerificationCoordinator":
        if cls._instance is None:
            cls._instance = HumanVerificationCoordinator()
        return cls._instance

    def register_listener(self, listener: Optional[Callable[[Dict[str, Any]], None]]):
        self._listener = listener

    async def pause_for_verification(self, reason: str = "CAPTCHA or security challenge detected on page") -> str:
        self.is_waiting = True
        self.last_reason = reason
        self.active_gate = asyncio.Event()

        if self._listener:
            try:
                self._listener({
                    "type": "verification_required",
                    "reason": reason,
                })
            except Exception:
                pass

        try:
            await self.active_gate.wait()
            return "Human verification confirmed by user. Re-inspect the page DOM and proceed with the browsing directive."
        finally:
            self.is_waiting = False
            self.active_gate = None

    def resolve_verification(self) -> bool:
        if self.active_gate and not self.active_gate.is_set():
            self.active_gate.set()
            return True
        return False

    def reset(self):
        if self.active_gate and not self.active_gate.is_set():
            self.active_gate.set()
        self.is_waiting = False
        self.active_gate = None
        self.last_reason = ""


def build_verification_controller() -> Controller:
    controller = Controller()

    @controller.action(
        description=(
            "Pause execution for human verification when ANY anti-bot challenge, CAPTCHA, reCAPTCHA, "
            "hCaptcha, Cloudflare turnstile, or 'verify you are human' prompt appears on the page. "
            "Alerts the user in the frontend UI to complete the verification in Microsoft Edge."
        )
    )
    async def pause_for_human_verification(reason: str = "CAPTCHA or security challenge detected") -> str:
        coordinator = HumanVerificationCoordinator.get_coordinator()
        return await coordinator.pause_for_verification(reason)

    return controller


def _find_edge_executable() -> Optional[str]:
    """Locate the installed Microsoft Edge binary, returning None to fall back to default Chromium discovery."""
    if sys.platform == "win32":
        candidates = []
        for env_var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base_dir = os.environ.get(env_var)
            if base_dir:
                candidates.append(Path(base_dir) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        which_edge = shutil.which("msedge")
        if which_edge:
            candidates.append(Path(which_edge))
    elif sys.platform == "darwin":
        candidates = [
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates = [
            Path(found)
            for name in ("microsoft-edge-stable", "microsoft-edge")
            if (found := shutil.which(name))
        ]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


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
            edge_executable = _find_edge_executable()
            browser_kwargs: Dict[str, Any] = {
                "headless": background_operation,
                "keep_alive": True,
            }
            if edge_executable:
                browser_kwargs["executable_path"] = edge_executable
            self.active_session = Browser("chromium", **browser_kwargs)

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
