"""容器内 CLI 入口。

Nuitka 编译后的模块没有 runpy 需要的 code object，``python -m src.start.commands``
不可用；从这里显式调用 click 入口。
"""

from src.start.commands import main

if __name__ == "__main__":
    main()
