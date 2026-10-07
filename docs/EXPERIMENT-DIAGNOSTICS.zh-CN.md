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

## baseline-49 失败机制与修复验证

历史 readback 的真实输入显示 Save 后返回 Vendors List，但新 Vendor 不在可见行中。模型引用列表标题、URL 和 New Vendor 控件，返回 pending；页面 loading=false。官方评分后来确认 Vendor 已保存，这份隐藏评分不能用于 agent 的运行中确认。因此有两个独立问题：本地 protected policy context 超过上限；静态列表上的一次 pending 判断被缓存，等待耗尽，缺少截止时的新观察复核。最初把 policy 超限归因于 before_controls，baseline-51 后核查发现此归因不成立：Memory.context() 不输出该字段，snapshot 压缩在 policy 的实际路径中没有生效；readback 的 visible_control_delta 压缩则有在线证据。

本轮给 pending.before_controls 和 readback 的 visible_control_delta 增加无损共享字段/重复 context 表示，只在实际变小时采用，原始 archive、当前证据、任务、schema、待确认键及执行 Action 不变。使用真实捕获请求做重新序列化验证：80,345 → 58,924 字节，329 个 evidence ID 全保留，解码后与原数据完全一致；这不是新的在线模型成绩。

context overflow 的 readback 等待截止时再观察一次，只额外做一次 required review，仍不重放原写入。未确认就保留 pending；静态页面未显示目标记录时不允许把 pending/unknown 当成功。测试覆盖同一 pending 的模型调用有界、截止后不继续重试、Save 只执行一次，以及新鲜证据确实确认时才解除 pending。

按原 provider/model、business_031、resume-03 memory、resume-02 UI 和预算自动重跑 baseline-50，保留 baseline-49 的 900 秒环境等待设置。是否提升成绩、是否越过 Vendor 保存后的停止点，须以新实验官方评分和清理后的结果判断。

最终源码在 macmini 的完整回归 **751 passed, 3 skipped**，所有本轮变更源码和测试 lint 通过。

baseline-50 已完成评分和清理：0/15，data_valid=true，19 actions / 22 cycles / 314.02 秒，39 次 DS 调用均有证据，无 transport 错误、空响应或 length 截断，cleanup_error=null。第 13 个 cycle 的反馈 JSON 语法错误已通过修复恢复；真正停止点是第 22 个 cycle 的 `pre-bound input is not an authorized literal candidate`，未到 Vendor 保存阶段，因此不能用这次成绩判断 context 修复收益。

选中的实际候选是第三行 User 的空字符串 FILL，描述为 `Clear combobox: User | 3 2 results found | value=Pooja Malhotra | grid=ge887bdc16465416b; row=3`。生成器加入 grid/row 身份，bind_input 的清空校验却只接受不带后缀的描述，误拒绝了自身合法候选；该分支在前一个版本也相同，是既有 harness 不一致。现把当前控件描述统一给生成和校验使用，继续要求当前控件可编辑、启用、非只读、非隐藏值及精确的 Clear 身份。身份或能力改变时拒绝旧候选。

远端相关回归 **259 passed**，lint 通过。用当时原候选和原 observation 做只读校验已通过，0 模型调用、0 浏览器操作。推送后自动启动全新 baseline-51，模型、任务、恢复点和所有预算保持不变。

## baseline-51 与有界列表查证实验

baseline-51 已完成有效评分和清理：**4/15（26.7%）**，strict_success=false，63 actions / 135 cycles / 1,535.65 秒，cleanup_error=null。117 次 HTTP 尝试均为官方 DS，其中同一 cycle-8 feedback 的两次 timeout 用满 120 秒调用额度，随后恢复；115 份响应无 length/空内容。成功执行了此前误拒绝的行级 Clear。后续 Vendor Save 仍只显示 18 行旧记录；cycle-135 readback 的捕获响应明确返回 unknown。policy 压缩后 50,467 字节仍超过 48,000，本地 fallback 请求 59,080 字节，低于 baseline-49 的 76,531 字节。context 压缩并未带来分数提升；135 个 cycle 包含 planning cooldown，不能当作业务进展。

本轮只新增有界的主动列表查证，不更改 context、模型、恢复点或业务预算：成功 dispatch 的 Save/Submit 离开表单进入同源、同 tab、无编辑控件的静态 grid 后，unknown/pending 的截止路径可调用 DS 选择滚动、明确分页/刷新控件、当前 grid 的列头和其排序菜单。最多 4 个动作，每个动作后新观察并重评原写入。它不授权 Filter 编辑、字段输入、新建、删除、重新 Save 或跨 tab；当前表单、未知 dispatch、对话框、新 page_error 和另一 pending 均禁止该路径。选择器只拿原任务、原 pending 的字段快照、当前证据和有限候选，不输入整份历史记忆；所有实际操作仍由官方 DS 选择。

