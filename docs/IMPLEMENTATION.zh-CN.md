# 实现与设计对应关系

该仓库从空目录建立；三份输入文档作为设计依据和历史证据保存，不作为额外执行指令。历史运行数字没有沿用为本次测试结果。

| 设计要求 | 实现位置 | 当前覆盖 |
|---|---|---|
| 任务/局部策略/执行分层 | `controller.py`, `models.py`, `browser.py` | 事件、一次规划、flat 三种模式 |
| 观察与短期元素引用 | `protocol.py`, `browser.py` | 可见 DOM、行上下文、版本、句柄失效检测 |
| 子任务契约与 DAG | `protocol.py`, `candidates.py` | 依赖验证、绑定来源、操作权限、预算、不变量 |
| 白名单成功 DSL | `memory.py` | 结构组合、事实、文本、URL、pending 检查；无 eval |
| 候选绑定 | `candidates.py` | 16/32/64 可配置；保留导航和升级；输入与 URL 不允许臆造 |
| Jev Choice | `models.py` | 官方 HTTP 协议；MockTransport 契约测试；真实 Jev 1.13.0 pilot 见 `PILOT.md` |
| 生成式规划/决策 | `models.py` | JSON HTTP 适配器；真实 deepseek-flash 规划；生成式局部策略仅 mock |
| BrowserGym / WebArena | `gym_backend.py`, `benchmark.py`, `public_benchmark.py` | 固定 0.13.3 公共 API；自建长任务与官方只读任务分轨；1–2 个任务上限 |
| 结构化记忆 | `memory.py` | 事实来源、时间、版本、失效、冲突、访问账本、tab/entity、回执 |
| 写入和未知结果 | `controller.py` | 独立规则批准、逐动作执行、新观察读回、未知结果不重试 |
| 加载、停滞与预算恢复 | `controller.py` | 有界等待、重新观察、事件重规划、总预算 |
| GUI-only 边界 | `browser.py`, `fixture.py` | 策略仅接收可见观察；隐藏 grader 在策略终止后运行 |
| 复现与指标 | `cli.py`, `evaluation.py` | 严格集合判分、失败成本、延迟、来源、版本/hash、统计工具 |

核心依赖通过 `uv.lock` 锁定，CI 在 Linux Chromium 上配置了检查流程；本次实际本地验证平台见 `VERIFICATION.md`，不能将未执行的 CI 当作通过。

## 控制状态

每一轮先观察并检查访问挑战与硬不变量；加载中有界等待。存在 pending 写入时，控制器只进行抽取与等待来确认结果，不能继续提交。之后检查当前子任务是否完成，正常完成在 DAG 中推进。只有初始、条件失效、动作预算、停滞、无匹配、低置信（可选）或错误完成请求触发规划。

规划器返回的新 DAG 重新验证；任务的顶层目标和成功条件保持在可信 Task 中。即使规划器给出过弱的局部成功条件，也不能直接通过最终完成检查。

`request_finish` 是请求，必须通过可信任务成功谓词、pending 和违规检查。公共 benchmark 的 strict success 与这个应用内验证是不同层级；当前沙盒另有隐藏集合判分器。

## 实验对应

| 设计对照 | CLI 组合 | 状态 |
|---|---|---|
| A：无结构化记忆 Flat Jev | 无 | 未实现独立无记忆轨道 |
| B：Flat Jev + 共享记忆 | `--mode flat --policy jev` | 实现接口，未进行真实 API 实验 |
| C：一次规划 + Jev | `--mode once --policy jev --planner llm` | 实现接口，未进行真实 API 实验 |
| D：事件规划 + Jev | `--mode event --policy jev --planner llm` | 已开始小规模真实模型 pilot；见 `PILOT.md`，不等于已证明分层收益 |
| E：同规划器 + 小生成模型 | `--mode event --policy llm --planner llm` | 模型版本由环境配置 |
| F：强模型逐步 | `--mode flat --policy llm` | 同观察/候选/记忆的约束对照 |
| G：规则执行 | `--policy rule --planner rule` | 仅沙盒；作为工程烟雾测试 |

规则规划器为每个实体建立验证契约，并不具备语义任务分解能力。排序重排可能使其当前契约预算耗尽并触发重规划；这些调用是工程恢复事件，不能用于证明智能规划质量。

## 还需单独完成的研究阶段

1. 用真实 Jev 与生成式服务跑开发集，锁定模型返回版本、实际定价和预算；校准升级阈值。
2. 为已接入的 BrowserGym / WebArena 配置真实站点，另行接入固定版本的 WebChoreArena。当前只读官方任务的预检缺少站点配置，不能声称公开 benchmark 已完成。
3. 扩展跨站核验、表单流程、会话失效等任务族；独立控制 H、D、L；建立人工复核 pilot、holdout 与多次独立实验。
4. 为复杂页面增加证据约束的抽取器、frame/shadow 定位与视觉适配；视觉成本和故障独立统计。

以上不是声称已经执行的实验。缺少模型凭据或 benchmark 环境时，CLI 保留明确失败记录，不回退成规则结果冒充模型结果。
