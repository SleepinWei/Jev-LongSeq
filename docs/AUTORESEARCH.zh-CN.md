# 可观测性与 autoresearch

这条循环自动执行：**benchmark → 汇总观测 → 诊断 → 单项配置变更 → 再跑同任务 → 保留或拒绝**。当前改进范围是用户选择的调度策略、上下文与提示词，不自动修改源码、任务目标、判分器、权限或完成校验。

## 运行

```bash
# 默认两轮：相同的两个公共网页任务，基线 + 一个候选（最多 4 个 episode）
.venv/bin/python -m jev_browser autoresearch \
  --env-file /path/to/your.env --brain api \
  --codex-model gpt-6-astra --codex-effort high \
  --suite public-web --task-ids 50 332 --max-trials 2 --metric tokens \
  --max-seconds 300 --study-seconds 1500 \
  --max-model-attempts 300 --max-tokens 600000 --live-preview \
  --output runs/research-01

# 给已有运行生成观测报告；不调用模型，也不重新执行任务
.venv/bin/python -m jev_browser observe runs/dynamic-efficiency-01

# 用保留的配置执行新任务（仅严格通过的配置才有 best-profile.json）
.venv/bin/python -m jev_browser browse https://example.com --goal '你的目标' \
  --tuning runs/research-01/best-profile.json \
  --env-file /path/to/your.env --brain api \
  --codex-model gpt-6-astra --codex-effort high
```

`autoresearch` 默认使用 `--suite public-web`，通过 Playwright 在真实 Books to Scrape 公共站点执行两个任务。它们借鉴 WebArena #50 / #332 的跨页筛选和分组汇总类型，**不是官方 WebArena 任务或成绩**。不需要 Docker、登录或 `WA_*` 配置。每轮同样两个任务，最多基线加一个候选，共四个 episode。

| 类型 | 公共网页任务 | 当前规模 |
|---|---|---|
| 50 | Fiction 全部页面中价格 ≤ £40 的书：数量与总价 | 65 本，4 页 |
| 332 | Mystery、Historical Fiction、Travel：逐类别数量与总价 | 69 本，5 页 |

执行 agent 只接收自然语言目标、允许的站点及输出 JSON 格式，不接收预设操作步骤或参考答案。独立判分器用 HTTP 读取公开列表，核对分页、去重及总数，按整数便士累计；严格校验最终 JSON 中每项数量和金额。任务开始和结束时各抓取一次，按商品 URL、名称和价格生成数据指纹。数据变化或抓取失败不会算成功；基线和候选的数据指纹不一致则无法比较。判分器与 Codex 分析器分离，参考数据保存在各任务的 `oracle.json`，不会放入执行模型或研究模型的上下文。

任务计时只计 agent 的浏览器与模型执行；独立抓取的时间记录为 `grade.oracle_elapsed_s`，研究总活跃时间包含判分开销。预检的时间记录在 `preflight.json`，位于研究执行预算之前。两次快照无法排除短暂变化后恢复的情况；这是公共网站小样本对照的限制。

仍可显式使用 `--suite webarena` 运行原任务（需要自建站点和 BrowserGym），或 `--suite catalog` 运行原有本地目录任务。历史研究保留原 suite，重试不会静默切换任务。

## 研究工作台

```bash
.venv/bin/python -m jev_browser.inspector --runs runs --port 8769 \
  --env-file /path/to/your.env
```

打开 `http://127.0.0.1:8769/research`。功能包括：

- 研究列表与环境检查、任务执行、Codex 分析、对照验证、结束状态；每 2 秒更新。
- 一键启动两轮研究，固定 DS + Jev 执行、Codex GPT‑6 high 分析；运行中的任务避免重复启动。
- 逐轮及逐任务完成结果、耗时、tokens、未知用量和模型调用数；点击进入现有 Trace Studio。
- 改动前后参数、诊断证据、改进假设、保留/拒绝/无法比较及原因。
- 当前保留配置、单独的 Codex 分析开销；运行时网页预览（有截图时）。

UI 启动预算固定为每任务 300 秒、每研究 1,500 秒、300 次模型尝试、600,000 已知 tokens。若公共站点或模型配置不可用，研究产生真实 `blocked` 状态和 `preflight.json`，不启动任务/模型调用。页面可重试尚未执行任务的环境检查；已有执行轮次的研究需使用原 CLI 参数 `--resume`，避免改变对照条件。