原 pending 和 consumed 写入键保持，排序/滚动的 receipt 不确认原 Save。没有新证据仍停止；不把离开表单、列表标题或官方隐藏评分注入成功判定。该实验检验的是“目标行不在当前视野内”的可能性，不声称能解决所有查询场景或已提高成功率。更复杂的查询和 protected policy 压缩仍需分别验证。

macmini 完整回归 **768 passed, 3 skipped**；本轮源码/测试 lint 通过。测试验证静态缺失记录到真实新证据才确认、4 次上限、未知 inspection receipt 不重试、原 Save 不重放、编辑表单/错误/另一 pending 不授权、实际 inspector 输入保留原任务和当前证据。使用 baseline-51 的 cycle-135 原 observation 和 pending 做只读候选探测，得到两种滚动及 4 个列头，0 模型调用/0 浏览器动作。推送后自动重跑 baseline-52；线上收益以其最终官方评分为准。

baseline-52（`2301d1a`）在 Vendor 前停止：**0/15，data_valid=true**，10 actions / 12 cycles / 122.51 秒，19 份请求和响应均为官方 DS，全部 stop，无 transport/length/空响应。cycle-12 的真实输出是 `{"choice":"a22","value":"Rajesh Kumar","outcome":"pending"}`；Decision 禁止 value 字段，JsonPolicy 没有格式恢复路径，直接抛 ValidationError。该值没有进入执行。总环境流程 223.41 秒，cleanup_error=null。未到列表查证，因此不能把此 0 分归因于该机制，也不能声称它提高了成功率。部分摘要在 macmini `/tmp/jev-baseline52-summary.json`。

后续修正补充 Decision 的明确 JSON schema 和原候选 ID enum，格式/不存在的候选/length 输出只允许一次重生成；再次不合规就停止。不执行、接受或带回模型额外生成的 value，不放宽绑定和业务确认。修复请求只包含无输入正文的 schema error，任务、当前观察和候选集合不变，所有调用仍进入真实请求/响应日志。

同时发现列表查证的 observation 绑定问题：unknown refresh 即使语义相同，也会产生新 observation_id，实际后端拒绝旧 ID 的动作。查证前重新观察；新语义先重做 readback，相同语义以新 ID 生成和执行候选。测试模拟实际后端的精确 ID 检查，而非仅用接受所有动作的 mock。选择器的 4,096 输出 token 上限与本地 readback 一致，保留全局时间/动作预算。baseline-52 运行期间，修正先在 macmini 隔离源码副本验证；它完成清理后才覆盖运行 checkout，并重新执行完整回归。

运行 checkout 上的最终完整回归 **774 passed, 3 skipped**，本轮 lint 通过。推送后自动启动 baseline-53，保留官方 DS deepseek-flash、原 task、resume-03 memory / resume-02 UI、600 actions / 1,800 seconds / 1,000 feedback calls 及 48 KB policy / 96 KB brain context；用新目录验证。

### baseline-53 最终结果与证据边界

baseline-53（`610bbed`）已完成评分和清理：**3/15（20%），data_valid=true，strict_success=false**。61 actions / 139 cycles / 45 feedback calls，agent 耗时 1,791.78 秒；最终 status=budget_exhausted，reason=`TimeoutError: `，不是 API context 超窗或余额不足。setup 60.75 秒、verification 2.70 秒、总流程 1,894.03 秒，cleanup_error=null。Vendor 还在 New Vendor 表单，未点击 Save，Journal、Payment、Twenty 后续工作未推进。新增列表查证 **0 次触发**；policy 的有界 schema repair 也未触发。这次不能证明这两项提升了成功率，也不能把未到触发点的低分归因于查证机制。

119 次 HTTP 尝试全部是官方 api.deepseek.com / deepseek-flash；114 份响应全部 stop，无 length 或空内容。5 次 timeout 均发生在 dynamic_feedback（cycle 47 两次、104、131、136），无响应证据明确为 no_response_received。累计模型请求 latency 约 **1,632.71 秒（agent 时间的 91%）**：full feedback 790.72、policy 812.21、input helper 11.43、readback 18.34 秒；其中 timeout 累计 324.51 秒。另有 59 条 planning_scope_wait，记录的请求等待总计 116.34 秒；这个数是日志计划的 sleep 时长，不是独立测量的完整冷却 wall time。cycle 数因冷却增长，不能当作有效业务步数。

第 39 个 cycle 执行 report reload，41 确认，42 模型再次选择 reload；页面语义改变使新动作键可用，DOM ID 本身已从 key 排除，不能把这个重复归因于 ID 变化。第 124 个 cycle 模型先探测空表单的派生 Display Name 菜单，之后等到 129 再继续填源字段；这类低效阶段选择和 readback 等待占用时间，但没有在这个节点终止。130/131、131/132 的菜单变化来自源字段产生实际新选项；核查后没有证据把它们判为纯 DOM handle 变化的误触发，因此未据此修改语义哈希。最后完成邮箱填写后，136 请求新规划，剩余时间内超时并进入冷却，最终整个运行截止；保存后查证未被测试到。

