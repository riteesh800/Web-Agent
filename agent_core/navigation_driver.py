import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from browser_use import Agent, Browser, Controller
from browser_use.agent.loop import last_text

from .provider_failover import construct_agent_llm, analyze_provider_exception

_LAST_INSPECT_OPEN_TS: float = 0.0
_INSPECT_OPEN_COOLDOWN_S: float = 15.0

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
    "(e.g. accounts.google.com/v3/signin/rejected) OR shows an unexpected sign-in page for a task expecting an already logged-in account:\n"
    "     DO NOT repeatedly click 'Try again' or attempt workarounds.\n"
    "     Stop immediately and report clearly that isolated automation mode does not share your Microsoft Edge logins and may be blocked by Google's browser-security check. "
    "Instruct the user to keep 'Use Open Edge Browser (Gmail / Logged-In Sites)' checked in the Web Agent UI, enable remote debugging "
    "at edge://inspect/#remote-debugging in Microsoft Edge, and sign in normally there.\n\n"
    "6. ATTACHED EDGE TAB SAFETY & GMAIL WORKFLOW:\n"
    "   - Never close or navigate away from the Web Agent UI tab (127.0.0.1:5000 or localhost:5000); always work in a separate tab.\n"
    "   - For Gmail / logged-in tasks, navigate to https://mail.google.com in your working tab. If multiple Gmail accounts are signed in, inspect/switch accounts as requested or clearly list which accounts/senders are visible if a specific sender was not named.\n\n"
    "7. STRUCTURED REPORTING:\n"
    "   - Conclude with a clean, bulleted Markdown synthesis including relevant summaries, names, links, and pricing as appropriate for the task."
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


def find_edge_devtools_endpoint() -> Optional[Tuple[int, Optional[str], Path]]:
    """Find live Microsoft Edge DevTools endpoint (port, ws_path, user_data_dir) if remote debugging is active."""
    import socket

    for base in get_edge_user_data_dirs():
        port_file = base / "DevToolsActivePort"
        if not port_file.is_file():
            continue
        try:
            lines = port_file.read_text(encoding="utf-8", errors="replace").splitlines()
            port = int(lines[0].strip())
            ws_path = lines[1].strip() if len(lines) > 1 and lines[1].strip().startswith("/") else None
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return port, ws_path, base
        except (OSError, ValueError, IndexError):
            continue
    return None


def find_edge_devtools_port() -> Optional[int]:
    """Find the live DevTools port for Microsoft Edge if remote debugging is active."""
    endpoint = find_edge_devtools_endpoint()
    return endpoint[0] if endpoint is not None else None


def resolve_edge_cdp_url(port: int) -> str:
    """Resolve the WebSocket CDP URL for Edge (modern Edge disables /json/version HTTP discovery on default dir)."""
    endpoint = find_edge_devtools_endpoint()
    if endpoint is not None and endpoint[0] == port and endpoint[1]:
        return f"ws://127.0.0.1:{port}{endpoint[1]}"
    return f"http://127.0.0.1:{port}"


def _read_edge_local_state(base: Path) -> Dict[str, Any]:
    """Read Microsoft Edge Local State JSON safely."""
    try:
        return json.loads((base / "Local State").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return {}


def get_edge_last_used_profile() -> Optional[str]:
    """Return the last active profile directory name (e.g. 'Default' or 'Profile 1') for Microsoft Edge."""
    for base in get_edge_user_data_dirs():
        if not base.exists():
            continue
        state = _read_edge_local_state(base)
        last = (state.get("profile") or {}).get("last_used")
        if isinstance(last, str) and (base / last).is_dir():
            return last
        if (base / "Default").is_dir():
            return "Default"
    return None


def has_edge_remote_debugging_toggle() -> Optional[bool]:
    """Check whether Edge Local State records remote debugging user-enabled as True or False."""
    seen: Optional[bool] = None
    for base in get_edge_user_data_dirs():
        if not base.exists():
            continue
        state = _read_edge_local_state(base)
        enabled = ((state.get("devtools") or {}).get("remote_debugging") or {}).get("user-enabled")
        if enabled is True:
            return True
        if enabled is False:
            seen = False
    return seen


def is_edge_running() -> bool:
    """Best-effort check whether Microsoft Edge is currently running."""
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq msedge.exe"],
                text=True,
                errors="replace",
                timeout=5,
            )
            return "msedge.exe" in out.lower()
        elif sys.platform == "darwin":
            proc = subprocess.run(
                ["pgrep", "-f", "Microsoft Edge"],
                capture_output=True,
                timeout=5,
            )
            return proc.returncode == 0
        else:
            proc = subprocess.run(
                ["pgrep", "-f", "microsoft-edge"],
                capture_output=True,
                timeout=5,
            )
            return proc.returncode == 0
    except Exception:
        return False


