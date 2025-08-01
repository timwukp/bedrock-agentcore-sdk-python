"""Bedrock AgentCore base implementation.

Provides a Starlette-based web server that wraps user functions as HTTP endpoints.
"""

import asyncio
import contextvars
import inspect
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .context import BedrockAgentCoreContext, RequestContext
from .models import (
    ACCESS_TOKEN_HEADER,
    SESSION_HEADER,
    TASK_ACTION_CLEAR_FORCED_STATUS,
    TASK_ACTION_FORCE_BUSY,
    TASK_ACTION_FORCE_HEALTHY,
    TASK_ACTION_JOB_STATUS,
    TASK_ACTION_PING_STATUS,
    PingStatus,
)

# Request context for logging
request_id_context: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)


class RequestContextFormatter(logging.Formatter):
    """Custom formatter that includes request ID in log messages."""

    def format(self, record):
        """Format log record with request ID context."""
        request_id = request_id_context.get()
        if request_id:
            record.request_id = f"[{request_id}] "
        else:
            record.request_id = ""
        return super().format(record)


class BedrockAgentCoreApp(Starlette):
    """Bedrock AgentCore application class that extends Starlette for AI agent deployment."""

    def __init__(self, debug: bool = False):
        """Initialize Bedrock AgentCore application.

        Args:
            debug: Enable debug actions for task management (default: False)
        """
        self.handlers: Dict[str, Callable] = {}
        self._ping_handler: Optional[Callable] = None
        self._active_tasks: Dict[int, Dict[str, Any]] = {}
        self._task_counter_lock: threading.Lock = threading.Lock()
        self._forced_ping_status: Optional[PingStatus] = None
        self._last_status_update_time: float = time.time()
        self._invocation_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="invocation")
        self._invocation_semaphore = asyncio.Semaphore(2)

        routes = [
            Route("/invocations", self._handle_invocation, methods=["POST"]),
            Route("/ping", self._handle_ping, methods=["GET"]),
        ]
        super().__init__(routes=routes)
        self.debug = debug  # Set after super().__init__ to avoid override

        self.logger = logging.getLogger("bedrock_agentcore.app")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = RequestContextFormatter("%(asctime)s - %(name)s - %(levelname)s - %(request_id)s%(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    def entrypoint(self, func: Callable) -> Callable:
        """Decorator to register a function as the main entrypoint.

        Args:
            func: The function to register as entrypoint

        Returns:
            The decorated function with added serve method
        """
        self.handlers["main"] = func
        func.run = lambda port=8080, host=None: self.run(port, host)
        return func

    def ping(self, func: Callable) -> Callable:
        """Decorator to register a custom ping status handler.

        Args:
            func: The function to register as ping status handler

        Returns:
            The decorated function
        """
        self._ping_handler = func
        return func

    def async_task(self, func: Callable) -> Callable:
        """Decorator to track async tasks for ping status.

        When a function is decorated with @async_task, it will:
        - Set ping status to HEALTHY_BUSY while running
        - Revert to HEALTHY when complete
        """
        if not asyncio.iscoroutinefunction(func):
            raise ValueError("@async_task can only be applied to async functions")

        async def wrapper(*args, **kwargs):
            task_id = self.add_async_task(func.__name__)

            try:
                self.logger.debug("Starting async task: %s", func.__name__)
                start_time = time.time()
                result = await func(*args, **kwargs)
                duration = time.time() - start_time
                self.logger.info("Async task completed: %s (%.3fs)", func.__name__, duration)
                return result
            except Exception as e:
                duration = time.time() - start_time
                self.logger.error(
                    "Async task failed: %s (%.3fs) - %s: %s", func.__name__, duration, type(e).__name__, e
                )
                raise
            finally:
                self.complete_async_task(task_id)

        wrapper.__name__ = func.__name__
        return wrapper

    def get_current_ping_status(self) -> PingStatus:
        """Get current ping status (forced > custom > automatic)."""
        current_status = None

        if self._forced_ping_status is not None:
            current_status = self._forced_ping_status
        elif self._ping_handler:
            try:
                result = self._ping_handler()
                if isinstance(result, str):
                    current_status = PingStatus(result)
                else:
                    current_status = result
            except Exception as e:
                self.logger.warning(
                    "Custom ping handler failed, falling back to automatic: %s: %s", type(e).__name__, e
                )

        if current_status is None:
            current_status = PingStatus.HEALTHY_BUSY if self._active_tasks else PingStatus.HEALTHY
        if not hasattr(self, "_last_known_status") or self._last_known_status != current_status:
            self._last_known_status = current_status
            self._last_status_update_time = time.time()

        return current_status

    def force_ping_status(self, status: PingStatus):
        """Force ping status to a specific value."""
        self._forced_ping_status = status

    def clear_forced_ping_status(self):
        """Clear forced status and resume automatic."""
        self._forced_ping_status = None

    def get_async_task_info(self) -> Dict[str, Any]:
        """Get info about running async tasks."""
        running_jobs = []
        for t in self._active_tasks.values():
            try:
                running_jobs.append(
                    {"name": t.get("name", "unknown"), "duration": time.time() - t.get("start_time", time.time())}
                )
            except Exception as e:
                self.logger.warning("Caught exception, continuing...: %s", e)
                continue

        return {"active_count": len(self._active_tasks), "running_jobs": running_jobs}

    def add_async_task(self, name: str, metadata: Optional[Dict] = None) -> int:
        """Register an async task for interactive health tracking.

        This method provides granular control over async task lifecycle,
        allowing developers to interactively start tracking tasks for health monitoring.
        Use this when you need precise control over when tasks begin and end.

        Args:
            name: Human-readable task name for monitoring
            metadata: Optional additional task metadata

        Returns:
            Task ID for tracking and completion

        Example:
            task_id = app.add_async_task("file_processing", {"file": "data.csv"})
            # ... do background work ...
            app.complete_async_task(task_id)
        """
        with self._task_counter_lock:
            task_id = hash(str(uuid.uuid4()))  # Generate truly unique hash-based ID

            # Register task start with same structure as @async_task decorator
            task_info = {"name": name, "start_time": time.time()}
            if metadata:
                task_info["metadata"] = metadata

            self._active_tasks[task_id] = task_info

        self.logger.info("Async task started: %s (ID: %s)", name, task_id)
        return task_id

    def complete_async_task(self, task_id: int) -> bool:
        """Mark an async task as complete for interactive health tracking.

        This method provides granular control over async task lifecycle,
        allowing developers to interactively complete tasks for health monitoring.
        Call this when your background work finishes.

        Args:
            task_id: Task ID returned from add_async_task

        Returns:
            True if task was found and completed, False otherwise

        Example:
            task_id = app.add_async_task("file_processing")
            # ... do background work ...
            completed = app.complete_async_task(task_id)
        """
        with self._task_counter_lock:
            task_info = self._active_tasks.pop(task_id, None)
            if task_info:
                task_name = task_info.get("name", "unknown")
                duration = time.time() - task_info.get("start_time", time.time())

                self.logger.info("Async task completed: %s (ID: %s, Duration: %.2fs)", task_name, task_id, duration)
                return True
            else:
                self.logger.warning("Attempted to complete unknown task ID: %s", task_id)
                return False

    def _build_request_context(self, request) -> RequestContext:
        """Build request context and setup auth if present."""
        try:
            agent_identity_token = request.headers.get(ACCESS_TOKEN_HEADER) or request.headers.get(
                ACCESS_TOKEN_HEADER.lower()
            )
            if agent_identity_token:
                BedrockAgentCoreContext.set_workload_access_token(agent_identity_token)
            session_id = request.headers.get(SESSION_HEADER) or request.headers.get(SESSION_HEADER.lower())
            return RequestContext(session_id=session_id)
        except Exception as e:
            self.logger.warning("Failed to build request context: %s: %s", type(e).__name__, e)
            return RequestContext(session_id=None)

    def _takes_context(self, handler: Callable) -> bool:
        try:
            params = list(inspect.signature(handler).parameters.keys())
            return len(params) >= 2 and params[1] == "context"
        except Exception:
            return False

    async def _handle_invocation(self, request):
        request_id = str(uuid.uuid4())[:8]
        request_id_context.set(request_id)
        start_time = time.time()

        try:
            payload = await request.json()
            self.logger.debug("Processing invocation request")

            if self.debug:
                task_response = self._handle_task_action(payload)
                if task_response:
                    duration = time.time() - start_time
                    self.logger.info("Debug action completed (%.3fs)", duration)
                    return task_response

            handler = self.handlers.get("main")
            if not handler:
                self.logger.error("No entrypoint defined")
                return JSONResponse({"error": "No entrypoint defined"}, status_code=500)

            request_context = self._build_request_context(request)
            takes_context = self._takes_context(handler)

            handler_name = handler.__name__ if hasattr(handler, "__name__") else "unknown"
            self.logger.debug("Invoking handler: %s", handler_name)
            result = await self._invoke_handler(handler, request_context, takes_context, payload)

            duration = time.time() - start_time
            if inspect.isgenerator(result):
                self.logger.info("Returning streaming response (generator) (%.3fs)", duration)
                return StreamingResponse(self._sync_stream_with_error_handling(result), media_type="text/event-stream")
            elif inspect.isasyncgen(result):
                self.logger.info("Returning streaming response (async generator) (%.3fs)", duration)
                return StreamingResponse(self._stream_with_error_handling(result), media_type="text/event-stream")

            self.logger.info("Invocation completed successfully (%.3fs)", duration)
            return JSONResponse(result)

        except json.JSONDecodeError as e:
            duration = time.time() - start_time
            self.logger.warning("Invalid JSON in request (%.3fs): %s", duration, e)
            return JSONResponse({"error": "Invalid JSON", "details": str(e)}, status_code=400)
        except Exception as e:
            duration = time.time() - start_time
            self.logger.exception("Invocation failed (%.3fs)", duration)
            return JSONResponse({"error": str(e)}, status_code=500)

    def _handle_ping(self, request):
        try:
            status = self.get_current_ping_status()
            self.logger.debug("Ping request - status: %s", status.value)
            return JSONResponse({"status": status.value, "time_of_last_update": int(self._last_status_update_time)})
        except Exception as e:
            self.logger.error("Ping endpoint failed: %s: %s", type(e).__name__, e)
            return JSONResponse({"status": PingStatus.HEALTHY.value, "time_of_last_update": int(time.time())})

    def run(self, port: int = 8080, host: Optional[str] = None):
        """Start the Bedrock AgentCore server.

        Args:
            port: Port to serve on, defaults to 8080
            host: Host to bind to, auto-detected if None
        """
        import os

        import uvicorn

        if host is None:
            if os.path.exists("/.dockerenv") or os.environ.get("DOCKER_CONTAINER"):
                host = "0.0.0.0"  # nosec B104 - Docker needs this to expose the port
            else:
                host = "127.0.0.1"
        uvicorn.run(self, host=host, port=port)

    async def _invoke_handler(self, handler, request_context, takes_context, payload):
        if self._invocation_semaphore.locked():
            return JSONResponse({"error": "Server busy - maximum concurrent requests reached"}, status_code=503)

        async with self._invocation_semaphore:
            try:
                args = (payload, request_context) if takes_context else (payload,)
                if asyncio.iscoroutinefunction(handler):
                    return await handler(*args)
                else:
                    loop = asyncio.get_event_loop()
                    return await loop.run_in_executor(self._invocation_executor, handler, *args)
            except Exception as e:
                handler_name = getattr(handler, "__name__", "unknown")
                self.logger.error("Handler '%s' execution failed: %s: %s", handler_name, type(e).__name__, e)
                raise

    def _handle_task_action(self, payload: dict) -> Optional[JSONResponse]:
        """Handle task management actions if present in payload."""
        action = payload.get("_agent_core_app_action")
        if not action:
            return None

        self.logger.debug("Processing debug action: %s", action)

        try:
            actions = {
                TASK_ACTION_PING_STATUS: lambda: JSONResponse(
                    {
                        "status": self.get_current_ping_status().value,
                        "time_of_last_update": int(self._last_status_update_time),
                    }
                ),
                TASK_ACTION_JOB_STATUS: lambda: JSONResponse(self.get_async_task_info()),
                TASK_ACTION_FORCE_HEALTHY: lambda: (
                    self.force_ping_status(PingStatus.HEALTHY),
                    self.logger.info("Ping status forced to Healthy"),
                    JSONResponse({"forced_status": "Healthy"}),
                )[2],
                TASK_ACTION_FORCE_BUSY: lambda: (
                    self.force_ping_status(PingStatus.HEALTHY_BUSY),
                    self.logger.info("Ping status forced to HealthyBusy"),
                    JSONResponse({"forced_status": "HealthyBusy"}),
                )[2],
                TASK_ACTION_CLEAR_FORCED_STATUS: lambda: (
                    self.clear_forced_ping_status(),
                    self.logger.info("Forced ping status cleared"),
                    JSONResponse({"forced_status": "Cleared"}),
                )[2],
            }

            if action in actions:
                response = actions[action]()
                self.logger.debug("Debug action '%s' completed successfully", action)
                return response

            self.logger.warning("Unknown debug action requested: %s", action)
            return JSONResponse({"error": f"Unknown action: {action}"}, status_code=400)

        except Exception as e:
            self.logger.error("Debug action '%s' failed: %s: %s", action, type(e).__name__, e)
            return JSONResponse({"error": "Debug action failed", "details": str(e)}, status_code=500)

    def _convert_to_sse(self, chunk):
        try:
            return f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
        except (TypeError, ValueError):
            try:
                return f"data: {json.dumps(str(chunk))}\n\n".encode("utf-8")
            except (TypeError, ValueError) as e:
                self.logger.warning("Failed to serialize SSE chunk: %s: %s", type(e).__name__, e)
                error_data = {"error": "Serialization failed", "original_type": type(chunk).__name__}
                sse_string = f"data: {json.dumps(error_data)}\n\n"
                return sse_string.encode("utf-8")

    async def _stream_with_error_handling(self, generator):
        """Wrap async generator to handle errors and convert to SSE format."""
        try:
            async for value in generator:
                yield self._convert_to_sse(value)
        except Exception as e:
            self.logger.error("Error in async streaming: %s: %s", type(e).__name__, e)
            error_event = {
                "error": str(e),
                "error_type": type(e).__name__,
                "message": "An error occurred during streaming",
            }
            yield self._convert_to_sse(error_event)

    def _sync_stream_with_error_handling(self, generator):
        """Wrap sync generator to handle errors and convert to SSE format."""
        try:
            for value in generator:
                yield self._convert_to_sse(value)
        except Exception as e:
            self.logger.error("Error in sync streaming: %s: %s", type(e).__name__, e)
            error_event = {
                "error": str(e),
                "error_type": type(e).__name__,
                "message": "An error occurred during streaming",
            }
            yield self._convert_to_sse(error_event)