119 份捕获请求的 trusted_goal 均与该次 task.json.objective 完全一致；49、51、52、53 的任务文字相同，protocol.digest 哈希均为 `28f8182189c063e9b7202312b2086b9c31ebaa2eae84446830663070061eb503`。启动前验证 116 个文件，保留原恢复点和预算。这是同一 task 的续跑实验，仍不能作为 DS 的总体成功率上限。模型输出格式/低效探测、调用延迟、硬阶段交接和时间预算混在一起；目前证据优先支持优化 planning/policy 的调用粒度和阶段交接效率，不支持仅凭此分数归因于 DS 业务推理能力不足。input helper 仅 11 秒，不是本轮主要耗时来源。

macmini 的 `/tmp/jev-baseline53-summary.json` 与 `/tmp/jev-baseline53-step136.json` 均限制为 6 KB，可按需取回；step 仍可能有 next_cursor，不能视为完整 cycle。原始运行产物留在忽略目录，不进入 Git。

## 阶段首步直接交付实验（2026-10-07）

baseline-53 的阶段规划已输出经过能力、范围和 consumed 校验的 `stage_entry`，但首步仍要再次请求 policy 选择。新增全 DS JsonPolicy 的首步交付：仅在原规划 observation_id、document_version、页面语义、environment 和 scope generation 都一致，且没有 pending / pending_writes / write handoff 时，在当前有限候选中匹配唯一首步。一次规划最多交付一次，在 dispatch 前花掉资格；等待、replan 和方向/候选歧义回退原 policy。Jev policy 不启用该路径。

fill/select 只交付未绑定值的候选，仍由原 DS input helper 产生和校验值；不从阶段文本直接执行 value。后续动作仍由 policy 选择，新的观察不会复用旧资格。Save 的 receipt 不证明成功，pending、fresh readback、业务写入 checkpoint 和禁止重提交机制均保留。日志 `stage_entry_selected` 标明源自 `validated_stage_planning`、跳过一次 policy、尚未确认动作；实际 DS 指令来自前一个已捕获规划请求/响应，不生成虚假的 policy 请求。

对 baseline-53 的同 cycle 规划首步与实际 policy 选择做只读有限对照，25 处唯一首步全部匹配后续选择的 operation / target。这个探测没有模型调用和浏览器操作，不是重跑，也不能当作实际节省耗时或得分。真实新实验还需检查交付次数、调用量、官方评分和清理。

macmini 完整回归 **790 passed, 3 skipped**；随后补充“交付后错误 input value 仍被拒绝”测试，最终新模块 **17 passed**。运行源码自完整回归后没有变更。本轮 lint 通过后按已有 push-before-benchmark 流程自动重跑 baseline-54，继续官方 api.deepseek.com / deepseek-flash，business_031 原任务、resume-03 memory / resume-02 UI，600 actions / 1,800 seconds / 1,000 feedback calls 和原 context 预算。单个续跑任务的成绩不代表总体成功率。

baseline-54（`1f549cb`）已完成评分和清理：**2/15（13.3%），data_valid=true，strict_success=false**，16 actions / 17 cycles / 274.54 秒，stop=`uncertain mutation; no resubmission`。24 次官方 DS 调用全部 stop，无 timeout、length、空响应；阶段首步交付实际触发 **7 次**，其余 10 次 policy 仍正常调用。full feedback / policy / input / readback 的累计 latency 分别约 119.04 / 131.11 / 4.32 / 10.43 秒。没有 planning cooldown、context fallback 或列表 inspection。这个更短的运行在离职保存后提前停止，不能把总耗时减少当成效率或成功率提升；官方成绩未提升。

捕获证据定位了明确的异步查证路由缺陷：cycle-17 的 policy 与 readback 使用 trajectory 行 124 的旧 `/new-employee-separation-...` 帧，正文仍为 `Not Saved`；readback 的真实响应（attempt `a80f29bedcb34f8d8d180dd93355168a`）返回 unknown。约 22 秒后行 128 的 fresh observation 已进入 `/HR-EMP-SEP-2026-00001`，正文显示 Ananya Reddy、Draft、Submit。但 `unknown_readback_refreshed.changed=false`，因为旧实现只接受同 URL 的新语义，误把保存后的路由变化排除；没有用新帧再次查证就停止。官方 2/15 和仅保存的 Draft 是一致的，后续 Submit、Employee 状态及其他应用未继续。