def launch_user_edge(target_url: Optional[str] = None) -> bool:
    """Launch or open a tab in the user's real laptop Microsoft Edge (never a temp isolated profile)."""
    edge_executable = _find_edge_executable()
    cmd = [edge_executable]
    profile_dir = get_edge_last_used_profile()
    if profile_dir:
        cmd.append(f"--profile-directory={profile_dir}")
    if target_url:
        cmd.append(target_url)

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return True


def open_edge_inspect_page(force: bool = False) -> bool:
    """Open edge://inspect/#remote-debugging in the user's laptop Microsoft Edge."""
    global _LAST_INSPECT_OPEN_TS
    now = time.monotonic()
    if not force and (now - _LAST_INSPECT_OPEN_TS) < _INSPECT_OPEN_COOLDOWN_S:
        return False
    launched = launch_user_edge("edge://inspect/#remote-debugging")
    if launched:
        _LAST_INSPECT_OPEN_TS = now
    return launched


async def ensure_edge_debugging_ready(
    on_status: Optional[Callable[[str], None]] = None,
    stop_event: Optional[asyncio.Event] = None,
    wait_timeout: float = 10.0,
) -> int:
    """Ensure Microsoft Edge is running with remote debugging active and return its DevTools port."""
    existing_port = find_edge_devtools_port()
    if existing_port is not None:
        return existing_port

    def _emit(msg: str):
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    async def _poll_for_port(timeout_s: float) -> Optional[int]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise asyncio.CancelledError()
            found = find_edge_devtools_port()
            if found is not None:
                return found
            await asyncio.sleep(0.35)
        return find_edge_devtools_port()

    if stop_event is not None and stop_event.is_set():
        raise asyncio.CancelledError()

    if not is_edge_running():
        _emit("Launching your laptop Microsoft Edge browser...")
        await asyncio.to_thread(launch_user_edge)
        if has_edge_remote_debugging_toggle() is True:
            boot_port = await _poll_for_port(wait_timeout)
            if boot_port is not None:
                return boot_port

    raise RuntimeError(
        "remote-debugging-setup: DevToolsActivePort not found for Microsoft Edge. "
        "Open edge://inspect/#remote-debugging in Microsoft Edge and tick "
        "'Allow remote debugging for this browser instance'."
    )


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
            "edge_running": is_edge_running(),
            "toggle_enabled": has_edge_remote_debugging_toggle(),
        }
    except Exception as exc:
        return {
            "ready": False,
            "browser": "Microsoft Edge",
            "browsers": [],
            "port": None,
            "dirs": [],
            "edge_running": False,
            "toggle_enabled": None,
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
            "Microsoft Edge remote debugging is not enabled yet (one-time setup; Edge blocks apps from toggling it automatically):\n"
            "1. Open a new tab in your Microsoft Edge (Ctrl+T).\n"
            "2. Paste  edge://inspect/#remote-debugging  into the address bar and press Enter (use the '📋 Copy' button above).\n"
            "3. Tick 'Allow remote debugging for this browser instance'.\n"
            "4. Come back here, click '▶ Run Task', and click 'Allow' on Microsoft Edge's permission prompt."
        )

    if any(
        token in lower
        for token in (
            "permission-blocked",
            "cdp ws handshake failed",
            "opening handshake",
            "http 403",
            "status 403",
        )
    ):
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


