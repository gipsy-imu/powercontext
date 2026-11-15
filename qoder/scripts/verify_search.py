#!/usr/bin/env python3
"""从项目根目录运行：验证 seekdb 中的导入与检索结果是否符合预期。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cli import main

if __name__ == "__main__":
    main(["verify"])
