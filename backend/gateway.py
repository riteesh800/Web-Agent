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
from agent_core.stream_collector import ConsoleLogCollector
from agent_core.provider_failover import (
    construct_agent_llm,
    analyze_provider_exception,
    execute_dialogue_query,
)
from agent_core.navigation_driver import BrowserSessionManager, BROWSER_SPEED_DIRECTIVES
from browser_use import Agent

service_logger = logging.getLogger("web_agent.gateway")


async def render_main_interface(incoming_call):
    index_document = ConfigRegistry.ui_directory / "index.html"
    if not index_document.exists():
        return HTMLResponse("<h3>Web Frontend not found.</h3>", status_code=404)
    return FileResponse(index_document, media_type="text/html")


async def serve_brand_mark(incoming_call):
    emblem_file = ConfigRegistry.ui_directory / "media" / "brand_mark.png"
    if emblem_file.exists():
        return FileResponse(emblem_file, media_type="image/png")
    return HTMLResponse(status_code=404)


async def save_token_action(incoming_call):
    try:
        payload_dict = await incoming_call.json()
        raw_token_entry = payload_dict.get("key", "").strip()
        if not raw_token_entry:
            return JSONResponse({"error": "Empty credential provided"}, status_code=400)

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


async def browser_agent_stream(incoming_call):
    goal_directive = incoming_call.query_params.get("task", "").strip()
    is_background = incoming_call.query_params.get("headless", "false").lower() == "true"

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

    async def _stream_producer():
        try:
            yield f"data: {json.dumps({'type': 'status', 'text': 'Starting Edge Browser Navigation Pipeline...'})}\n\n"

            def _publish_status(status_memo: str):
                try:
                    event_loop_ref.call_soon_threadsafe(stream_queue.put_nowait, status_memo)
                except Exception:
                    pass

            llm_runtime = construct_agent_llm(on_status_change=_publish_status)
            manager_handle = BrowserSessionManager.obtain_manager()
            browser_handle = await manager_handle.acquire_session(background_operation=is_background)

            navigation_agent = Agent(
                task=goal_directive,
                llm=llm_runtime,
                browser=browser_handle,
                extend_system_message=BROWSER_SPEED_DIRECTIVES,
                highlight_actions=False,
                mode="ultrafast",
                judge=False,
            )

            async def _execute_loop():
                return await navigation_agent.run()

            async_worker = asyncio.create_task(_execute_loop())

            while not async_worker.done():
                try:
                    packet = await asyncio.wait_for(stream_queue.get(), timeout=0.25)
                    yield f"data: {json.dumps({'type': 'log', 'text': packet})}\n\n"
                except asyncio.TimeoutError:
                    continue

            while not stream_queue.empty():
                packet = stream_queue.get_nowait()
                yield f"data: {json.dumps({'type': 'log', 'text': packet})}\n\n"

            loop_outcome = await async_worker
            extracted_text, is_healthy = manager_handle.extract_task_conclusion(loop_outcome)

            yield f"data: {json.dumps({'type': 'result', 'text': extracted_text, 'success': is_healthy, 'is_api_limit': not is_healthy and bool(analyze_provider_exception(extracted_text))})}\n\n"

        except Exception as caught_fault:
            fault_diagnostic = analyze_provider_exception(caught_fault)
            if fault_diagnostic:
                yield f"data: {json.dumps({'type': 'api_limit', 'is_api_limit': True, 'api_name': fault_diagnostic['provider_title'], 'limit_type': fault_diagnostic['category'], 'text': str(caught_fault)})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'error', 'text': str(caught_fault)})}\n\n"
        finally:
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
        Route("/api/chat", dialogue_turn_action, methods=["POST"]),
        Route("/api/save-key", save_token_action, methods=["POST"]),
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
