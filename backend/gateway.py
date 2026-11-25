import asyncio
import json
import logging
import threading
import webbrowser
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from core_config.environment_loader import ConfigRegistry, VaultManager
from core_config.history_db import TaskHistoryDatabase
from agent_core.stream_collector import ConsoleLogCollector
from agent_core.provider_failover import (
    construct_agent_llm,
    analyze_provider_exception,
    execute_dialogue_query,
)
from agent_core.navigation_driver import (
    BrowserSessionManager,
    BROWSER_SPEED_DIRECTIVES,
    HumanVerificationCoordinator,
    TaskExecutionCoordinator,
    build_verification_controller,
)
from browser_use import Agent

service_logger = logging.getLogger("web_agent.gateway")


async def render_main_interface(incoming_call):
    index_document = ConfigRegistry.ui_directory / "index.html"
    if not index_document.exists():
        return HTMLResponse("<h3>Web Frontend not found.</h3>", status_code=404)
    return FileResponse(
        index_document,
        media_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


async def serve_brand_mark(incoming_call):
    emblem_file = ConfigRegistry.ui_directory / "media" / "brand_mark.png"
    if emblem_file.exists():
        return FileResponse(emblem_file, media_type="image/png")
    return HTMLResponse(status_code=404)


async def get_keys_action(incoming_call):
    ConfigRegistry.sync_environment()
    return JSONResponse({
        "openrouter": ConfigRegistry.lookup_secret("OPENROUTER_API_KEY") or "",
        "gemini": ConfigRegistry.lookup_secret("GOOGLE_API_KEY", "GEMINI_API_KEY") or "",
        "groq": ConfigRegistry.lookup_secret("GROQ_API_KEY") or "",
        "openai": ConfigRegistry.lookup_secret("OPENAI_API_KEY") or "",
        "anthropic": ConfigRegistry.lookup_secret("ANTHROPIC_API_KEY") or "",
    })


async def get_task_history_action(incoming_call):
    try:
        history_records = TaskHistoryDatabase.get_recent_tasks(limit=10)
        return JSONResponse(
            {"history": history_records},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )
    except Exception as caught_fault:
        return JSONResponse({"error": str(caught_fault), "history": []}, status_code=500)


async def record_task_history_action(incoming_call):
    try:
        payload_dict = await incoming_call.json()
        task_str = payload_dict.get("task", "").strip()
        is_success = bool(payload_dict.get("succeeded", False))
        if task_str:
            TaskHistoryDatabase.record_task(task_str, is_success)
        history_records = TaskHistoryDatabase.get_recent_tasks(limit=10)
        return JSONResponse({"success": True, "history": history_records})
    except Exception as caught_fault:
        return JSONResponse({"error": str(caught_fault)}, status_code=500)


async def test_key_action(incoming_call):
    try:
        payload_dict = await incoming_call.json()
        provider_name = (payload_dict.get("provider", "")).strip().lower()
        key_val = (payload_dict.get("key", "")).strip()

        if not key_val:
            return JSONResponse({"success": False, "error": "Please enter an API key to test."}, status_code=200)

        if provider_name == "openrouter":
            import openai
            client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=key_val, timeout=12.0)
            client.models.list()
            return JSONResponse({"success": True, "message": "api test success"})

        elif provider_name == "gemini":
            from google import genai
            client = genai.Client(api_key=key_val)
            client.models.generate_content(model="gemini-flash-lite-latest", contents="hi")
            return JSONResponse({"success": True, "message": "api test success"})

        elif provider_name == "groq":
            import openai
            client = openai.OpenAI(base_url="https://api.groq.com/openai/v1", api_key=key_val, timeout=12.0)
            client.models.list()
            return JSONResponse({"success": True, "message": "api test success"})

        elif provider_name == "openai":
            import openai
            client = openai.OpenAI(api_key=key_val, timeout=12.0)
            client.models.list()
            return JSONResponse({"success": True, "message": "api test success"})

        elif provider_name == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=key_val, timeout=12.0)
            client.models.list()
            return JSONResponse({"success": True, "message": "api test success"})

        else:
            return JSONResponse({"success": False, "error": f"Unknown provider: {provider_name}"}, status_code=200)

    except Exception as exc:
        err_msg = str(exc)
        if hasattr(exc, "message") and exc.message:
            err_msg = str(exc.message)
        elif hasattr(exc, "body") and isinstance(exc.body, dict) and "error" in exc.body:
            err = exc.body["error"]
            if isinstance(err, dict) and "message" in err:
                err_msg = err["message"]
            else:
                err_msg = str(err)
        elif "message': '" in err_msg:
            try:
                start = err_msg.index("message': '") + len("message': '")
                end = err_msg.index("'", start)
                err_msg = err_msg[start:end]
            except Exception:
                pass
        return JSONResponse({"success": False, "error": err_msg}, status_code=200)