新增修复允许成功 dispatch 的 pending 在同 scheme/origin、同 tab、授权 URL 上因路由改变触发重评；仍最多刷新一次。跨源、跨 tab、未知 receipt 或无新语义不会释放路径。刷新不确认动作、不清 pending/consumed、不重放原 Save；下一次 loop 必须从新帧重新检查并获取 readback。用上述两个捕获帧做只读离线探测，修复返回 reassess=true，pending 保留，confirmed=0，0 浏览器 dispatch / 0 模型调用。远端回归 **798 passed, 3 skipped**，相关 130 项与 lint 通过；推送后按原配置自动重跑 baseline-55。摘要与 step-17 部分报告留在 macmini `/tmp/jev-baseline54-summary.json`、`/tmp/jev-baseline54-step17.json`，均最多 6 KB。

### baseline-55 与截断修复额度

baseline-55（`08ef946`）已完成有效官方评分和清理：**4/15（26.7%），strict_success=false**，60 actions / 70 cycles / 1,036.86 秒，stop=`feedback failed schema/evidence checks after one repair; no action replayed`。setup 111.87 秒，总流程 1,198.29 秒，cleanup_error=null。离职提交的 cycle-32 查证为 business_commit；官方通过 Employee Separation（docstatus=1）、三项正确分配的活动、Vendor 检查。Journal / Payment / Twenty 未完成。这是与历史最好 4/15 持平的一次同任务续跑，不能证明总体成功率提高；官方 Vendor 检查通过时 detail 的 display_name 实际是 `Ananya Reddy`，而 label 是 `Ananya Reddy - Ex Employee`，不能把该分数当成每条原始要求都已满足。

阶段首步交付实际 **19 次**，policy 仍有 47 次尝试；20 feedback、12 input、5 readback、1 inspection，共 85 次均为官方 api.deepseek.com / deepseek-flash。所有历史请求 trusted_goal 与 task 原文一致，digest 仍为 `28f8182189c063e9b7202312b2086b9c31ebaa2eae84446830663070061eb503`。累计 latency：feedback 301.26、policy 634.83、input 13.80、readback 53.84、inspection 3.29 秒。1 次 policy timeout；0 planning_scope_wait，3 次 policy context readback fallback；列表查证实际点击一次 Display Name 列头，原 Save 保留。Save 的这次查证走 pending / 等待后确认，新增同源路由 unknown-refresh 分支未触发，因此它的实测因果收益尚未建立。

最终 cycle-70 的两次 readback（`5ec35dd7610a4643a610712f51f189d7`、`049ab00db8d54d5fa37b278bffbab7a3`）均 finish_reason=length、content 为空。两份真实 request 的 max_tokens 都是 4,096，response usage 的 completion_tokens_details.reasoning_tokens 都是 4,096；hidden reasoning 用尽输出额度，未产生答案。请求约 59.6 KB，低于 96 KB brain context 限制；不是本地输入超限、余额不足或“Vendor 不存在”的模型有效判定。第二次仅附加格式诊断而重用相同输出上限，不能解决这个截断机制。

新增调整只针对 `readback response truncated`：首请求仍 4,096，唯一一次修复为 8,192 token。其他 JSON/schema 错误不增额度，第二次仍截断就停止。保留原全局时间、尝试次数、证据引用校验和原 pending；不接受 reasoning 或空/截断内容为业务证据，不增加不受控 retry。相关远端 **112 passed**、lint 通过；完整回归及 push 后按原 provider/task/恢复点/全局预算自动重跑 baseline-56。唯一新增实验变量是上述反应式 readback 修复输出额度。

本轮 macmini 完整回归 **801 passed, 3 skipped**。输出额度测试验证两份请求除 schema_error 与 max_tokens 外任务/当前帧/证据/原 transition 保持一致，最多两次请求，连续 length 仍保留 pending 并停止，普通 schema repair 仍用 4,096，0 浏览器 dispatch。

### baseline-56：局部恢复有效，最终得分仍未提升

baseline-56（`43b42ee`，启动前验证 119 个文件）已完成有效官方评分和清理：**4/15（26.7%），strict_success=false**，77 actions / 88 cycles / 1,403.99 秒；stop=`feedback failed schema/evidence checks after one repair; no action replayed`。setup 87.26 秒、verification 2.51 秒、总流程 1,547.99 秒，cleanup_error=null。通过项与 baseline-55 相同：已提交离职记录、三项正确活动、Vendor；Journal / Payment / Twenty 仍未完成。实际 display_name 仍是 `Ananya Reddy`，不能把官方 Vendor 通过等同于其后缀要求完全满足。

阶段首步交付 **20 次**，0 planning cooldown，4 次 policy context readback fallback，1 次列表 header inspection，16 次 grounding rejection。107 次尝试均为官方 DS，同一原任务 digest / 恢复点 / 全局预算，历史 trusted_goal 无不一致。21 feedback / 17 input / 59 policy / 9 readback / 1 inspection，累计 latency 约 408.20 / 19.12 / 821.30 / 114.50 / 3.70 秒；1 policy timeout、3 length/空答案。原 Save 没有被重放，新 inspector receipt 没有确认原业务写入。

