"""pytest 全局配置。

Windows 控制台默认是 GBK(936)：断言失败时 pytest 会打印模型的真实回复，
其中可能含 Emoji —— GBK 无法编码会在报告阶段再抛 UnicodeEncodeError，
把真正的失败原因盖掉。这里统一把标准流切到 UTF-8。
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.console import force_utf8_console

force_utf8_console()
