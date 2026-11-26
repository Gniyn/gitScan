import json
import os

import httpx


def ai_enabled():
    return bool(os.environ.get("AI_API_KEY"))


async def generate_advice(report):
    api_key = os.environ.get("AI_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 AI_API_KEY")
    base = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("AI_MODEL", "gpt-4.1-mini")
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是代码仓库接入审阅助手。只依据提供的脱敏体检结果给出建议，"
                    "不得声称执行过仓库代码，不得编造文件或密钥内容，"
                    "不得编造输入中不存在的文件路径、分数或命中数量，"
                    "不得断言某个文件导致了某条扣分（输入未提供对应关系）。"
                    "输出简体中文，包含优先整改项、接入步骤、需人工确认项三部分，简洁可操作。\n"
                    "输入中的 dimensions[].score 是最终得分，deduction 是已扣分；"
                    "不要用自己的算术推断某个维度扣了多少分，只能引用输入给出的数值。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "repository": report.repository,
                        "commit_sha": report.commit_sha,
                        "score": report.score,
                        "status": report.status,
                        "dimensions": report.dimensions,
                        "checks": {
                            name: {
                                "label": check["label"],
                                "state": check["state"],
                                "evidence_paths": check.get("evidence_paths", [])[:10],
                            }
                            for name, check in report.checks.items()
                        },
                        "findings": [
                            {
                                "rule_id": finding.rule_id,
                                "title": finding.title,
                                "dimension": finding.dimension,
                                "severity": finding.severity,
                                "path": finding.location.path if finding.location else None,
                                "message": finding.message,
                                "recommendation": finding.recommendation,
                                "applied_deduction": finding.applied_deduction,
                            }
                            for finding in report.findings
                            if finding.severity != "info"
                        ][:80],
                        "coverage": {
                            key: value for key, value in report.coverage.items() if key != "files"
                        },
                        "limitations": report.limitations,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            base + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code != 200:
        raise RuntimeError("AI 服务返回 HTTP " + str(response.status_code))
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as error:
        raise RuntimeError("AI 服务返回格式不兼容") from error
    return {"model": model, "content": content, "notice": "AI 建议不参与评分，请结合人工复核。"}
