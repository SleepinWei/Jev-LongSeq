# Codex 按需检查实验结果

这里的观测对象是 **Codex 检查 LongSeq/Jev 实验**。诊断查询只读运行产物，不调用模型，不改变被测 agent 的观察、记忆或决策。

## 最小读取流程

1. 查 `summary`：官方分数是否有效、agent 停止原因、生命周期、异常调用、最后异常 cycle、证据是否存在。
2. 查失败 cycle 的 `step`，或筛选 `calls`。这一步只返回缩略记录和原始字段的展开引用。
3. 根据调用的 `request_artifact` / `response_artifact` 引用，读取具体 JSON 字段。验证模型当时究竟收到什么、返回什么，再判断 harness 或模型的问题。
4. 有需要才逐页读取附近 cycle、官方失败检查、记忆节点或截图索引。
5. 对照实验用 `compare` 检查任务、模型、预算、代码和恢复 checkpoint 差异。单次续跑成绩不能代表总体成功率，也不能独立证明改动导致提升。

**不要默认读取完整 `/api/run`、`trajectory.jsonl`、`report.json` 或 `memory.json` 到 Codex context。** 原有回放 API 保留给浏览器；诊断接口供按需取证。

## 从 macmini 查询、导出部分报告

下面命令都在 macmini 执行。将 `RUN_ID` 换成 Studio URL 的 `run` 参数解码后的值，例如 `saas-longseq-business031-20261003-ds-baseline-47/saas-bench-business_031`。

```bash
ssh macmini
cd /Users/octopusz/CodeProjects/Jev-LongSeq
export PYTHONPATH=src
RUN_ID='saas-longseq-business031-20261003-ds-baseline-47/saas-bench-business_031'

# 默认只取 12 KB 以内的摘要
.venv/bin/python -m jev_browser.diagnostics --run "$RUN_ID"

# 某个 cycle 的事件、模型调用、开始记录、span 和压缩指标
.venv/bin/python -m jev_browser.diagnostics --run "$RUN_ID" --view step --cycle 60

# 只取该 cycle 的 readback 调用，限制每页记录数
.venv/bin/python -m jev_browser.diagnostics --run "$RUN_ID" \
  --view calls --cycle 60 --kind dynamic_readback --limit 5

# 只读取官方逐项评分的子项索引；随后展开对应 pointer
.venv/bin/python -m jev_browser.diagnostics --run "$RUN_ID" \
  --view artifact --file report.json --pointer /grade/checks --limit 5

# 取一条原始事件的某个字段；line 是零起始物理行号
.venv/bin/python -m jev_browser.diagnostics --run "$RUN_ID" \
  --view artifact --file trajectory.jsonl --line 100 --pointer /observation/text

# 把这一份部分报告保存为 JSON，不打印完整 trace
.venv/bin/python -m jev_browser.diagnostics --run "$RUN_ID" \
  --view calls --cycle 60 --max-bytes 8000 --output /tmp/jev-cycle60-calls.json
```

需要交给 Codex 时，只复制这一份部分报告，例如在开发机执行 `scp macmini:/tmp/jev-cycle60-calls.json /tmp/jev-cycle60-calls.json`。报告可能含任务正文或应用数据，应放在 Git 之外。安装项目后也可使用 `jev-diagnostics` 命令。

模型请求引用示例（`ATTEMPT_ID` 使用 `calls` 返回的真实值）：

```bash
ATTEMPT_ID='这里填写真实的32位attempt_id'
.venv/bin/python -m jev_browser.diagnostics --run "$RUN_ID" --view artifact \
  --file "model-artifacts/$ATTEMPT_ID.request.json" \
  --pointer /data/messages/1/content/untrusted_memory

.venv/bin/python -m jev_browser.diagnostics --run "$RUN_ID" --view artifact \
  --file "model-artifacts/$ATTEMPT_ID.response.json" \
  --pointer /data/body/choices/0/message/content
```

JSON pointer 支持 `~1` 表示 `/`、`~0` 表示 `~`。继续进入一个 JSON 编码的字符串时，会解析该字符串再选择内部字段；这样无需把整段消息或响应正文塞进 context。非法或未完成 JSON 不能这样展开，应读取其原始字符串片段。