提高修复额度在 cycle-30 有真实成功证据：attempt `818d36779e674bcf8621e43f4b4a55aa` 首次 4,096 token 全为 reasoning、length/空内容；`abdac1d3c75f4757b6bb40aea23d824f` 的捕获 max_tokens=8,192，4,624 reasoning + 有效 pending JSON，finish_reason=stop，总输出 4,682 token。它没有把 Save 确认为成功，而是让等待/查证继续，随后进入 Submit。这证明局部格式恢复路径有效，不等于得分提升。

最终 cycle-88 仍在保存后的 Vendor 列表查证。`54c9e9f715314925a585f61a907b537b`（4,096）与 `83cd47bbfbd147aeb97f3b39c71702ba`（8,192）均 length、空内容，reasoning_tokens 分别恰为 4,096 / 8,192。后者约 59.6 KB / 22,760 输入 token，低于本地 96 KB brain context 上限；这次仍是输出推理额度耗尽，而非模型给出有效“保存失败”判定。不能用 reasoning 正文当作结果，也不能因官方 Vendor 已得分而解除运行时 pending。

三次本轮实验（54 / 55 / 56）得分为 2 / 4 / 4，历史最好仍为 4/15。修复了确定的重复首步选择、同源异步路由忽略问题，并实测恢复了一次截断；**没有证据证明总体成功率提高**。下一项需要单独验证目标相关的结构化 readback 证据包、按需查证及缺失记录定位；保留原任务、关键 pending 和前后状态来源，明确省略范围，不能靠无限增加输出额度或 retry 来替代证据。实际列表数据是否缓存、是否需要同页重新加载仍是待验证假设，尚未作为事实或修复收益。

部分报告：macmini `/tmp/jev-baseline56-summary.json`（最多 6 KB）和 cycle-88 调用/事件可按 cursor 展开；报告与真实请求产物均留在 Git 外。运行源码自 801 passed / 3 skipped 完整回归后保持不变。

### 列表回执去重与按需分页实验

baseline-56 cycle-88 的实际捕获请求约 59,590 字节 / 22,760 输入 token：同一份 18 行列表在正文、行控件、control delta、逐格证据中重复，329 个证据 ID 又重复进入 schema。两次输出全部耗在 hidden reasoning；这支持减少重复输入的实验，不证明列表缓存或记录未保存。

仅对成功 dispatch 的 Save/Submit 等写入、离开表单后的同源同 tab 静态 grid 启用行级证据包。保留原任务、约束、原 pending 字段快照、前后来源和当前状态；每页最多 6 行，按字段值的字面匹配优先取回，其余保持原顺序。匹配只作检索提示，不确认身份或保存。行引用是原 observation 的精确连续子串，单条最多 1,200 字符；过长单格明确计入省略，不能跨省略内容拼造引文。表格值以行证据表达一次，当前非行控件、状态和警告继续保留。

模型可返回当前包给出的 next_cursor 请求同一捕获帧的下一页；最多 3 页，共享一次格式修复，修复不会重置页数。每次请求计入 feedback 和模型调用预算。未提供的行是未看见，不是不存在；取页时禁止同时 confirmed。请求不存在的 cursor、引用未提供页的证据均拒绝；请求第四页停止并保留 pending。该路径不产生浏览器动作，不解除 consumed，不重放 Save，也不能标记整个任务完成。对话框、可编辑表单、未知 dispatch、新 page_error 或跨源仍用原查证流程。有界浏览器 inspection 仍由 DS 从原有限候选选择。

使用 cycle-88 的原 task、transition、observation 通过实际 JsonFeedback 和请求投影做只读探测，原请求 59,590 字节，三页分别 **20,101 / 20,126 / 20,155 字节**，每页 49 个引用；任务、字段快照、current_page 与原请求完全相同，0 模型调用、0 浏览器操作。单次约缩小 66%，三页累计大小仍接近旧请求；不能据此声称延迟或成功率已提升。需要用相同官方 DS、任务、恢复点和全局预算重跑 baseline-57，检查实际分页次数、输出截断、官方成绩和清理结果。

本轮 macmini 完整回归 **817 passed, 3 skipped**，最终源码/测试 lint 通过。新增验证包括三页完整覆盖、迟出现的字面匹配优先、精确原始引文、过长字段省略不跨段拼接、无效 cursor/未提供引文拒绝、格式修复不重置三页限制、pending 和 consumed 保留，以及复合行引文通过原确认 guard；未知 dispatch 仍无法确认。推送后自动重跑 baseline-57。

### baseline-57：输出恢复，分页控件误判阻断后续查证

