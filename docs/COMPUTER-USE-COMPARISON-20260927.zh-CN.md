# Jev LongSeq 与 Codex Astra 截图操作对比

实测日期：2026-09-27。任务：访问本地目录的全部 12 条记录，保存 Rating ≥ 4 且 Price ≤ 60 的记录，并读回保存状态，不重复保存、不删除。

本次结果：LongSeq 的模型费用明显较低，但完成判断不稳定；第二轮网页结果已经正确，控制器仍继续循环并报错。Astra 第二次有效测试正确完成并正常结束。不能把失败运行的较短耗时解释为成功完成的加速比。

## 结果

| 系统／轮次 | 端到端耗时 | 网页终态独立评分 | 流程正常结束 | 模型费用（美元） |
| --- | ---: | --- | --- | ---: |
| LongSeq 第 1 轮 | 32.17 秒 | 未达标：访问 5/12，保存 2/3 | 否，反馈校验失败 | 已知用量约 $0.00436–$0.00574 |
| LongSeq 第 2 轮 | 143.91 秒 | 达标：访问 12/12，保存 3/3，无重复或违规 | 否，反馈校验失败 | 已知用量 $0.023249 |
| Astra 第 1 次有效执行 | 66.95 秒 | 未取得终态评分，执行途中断线 | 否，连接失败 | 未返回总 usage，未知 |
| Astra 第 2 次有效执行 | 254.12 秒 | 达标：访问 12/12，保存 3/3，无重复或违规 | 是 | 标准短上下文 API 等价估算 $3.466636 |

两个终态达标的运行，其已知用量费用相差约 **149.1 倍**。这不是包含失败重试的“每次成功总成本”，因为 Astra 中断轮的用量未知。

正确保存集合均为 `item-001`、`item-004`、`item-009`。Astra 重新打开这三条并从截图确认 `Saved: yes`。

## 为什么 LongSeq 达标后仍失败

第二轮在约 **51.82 秒**时，轨迹中的可见证据已覆盖全部 12 条记录和三个正确保存状态。此时间由观察事件时间戳相对运行 manifest 的开始时间计算，不是控制器的正常完成时间。

之后它仍继续浏览和请求大脑指导，直到反馈校验失败。其网页评分 `grade.strict_success=true`，但控制器 `result.status=failed`、`result.strict_success=false`。本报告保留这两个不同含义的结果。

两轮出现的校验错误包括：

- `evidence quote is not present in current observation`：反馈引用的证据不在当前观察中。
- `next_goal_note / extra_forbidden`：模型返回 schema 不允许的字段。

第二轮 Jev 请求合计 19.86 秒，大脑请求及重试合计 108.67 秒；两者分别占 143.91 秒端到端时间的约 13.8% 和 75.5%。当前这项任务的主要延迟来自大脑反馈和结束判断，不能只看 Jev 的单次速度。

## 用量与价格

价格核对日期同实测日期；DeepSeek 当天为周日，使用 off-peak 价格。费用均只覆盖模型推理，不含本机计算、浏览器、报告整理、资料查询或订阅费。

| 模型 | 输入／百万 token | 缓存输入／百万 token | 缓存写入／百万 token | 输出／百万 token |
| --- | ---: | ---: | ---: | ---: |
| Jev 1.13 | $0.042 | 未使用独立缓存费率 | — | 免费 |
| DeepSeek Flash，off-peak | $0.15 | $0.003 | — | $0.60 |
| GPT-6 Astra，Standard 短上下文 | $10 | $1 | $12.50 | $50 |

LongSeq 第 2 轮：

- Jev：47 次成功响应，152,547 输入 token、4,470 输出 token；$0.006406974。
- DeepSeek：12 次成功响应，36,280 输入 token，其中缓存命中 13,184；22,230 输出 token；$0.016841952。
- 合计已知用量费用：$0.023248926。
- 另有一次 TLS 建连失败，没有 HTTP 响应或 usage。因此原始账本的完整总费用保持 `null`，没有把未知项改写成免费。上表给的是有 usage 的响应费用。
- 第一轮未保留缓存命中明细，因此以全部输入命中缓存和全部未命中缓存计算范围；另外同样存在一次 TLS 建连失败。

Astra 第 2 次有效执行由 Codex CLI 返回整轮累计 usage：

```json
{
  "input_tokens": 1913503,
  "cached_input_tokens": 1746816,
  "cache_write_input_tokens": 0,
  "output_tokens": 1059,
  "reasoning_output_tokens": 29
}
```

输出 token 已包含推理 token，不重复相加。按 Standard 短上下文价格折算：

```text
((1,913,503 - 1,746,816) × 10 + 1,746,816 × 1 + 1,059 × 50) / 1,000,000
= $3.466636
```

