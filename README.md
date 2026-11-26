# Git 仓库接入体检

输入公开 GitHub 仓库链接，固定默认分支当前 commit，只读扫描 README、依赖清单、启动入口、Windows 脚本、硬编码路径和疑似密钥，输出 100 分制评分、可追溯风险清单和覆盖率。

## 运行

需要 Python 3.10+。

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
$env:GITHUB_TOKEN = "只读 token（可选）"
.\.venv\Scripts\uvicorn.exe app.main:app --reload
```

打开 `http://127.0.0.1:8000`。未配置 `GITHUB_TOKEN` 时使用匿名 GitHub 配额。

## 设计约束

- 使用 `GET /repos/{owner}/{repo}/tarball/{sha}` 流式下载，在内存中顺序解包，不落盘、不执行仓库内容。
- 归档下载、解压量和扫描时长有独立保护上限，超限直接失败，不输出残缺评分。
- 固定相对路径排序，最多处理 2000 个普通文件；超限标记 `partial`。
- 单文件只扫描前 262144 字节，超出部分截取而非跳过，标记 `truncated`；报告标记 `partial`。
- 二进制、忽略目录和不支持的编码单独计入覆盖率。
- 同一 commit、同一引擎与规则版本得到相同分数；时间、AI 输出和请求完成顺序不参与评分。
- 所有检测规则、正则、权重、例外和上限在 `rules.yaml`；Python 只实现通用执行器。
- `GITHUB_TOKEN` 只从服务端环境变量读取，不进入页面、不返回前端。归档重定向到 codeload 时不转发 token。

## 评分

| 维度 | 满分 |
|---|---:|
| 接入完整性 | 35 |
| Windows 依赖 | 15 |
| 硬编码路径 | 20 |
| 密钥与敏感文件 | 30 |

总分是 `max(0, 100 - 实际扣分之和)`。各维度扣分不超过满分，得分不低于 0。密钥规则按 `once_per_rule` 只计一次，密钥维度最多扣 30 分。每条 finding 的 `proposed_deduction`、`applied_deduction` 和 `deduction_reason` 可在报告中查看。

## API

- `POST /api/scans`，请求体 `{"url":"https://github.com/owner/repo"}`，返回任务 ID。
- `GET /api/scans/{id}`，返回排队、进度或最终报告。
- `POST /api/scans/{id}/advice`，可选 AI 建议。
- `GET /api/health`，返回 token 和 AI 是否配置。

任务状态保存在内存中，有 TTL 和容量上限，重启后清空。

## 可选 AI 建议

设置 `AI_API_KEY`、`AI_BASE_URL` 和 `AI_MODEL` 后，页面可请求接入建议。发送内容只有脱敏 finding、检查状态和覆盖信息；AI 输出不参与评分。接口使用 OpenAI 兼容的 `/chat/completions` 格式。

## 测试

```powershell
.\.venv\Scripts\pytest.exe -q
```

测试覆盖确定性、密钥去重与封顶、256 KiB 截取、2000 文件预算、二进制跳过、Windows 证据和 URL 校验。

## 已知边界

- 只检查当前 commit，不检查 Git 历史。
- 不验证疑似密钥是否有效，不判断仓库是否恶意。
- 缺失项只代表在已扫描范围内未发现，覆盖率不足时显示“无法确认”或 `partial`。
- 纯库项目可能因没有独立启动入口被扣分，报告会保留该评分口径。