def _is_ui_tab_url(tab_url: str, ui_netloc: Optional[str] = None) -> bool:
    """Return True if a browser tab URL points to the Web Agent frontend itself."""
    if not tab_url:
        return False
    try:
        parsed = urlparse(tab_url)
    except ValueError:
        return False
    netloc = (parsed.netloc or "").lower()
    if not netloc:
        return False
    if ui_netloc and netloc == ui_netloc.lower():
        return True
    return netloc in {"127.0.0.1:5000", "localhost:5000"}


async def ensure_attached_working_tab(browser_session: Any, ui_netloc: Optional[str] = None) -> None:
    """Ensure an attached Edge Browser session works in a dedicated visible tab, never hijacking the Web Agent UI tab."""
    if getattr(browser_session, "kind", None) != "cdp":
        return
    start_fn = getattr(browser_session, "start", None)
    daemon_name_fn = getattr(browser_session, "_daemon_name", None)
    if not callable(start_fn) or not callable(daemon_name_fn):
        return

    await start_fn()

    from browser_use.browser.browser import _daemon_cdp, _daemon_request

    daemon_name = daemon_name_fn()
    try:
        current = await asyncio.to_thread(_daemon_request, daemon_name, {"meta": "current_tab"}, 5.0)
    except Exception:
        return

    current_id = current.get("targetId") if isinstance(current, dict) else None
    current_url = (current.get("url") or "") if isinstance(current, dict) else ""

    if current_id and not _is_ui_tab_url(current_url, ui_netloc=ui_netloc):
        try:
            await asyncio.to_thread(_daemon_cdp, daemon_name, "Target.activateTarget", None, targetId=current_id)
        except Exception:
            pass
        return

    # _attach_open_page() attached to the Web Agent UI tab because it was the only open non-internal page.
    # Open a fresh foreground tab in the same Edge window so the SSE stream and UI remain intact.
    created = await asyncio.to_thread(
        _daemon_cdp,
        daemon_name,
        "Target.createTarget",
        None,
        url="about:blank",
        background=False,
    )
    new_tid = created.get("targetId") if isinstance(created, dict) else None
    if not new_tid:
        return

    attached = await asyncio.to_thread(
        _daemon_cdp,
        daemon_name,
        "Target.attachToTarget",
        None,
        targetId=new_tid,
        flatten=True,
    )
    new_sid = attached.get("sessionId") if isinstance(attached, dict) else None
    if new_sid:
        await asyncio.to_thread(
            _daemon_request,
            daemon_name,
            {"meta": "set_session", "session_id": new_sid, "target_id": new_tid},
            5.0,
        )
        browser_session._agent_tab = new_tid
        try:
            await asyncio.to_thread(_daemon_cdp, daemon_name, "Target.activateTarget", None, targetId=new_tid)
        except Exception:
            pass


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

        expected_cdp_url: Optional[str] = None
        if target_kind == "cdp":
            edge_port = find_edge_devtools_port()
            if edge_port is None:
                raise RuntimeError(
                    "remote-debugging-setup: DevToolsActivePort not found for Microsoft Edge. "
                    "Please open Microsoft Edge, navigate to edge://inspect/#remote-debugging, "
                    "and tick 'Allow remote debugging for this browser instance'."
                )
            expected_cdp_url = resolve_edge_cdp_url(edge_port)

        if self.active_session is not None:
            session_kind = getattr(self.active_session, "kind", None)
            session_headless = getattr(self.active_session, "headless", None)
            needs_replace = session_kind != target_kind or (
                target_kind == "chromium" and session_headless != background_operation
            )
            if not needs_replace and target_kind == "cdp":
                current_cdp_url = getattr(self.active_session, "cdp_url", None)
                if current_cdp_url != expected_cdp_url:
                    needs_replace = True
                else:
                    daemon_name = getattr(self.active_session, "name", None)
                    if daemon_name:
                        try:
                            from browser_harness import admin
                            if not admin.daemon_alive(daemon_name):
                                needs_replace = True
                        except Exception:
                            needs_replace = True
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
                assert expected_cdp_url is not None
                self.active_session = Browser(cdp_url=expected_cdp_url, keep_alive=True)
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
