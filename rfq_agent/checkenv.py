"""``python -m rfq_agent.checkenv`` — is this machine ready to run the demo?

The launcher calls it before starting the server. It answers the same question the
app itself answers: a vendored ``.deps/`` cache counts, so does a normal install.
Exit code 0 = ready, 1 = something must be installed.
"""

import sys
from pathlib import Path

from .deps import ensure_deps

REQUIRED = (("docx", "导出 Word 草案"), ("openpyxl", "读取 Excel 附件"))


def main() -> int:
    problems = []
    if sys.version_info < (3, 11):
        problems.append(f"Python {sys.version.split()[0]} 过旧，需要 3.11 或更高")

    cache = ensure_deps()
    for module, purpose in REQUIRED:
        try:
            __import__(module)
        except ImportError:
            problems.append(f"缺少 {module}（{purpose}）")

    if problems:
        print("环境未就绪：")
        for item in problems:
            print(f"  - {item}")
        print("\n安装方式：pip install -e \".[documents]\"")
        return 1

    source = f"内置缓存 {cache.name}/" if cache else "系统已安装"
    print(f"环境就绪：Python {sys.version.split()[0]}，依赖来自{source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())