"""检查内联脚本里「const 声明后又被重新赋值」这类运行时炸弹。

浏览器只在真正执行到那一行时才报 "Assignment to constant variable"，静态语法检查
（node --check）看不出来，所以单独做一次作用域内的赋值扫描：先把所有声明语句整段
抹掉再找剩下的赋值，避免把声明本身误判成二次赋值。
"""

import re
import sys
from pathlib import Path

HTML = Path(__file__).resolve().parent / "static" / "index.html"
DECL_STATEMENT = re.compile(r"\b(?:const|let|var)\s+(?:[A-Za-z_$][\w$]*\s*=\s*[^;]*;?)(?:\s*,\s*[A-Za-z_$][\w$]*\s*=\s*[^;]*;?)*")
ASSIGN = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*(?:\+|-|\*|/|\*\*|%|\|\||&&|\?\?)?=(?!=)")
IGNORE = {"const", "let", "var", "return", "if", "else", "for", "while", "typeof", "await", "new"}


def main():
    text = HTML.read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", text, re.S).group(1)
    lines = script.splitlines()

    declarations = []
    for number, line in enumerate(lines, 1):
        for match in re.finditer(r"\b(const|let|var)\s+([A-Za-z_$][\w$]*)", line):
            declarations.append((number, match.group(2), match.group(1)))

    problems = []
    for number, line in enumerate(lines, 1):
        code = DECL_STATEMENT.sub("", line)
        for match in ASSIGN.finditer(code):
            name = match.group(1)
            if name in IGNORE:
                continue
            seen = [d for d in declarations if d[1] == name and d[0] <= number]
            if not seen:
                continue
            if seen[-1][2] == "const":
                problems.append((number, name, line.strip()[:90]))

    for number, name, code in problems:
        print(f"line {number}: const {name} 被重新赋值 -> {code}")
    print("const 重赋值问题:", len(problems))
    return 1 if problems else 0


sys.exit(main())
