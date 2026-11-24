import asyncio
import logging


class ConsoleLogCollector(logging.Handler):
    def __init__(self, message_channel: asyncio.Queue, target_loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.message_channel = message_channel
        self.target_loop = target_loop

    def emit(self, log_item: logging.LogRecord):
        try:
            formatted_output = self.format(log_item)
            target_facility = log_item.name.lower()
            if "browser_use" in target_facility:
                if "step " in formatted_output.lower():
                    sanitized_output = formatted_output
                else:
                    sanitized_output = f"[{log_item.levelname}] {log_item.getMessage()}"
                self.target_loop.call_soon_threadsafe(self.message_channel.put_nowait, sanitized_output)
        except Exception:
            pass
