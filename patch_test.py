import re
from pathlib import Path

p = Path(r"D:\gitScan\tests\test_scanner.py")
src = p.read_text(encoding="utf-8")
lines = src.splitlines(keepends=True)

start = next(i for i, l in enumerate(lines) if l.startswith("def test_hardcoded_path_variants_are_detected"))
end = next(i for i, l in enumerate(lines) if l.startswith("def test_test_fixtures_are_excluded_from_content_scan"))

BS = chr(92)
block = (
    "def test_hardcoded_path_variants_are_detected():\n"
    "    # P001 按文件合并上报：单文件多条命中合并为一条，命中条数与行号写进消息\n"
    "    report = scan(\n"
    "        {\n"
    '            "README.md": "Getting Started' + BS + 'nnpm start' + BS + 'n",\n'
    '            "requirements.txt": "fastapi' + BS + 'n",\n'
    '            "main.py": "print(\'ok\')' + BS + 'n",\n'
    '            "config.txt": "E:" + BS + BS + "data' + BS + "n/home/alice/x" + BS + "n" + BS + BS + BS + BS + 'nas' + BS + BS + 'share' + BS + BS + 'f.txt' + BS + 'n",\n'
    "        }\n"
    "    )\n"
    '    path_findings = [item for item in report.findings if item.rule_id == "P001"]\n'
    "    assert len(path_findings) == 1\n"
    "    merged = path_findings[0]\n"
    "    assert merged.hit_count == 3\n"
    '    assert "命中 3 处" in merged.message\n'
    "    assert merged.evidence\n"
    "\n\n"
    "def test_path_findings_are_collapsed_per_file():\n"
    "    report = scan(\n"
    "        {\n"
    '            "README.md": "Getting Started' + BS + 'nnpm start' + BS + 'n",\n'
    '            "requirements.txt": "fastapi' + BS + 'n",\n'
    '            "main.py": "print(\'ok\')' + BS + 'n",\n'
    '            "a.txt": "C:" + BS + BS + "one" + BS + "nC:" + BS + BS + "two" + BS + "nC:" + BS + BS + 'three' + BS + 'n",\n'
    '            "b.txt": "D:" + BS + BS + 'four' + BS + 'n",\n'
    "        }\n"
    "    )\n"
    '    path_findings = [item for item in report.findings if item.rule_id == "P001"]\n'
    '    assert sorted(item.location.path for item in path_findings) == ["a.txt", "b.txt"]\n'
    "    assert sum(item.applied_deduction > 0 for item in path_findings) == 2\n"
    "\n\n"
)
lines[start:end] = [block]
p.write_text("".join(lines), encoding="utf-8")
print("".join(lines[start:start + 40]))
