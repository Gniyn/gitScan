# AI 使用说明

本项目在 AI Coding 工具辅助下完成。核心结论：**AI 能写出跑得通的代码，但它会悄悄改写设计口径，且改得看起来合理。** 因此所有 AI 产出都过了三道关——测试、真实仓库扫描、逐行对照设计文档。

## 使用的工具

| 环节 | 工具 | 用法 |
|---|---|---|
| 需求拆解与架构 | 对话式大模型 | 拆四个检测项与评分维度，确定"规则外置、执行器通用"的分层 |
| 代码生成 | AI Coding（编辑器内） | 按模块生成 `rules.yaml` 规则、扫描器、评分器、前端页面 |
| 规则正则验证 | 本地 Python 探针脚本 | 直接 import `Rulebook` 跑正则，打印命中/未命中 |
| 端到端验证 | 本地服务 + 真实仓库 | 扫描公开仓库，比对分数与人工判断 |
| 代码审查 | 对话式大模型（第二意见） | 我提供 diff，让它指出边界条件遗漏 |
| 前端体检 | 自写静态检查 + 无头浏览器 | `check_frontend.py` 校验 DOM/资源/字段引用，`check_js_scope.py` 扫描 `const` 重赋值，`probe_frontend.py` 抓控制台报错 |
| 接入建议 | DeepSeek（`deepseek-chat`，OpenAI 兼容接口） | 只发送脱敏后的评分、规则编号与命中数量，不发文件内容 |

## 三道验证手段

1. **自动化测试**：`pytest -q`，20 条，覆盖确定性、密钥封顶、截断、文件预算、路径变体、BOM、测试样本排除。
2. **真实仓库扫描**：不只听 AI 说"改好了"，而是起服务、扫真实仓库、看分数和 `limitations` 原文。样例报告在 [`docs/samples/`](docs/samples/)，`gitscan-self.json` 是本项目自扫结果（77 分），`hello-world.json` 是 `octocat/Hello-World`（75 分）。
3. **探针脚本**：对正则、glob、评分这类"AI 说改了但可能只改了表面"的逻辑，写一次性脚本直接喂样本，看输出是否符合设计口径。验证完即删，不留在仓库里。

## AI 答错但被我发现的记录

完整过程见 [`docs/ai-log.md`](docs/ai-log.md)。下面按发现顺序摘录 9 条，每条都记了**错在哪、怎么发现的、最后改成什么**，不是笼统的"AI 出过错"。前 4 条是代码层错误，第 5~7 条是"看起来改好了但没改对"，第 8 条是设计层错误（规则对了，聚合方式错了），第 9 条是拿工具扫自己扫出来的误报与真阳性。

### 摘录 1：截断读取被擅自改成跳过（最典型）

- **我给的设计**：单文件超过 256 KiB 时，读前 256 KiB 继续扫描，超出部分丢弃。
- **AI 的实现**：改成了"跳过不扫描"，并把 `truncated` 算进 `partial`，代码在 `app/scanner.py:279`：

  ```python
  partial = bool(excluded or truncated or unsupported)
  ```

- **为什么这是错的**：跳过 = 漏报，而漏报比截断严重得多，恰好可能漏掉真正含密钥的大文件；同时一个超大 `package-lock.json` 就让整份报告降级为 `partial`，与"只有文件数超 2000 才降级"的口径冲突。
- **怎么发现的**：逐行比对设计文档与 `app/scanner.py`，注意到 `partial` 的判定条件多了一项。
- **修复**：恢复截断读取语义，`partial = bool(excluded)`，截断只写进 `limitations`。回归测试为 `tests/test_scanner.py:73` 的 `test_large_file_is_truncated_not_skipped`，其中断言 `report.status == "complete"`、`scanned_bytes == max_file_bytes`。

### 摘录 2：密钥评分把"泄露 1 条"和"泄露 10 条"算成同分

- **AI 的实现**：每命中一条 `sk-` 扣 15 分，不做维度封顶。
- **为什么这是错的**：扫出 2 条时密钥维度直接归零，扫出 10 条也是 0 分，真实差异被抹平；总分还可能出现大额负数。
- **怎么发现的**：让 AI 自己给算例，它算出"3 条 sk- 扣 45 分"，而维度满分只有 30——它自己的算例就说明了没有封顶。
- **修复**：密钥类规则统一 `once_per_rule`（同规则只计一次），加规则级与维度级双重封顶，总分 `max(0, 100 - Σ)`。启动校验强制要求：密钥维度规则若没写 `count_mode: once_per_rule`，直接拒绝启动（`app/config.py:45`）。

### 摘录 3：文件获取方案在匿名配额下不可行

