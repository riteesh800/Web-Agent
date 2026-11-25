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
    "5. ACCOUNT SIGN-IN SECURITY REJECTIONS (E.G. GMAIL / GOOGLE):\n"
    "   - If Google Sign-In or any login portal shows 'Couldn't sign you in' / 'This browser or app may not be secure' "
    "(e.g. accounts.google.com/v3/signin/rejected):\n"
    "     DO NOT repeatedly click 'Try again' or attempt workarounds.\n"
    "     Stop immediately and report clearly that isolated automation mode is blocked by Google's browser-security check "
    "(Chromium launched with --remote-debugging-port sets navigator.webdriver=true and starts with an empty temporary profile). "
    "Instruct the user to enable 'Use Open Browser (Gmail / Logged-In Sites)' in the Web Agent UI, turn on remote debugging "
    "at edge://inspect/#remote-debugging in Microsoft Edge, and sign in normally there.\n\n"
    "6. STRUCTURED REPORTING:\n"
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


class TaskExecutionCoordinator:
    """Tracks the active browser-agent task so it can be cancelled immediately on user stop."""

    _instance: Optional["TaskExecutionCoordinator"] = None

    def __init__(self):
        self.active_task: Optional[asyncio.Task] = None
        self.stop_event: Optional[asyncio.Event] = None
        self.is_running: bool = False
        self.stop_requested: bool = False
        self.run_id: int = 0

    @classmethod
    def get_coordinator(cls) -> "TaskExecutionCoordinator":
        if cls._instance is None:
            cls._instance = TaskExecutionCoordinator()
        return cls._instance

    def begin_run(self) -> Tuple[int, asyncio.Event]:
        if self.active_task is not None and not self.active_task.done():
            self.active_task.cancel()
        self.run_id += 1
        self.is_running = True
        self.stop_requested = False
        self.active_task = None
        self.stop_event = asyncio.Event()
        return self.run_id, self.stop_event

    def attach_task(self, run_id: int, task: asyncio.Task):
        if self.run_id != run_id:
            return
        self.active_task = task
        if self.stop_requested and not task.done():
            task.cancel()

    async def request_stop(self, terminate_browser: bool = True) -> bool:
        """Immediately halt the running agent task, clear any verification wait, and close the browser session."""
        has_active_work = self.is_running or (self.active_task is not None and not self.active_task.done())
        HumanVerificationCoordinator.get_coordinator().reset()

        if not has_active_work:
            return False

        self.stop_requested = True
        if self.stop_event is not None:
            self.stop_event.set()

        task_ref = self.active_task
        if task_ref is not None and not task_ref.done():
            task_ref.cancel()
            try:
                await asyncio.wait([task_ref], timeout=2.0)
            except Exception:
                pass

        if terminate_browser:
            try:
                await asyncio.wait_for(BrowserSessionManager.obtain_manager().terminate_session(), timeout=5.0)
            except Exception:
                pass

        return True

    def finish_run(self, run_id: int):
        if self.run_id == run_id:
            self.is_running = False
            self.active_task = None
            self.stop_event = None

    def reset(self):
        if self.active_task is not None and not self.active_task.done():
            self.active_task.cancel()
        self.is_running = False
        self.stop_requested = False
        self.active_task = None
        self.stop_event = None


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


def _find_edge_executable() -> str:
    """Locate the installed Microsoft Edge binary. Raises RuntimeError if not found to strictly enforce Edge."""
    candidates = []
    if sys.platform == "win32":
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

    raise RuntimeError(
        "Microsoft Edge executable was not found. This project is strictly configured to use Microsoft Edge only. "
        "Please ensure Microsoft Edge is installed."
    )


def normalize_browser_mode(browser_mode: Optional[str]) -> str:
    """Normalize requested browser session mode to 'attached' or 'isolated'. Strictly forbids Chrome."""
    cleaned = (browser_mode or "").strip().lower()
    if "chrome" in cleaned:
        raise ValueError("Google Chrome is strictly disallowed in this project. Only Microsoft Edge is supported.")
    if cleaned in {"attached", "open", "open_browser", "edge"}:
        return "attached"
    return "isolated"


def get_edge_user_data_dirs() -> list[Path]:
    """Locate Microsoft Edge user-data directories across platforms."""
    dirs = []
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base = Path(local_app_data) / "Microsoft"
            for variant in ("Edge", "Edge Beta", "Edge Dev", "Edge SxS"):
                dirs.append(base / variant / "User Data")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Microsoft Edge"
        dirs.append(base)
    else:
        dirs.append(Path.home() / ".config" / "microsoft-edge")
        dirs.append(Path.home() / ".config" / "microsoft-edge-beta")
        dirs.append(Path.home() / ".config" / "microsoft-edge-dev")
    return dirs


