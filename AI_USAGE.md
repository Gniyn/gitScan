# AI 使用说明

这份东西主要讲两件事：我用了哪些 AI 工具，以及 AI 给出的东西我是怎么验证的。

说个最直接的感受：**AI 能写出跑得通的代码，但它会悄悄改设计口径，而且改得看起来挺合理。** 比如我明明要求"超长文件截断读取"，它给我写成"跳过不扫"——代码能跑、测试能过，语义已经完全变了。所以后面凡 AI 交回的东西都过三道：跑测试、扫真实仓库、拿设计文档逐行对。

## 使用的工具

| 环节 | 工具 | 我怎么用的 |
|---|---|---|
| 需求拆解与架构 | Codex | 先把四个检测项和评分维度拆清楚，定下"规则外置、执行器通用"的分层，再让它按层实现 |
| 代码生成 | Codex（命令行，直接改本地文件） | 按模块生成 `rules.yaml`、扫描器、评分器、前端页面，改完我自己看 diff |
| 规则正则验证 | 自己写的 Python 探针 | 直接 import `Rulebook` 跑正则，打印命中/未命中，看它到底认不认 |
| 端到端验证 | 本地起服务 + 真实仓库 | 扫公开仓库，比对分数和 `limitations` 原文是否符合我的判断 |
| 代码审查 | Codex（当第二意见） | 把 diff 贴过去让它挑边界条件，采不采纳我自己定 |
| 前端体检 | 自己写的静态检查 + 无头浏览器 | `check_frontend.py` 查 DOM/资源/字段引用，`check_js_scope.py` 查 `const` 重赋值，`probe_frontend.py` 抓控制台报错 |
| 接入建议 | DeepSeek（`deepseek-chat`，OpenAI 兼容接口） | 只发脱敏后的评分、规则编号和命中数量，文件内容一个字不发 |

## 三道验证手段

1. **跑测试**：`pytest -q`，20 条，覆盖确定性、密钥封顶、截断、文件预算、路径变体、BOM、测试样本排除。AI 说"改好了"我第一件事是看测试红没红。
2. **扫真实仓库**：光看测试不够。每轮改动我都起服务扫一个真仓库，看分数和 `limitations`。样例报告在 [`docs/samples/`](docs/samples/)，`gitscan-self.json` 是本项目自扫结果，`hello-world.json` 是 `octocat/Hello-World`。
3. **写探针脚本**：正则、glob、评分这类"AI 说改了但可能只改了表面"的逻辑，写一次性脚本直接喂样本看输出，验完就删，不留仓库里。

## AI 答错但被我发现的记录

完整过程在 [`docs/ai-log.md`](docs/ai-log.md)。下面按发现顺序摘 9 条，每条都有**错在哪、我怎么发现的、最后改成什么**，不是笼统的"AI 出过错"。前 4 条是代码层错误，第 5~7 条是"看着改好了其实没对"，第 8 条是设计层（规则对，聚合方式错），第 9 条是扫自己扫出来的误报和真阳性。

### 摘录 1：截断读取被擅自改成跳过（最典型）

- **我当时要的**：单文件超过 256 KiB 就读前 256 KiB 接着扫，超出部分丢弃。
- **AI 交回的**：改成"跳过不扫描"，还把 `truncated` 算进 `partial`，代码在 `app/scanner.py:279`：

  ```python
  partial = bool(excluded or truncated or unsupported)
  ```

- **错在哪**：跳过就是漏报，而漏报比截断严重得多，恰恰可能漏掉真正含密钥的大文件；另外随便一个超大 `package-lock.json` 就能让整份报告降级成 `partial`，跟我定的"只有文件数超 2000 才降级"直接冲突。
- **我怎么发现的**：拿设计文档和 `app/scanner.py` 逐行对，发现 `partial` 的判定条件多了一项。
- **怎么改的**：恢复截断语义，`partial = bool(excluded)`，截断只写进 `limitations`。回归测试是 `tests/test_scanner.py:73` 的 `test_large_file_is_truncated_not_skipped`，断言 `status == "complete"` 且 `scanned_bytes == max_file_bytes`。

### 摘录 2：密钥评分把"泄露 1 条"和"泄露 10 条"算成同分

- **AI 交回的**：每命中一条 `sk-` 扣 15 分，不做维度封顶。
- **错在哪**：扫出 2 条密钥维度就归零，扫出 10 条也是 0 分，真实差异全被抹平；总分还可能蹦出大负数。
- **我怎么发现的**：让它自己给算例，它算出"3 条 sk- 扣 45 分"，可密钥维度满分才 30——它自己的算例就证明没封顶。
- **怎么改的**：密钥类规则统一 `once_per_rule`，加规则级和维度级双重封顶，总分 `max(0, 100 - Σ)`。启动校验强制：密钥维度规则没写 `count_mode: once_per_rule` 直接拒绝启动（`app/config.py:45`）。

