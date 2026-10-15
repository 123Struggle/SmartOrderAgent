"""控制台编码工具。

Windows 默认代码页是 GBK(936)：GBK 无法编码 Emoji，且在 UTF-8 终端下会把中文
显示成乱码或问号。启动入口调用 force_utf8_console() 统一切到 UTF-8。
"""

import ctypes
import os
import sys


def force_utf8_console() -> None:
    """把 Windows 控制台代码页与 Python 标准流都切到 UTF-8。

    非 Windows 或非真实控制台（重定向/IDE）时静默跳过，不影响程序运行。
    """
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