## 模型分工：Codex 研究，DS + Jev 执行

autoresearch 的分析与改进默认通过本机 Codex CLI 的 `codex exec --json --output-schema` 调用 `gpt-6-astra`，推理强度 `high`。先安装 Codex CLI 并通过 `codex login` 完成 ChatGPT 登录。每次请求使用临时目录和只读沙盒，关闭工具、插件与网页搜索，强制 ChatGPT 登录；不继承 API key 环境变量，不读取 `PLANNER_*` 配置，失败不回退到 API。

Codex 调用采用临时会话，结构化上下文由本项目控制；CLI 启动、连接、推理和返回都计入耗时。tokens 来自 `turn.completed.usage`，缓存输入单独记录，已包含在 input tokens 中，不能重复相加。若退出/断连后没有 usage，就记录未知，不能记为零。订阅额度没有可靠的美元单价，因此 Codex 模式只支持 `tokens` / `latency` 目标，不接受 `cost` 目标或美元支出上限。

LongSeq 本身默认 `--brain api`：大脑、完成复核和输入助手使用项目已有的 DeepSeek API，小脑使用已有 Jev TypeSafe API。`PLANNER_*` 优先，未配置时自动兼容 `TEXT_MODEL_API_KEY`、`TEXT_MODEL_BASE_URL` 和 `TEXT_MODEL`。现有项目模型为 `deepseek-flash`。Codex 仅承担 autoresearch 的分析与提案；`--codex-model` / `--codex-effort` 默认配置这一研究模型。仍保留显式 `--brain codex` 作为可选对照，但不默认启用。

执行 trial 的 DS/Jev 用量写入每轮账本，研究分析的 Codex 用量写入 `researcher/`；整个 study 的预算包含两者。配置指纹分别记录 trial 的模型与 Codex 研究模型，避免两种成本混淆。

## 观测

| 产物 | 记录内容 |
|---|---|
| `model-request-starts.jsonl` | Codex CLI / HTTP 派发前记录 call/attempt ID、run/cycle/span、时间、模型与请求体字节统计 |
| `model-calls.jsonl` | 每次尝试结束立即记录状态、实际 usage、重试、价格计算及请求延迟；中断也尽可能落盘 |
| `spans.jsonl` | 浏览器观察/执行、策略选择、大脑复核、输入助手耗时及父子关系 |
| `trajectory.jsonl` | 原有完整页面/候选/反馈轨迹，新增 run ID、cycle、event ID |
| `observability.json` | 质量、效率、重试、脑反馈触发原因、上下文大小、耗时分位数和诊断假设 |
| `observability.md` | 可直接阅读的指标与问题报告 |

模型请求和 span 可按 run ID、cycle、span ID 关联。指标分别列出全部尝试数、HTTP 尝试数与 Codex 调用数。HTTP 重试共享 call ID，各自有 attempt ID。开始日志中尚无对应结束记录的请求标记为 inflight；进程被强制终止后仍可从已写入的日志观察进展，不会假定这些请求免费。

新增模型遥测只记录请求大小和哈希，不复制 prompt/headers/API key。原有 trajectory 和 memory 仍包含任务数据。上下文字节数不是 token 估算；只有服务返回的 usage 才计为 tokens。嵌套 span 存在重叠，不能把所有 span 时间相加当作总延迟。

诊断覆盖传输不稳定、环境完成但 agent 未收尾、结构化输出纠正、大脑耗时占比、Jev 上下文体积、错误完成和过期动作。每个诊断带观测计数和证据，标注为原因假设；网络耗时不能被解释为纯模型推理时间。

## 自动改进

可调整的 `AgentTuning` 只有四项：

| 项目 | 范围 | 用途 |
|---|---|---|
| brain_interval | 1–48 个动作 | 调整 LLM 指导频率 |
| recent_evidence | 1–8 段 | 控制 Jev 近期证据上下文 |
| excerpt_chars | 600–4,000 字符 | 控制每视图保存的证据摘录；完整观察仍在 trajectory 中 |
| prompt_variant | balanced / compact / coverage | 标准、简洁输出、覆盖与收尾提示 |

提案器根据诊断选择有限、可解释的候选，每轮仅改一个字段。例如，大脑耗时高可测试更长阶段窗口；上下文体积高可缩小近期证据窗口；收尾问题可测试覆盖提示。每次保存变更前后值、假设和相关诊断，已试过的配置不会重复搜索。