### 摘录 3：文件获取方案在匿名配额下根本跑不动

- **AI 交回的**：按文件逐个调 `GET /repos/{o}/{r}/contents/{path}`。
- **错在哪**：匿名 API 额度约 60 次/小时，一个文件一次请求，扫五十来个文件就耗光，而我预算是 2000 个文件——演示时必然当场 403。
- **我怎么发现的**：让它自己核算请求数，它写的"最坏情况接近每文件一次请求"已经说明问题了。
- **怎么改的**：换成 `GET /repos/{o}/{r}/tarball/{sha}` 一次取整包，内存流式解包，不落盘。

### 摘录 4：路径规则只覆盖了一半盘符

- **AI 交回的**：只写 `[CD]:` 和 `/Users/`，`E:\`、`F:\`、`/home/`、UNC 路径全漏。
- **我怎么发现的**：写探针直接跑正则，四个样本里三个打不中：

  ```python
  for s in ["C:/Users/alice/x", "E:/data/log.txt", "/home/alice/app.py", r"\\nas\share\f.txt"]:
      print(bool(pattern.search(s)), s)   # True False False False
  ```

- **怎么改的**：扩到 `[A-Z]:[\\/]`、`/home|Users/<user>/`、UNC `\\\\host\\share`，四个样本全写进回归测试。

### 摘录 5：AI 在建议里编造了扣分归因

- **AI 的输出**：接上 AI 建议后第一次生成，里面写着"硬编码路径维度 0 分，涉及 `AI_USAGE.md`、`docs/ai-log.md`、`docs/samples/*.json`"。但当时真实数据是路径维度 17 分（只扣 3 分），真正命中的只有 `rules.yaml` 一处，`AI_USAGE.md` 根本没被任何规则命中。
- **我怎么发现的**：拿同一份报告的 `dimensions.paths.score` 和 `findings[].location.path` 逐条核对。它说的文件在 `findings` 里查不到，分数也和输入对不上——它把自己"觉得这种仓库应该有路径问题"当成了输入里的事实。
- **怎么改的**：没换模型，而是收窄输入、收紧约束。
  1. 输入里去掉 `evidence` 原文，只留 `evidence_paths`（截断 10 条），让 AI 没有编造的材料；同时明说"某文件导致某条扣分"这个对应关系输入里本来就没有。
  2. system prompt 加三条硬约束：不得编造输入里没有的路径/分数/命中数量；不得断言某个文件导致了某条扣分；不得自己算某个维度扣了多少，只能引用输入里的数。
- **改完的效果**：重扫 `pallets/flask`，每条命中都带真实文件和处数（`docs/api.rst` 4 处等），还会主动写"输入未提供逐条扣分归因，以上仅为命中位置清单"。
- **体会**：给模型的上下文越干净，它越不会拿想象填空白；但约束得写进 prompt，不能指望它自觉。

### 摘录 6：`const` 重赋值——三轮才定位到那一行

- **AI 交回的**：列表渲染函数里先写 `const meta = finding.location ? ... : "仓库级检查";`，紧接着又 `meta += ":" + finding.location.line_start;`。
- **我怎么发现的**：这条最费劲。
  1. 页面一直挂着红条"扫描状态获取失败 / Assignment to constant variable."，但 `node --check` 通过、`check_frontend.py` 全 PASS——因为它只在**执行到那一行**时才抛错。
  2. 第一轮我改错了地方，以为是 `currentReport` 的赋值顺序，改完截图红条照旧。
  3. 第二轮让 Chrome 把 console 吐出来（`--enable-logging=stderr --v=0`），拿到 `poll failed TypeError ... (353)`，可 353 行是 `catch` 那行，不是出错行——`error.message` 骗人。
  4. 第三轮做了个最小复现页：单独注入脚本、只调 `render()`，把 `e.stack` 打出来，才拿到 `renderFindings ... :472:67`，正是 `meta +=` 那一行。
- **怎么改的**：`const` 改 `let`，另外新增 `check_js_scope.py` 专扫"const 声明后又被赋值"，往里注入一处坏代码反向验证它能报出来（报出 `line 199: const meta 被重新赋值`）。
- **体会**：前端"语法通过"和"运行正确"是两件事；拿 `e.stack` 不要拿 `e.message`；定位不到就做最小复现，别靠猜。

### 摘录 7：AI 写的异步接口把协程对象直接返回了

- **AI 交回的**：把 AI 建议改异步之后，`main.py` 只接了一根线，把 `generate_advice(report)` 丢给线程池；但 `generate_advice` 是 `async def`。
- **报错**：`TypeError: 'coroutine' object is not iterable` + `ValueError: [TypeError("'coroutine' object is not iterable")]`，`GET /api/scans/{id}` 直接 500。
- **我怎么发现的**：改完先跑端到端脚本（建任务 → 提交建议 → 轮询），第一次轮询就 500，翻 uvicorn 日志拿到上面两行。
- **怎么改的**：加一层同步入口 `def run_advice(report): return asyncio.run(generate_advice(report))`，线程池只拿同步函数。
- **体会**：把异步函数从事件循环搬进线程池必须显式桥接；`ThreadPoolExecutor.submit` 不认协程，也不会当场报错，只在序列化时炸。

### 摘录 8：同一类问题刷屏，14 处真实命中变成 14 条重复告警

**背景**：拿 `cmderdev/cmder` 做第二轮真实验证（这仓库有 `.bat`/`.ps1`，README 里全是 Windows 路径，正是题目关心的形态）。首扫 45 分，`paths` 直接 0/20。

**AI 的第一版行为**：每条正则命中生成一条独立 finding。结果清单里 14 条一模一样的"硬编码本机绝对路径"，其中 6 条来自同一个 `CHANGELOG.md`。前 6 条各扣 3 分，后 8 条扣 0 分，界面上看着像"扣了 6 次"，可规则本该按文件算一次。

**我怎么发现的**：把 findings 按 `rule_id` 分组打出来，`P001` 占 14/19，而 `applied_deduction>0` 只有 6 条；再对一遍 `coverage.scanned_files=51`，51 个文件不可能有 14 个独立路径问题。

**怎么改的**（改在 `rules.yaml`、`app/scoring.py`、`app/scanner.py`，不是改文案）：

1. `rules.yaml` 给 P001 加 `count_mode: per_file` + `report_mode: file_summary`；
2. `app/scoring.py` 计分键从"每条 finding"改成 `(rule_id, path)`，同文件同规则只扣一次，其余条目 `related_finding_id` 指回代表条目；
3. `app/scanner.py` 按文件合并上报，命中数和行号写进 `message`，`Finding` 新增 `hit_count` 字段；
4. `app/config.py` 启动校验放行这两个新枚举，且 `file_summary` 只对 `kind: content` 合法（写错直接拒绝启动）。

**改完的效果**：`cmder` 45 → 59 分，`paths` 5/20；清单从 14 条压到 5 条，每条带"本文件命中 N 处，行 …"。值得记的是那个问题：**45 和 59 哪个对？** 答案是 59，但 45 也谈不上"算错"，它是同一条问题被重复计价的结果。这类毛病不看清单结构发现不了，光看总分反而会被"分数挺合理"糊过去。

### 摘录 9：工具扫自己，扫出一堆「自指噪声」

- **背景**：把 `Gniyn/gitScan` 自己丢进工具扫，`paths` 只拿 2/20。
- **AI 的第一版解释**：认为"路径命中集中在文档里，说明文档质量差"。判断错了——`AI_USAGE.md` 里写 `D:\gitScan` 是在举例说明反例，不是缺陷；`docs/samples/*.json` 存的是别的仓库的命中原文。
- **怎么改的**：`policy.content_exclude_globs` 补 `docs/samples/**`、`*.min.js`、`vendor/**`、`node_modules/**`、`*.lock`、`*.snap`；P001 的 `exclude_paths` 单独排除 `AI_USAGE.md`、`docs/ai-log.md`。
- **结果**：62 → 68 → 74 分，`paths` 从 2/20 回到 14/20。剩下两条命中在 `check_frontend.py:8`、`check_js_scope.py:12`，是我自己脚本里写死的本机路径——**这两条是真的**，改成 `Path(__file__).resolve().parent` 才对。

### 补充：AI 说"改好了"但没改对的地方

修完 P001 我又扫了一次自己，44 分、密钥维度 0 分——原因是 `tests/test_scanner.py` 里为了测试构造的假密钥被当成真实泄露。AI 的修复只动了规则，没动检测范围。这事儿提醒我：**AI 的"已验证"必须自己复核**，`17 passed` 不代表真实仓库上的行为就对。最后改成测试样本目录不参与内容检测，并在 `limitations` 里显式说明跳过了几个文件。

## 我复核 AI 输出的清单

每次 AI 交代码回来，我都过一遍这几项：

- [ ] 设计文档里的口径有没有被悄悄改写（尤其"降级/跳过/封顶"这类语义）
- [ ] 新增的例外有没有在 `limitations` 里显式说明，而不是静默丢弃
- [ ] 规则是否还在 `rules.yaml` 里，Python 侧有没有偷偷硬编码正则
- [ ] 测试覆盖的是刚修的那个 bug，还是只加了个"能跑通"的用例
- [ ] 真实仓库的扫描结果，和我人工判断是否一致

## 说明

AI 没有参与任何对目标仓库的写操作。全部扫描只读，只在内存里解包 tarball，不执行仓库里的任何代码。