async def save_token_action(incoming_call):
    try:
        payload_dict = await incoming_call.json()
        raw_token_entry = payload_dict.get("key", "").strip()
        provider_name = payload_dict.get("provider", "").strip().lower()

        if not raw_token_entry:
            return JSONResponse({"error": "Empty credential provided"}, status_code=400)

        if provider_name:
            is_stored, vendor_title, outcome_memo = VaultManager.set_provider_secret(provider_name, raw_token_entry)
        else:
            is_stored, vendor_title, outcome_memo = VaultManager.store_secret_token(raw_token_entry)

        if not is_stored:
            return JSONResponse({"error": outcome_memo}, status_code=400)

        return JSONResponse({"success": True, "provider": vendor_title, "message": outcome_memo})
    except Exception as caught_fault:
        return JSONResponse({"error": str(caught_fault)}, status_code=500)


async def dialogue_turn_action(incoming_call):
    try:
        payload_dict = await incoming_call.json()
        client_question = payload_dict.get("message", "").strip()
        prior_messages = payload_dict.get("history", [])
        attached_findings = payload_dict.get("context", "").strip()

        if not client_question:
            return JSONResponse({"error": "Query message cannot be blank"}, status_code=400)

        ConfigRegistry.sync_environment()
        assistant_statement = await execute_dialogue_query(
            prompt_query=client_question,
            past_conversation=prior_messages,
            context_brief=attached_findings,
            timeout_allowance=12.0,
        )
        return JSONResponse({"reply": assistant_statement})

    except Exception as caught_fault:
        fault_diagnostic = analyze_provider_exception(caught_fault)
        if fault_diagnostic:
            guidance_advice = (
                f"⚠️ **{fault_diagnostic['provider_title']} Boundary Hit ({fault_diagnostic['category'].replace('_', ' ').title()})**\n\n"
                f"The AI provider exceeded quota or connection limits.\n\n"
                f"**Recommendations:**\n"
                f"1. Wait 30-60 seconds for renewal.\n"
                f"2. Add alternative provider keys in the configuration bar."
            )
            http_status = 429 if "rate" in fault_diagnostic["category"] else 403
            return JSONResponse({
                "is_api_limit": True,
                "api_name": fault_diagnostic["provider_title"],
                "limit_type": fault_diagnostic["category"],
                "error": str(caught_fault),
                "reply": guidance_advice,
            }, status_code=http_status)

        return JSONResponse({"error": str(caught_fault), "reply": f"❌ Error: {str(caught_fault)}"}, status_code=500)


async def resume_verification_action(incoming_call):
    try:
        coordinator = HumanVerificationCoordinator.get_coordinator()
        resumed = coordinator.resolve_verification()
        if resumed:
            return JSONResponse({
                "success": True,
                "message": "Human verification confirmed. Resuming browser agent...",
            })
        return JSONResponse({
            "success": False,
            "message": "No active verification gate found to resume.",
        }, status_code=200)
    except Exception as caught_fault:
        return JSONResponse({"error": str(caught_fault)}, status_code=500)


async def stop_browser_task_action(incoming_call):
    try:
        task_coordinator = TaskExecutionCoordinator.get_coordinator()
        stopped = await task_coordinator.request_stop(terminate_browser=True)
        if stopped:
            return JSONResponse({
                "success": True,
                "message": "Task and browser agent stopped.",
            })
        return JSONResponse({
            "success": False,
            "message": "No active task running to stop.",
        }, status_code=200)
    except Exception as caught_fault:
        return JSONResponse({"success": False, "error": str(caught_fault)}, status_code=500)