- **AI 的实现**：按文件逐个调 `GET /repos/{o}/{r}/contents/{path}`。
- **为什么这是错的**：匿名 API 额度约 60 次/小时，按每文件一次请求算，最多扫 50 来个文件就耗尽，而预算是 2000 个文件——演示时会当场 403。
- **怎么发现的**：让 AI 核算请求数，它自己写的"最坏接近每文件一次请求"已经说明问题。
- **修复**：改为 `GET /repos/{o}/{r}/tarball/{sha}` 一次取整包，内存流式解包不落盘。

### 摘录 4：路径规则只覆盖了一半盘符

- **AI 的实现**：`[CD]:` 和 `/Users/`，即 `E:\`、`F:\`、`/home/`、UNC 全部漏报。
- **怎么发现的**：写探针脚本直接跑正则，四个样本里三个打不中：

  ```python
  for s in ["C:/Users/alice/x", "E:/data/log.txt", "/home/alice/app.py", r"\\nas\share\f.txt"]:
      print(bool(pattern.search(s)), s)   # True False False False
  ```

- **修复**：扩展到 `[A-Z]:[\\/]`、`/home|Users/<user>/`、UNC `\\\\host\\share`，四个样本全部写进回归测试。

### 摘录 5：AI 在建议里编造了扣分归因

- **AI 的输出**：接入 AI 建议后，第一次生成的内容里写着"硬编码路径维度 0 分，涉及 `AI_USAGE.md`、`docs/ai-log.md`、`docs/samples/*.json`"。而当时真实数据是路径维度 17 分（只扣 3 分），真正命中的只有 `rules.yaml` 一处；`AI_USAGE.md` 根本没被任何规则命中。
- **怎么发现的**：对照同一份报告的 `dimensions.paths.score` 和 `findings[].location.path` 逐条核对。AI 说的文件在 `findings` 里查不到，分数也与输入不符——它把"我认为这种仓库应该有路径问题"当成了输入里的事实。
- **修复**：不是换模型，而是收窄输入和收紧约束。
  1. 输入里去掉了 `evidence` 原文，只保留 `evidence_paths`（截断 10 条），让 AI 没有编造的空间；同时说明"某文件导致某条扣分"这个对应关系输入里本来就不存在。
  2. system prompt 增加三条硬约束：不得编造输入中不存在的路径/分数/命中数量；不得断言某个文件导致了某条扣分；不得用自己的算术推断某维度扣了多少分，只能引用输入数值。
- **修复后的表现**：重扫 `pallets/flask`，AI 的每条命中都带真实文件与处数（`docs/api.rst` 4 处等），并主动写出"输入未提供逐条扣分归因，以上仅为命中位置清单"。
- **教训**：给模型的上下文越"干净"，它越不会拿想象填补空白；但约束必须写在 prompt 里，不能指望模型自觉。

### 摘录 6：`const` 重赋值——三轮才定位到的那一行

- **AI 的实现**：列表渲染函数里写了 `const meta = finding.location ? ... : "仓库级检查";`，紧接着又 `meta += ":" + finding.location.line_start;`。
- **怎么发现的**：这条最费劲，值得记下来。
  1. 页面一直显示红条"扫描状态获取失败 / Assignment to constant variable."，但 `node --check` 通过、`check_frontend.py` 全 PASS——因为它只在**执行到那一行**时才抛错。
  2. 我第一轮改错了地方：以为是 `currentReport` 的赋值顺序，改完截图错误照旧（截图未变，说明我的改动根本没生效或不是根因）。
  3. 第二轮让 Chrome 把 console 打出来（`--enable-logging=stderr --v=0`），拿到 `poll failed TypeError ... (353)`，但 353 行是 `catch` 那一行，不是出错行——`error.message` 骗人。
  4. 第三轮做了最小复现页：单独注入脚本、只调 `render()`，把 `e.stack` 打出来，才拿到 `renderFindings ... :472:67`，正是 `meta +=` 那一行。
- **修复**：`const` 改 `let`，并新增 `check_js_scope.py` 专扫"const 声明后又被赋值"，用注入一处坏代码反向验证它能报出来（报出 `line 199: const meta 被重新赋值`）。
- **教训**：前端的"语法通过"和"运行正确"是两件事；错误堆栈要拿 `e.stack` 而不是 `e.message`；定位不到就做最小复现，别靠猜。

### 摘录 7：AI 写的异步接口把协程对象直接返回了

- **AI 的实现（我改的版本）**：把 AI 建议改成异步执行后，`main.py` 只接了一根线，把 `generate_advice(report)` 交给线程池；而 `generate_advice` 是 `async def`。
- **报错**：`python TypeError: 'coroutine' object is not iterable` + `ValueError: [TypeError("'coroutine' object is not iterable")]`，`GET /api/scans/{id}` 直接 500。
- **怎么发现的**：改完先跑端到端脚本（创建任务 → 提交建议 → 轮询），第一次轮询就 500，再翻 uvicorn 日志拿到上面两行。
- **修复**：加一层同步入口 `def run_advice(report): return asyncio.run(generate_advice(report))`，线程池只拿同步函数。
- **教训**：把异步函数从事件循环搬到线程池，必须显式桥接；`ThreadPoolExecutor.submit` 不认协程，也不会报错，只会在序列化时炸。

### 摘录 8：同一类问题刷屏，把 14 处真实命中变成 14 条重复告警

**背景**：拿 `cmderdev/cmder` 做第二轮真实验证（这个仓库有 `.bat`/`.ps1`、README 里全是 Windows 路径，正是题目关心的形态）。首扫 45 分，`paths` 维度直接 0/20。

**AI 给的第一版行为**：每条正则命中都生成一条独立 finding。结果清单里出现 14 条一模一样的"硬编码本机绝对路径"，其中 6 条来自同一个 `CHANGELOG.md`。前 6 条各扣 3 分，后 8 条扣 0 分，界面上看起来像"扣了 6 次"，实际规则只该按文件算一次。

**我怎么发现的**：把 findings 按 `rule_id` 分组打出来，`P001` 占 14/19，而 `applied_deduction>0` 的只有 6 条；再对一遍 `coverage.scanned_files=51`，51 个文件不可能有 14 个独立路径问题。

**修法**（改在了 `rules.yaml` 与 `app/scoring.py`、`app/scanner.py`，不是改输出文案）：

1. `rules.yaml` 给 P001 加 `count_mode: per_file` + `report_mode: file_summary`；
2. `app/scoring.py` 的计分键从"每条 finding"改成 `(rule_id, path)`，同文件同规则只扣一次，其余条目 `related_finding_id` 指回代表条目；
3. `app/scanner.py` 按文件合并上报，命中数与行号写进 `message`，`Finding` 模型新增 `hit_count` 字段；
4. `app/config.py` 的启动校验允许这两个新枚举，且 `file_summary` 只对 `kind: content` 合法（写错就拒绝启动）。

**修完的效果**：`cmder` 从 45 分变成 59 分，`paths` 5/20；清单从 14 条压到 5 条，每条都带"本文件命中 N 处，行 …"。AI 追问我一句很有用的：**"45 分和 59 分哪个才是对的？"**——答案是 59，但 45 也没"算错"，它是"同一条问题被重复计价的产物"。这类问题不看清单结构是发现不了的，看总分反而会被"分数挺合理"骗过去。

### 摘录 9：工具扫自己，扫出一堆「自指噪声」

- **背景**：把 `Gniyn/gitScan` 自己丢进工具扫，`paths` 维度只拿 2/20。
- **AI 的初版解释**：认为「路径命中集中在文档里，说明文档质量差」。判断错了——`AI_USAGE.md` 里写 `D:\gitScan` 是在举例说明反例，不是缺陷；`docs/samples/*.json` 存的是别的仓库的命中原文。
- **修法**：`policy.content_exclude_globs` 补 `docs/samples/**`、`*.min.js`、`vendor/**`、`node_modules/**`、`*.lock`、`*.snap`；P001 的 `exclude_paths` 单独排除 `AI_USAGE.md`、`docs/ai-log.md`。
- **结果**：62 → 68 → 74 分，`paths` 从 2/20 回到 14/20。剩下两条命中是 `check_frontend.py:8`、`check_js_scope.py:12` 里的本机路径常量——**这两条是真的**，我自己写的检查脚本确实硬编码了本机目录，改成 `Path(__file__).resolve().parent` 才对。

### 补充：AI 说"改好了"但没改对的地方

修完 P001 之后我又扫了一次自己，结果 44 分、密钥维度 0 分——原因是 `tests/test_scanner.py` 里为了测试构造的假密钥被当成真实泄露。AI 的修复只动了规则，没有动检测范围。这提醒了一件事：**AI 的"已验证"要自己复核**，`17 passed` 不代表真实仓库上行为正确。最终改为测试样本目录不参与内容检测，并在 `limitations` 里显式说明跳过了几个文件。

## 我对 AI 输出的复核清单

每次 AI 交回代码，我都检查这几项：

- [ ] 设计文档里的口径有没有被悄悄改写（尤其"降级/跳过/封顶"这类语义）
- [ ] 新增的例外有没有在 `limitations` 里显式说明，而不是静默丢弃
- [ ] 规则是否仍在 `rules.yaml` 里，Python 侧有没有硬编码正则
- [ ] 测试是否覆盖了刚修的那个 bug，还是只加了"能跑通"的用例
- [ ] 真实仓库扫描结果是否与人工判断一致

## 说明

AI 没有参与任何对目标仓库的写操作。全部扫描只读，只在内存中解包 tarball，不执行仓库里的任何代码。