def find_edge_devtools_port() -> Optional[int]:
    """Find the live DevTools port for Microsoft Edge if remote debugging is active."""
    import socket

    for base in get_edge_user_data_dirs():
        port_file = base / "DevToolsActivePort"
        if not port_file.is_file():
            continue
        try:
            port = int(port_file.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip())
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return port
        except (OSError, ValueError, IndexError):
            continue
    return None


def detect_open_browser_debugging() -> Dict[str, Any]:
    """Check whether Microsoft Edge currently has remote debugging enabled."""
    try:
        live_port = find_edge_devtools_port()
        edge_dirs = [str(d) for d in get_edge_user_data_dirs() if d.exists()]
        return {
            "ready": live_port is not None,
            "browser": "Microsoft Edge",
            "browsers": ["Microsoft Edge"] if live_port is not None else [],
            "port": live_port,
            "dirs": edge_dirs,
        }
    except Exception as exc:
        return {
            "ready": False,
            "browser": "Microsoft Edge",
            "browsers": [],
            "port": None,
            "dirs": [],
            "error": str(exc),
        }


def format_browser_connection_guidance(error: Any) -> Optional[str]:
    """Translate browser attachment errors into clear Microsoft Edge instructions."""
    raw = str(error or "")
    lower = raw.lower()

    if any(
        token in lower
        for token in (
            "remote-debugging-setup",
            "devtoolsactiveport not found",
            "not live yet",
            "edge remote debugging is not enabled",
        )
    ):
        return (
            "Open-browser attachment needs remote debugging enabled in Microsoft Edge (one-time per browser session):\n"
            "1. Open Microsoft Edge normally and sign into Gmail or your sites there.\n"
            "2. Visit edge://inspect/#remote-debugging in Microsoft Edge.\n"
            "3. Tick 'Allow remote debugging for this browser instance'.\n"
            "4. Run your task again and click 'Allow' on Microsoft Edge's permission prompt."
        )

    if "permission-blocked" in lower:
        return (
            "Microsoft Edge did not approve the remote debugging connection (the 'Allow remote debugging?' prompt was dismissed, denied, or timed out). "
            "Run the task again and click 'Allow' when Microsoft Edge asks."
        )

    if "edge-not-running" in lower or "no running microsoft edge" in lower:
        return (
            "No running Microsoft Edge window with active remote debugging was found to attach to. "
            "Open Microsoft Edge normally, enable remote debugging at edge://inspect/#remote-debugging, and run the task again."
        )

    return None


class BrowserSessionManager:
    _singleton_handle: Optional["BrowserSessionManager"] = None

    def __init__(self):
        self.active_session: Optional[Browser] = None
        self.active_mode: Optional[str] = None

    @classmethod
    def obtain_manager(cls) -> "BrowserSessionManager":
        if cls._singleton_handle is None:
            cls._singleton_handle = BrowserSessionManager()
        return cls._singleton_handle

    async def acquire_session(self, background_operation: bool, browser_mode: str = "isolated") -> Browser:
        normalized_mode = normalize_browser_mode(browser_mode)
        target_kind = "cdp" if normalized_mode == "attached" else "chromium"

        if self.active_session is not None:
            session_kind = getattr(self.active_session, "kind", None)
            session_headless = getattr(self.active_session, "headless", None)
            needs_replace = session_kind != target_kind or (
                target_kind == "chromium" and session_headless != background_operation
            )
            if needs_replace:
                try:
                    await self.active_session.stop()
                except Exception:
                    pass
                self.active_session = None
                self.active_mode = None

        if (
            self.active_session is None
            or getattr(self.active_session, "stopped", False)
            or (
                getattr(self.active_session, "_local", None)
                and self.active_session._local.proc
                and self.active_session._local.proc.poll() is not None
            )
        ):
            if target_kind == "cdp":
                edge_port = find_edge_devtools_port()
                if edge_port is None:
                    raise RuntimeError(
                        "remote-debugging-setup: DevToolsActivePort not found for Microsoft Edge. "
                        "Please open Microsoft Edge, navigate to edge://inspect/#remote-debugging, "
                        "and tick 'Allow remote debugging for this browser instance'."
                    )
                self.active_session = Browser(cdp_url=f"http://127.0.0.1:{edge_port}", keep_alive=True)
            else:
                edge_executable = _find_edge_executable()
                browser_kwargs: Dict[str, Any] = {
                    "headless": background_operation,
                    "keep_alive": True,
                    "executable_path": edge_executable,
                }
                self.active_session = Browser("chromium", **browser_kwargs)
            self.active_mode = normalized_mode

        return self.active_session

    async def terminate_session(self):
        session = self.active_session
        self.active_session = None
        self.active_mode = None
        if session is not None:
            try:
                await session.stop()
            except Exception:
                pass

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
            connection_guidance = format_browser_connection_guidance(fault_memo) if fault_memo else None
            diagnostic_details = analyze_provider_exception(fault_memo) if fault_memo else None
            if connection_guidance:
                extracted_summary = f"🔐 **Open-Browser Attachment Setup Needed**\n\n{connection_guidance}"
            elif diagnostic_details:
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