提案器通过本机 Codex GPT‑6 high 读取指标、诊断与历史试验，提出带证据和假设的单字段配置变更；可选择范围内任意值，也可以判断证据不足而停止。提示词调整限定为已有的三个版本，不生成任意源码或任意提示文本。代码会拒绝多字段修改、越界参数、虚构诊断或重复配置。

研究分析本身的 Codex 调用也计入总次数、tokens 和时间预算，单独写入 `researcher/` 的观测账本。只在还有预算执行下一个候选时调用分析器；默认两轮研究最多新增一次分析调用，最后一轮后不做无用提案。分析失败时保留原配置并停止，绝不改用 API 或启发式来伪装 Codex 结果。

## 保留标准

1. 任务、fixture、代码、模型标识、Codex 推理强度/超时、后端、候选数及非可调预算相同，才能比较。
2. WebArena 候选必须在两个任务上都严格完成，且没有违规或重复写入。失败后低 tokens/低耗时不算优化。
3. 基线失败、候选严格通过时，可标记为“恢复完成能力”，不伪报效率提升。
4. 两者都成功时，目标指标默认至少改善 5%；同时总耗时与总 tokens 各不得恶化超过默认 10%；逐个已成功任务也检查这一上限，不能用另一任务的收益掩盖退化。可通过 CLI 调整这些阈值。
5. 网络失败、usage 缺失或不可比较条件产生 `inconclusive`，保留原配置。Codex 订阅无法提供完整美元单价，费用目标不启用。

默认目标 `tokens` 是实际已知的输入加输出 tokens。`latency` 使用端到端墙钟；完整美元费用需要所有模型与浏览器的实际价格，Codex 研究模式不启用 `cost` 目标。不同模型 tokens 的单价不同，因此 tokens 改善不等于美元同比改善。

WebArena 保留结果标记 `two_task_provisional`（目录任务为 `single_task_provisional`）：两任务一次对照尚不足以证明泛化或统计显著性。没有严格通过的配置时不生成 `best-profile.json`，只保留基线配置和失败证据。

## 预算、停止与恢复

- 每轮有动作、反馈调用、时限预算，整个研究另有限定轮数、总时间、模型尝试数（Codex CLI + Jev HTTP）和已知 token 阈值。
- 模型尝试次数在派发前落盘，Jev HTTP 重试也占次数。token/cost 在收到 usage 后结算，达到阈值后阻止下一次请求；**一次在途请求可能越过 token/cost 阈值**，这不是 provider 侧硬封顶。
- 研究分析器始终使用 Codex 订阅，因此 `cost` 和 `--max-cost-usd` 会报参数错误；使用次数、tokens 和时间限制支出。单次运行仍能报告具有实际单价的 Jev / 旧 API 分项费用，但不会把 Codex 订阅计为免费。
- 启动下一轮前必须剩余足够的完整单轮时限（WebArena 为两个任务时限之和），避免用更短预算跑候选造成不公平比较。
- 传输不稳定时停止研究，不通过不断重试模型/benchmark 消耗预算。
- `study.json`、`budget.json` 和每轮产物持续保存；`--resume` 保留已用次数/tokens/费用，不重跑已完成轮次。恢复时任务、代码、模型设置和比较标准必须相同；可显式增加总轮数/资源上限。
- 中断时仍标记 running 的轮次不能自动重放或提升为胜者，需先检查产物。不会因为没有最终报告就忽略已派发请求。

研究目录包含 `research.md`、`study.json`、`budget.json`、每轮的 `selection.json` 和观测报告。拒绝候选后，后续轮次继续从原保留配置提出改进；保留候选后，后续轮次实际使用新配置。全局源码和默认配置不被覆盖。

## 本轮验证

新增的 Codex 调用、研究提案与保留逻辑使用离线替身做回归；离线结果不是实际速度提升。真实 Codex 短调用已尝试，CLI 在返回结构化结果前报告连接错误，没有返回 usage，未回退到 API；证据位于 `runs/codex-smoke-01` 至 `runs/codex-smoke-03`。本机到 ChatGPT 的 TLS 握手正常，但这不足以证明 Codex 请求链路正常。真实 Jev 两轮对照尚未运行，因此当前没有成功率、加速比或 token 降幅结论。

