import os as _os_system
import sys as _sys_runtime

_base_directory = _os_system.path.dirname(_os_system.path.abspath(__file__))
if _base_directory not in _sys_runtime.path:
    _sys_runtime.path.insert(0, _base_directory)

from backend.gateway import start_web_server

if __name__ == "__main__":
    start_web_server()