baseline-57（`6e73443`，启动验证 121 个文件）已完成有效评分和清理：**4/15（26.7%），strict_success=false**，65 actions / 77 cycles / 1,008.81 秒，stop=`uncertain action after context readback; no resubmission`，setup 69.24 秒，总流程 1,128.07 秒，cleanup_error=null。官方通过项仍是提交离职记录、三项正确活动、Vendor；Vendor 的实际 display_name 仍为 `Ananya Reddy`，Journal / Payment / Twenty 未完成。94 次尝试全部官方 DS，原任务 digest、恢复点和预算保持，捕获请求 trusted_goal 无不一致；1 RemoteProtocolError、1 policy timeout，92 份响应全 stop，0 length/空答案。21 feedback / 14 input / 50 policy / 8 readback / 1 inspection，累计 latency 约 294.92 / 15.46 / 638.99 / 28.40 / 1.06 秒；21 次阶段首步交付、0 planning cooldown、2 context fallback、12 grounding rejection。

cycle-76 的新列表包真实请求依次提供 cursor 0 / 6 / 12，18 个可见行全部取完，字面匹配行数为 0，49 个引用/页。实际 input_tokens 6,032 / 6,035 / 6,045，output_tokens 560 / 881 / 1,626，全部 stop，模型按需取页后返回 unknown，没有确认或重放原 Save。随后模型选择滚动一次。这建立了该例中有界输出恢复的证据，未建立成功率提高；总耗时缩短也不能全归因于包压缩，业务路径和 provider 延迟不同。

cycle-77 的原观察新增一个匿名 native combobox：value=20，options=[20,30,50,75,100,150]，非 editable；列表底部可见 `Previous / 1 / 2 / Next / Page size / Showing 1 to 2 of 40 entries`。现有 guard 将任何 selectable 都判为表单，故仅滚动一次后便禁用压缩包和剩余查证，回退到 64,027 字节 / 24,421 input_tokens 的原 readback，得到有效 unknown 后停止。当前三个 errors 均为 blocked_request socket.io，没有新 page_error；不能把停止归因于页面异常。已有离职后续财务记录存在，但当前仅看见 18 个旧行；不能把局部缺失等同于全库缺失。

下一项修复只让明确命名的分页尺寸控件不阻断列表观察：浏览器将一个 select 周围的精确可见 `Page size` / `Rows per page` caption 关联为 name（最多三个局部祖先，单一字段，隐藏 caption/其他文字不匹配）。guard 同时要求非 editable、非 required、无 form/row context、无 grid/row 身份、当前值属于至少两个正整数选项。匿名数字框和业务 Quantity/Currency 等仍阻止该路径。该控件本身不进入 inspection 候选，不授权 SELECT 或字段写入；原最多四次 inspection、pending/consumed 和无重提交规则保持。静态 readback 与 inspection 共享同一 guard，避免判断分叉。远端相关 **162 passed**，lint 通过；完整回归和 push 后自动重跑 baseline-58，仍保留原配置。部分报告在 macmini `/tmp/jev-baseline57-summary.json`。

分页控件修复的最终 macmini 完整回归 **831 passed, 3 skipped**。浏览器测试使用实际渲染 DOM，验证局部可见 caption、隐藏 caption 和业务 Quantity 的区别；guard 测试验证匿名、required、editable、row、form context、非数字选项和未观察值均拒绝；inspection 测试验证允许后续列头/Next/滚动，但不产生 page-size SELECT，也不确认原 Save。

### baseline-58：当前页自链绕过阶段范围

baseline-58（`64e312f`）已完成有效官方评分和清理：**0/15**，18 actions / 20 cycles / 231.51 秒，stop=`readback unresolved; no resubmission`，setup 67.01 秒，总流程 342.78 秒，cleanup_error=null。27 份响应全 stop，0 transport 错误、length 或空答案；没有 Vendor、列表包或 inspection。因此未验证分页修复，不能把 0 分归因于该尚未触发路径。部分报告在 macmini `/tmp/jev-baseline58-summary.json`。

真实失败动作是当前草稿的 `New Employee Separation` 面包屑 link，href 与当前 obs.url 完全相同。当时阶段规划要求填写第三条活动，候选 guard 已过滤掉 55 个其他动作，但全局 navigation 例外无条件放行非行级 link，让这个自链绕过阶段范围。点击后 5 次有界查证无法取得有效新语义；cycle-20 首次输出违反证据要求，修复后的实际响应为 confirmed + 三个当前引用，但原 visibility guard 仍拒绝确认，没有保存或提交。不要以模型声称 confirmed 当作运行时确认，也不能因这一类无变化点击扩大所有业务写入的确认条件。

新增窄修复：只有 href 不等于当前 obs.url 的 link 才能走全局 navigation 例外；自链仍可由阶段规划明确列为 click 控件后使用。不同页面、query、fragment 导航保持。候选生成前范围过滤与 perform 前范围复核共享该规则，避免派发这个无计划的自链再等待到截止。它不自动确认、不重试或删除 pending。测试验证未授权自链不进入阶段候选且不派发，而明确规划后允许；其他实际导航仍可用。完整远端回归和 push 后自动重跑 baseline-59；任务、模型、恢复点、预算不变，新增实验变量为上述 stage/navigation 规则。若到达列表，可同时验证前一轮未触发的分页标签修复，但总体分数不能单独归因于其中一项。