对象/数组查询默认给出一层子项及其 pointer；字符串给出一个片段。用返回的 `data.next_cursor` 原样续读，并保持其他参数不变。不要自己按返回条数推算 cursor。

## Studio HTTP 接口

Studio 在 **macmini 的 127.0.0.1:8768**。通过现有 SSH 隧道访问；8767 属于其他应用。CLI 不依赖 Studio 进程，可在 UI 服务不可用时检查产物。

```text
GET /api/diagnostics?id=RUN_ID
GET /api/diagnostics?id=RUN_ID&view=step&cycle=60
GET /api/diagnostics?id=RUN_ID&view=calls&cycle=60&kind=dynamic_readback
GET /api/diagnostics?id=RUN_ID&view=artifact&file=report.json&pointer=/grade/checks
GET /api/diagnostics?id=RUN_ID&view=compare&baseline=BASELINE_RUN_ID
```

参数需要 URL 编码。CLI 和 HTTP 使用同一个查询实现。支持 `events`、`calls`、`starts`、`spans`、`contexts`、`frames` 视图；可按 `cycle`、`kind`、`call_id`、`attempt_id`、`span_id`、`observation_id` 过滤。过滤要求对应日志确实记录了这个字段，不能据此推断不存在的关联。

`step` 按记录中的 cycle 关联不同证据源。截图通常没有 cycle，因此应另查 `frames` 的时间和 observation_id，再通过已有 `/api/image?id=RUN_ID&frame=INDEX` 查看图像。帧是采样结果，不能保证每个动作都有完整的前后截图。

## 大小、分页与证据缺失

- 默认序列化响应上限 **12,000 UTF-8 字节**；`max_bytes` 可取 4,000–64,000，`limit` 为 1–100。
- 返回有效 JSON。`preview`、`omitted_chars`、`omitted_items`、`omitted_content` 或 `truncated` 明确标记省略。摘要超限时优先保留评分、停止原因和生命周期，并提示进一步取字段。
- 单个日志页的 cursor 是物理行位置；`step` 的 cursor 是 `文件序号:物理行号`，前一个日志追加内容不会挤移后一个日志的续读位置。artifact 字符串是 Unicode 字符位置；对象/数组是子项位置。已经越过的文件后来追加的行需重新查询，重写/轮转日志后也应重新查询。
- JSONL 流式扫描，坏行/末尾半行计入 `log_health.invalid_lines`，不当作成功记录。缺文件明确标为 `missing`。
- 单个 JSON 产物/日志行的服务端读取上限为 32 MiB；超限明确不可用。模型产物捕获上限为 16 MiB，失败仅返回捕获状态，不中断模型执行。
- `memory.json` 只代表最新快照，标为 `latest_only`。某个调用实际用到的历史 memory 应读该调用请求中的字段；恢复初始快照另读 `resume-memory-initial.json`。
- 未记录原始请求的旧 run 无法补造历史输入。调用计数可从旧 report 的 model_calls 回退，原始输入/响应仍显示缺失。
- 请求开始但未见完成的 attempt 可能是仍在执行，也可能中断；必须结合生命周期判断。

## 实际调用证据与隐私

HTTP transport 在 context projection **之后**捕获实际送出的 JSON payload；每次重试有独立 attempt_id，属于同一个 call_id。收到响应后先记录正文，再进行状态检查/JSON 解析，因此 HTTP 错误、无效 JSON、`finish_reason=length` 和空 content 都可取证。没有收到响应时明确显示 `no_response_received`。

Codex CLI transport 记录实际 stdin prompt、严格输出 schema、命令参数、stdout/stderr 和 answer 文件；它不能观察 CLI 内部发送给 provider 的 HTTP 请求，报告明确说明这个范围。CLI 超时而没有完整输出时也不会伪造响应。

