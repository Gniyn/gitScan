# AI 协作记录（工作草稿）

> 本文件是开发过程中的原始记录，最终会整理进 `AI_USAGE.md`。
> 每条记录都要求：场景 / AI 给了什么 / 错在哪 / 怎么发现的（可复现的验证手段）/ 最后改成什么。

---

## 记录 1 · 截断读取被擅自改成跳过

- **场景**：设计阶段明确要求「单文件超过 256 KiB 时，读取前 256 KiB 继续扫描，超出的部分丢弃」，防止大体积 `.env` / `package-lock.json` 漏报。
- **AI 给了什么**：实现时把语义改成了「跳过不扫描」，只在 `FileRecord` 上留了一个 `truncated` 标志，并把该标志算进了 `partial` 判定。
- **错在哪**：
  1. 跳过等于漏报，而漏报比截断更严重 —— 恰好可能漏掉真正含密钥的大文件。
  2. `partial` 的触发条件被扩大：一个超大 `package-lock.json` 就让整份报告的评分可信度降级，与「只有文件数超 2000 或下载失败才降级」的口径冲突。
- **怎么发现的**：逐行比对设计文档与 `app/scanner.py`，定位到 `app/scanner.py:279`：

  ```python
  partial = bool(excluded or truncated or unsupported)
  ```

  按设计口径，`truncated` 不应参与 `status` 判定，只应写入 `limitations`。
- **改成什么**：
  - 恢复截断读取语义：读到 `max_file_bytes` 为止，超出部分丢弃，`truncated=true`，`limitations` 记录「N 个文件仅扫描前 262144 字节」。
  - `partial = bool(excluded)`，仅文件数超预算时降级。

---

## 记录 2 · 路径规则只覆盖 Windows 盘符和 macOS 用户目录