最终 macmini 完整回归 **832 passed, 3 skipped**，lint 与相关 44 项检查通过。新测试最初使用 about:blank fixture，未授权比较用导航的 origin，造成预期错误；改为授权的实际 HTTP URL 后通过，运行源码没有为此放宽 origin 检查。baseline-58 的 27 份历史 trusted_goal 均与原任务一致，task digest 不变。

### baseline-59：越过 Vendor 查证，仍受时间预算限制

baseline-59（`265ede8`，启动验证 121 个文件）已完成有效官方评分和清理：**4/15（26.7%），strict_success=false**，78 actions / 103 cycles / 1,780.82 秒，status=budget_exhausted，最后 reason=`TimeoutError: `。setup 66.96 秒，总流程 1,894.35 秒，cleanup_error=null。已提交离职记录、三项正确活动和 Vendor 通过；Journal、Payment、Twenty 未通过。Journal 已开始填写 Reference 和第一行账户搜索但没有完成保存。官方 Vendor detail 仍显示 display_name=`Ananya Reddy`，不是原后缀要求；不要把 4 分等同于全部已完成项都满足全部细节。

127 次尝试全部官方 api.deepseek.com / deepseek-flash，原 task digest / 恢复点 / 全局预算保持，历史 trusted_goal 无不一致。3 policy timeout，124 份响应均 stop，**0 length、0 空内容、0 invalid_feedback**；29 次阶段首步交付、0 planning cooldown、5 context fallback、13 grounding rejection、1 越界 switch_tab 被拒绝后重新规划。30 feedback / 18 input / 65 policy / 13 readback / 1 inspection，累计 latency 约 **665.41 / 17.86 / 997.78 / 51.83 / 6.72 秒**；合计约 1,739.59 秒，占 agent elapsed 约 97.7%。这轮停止由剩余时间及最后 policy timeout 主导，未再出现 baseline-55/56 的输出推理额度耗尽。

局部收益有真实捕获证据：cycle-79 的三页全部 unknown，DS 选择 Display Name 列头，原 Save 和 pending 保留；cycle-80 仍三页 unknown，下一次 fresh observation 发生变化，先重评而非继续派发 inspection。cycle-81 `ddecbdc9403e4153a84bcffaed0470f4` 的实际输入有 1 个字面匹配行，将真实 row-3 引文优先放入首页；捕获响应为 confirmed + 三个引用，随后原 transition guard 接受并建立 write checkpoint。引文显示 Display Name=`Ananya Reddy`、Company Name=`Ananya Reddy - Ex Employee`、Receivable balance=$0.00；这只确认该局部保存效果，不证明全部原字段要求。7 次 paged readback 的 input_tokens 约 5,973–6,178，output_tokens 670–2,164，全部 stop；加一个 inspector，总 latency 约 47.26 秒。原 Save 未重放，菜单/列头 receipt 未直接确认业务写入。

本轮没有出现带 Page size 的 observation，走的是列头/异步 fresh-frame 路径；**分页尺寸 guard 的线上收益仍未验证**，仅有 DOM 和 guard 回归证据。当前页自链未再次触发早停，但单次不同路径实验不能证明这项修复带来的整体成功率增量。cycle-82 捕获规划明确选择返回 HRMS 做只读报表验证，随后回到 BigCapital 打开 Journal；这是实际 DS 规划，不是 harness 强制返回。减少规划/动作选择往返、无变化等待和重复验证，并在当前观察中保留可行动的依赖与未完成义务，是后续效率实验的重点；不能将未确认的写入直接视为完成，也不应把隐藏评分或任务特例注入模型。

本轮三个结果为 baseline-57 **4/15**、58 **0/15**、59 **4/15**，历史最好仍为 4/15；证据包已实测恢复有效输出并越过 Vendor 查证停止点，**尚无最终成功率提高的证据**。运行源码自最终 832 passed / 3 skipped 后保持不变。部分报告在 macmini `/tmp/jev-baseline59-summary.json`、`/tmp/jev-baseline59-step81.json`（各最多 6 KB），可按 cursor 和请求 artifact 指针展开；完整产物与凭证不入 Git。

### 成功率优先试验：baseline-60 / 61

`954797d` 增加可选 DS 独立文本框输入序列：最多四项，逐项原输入辅助、范围检查和精确新观察回读；任何其他页面/值/控件变化即撤销，不批量提交。macmini 完整回归 **862 passed, 3 skipped**，lint 通过，push 后验证 118 个 source/test/config 哈希启动。模型仍为官方 api.deepseek.com / deepseek-flash，任务、resume-03 memory / resume-02 UI、1,800 秒及其他预算均保持。