**Astra 使用 ChatGPT 订阅登录，并非 API key 调用；$3.466636 是 API 等价估算，不是 API 实际扣款。** 累计输入量并不等于单次上下文长度。该估算采用单次请求不超过 272K 的标准费率；CLI 没有逐请求计费明细，不能据此还原一张正式账单。Fast 模式按同样用量约为 $6.933272；超过 272K 的请求应按官方长上下文费率另算。订阅通道测得的延迟也不是对标准 API 通道延迟的保证。

## 测试条件及边界

- 两边使用同一个未修改的 `catalog_html(12)`，fixture hash 完全一致，Chromium 均为 `153.0.8010.12`，窗口均为 1280×900，分别使用隔离浏览器，顺序执行。
- LongSeq：当前动态策略，DeepSeek `deepseek-flash` 大脑 + `jev-latest` 小脑；实际 Jev 返回 `jev-1.13.0`；brain interval 12，100 动作上限，600 秒上限。未为了本次对比修改产品策略或提示词。
- Astra：显式指定 `gpt-6-astra / high`，通过 Codex CLI 0.151.0 调用自建 MCP 截图／坐标操作接口。它看不到 DOM、页面源码或独立评分结果。22 次 computer 工具调用、36 次鼠标操作；另有一次插件工具发现调用，耗时及 token 已计入。
- 这是 **Codex CLI + 自建截图接口** 的结果，不能冒充桌面端原生 CUA 插件完整链路或 OpenAI 内置 computer API 的实测。
- 两边目标条件一致；Astra 提示词额外明示记录总数为 12，LongSeq 自带目标写的是“所有记录”。因此它是任务规模已知的小型工程 pilot，不是提示词完全相同的盲测。
- Astra 成功轮允许请求和流各最多 2 次重试；早先断线轮配置为 0 次。LongSeq 保留项目默认最多 2 次重试。不同运行间网络抖动、上下文、缓存和工具开销均会影响结果。
- Codex CLI 对该显式模型名称报告本地 metadata 缺失并使用 fallback metadata；没有换用其他模型，模型身份依据显式 CLI 参数，未获得服务端版本快照验证。
- 不包含公开站点、登录、复杂表单或长任务分布；不能据此外推通用成功率或稳定速度优势。

## 启动调试开销

正式截图操作前的 Astra `r1`–`r4` 为工具加载／权限配置调试，不作为有效任务样本。三个有 usage 的调试调用折算合计 **$0.940574**，另一次在本地配置检查阶段退出、没有模型 usage。它们的原始记录全部保留。`r5` 是真实执行后的连接中断；`r6` 是成功执行。

## 原始证据与复跑

- [LongSeq 第 1 轮](../runs/jev-vs-astra-20260927/longseq-r1/report.json)
- [LongSeq 第 2 轮](../runs/jev-vs-astra-20260927/longseq-r2/report.json)：含缓存明细 `raw_usage`，费用分析以该 report 为准。原来的 JSONL 事件是在补充计费信息前发出的。
- [Astra 成功轮](../runs/jev-vs-astra-20260927/astra-r6/report.json)、[原始 Codex 事件](../runs/jev-vs-astra-20260927/astra-r6/codex-events.jsonl)、[动作和截图索引](../runs/jev-vs-astra-20260927/astra-r6/actions.jsonl)、[浏览器 trace](../runs/jev-vs-astra-20260927/astra-r6/trace.zip)
- [机器可读汇总](../runs/jev-vs-astra-20260927/comparison.json)
- [Astra 测试脚本](../examples/astra_computer_benchmark.py)、[LongSeq 用量记录包装器](../examples/measure_longseq.py)

使用空的新输出目录，现有密钥仍从自己的 env 文件读取，不复制到产物：

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" .venv/bin/python examples/measure_longseq.py \
  demo --mode dynamic --policy jev --records 12 --env-file /path/to/your.env \
  --brain api --brain-interval 12 --max-feedback-calls 40 --max-actions 100 \
  --max-seconds 600 --output runs/comparison-new/longseq

PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" .venv/bin/python examples/astra_computer_benchmark.py \
  run --records 12 --effort high --max-seconds 600 --output runs/comparison-new/astra
```

包装器价格固定为本次日期的 off-peak 费率；其他日期或时段复跑应重新核价。新增计费测试 3 项通过，新增脚本和测试的 Ruff 检查通过。

官方价格来源：[TypeSafe 模型](https://docs.typesafe.ai/models)、[DeepSeek 价格](https://api-docs.deepseek.com/quick_start/pricing/)、[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)、[OpenAI API 价格](https://developers.openai.com/api/docs/pricing)。