- **场景**：需求明确列出「硬编码路径（`C:\`、`/Users/`、绝对路径等）」。
- **AI 给了什么**：`rules.yaml` 中 P001 的正则只写了 `[CD]:` 和 `/Users/`：

  ```yaml
  pattern: '(?i)(?<![A-Za-z0-9])(?:[CD]:[\\/](?:[^\s"''`<>|;,]+)?|/Users/[^\s"''`<>|;,]+)'
  ```

- **错在哪**：`E:\`、`F:\` 等其它盘符、`/home/alice/`（Linux）、`\\server\share`（UNC）全部漏报，检测项 C 覆盖率不足。
- **怎么发现的**：写探针脚本直接跑正则，逐条打印命中结果：

  ```python
  from app.config import Rulebook
  rb = Rulebook()
  r = rb.patterns['P001']
  for s in ['C:/Users/alice/x', 'E:/data/log.txt', '/home/alice/app.py', '\\\\nas\\share\\f.txt']:
      print(bool(r.search(s)), s)
  ```

  前一个命中，后三个全部 `False`。
- **改成什么**：正则扩展为覆盖所有盘符 `[A-Z]:[\\/]`、`/home/<user>/`、UNC `\\\\<host>\\<share>`，并把上述四个样本写进 `tests/test_scanner.py` 作为回归用例。

---

## 记录 3 · 评分逻辑：一条密钥泄露和十条同分

- **场景**：密钥维度满分 30 分，需要给出合理的封顶语义。
- **AI 给了什么**：初版每命中一条 `sk-` 就扣 15 分，不做维度封顶。
- **错在哪**：扫出 2 条 `sk-` 时该维度直接归零，扫出 10 条同样是 0 分 —— 真实的「泄露 1 条」与「泄露 10 条」在评分上完全无差别，且总分可能出现大额为负。
- **怎么发现的**：让 AI 给出算例，它自己算「3 条 sk- = 扣 45 分」，与维度满分 30 对上不了；再对照 `score = 100 - Σ扣分`，发现没有任何封底保护。
- **改成什么**：
  - 密钥类规则统一 `count_mode: once_per_rule`，同一规则只计一次（所有命中仍展示，其余标 `该规则已计分`）。
  - 规则级封顶 `max_deduction`，维度级封顶用维度满分。
  - `score = max(0, 100 - Σ applied_deduction)`，维度得分不得为负。
  - 在 `app/config.py:45` 加了启动校验：`secrets` 维度的规则若没写 `count_mode: once_per_rule` 直接拒绝启动。

---

## 记录 4 · 文件获取方案在匿名额度下不可行

- **场景**：需要拉取仓库内容做扫描。
- **AI 给了什么**：按文件逐个调 `GET /repos/{o}/{r}/contents/{path}`（或 blob）的粒度拉取。
- **错在哪**：GitHub 匿名 API 额度约 60 次/小时。按每文件一次请求算，匿名模式下最多扫 50 来个文件就耗尽额度，而预算是 2000 个文件 —— 演示时会当场 403。
- **怎么发现的**：让 AI 自己核算请求数，它给出的「Blob 方案最坏接近每文件一次请求」就已经说明了问题；结合 GitHub 文档中匿名 60 次/小时的限额即可推翻。
- **改成什么**：改为 `GET /repos/{o}/{r}/tarball/{sha}` 一次请求取整包，内存中流式解包（`tarfile` stream 模式），不落盘、不 `extract`、不执行内容。锚定 commit sha 保证快照不可变；重定向仅允许 `api.github.com` / `codeload.github.com`，跨域下载不转发 token。另加下载量、解压量、超时、成员数四道保护，触发即 `failed` 不出分。

---

## 待补（后续开发中遇到再做记录）

- 前端 evidence 渲染是否需要 `textContent` 而非 `innerHTML`（XSS）
- AI 建议模块的输入白名单是否被遵守（不得发送路径与原文）
- 大小写不敏感匹配、`.env` 分类等边界条件

## 记录 5：AI 在接入建议里编造了扣分归因

- 我给的表数据：`paths` 维度 17/20，仅 `rules.yaml` 命中 P001。
- AI 输出：声称"`paths` 维度 0 分，涉及 `AI_USAGE.md`、`docs/ai-log.md`、`docs/samples/*.json`"，并断言 `windows_entry`/`windows_docs` 为 absent。
- 怎么发现的：把 AI 原文和真实报告 JSON 逐字对齐，文件路径全部对不上，分数也对不上。
- 修法：payload 增加 `evidence_paths`（每维度最多 10 条），并在 system prompt 里加三条硬约束——不得编造输入中不存在的路径/分数/命中数、不得断言某文件导致某条扣分、不得自行推算维度扣分。修完后 AI 的输出逐条带文件与处数，与报告一致。

## 记录 6：`const` 重赋值——三轮才定位到的那一行

- 现象：页面顶部常驻红条「扫描状态获取失败 / Assignment to constant variable.」。
- 第一轮：以为是 `currentReport = job.report` 的赋值顺序问题，调了顺序，无效。
- 第二轮：控制台只拿到 `poll failed TypeError ... (353)`，353 是 catch 行，`e.message` 本身没信息量。
- 第三轮：把 `e.stack` 打出来做最小复现页，才定位到 `renderFindings` 里的 `const meta` 后面又 `meta += ...`。
- 教训：要 `e.stack` 不要 `e.message`；静态检查 `node --check` 查不出这类运行时错误，为此补了 `check_js_scope.py` 专门扫 `const` 声明后被赋值的模式。

## 记录 7：AI 写的异步接口把协程对象直接返回了

- 现象：点"生成 AI 建议"后接口 500，报 `TypeError: 'coroutine' object is not iterable`。
- 原因：把 `async def generate_advice()` 直接丢给 `ThreadPoolExecutor.submit()`，线程里拿到的是未 await 的协程对象。
- 修法：加一层 `def run_advice(report): return asyncio.run(generate_advice(report))` 再提交。

## 记录 8：同一类问题刷屏，把 14 处真实命中变成 14 条重复告警

- 现象：扫 `cmderdev/cmder` 得 45 分，`paths` 维度 0/20；清单前 14 条全是 P001，其中 `CHANGELOG.md` 一个文件占 6 条。真扫了 51 个文件，不可能有 14 个独立问题。
- AI 的初版修法：给密钥类规则加封顶。方向不对——这里是"同一文件同一规则刷屏"，不是"惩罚过重"。
- 我的修法：按 `(rule_id, path)` 合并上报，同文件命中计一处扣分、其余折叠并回指代表条目，消息里写明"本文件命中 N 处，行 a、b、c"。改完 59 分、`paths` 5/20、清单 14→5 条。
- 顺带发现：`.git-blame-ignore-revs` 里的历史提交哈希被盘符正则误判成绝对路径，加进 P001 的排除清单。
