"""对 static/index.html 做静态检查：资源引用、JS 语法、接口约定一致性。"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\gitScan")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

fails = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ((" | " + detail) if detail and not ok else ""))
    if not ok:
        fails.append(label)


# 1. 静态资源引用是否都存在
for tag in re.findall(r'<(?:link|script)[^>]*>', html):
    for attr in ("href", "src"):
        match = re.search(attr + r'="([^"]+)"', tag)
        if not match:
            continue
        value = match.group(1)
        if value.startswith(("http", "//", "data:")):
            continue
        target = ROOT / "static" / value.lstrip("/")
        check("资源存在: " + value, target.exists())

# 2. 页面里引用的 DOM id 是否都定义
used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', html))
defined = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
missing = sorted(used - defined)
check("DOM id 全部存在", not missing, "缺少: " + ", ".join(missing))

# 3. form 里的 input 是否被 JS 使用（避免点了没反应）
for element_id in re.findall(r'<input[^>]*id="([A-Za-z0-9_-]+)"', html):
    check("input 被引用: " + element_id, element_id in used)

# 4. 抽出 <script> 做语法检查
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
check("只有一个内联脚本块", len(scripts) == 1, "found " + str(len(scripts)))
if scripts:
    probe = ROOT / "_syntax_check.js"
    probe.write_text(scripts[0], encoding="utf-8")
    node = subprocess.run(["node", "--check", str(probe)], capture_output=True, text=True)
    check("JS 语法", node.returncode == 0, (node.stderr or "").strip()[:400])
    probe.unlink(missing_ok=True)

# 5. 前端读的字段是否真的存在于报告里
report_keys = {"score", "dimensions", "checks", "coverage", "findings", "deductions", "limitations",
               "status", "commit_sha", "repository"}
for key in sorted(report_keys):
    check("前端依赖字段存在: report." + key, key in html)

sys.exit(1 if fails else 0)