原始内容经过脱敏，JSON 字符串可能被格式标准化；它是内容证据，不是 HTTP 字节级抓包。不会记录 HTTP 认证头或子进程环境；敏感字段、Bearer token、传入 API key 和配置中的凭据值会被替换。文件权限为 0600，目录 0700。`model-calls.jsonl` 等公共元数据只保存引用，不复制 prompt 正文。

这些文件仍可能含业务数据，应留在 macmini 的忽略目录，不能提交 Git。可设置 `JEV_CAPTURE_MODEL_ARTIFACTS=0` 关闭捕获，调用记录会标注 `capture_disabled`。只读诊断接口使用固定文件白名单及 run 边界校验，不提供任意文件浏览。

## 生命周期及手动运行

注册状态放在输出目录旁的 `.run-status/`，避免破坏 LongSeq 的“输出目录必须为空”要求：

```text
setup → running → agent_complete → grading → cleanup → finished/failed
```

`finished` 表示评分和环境清理已走完，不表示 benchmark 成功；成绩以有效官方 grade 为准。失去原进程且没有终态的状态标为 `interrupted`，使用 PID 和进程启动时间防止 PID 复用误判。

`/api/launcher` 的 `running` 包含注册的手动实验，`activities` 列出其 run、阶段和 PID。旧实验仅能保守识别直接 CLI/script 启动且绝对 `--output` 位于 runs 根目录中的进程；相对输出路径、其他包装启动或缺失注册的历史进程不保证发现。重启和启动 benchmark 前仍须同时检查 launcher 与手动进程/slot 占用。

## 当前交付验证状态（2026-10-05）

macmini 已恢复临时直连：原域名在开发机解析到 `198.18.0.97` 后于 SSH 握手阶段断开，公共 DNS 查询返回的真实地址可以连接。连接保留原域名的 HostKeyAlias，并成功匹配已有主机密钥；没有修改 SSH 配置或关闭主机校验。8768 隧道已恢复。

先确认待修改远端文件与基线一致，再同步本轮改动；随后核对 115 个源文件/测试/运行配置的 SHA-256，没有不一致。通过 `scripts/test_macmini.sh` 完整远端回归：**739 passed, 3 skipped**；源码和本轮修改测试的 ruff 检查通过。全 tests lint 另外存在 8 处既有样式问题，集中于未修改的上游验证 fixture 和旧测试；本轮未改动该验证器内容。

确认 launcher 和手动实验都空闲后，重启了 macmini 的 Studio（8768）。真实历史 baseline-47 的 HTTP 摘要返回 **3,979 字节**，保留有效官方 4/15 分、停止原因、两次 length 截断及空响应；单页 readback 查询返回 **1,494 字节**并提供下一页 cursor。输入/响应缺失明确保留，不补造历史证据。

按既有 push-before-benchmark 流程，将实现提交并推送（`9b84471`），然后自动重跑 `saas-longseq-business031-20261005-ds-baseline-48`。保留官方 DeepSeek `deepseek-flash` 全 DS baseline、business_031 原任务、resume-03 memory、resume-02 UI 恢复点和既有预算，使用全新输出目录。

该次重跑因环境启动失败结束：Twenty 在 **360 秒**内未通过 31004 端口的就绪检查（最后错误为 `RemoteDisconnected`），容器仍停在数据库 setup/migrations；HRMS 就绪耗时约 459 秒。**0 个模型调用、0 个动作，官方分数不可用、data_valid=false**，不能计作模型 0 分或成功率对比。总环境耗时约 650 秒，清理完成且 `cleanup_error=null`，slot 0 的容器已清空，launcher 回到空闲。

本次真实运行验证了手动实验从 setup、cleanup 到 failed 的状态和缺失评分表达；真实模型输入/响应捕获未进入执行阶段，已由远端模拟 transport 测试覆盖，仍需环境恢复后做一次有效模型运行验证。单元测试和历史 HTTP 读取验证不替代新的有效 benchmark 成绩。

清理后还修正了状态扫描的开销：不再遍历 preview、model-artifacts 等大产物目录，终态记录也不逐一查询 PID。相关远端回归 **53 passed, 1 skipped**，lint 通过；实际隧道请求 launcher 约 **0.226 秒**，失败 run 的 **3,185 字节**部分摘要约 **0.128 秒**。此改动仅影响 Codex/Studio 的状态查询，不改变 agent 的输入、动作或模型请求。