baseline-60 在恢复 UI 的 derived-reselect 阶段因 `ElementHandle.click: Element is not attached to the DOM` 返回 unknown 停止；未重放该动作。0 模型调用、0 agent 动作，官方状态检查 **0/15**，setup 120.41 秒，总流程 184.78 秒，cleanup_error=null。它是恢复失败，未验证输入序列；不能隐去该端到端失败，也不能把它归因于未执行的模型机制。

相同源码与配置的新环境补跑 baseline-61：有效官方 **3/15**、strict_success=false；37 actions / 45 cycles / 1,321.84 秒，stop=`needs_attention`，原因是 llm_policy 三次尝试超时，运行总预算未耗尽，pending 保留、无动作重放。setup 89.16 秒，总流程 1,468.42 秒，cleanup_error=null。58 尝试（13 feedback / 8 input / 36 policy / 1 readback），累计 6 个 Policy TimeoutError；**0 input_sequence_armed/selected**，尚未进入 Vendor，不能宣称输入序列的线上效率或成功率收益。

具体阻塞在 Employee Exits 报表刷新：cycle-40 捕获阶段指导明确要求只读 reload，随后待确认动作是 `button: reload (icon control)`。页面重新加载后可能展示相同内容，普通语义变化检查无法证明刷新；pending 又阻止耗尽的验证阶段转向独立工作，连续 Policy 请求与重试耗时。当前 memory 的 pending 仅用于定位，历史指导见 trajectory line 251 `/feedback/next_goal`，实际模型输入应沿该 cycle 的 request artifact 展开，不能以最新 memory 代替。

下一项独立变量增加浏览器新文档证据，仅确认已派发、受只读阶段授权的 Refresh/Reload UI 效果，验证义务和业务写入确认保持。相关真实浏览器与保护检查通过后，完整回归、push、精确源码验证，再以原配置在新目录 baseline-62 重跑。结果必须按有效评分、整项成功、耗时和实际请求变化评估，不能以不再等待 reload 当作任务成功。

新文档修复最终 macmini 回归 **886 passed, 3 skipped**，lint 通过。真实 HTTP 页面测试验证相同内容 reload 的 timeOrigin 改变、无需 Policy 查证、验证仍保留；负例覆盖 Save/Submit、业务表单、行内按钮、弹窗、未知派发、同文档、旧观察、不同 URL/tab/frame、HTTP/运行错误和缺失文档标识。上下文测试曾因断言 24 KB 却使用默认 48 KB 而超出断言 13 字节，现明确配置 24 KB 后通过；运行预算没有扩大。

### baseline-62：进入 Journal，搜索输入被清空

`1e8017d` push 后以 119 个哈希验证源码、原配置重跑。有效官方 **4/15**、strict_success=false，64 actions / 82 cycles / 1,004.25 秒，stop=`needs_attention: uncertain mutation; no resubmission`；总流程 1,129.61 秒，cleanup_error=null。103 尝试（25 feedback / 16 input / 48 policy / 13 readback / 1 inspector），0 transport 错误，累计请求 latency 974.17 秒。Vendor 查证完成后进入 Journal。**0 input_sequence_selected、0 fresh_browser_document**，所以这轮没有证明两项新机制的线上收益；与 baseline-59 同分更快但停点、执行范围不同，不能把少调用或较快结束归因为成功率/效率提高。

最后一次变更是 cycle-80 的 FILL，row-1 Account 的 `textbox Search...`、e198、value=`Rent`，receipt=ok，duration=0.06765 秒。cycle-82 新观察与再观察均显示该字段为空，Policy 与必要 readback 均 unknown，保留 pending 并停止。实际历史输入可查 `model-artifacts/e25c27224093439db3e9bfe86c550e87.request.json` 的 `/data/messages/1/content/last_transition/action`；动作记录为 trajectory line 511。仅由该症状不能证明浏览器实际按了 Tab；trace.zip 不能作为有效 ZIP 读取，这项历史键盘证据缺失需保留。

当前执行器对非 combobox、非 type=search 的 native input 填值后自动 Tab。真实浏览器的表格内 type=text、placeholder=Search... 且 blur 清空查询的复现，修复前四种搜索名称均丢失查询，修复后保留 Rent、焦点与选项；普通字段和文本日期控件仍 blur 提交。新增窄修复按精确渲染搜索名称保留焦点，记录是否跳过自动 blur，不放宽回读或重放未知输入。下一轮 baseline-63 只新增此变量，仍需有效官方评分验证真实收益。

搜索焦点修复 macmini 完整回归 **894 passed, 3 skipped**。最终导入格式整理后，相关真实浏览器、输入序列与浏览器回归再次通过，lint 通过；source/test/config 哈希在 push 后启动前重新验证。测试复现使用文本日期控件，原生 type=date 未暴露 fill 能力，不据此宣称扩展了原生日期支持。