当前已启动工作台和一次 WebArena 研究：`runs/autoresearch-webarena-1790496133193115000`。按用户要求不部署或配置 WebArena，研究停在环境检查（blocked），0 个 episode、0 次模型调用。结果与改进 UI 另用隔离的临时模拟数据验证，模拟成绩未放入真实 runs。

## 公共网页首次实跑（2026-09-27）

研究记录：`runs/autoresearch-public-web-1790498559959980000`。预检覆盖 9 个列表页、134 本书；两任务前后数据指纹均一致。实际执行两个基线 episode 后，因模型传输失败停止，未调用 Codex 分析、未执行候选、未保留改进。

| 任务 | 严格通过 | Agent 端到端时间 | 已知 tokens | 问题 |
|---|---|---|---|---|
| 类型 50 | 否 | 60.7 秒 | 111,675 | 翻页点击超时，结束截图超时覆盖了原始 `needs_attention` 结果 |
| 类型 332 | 否 | 56.9 秒 | 135,251 | 操作后的读回结果无法确认，按策略停止 |

共 25 次模型尝试，已知 246,926 tokens；另有 2 次 DS 连接失败，用量未知。Jev 的 18 次请求均返回成功；DS 的 7 次尝试有 5 次成功。研究总活跃时间约 131.8 秒。运行时本机负载较高，且两个任务记录的源码指纹不同（工作区有并行修改），因此不能用此轮推导性能收益。

截图和 trace 保存已改为有限时的尽力保存，并单独记录 `artifact-errors.json`，避免覆盖真实任务结果；回归测试覆盖截图失败后仍保存 trace、关闭浏览器。修复后没有追加付费任务重跑。本轮历史结果保留原样。

## 后续修复与固定代码版本

公共网页试验暴露的导航问题按通用浏览器语义修复：

- 链接的待确认目标是到达已观察到的目标 URL，而不是完成整个阅读任务。新观察中同一目标页面返回成功状态后，只确认导航，不确认任务完成。
- 点击链接不等待所有资源下载；观察前等待 `domcontentloaded`，避免把缓慢 HTML 解析压进五次短等待。
- 导航打断 DOM 快照时最多重读三次，其他错误仍直接报告；不重发原点击。
- 精简大脑阶段提示仍传入上次反馈后新收集的跨页证据，避免近期窗口把较早页面挤掉。
- UI 启动器先把完整 Python 包固定到 `runs/.code-snapshots/<源码哈希>/`，子进程通过 `PYTHONPATH` 使用该版本。工作区之后的修改不影响同一 study 中的基线与候选。运行记录中的 `config.code_hash` 与逐任务 `manifest.code_hash` 可核对。

本次还使用已登录的 `gpt-6-astra / high` 完成了一次独立 Codex 诊断，记录在 `runs/codex-public-web-diagnosis-01/`。它属于人工调试过程的研究开销，不混入某个 study 的基线或候选成绩；7754 输入 tokens、195 输出 tokens、20.4 秒。

### 本次继续验证的结论

最新固定源码研究：`runs/autoresearch-public-web-1790499979077707000`。第一项任务完成了多次分页导航，无原来的上下文销毁或导航读回卡死；但最终复核缺少早期页面证据，引发回访，最终因重复状态停止。随后公共站点抓取出现连接错误，第二项未进入模型执行。该研究 44 次模型尝试，386,856 已知 tokens，另 2 次未知用量；没有候选对照或保留的改进。

此前两次修复验证分别记录在 `autoresearch-public-web-1790499212932658000`（96,814 已知 tokens）和 `autoresearch-public-web-1790499423570265000`（168,359 已知 tokens）；它们是不同代码版本的调试记录，不能作为效率 A/B 对照。本次所有执行研究合计 652,029 已知 tokens，另 4 次未知调用；独立 Codex 诊断另计 7,949 tokens。每个 study 的上限是 600,000 已知 tokens，并非整个调试会话的累计上限。

在本轮付费验证之后追加了两个修复：最终复核接收完整的来源证据归档；公共网页判分抓取只对连接建立失败重试一次，仍失败则留下零 agent 调用的失败报告，研究停止而非调参。历史中断轮次在面板显示“已中断”，不重写历史测量结果。58 项相关测试通过；这两个追加修复尚未再次付费重跑，因此不能声称任务通过或效率收益。