## 环境冷启动验证（2026-10-06）

新增可选 `--saas-startup-timeout 900`：只覆盖本次所选应用的就绪等待秒数，默认仍遵循上游配置。原始/实际等待上限记录在 manifest 和 environment 的 `startup` 中；不修改上游 apps.yaml、任务、评分器、fixture hash 或 agent 的运行预算。启动失败也记录实际 `setup_s`，不再误报为 0。此项是环境启动实验，不能视为已证明 Twenty 迁移问题修复。

相关 macmini 回归 **79 passed**，本轮源代码和测试 lint 通过。下一次有效运行仍保留官方 DeepSeek `deepseek-flash`、business_031、resume-03 memory、resume-02 UI 恢复点、600 actions / 1,800 seconds 及原 context 上限；全新输出目录用于验证真实调用证据、官方评分和清理。

实现已推送为 `69a9d6e`。本次首次 Git 提交因沙箱无法写 index.lock 而失败，实验启动后立即完成提交和推送；因此本轮未严格满足 push-before-launch 的时间顺序，但启动前已验证 115 个运行文件的 SHA-256 与测试源码一致，推送内容和运行内容相同。

`saas-longseq-business031-20261006-ds-baseline-49/saas-bench-business_031` 已完成官方评分和清理：**4/15（26.7%），data_valid=true，strict_success=false**，与上次有效 baseline-47 相同。58 个动作、69 个 cycle，agent 耗时 860.37 秒；setup 73.35 秒、verification 3.63 秒、总环境流程 982.08 秒，cleanup_error=null，launcher 空闲，slot 0 容器清空。Twenty 本次 72.4 秒就绪，HRMS 28.0 秒、BigCapital 14.1 秒；没有用到额外等待窗口，不能据此断言 900 秒上限修好了前一次迁移停滞。

官方通过项为已提交的 Employee Separation、三个正确指派的 exit activities、Vendor；未完成 settlement journal、payment、Twenty tasks 和 note。停止原因为 `readback unresolved after policy context overflow; no resubmission`。cycle 69 的事件记录 policy 从 151,622 字节压缩至 48,237 字节，仍超过 48,000 上限；候选页缩小后走 required readback fallback，等待额度耗尽而停止，没有重复提交。该 cycle 未发起新的 policy HTTP 请求，不应把本地 context overflow 说成 DeepSeek API 拒绝。

本轮 **92 次调用全部为官方 api.deepseek.com / deepseek-flash**，92 份请求、92 份响应证据均捕获；finish_reason 全部 stop，没有空响应和 transport 错误。首个 resume 请求的 trusted_goal 与 task.json.objective 相同，哈希为 `28f8182189c063e9b7202312b2086b9c31ebaa2eae84446830663070061eb503`，与 manifest 一致；证据文件权限 0600、目录 0700。CLI 和 Studio HTTP 实测可以通过 `/data/messages/1/content/trusted_goal`、`/data/body/choices/0/finish_reason` 等 pointer 选取历史字段，字符串分页返回 next_cursor；无需读整份 memory 或 trajectory。

最终部分报告保存在 macmini 的 `/tmp/jev-baseline49-summary.json` 和 `/tmp/jev-baseline49-step69.json`，分别为有界摘要及停止 cycle 的一页证据；step 页仍有 next_cursor，不能把这一页视为该 cycle 的完整记录。可用文档前述 scp 命令取回，或重新生成；临时目录不是长期归档。本次观测 HTTP 曾超时，最终确认旧 SSH 隧道失去响应，替换本轮创建的隧道后 8768 接口恢复；没有重启运行中的 Studio，也未触碰 8767。

这次有效实跑完成了历史模型输入/响应及终态取证验证，但没有提高任务分数；下一步成功率优化应调查 protected context 的最小表示和 pending readback 确认机制，不能把更完整的观测误报为 agent 能力提升。