async def browser_agent_stream(incoming_call):
    goal_directive = incoming_call.query_params.get("task", "").strip()
    is_background = (
        incoming_call.query_params.get("background_mode")
        or incoming_call.query_params.get("headless", "false")
    ).lower() == "true"

    if not goal_directive:
        async def _missing_goal_stream():
            yield f"data: {json.dumps({'type': 'error', 'text': 'Task directive is required'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return StreamingResponse(_missing_goal_stream(), media_type="text/event-stream")

    stream_queue = asyncio.Queue()
    event_loop_ref = asyncio.get_running_loop()
    collector_broker = ConsoleLogCollector(stream_queue, event_loop_ref)
    collector_broker.setFormatter(logging.Formatter("%(message)s"))

    agent_logger_handle = logging.getLogger("browser_use")
    agent_logger_handle.addHandler(collector_broker)

    coordinator = HumanVerificationCoordinator.get_coordinator()
    coordinator.reset()

    task_coordinator = TaskExecutionCoordinator.get_coordinator()
    run_id, stop_event = task_coordinator.begin_run()
    manager_handle = BrowserSessionManager.obtain_manager()

    def _on_verification_signal(evt_dict: dict):
        try:
            event_loop_ref.call_soon_threadsafe(
                stream_queue.put_nowait,
                {"__custom_event__": True, **evt_dict}
            )
        except Exception:
            pass

    coordinator.register_listener(_on_verification_signal)

    def _format_stream_packet(packet: Any) -> str:
        if isinstance(packet, dict) and packet.get("__custom_event__"):
            evt_type = packet.get("type", "verification_required")
            return f"data: {json.dumps({'type': evt_type, 'reason': packet.get('reason', '')})}\n\n"
        return f"data: {json.dumps({'type': 'log', 'text': str(packet)})}\n\n"

    async def _is_client_disconnected() -> bool:
        try:
            return await incoming_call.is_disconnected()
        except Exception:
            return False

    async def _stream_producer():
        async_worker = None
        try:
            yield f"data: {json.dumps({'type': 'status', 'text': 'Starting Edge Browser Navigation Pipeline...'})}\n\n"

            def _publish_status(status_memo: str):
                try:
                    event_loop_ref.call_soon_threadsafe(stream_queue.put_nowait, status_memo)
                except Exception:
                    pass

            async def _execute_pipeline():
                llm_runtime = construct_agent_llm(on_status_change=_publish_status)
                browser_handle = await manager_handle.acquire_session(background_operation=is_background)
                verification_controller = build_verification_controller()

                navigation_agent = Agent(
                    task=goal_directive,
                    llm=llm_runtime,
                    browser=browser_handle,
                    controller=verification_controller,
                    extend_system_message=BROWSER_SPEED_DIRECTIVES,
                    highlight_actions=False,
                    mode="ultrafast",
                    judge=False,
                )
                return await navigation_agent.run()

            async_worker = asyncio.create_task(_execute_pipeline())
            task_coordinator.attach_task(run_id, async_worker)

            while not async_worker.done():
                if stop_event.is_set() or await _is_client_disconnected():
                    if not async_worker.done():
                        async_worker.cancel()
                    break
                try:
                    packet = await asyncio.wait_for(stream_queue.get(), timeout=0.15)
                    yield _format_stream_packet(packet)
                except asyncio.TimeoutError:
                    continue

            if stop_event.is_set() or task_coordinator.stop_requested or await _is_client_disconnected():
                if not async_worker.done():
                    async_worker.cancel()
                    try:
                        await asyncio.wait([async_worker], timeout=2.0)
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(manager_handle.terminate_session(), timeout=5.0)
                except Exception:
                    pass
                try:
                    TaskHistoryDatabase.record_task(goal_directive, False)
                except Exception as history_fault:
                    service_logger.warning(f"Could not record stopped task history: {history_fault}")
                yield f"data: {json.dumps({'type': 'stopped', 'text': 'Task and browser agent stopped by user.'})}\n\n"
                return

            while not stream_queue.empty():
                yield _format_stream_packet(stream_queue.get_nowait())

            loop_outcome = await async_worker
            extracted_text, is_healthy = manager_handle.extract_task_conclusion(loop_outcome)

            try:
                TaskHistoryDatabase.record_task(goal_directive, is_healthy)
            except Exception as history_fault:
                service_logger.warning(f"Could not record history: {history_fault}")

            yield f"data: {json.dumps({'type': 'result', 'text': extracted_text, 'success': is_healthy, 'is_api_limit': not is_healthy and bool(analyze_provider_exception(extracted_text))})}\n\n"

        except asyncio.CancelledError:
            if async_worker is not None and not async_worker.done():
                async_worker.cancel()
            try:
                await asyncio.wait_for(manager_handle.terminate_session(), timeout=5.0)
            except Exception:
                pass
            try:
                TaskHistoryDatabase.record_task(goal_directive, False)
            except Exception:
                pass
            yield f"data: {json.dumps({'type': 'stopped', 'text': 'Task and browser agent stopped by user.'})}\n\n"

        except Exception as caught_fault:
            if stop_event.is_set() or task_coordinator.stop_requested:
                try:
                    await asyncio.wait_for(manager_handle.terminate_session(), timeout=5.0)
                except Exception:
                    pass
                try:
                    TaskHistoryDatabase.record_task(goal_directive, False)
                except Exception:
                    pass
                yield f"data: {json.dumps({'type': 'stopped', 'text': 'Task and browser agent stopped by user.'})}\n\n"
            else:
                try:
                    TaskHistoryDatabase.record_task(goal_directive, False)
                except Exception:
                    pass
                fault_diagnostic = analyze_provider_exception(caught_fault)
                if fault_diagnostic:
                    yield f"data: {json.dumps({'type': 'api_limit', 'is_api_limit': True, 'api_name': fault_diagnostic['provider_title'], 'limit_type': fault_diagnostic['category'], 'text': str(caught_fault)})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'text': str(caught_fault)})}\n\n"
        finally:
            coordinator.reset()
            task_coordinator.finish_run(run_id)
            agent_logger_handle.removeHandler(collector_broker)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        _stream_producer(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


async def system_readiness_check(incoming_call):
    ConfigRegistry.sync_environment()
    return JSONResponse({
        "status": "online",
        "providers": {
            "openrouter": bool(ConfigRegistry.lookup_secret("OPENROUTER_API_KEY")),
            "gemini": bool(ConfigRegistry.lookup_secret("GOOGLE_API_KEY", "GEMINI_API_KEY")),
            "groq": bool(ConfigRegistry.lookup_secret("GROQ_API_KEY")),
            "openai": bool(ConfigRegistry.lookup_secret("OPENAI_API_KEY")),
            "anthropic": bool(ConfigRegistry.lookup_secret("ANTHROPIC_API_KEY")),
        },
    })


def build_server_app() -> Starlette:
    static_assets_path = ConfigRegistry.ui_directory
    configured_routes = [
        Route("/", render_main_interface, methods=["GET"]),
        Route("/media/brand_mark.png", serve_brand_mark, methods=["GET"]),
        Route("/logo.png", serve_brand_mark, methods=["GET"]),
        Route("/api/run", browser_agent_stream, methods=["GET"]),
        Route("/api/stop", stop_browser_task_action, methods=["POST"]),
        Route("/api/resume-verification", resume_verification_action, methods=["POST"]),
        Route("/api/chat", dialogue_turn_action, methods=["POST"]),
        Route("/api/keys", get_keys_action, methods=["GET"]),
        Route("/api/history", get_task_history_action, methods=["GET"]),
        Route("/api/history", record_task_history_action, methods=["POST"]),
        Route("/api/test-key", test_key_action, methods=["POST"]),
        Route("/api/save-key", save_token_action, methods=["POST"]),
        Route("/api/set-key", save_token_action, methods=["POST"]),
        Route("/api/health", system_readiness_check, methods=["GET"]),
        Mount("/static", StaticFiles(directory=str(static_assets_path)), name="static"),
    ]
    return Starlette(routes=configured_routes)


def start_web_server(network_host: str = "127.0.0.1", port_number: int = 5000, trigger_browser: bool = True):
    application_hub = build_server_app()

    if trigger_browser:
        def _spawn_browser():
            import time
            time.sleep(1.0)
            webbrowser.open(f"http://{network_host}:{port_number}")
        threading.Thread(target=_spawn_browser, daemon=True).start()

    uvicorn.run(application_hub, host=network_host, port=port_number, log_level="warning")


if __name__ == "__main__":
    start_web_server()
