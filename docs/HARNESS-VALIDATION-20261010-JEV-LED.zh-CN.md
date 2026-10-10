# Jev 主导执行循环的验证

## 实验目标与边界

参照 abstain-83：DS 规划/反馈 27 次、累计 734.487 秒；DS 读回 10 次、19.390 秒；
Jev 81 次、26.968 秒。Agent 共 939.753 秒，官方有效评分 0/15，整项未通过。
本次验证减少普通页面/表单变化引起的 DS 规划，是否能提高最终有效评分和整项成功，
同时比较耗时与模型请求量。单次 business_031 实验不代表套件成功率。

新增实验开关 `JEV_FEEDBACK_MODE=jev_led`，记录在 manifest.tuning.feedback_mode
及 policy_context_profile。默认仍为 checkpoint；不是直接全局替换原流程。

DS 保留初始规划、Jev 的 NO ACTION/低置信度干预、必要的未确认动作诊断和最终复核。
普通 navigation_checkpoint、ui_checkpoint、draft_row_added、write_checkpoint、
stage_budget 不再自动规划。持续无进展、过期目标、必要派生字段异常或读回截止触发
controller 强制 NO ACTION。普通动作由 Jev 在当前观测上选择，DS 的旧 stage_controls
不再作为这个实验模式的控件白名单；实际任务权限、origin、控件能力、置信度 0.5、
业务前置条件、重复提交和 pending guard 保留。取消 DS stage_entry / input_sequence
自动交付，避免绕过 Jev 选择。原执行组继续作为可读的阶段指导，而非扩大任务权限。

缺少值的 fill/select 候选明确标记为 NO ACTION 补值请求。DS input helper 只准备值，
这一轮不 dispatch；Jev 必须在下一次新观测上重新选择带值候选。准备值只绑定到同一
环境、文档、tab、URL、dialog、控件身份及其他字段状态；依赖字段、控件格式或 options
变化即失效。低置信度候选在补值之前拦截。既有同阶段验证过的输入和原文引用值仍可复用。

Jev 每步保留原始 trusted_goal 和约束、当前观测、上一步动作、精确 pending 目标、
最近六条操作、可区分确认层级的业务检查点、未解决验证义务和台账、当前页关键证据、
旧节点有限提示及访问页面图。DS working_memory 和当前执行组是建议，不是证明。
过期规划明确标为 scope 不匹配。完整档案继续保存，DS 最终复核可读全部证据。
本次仍使用既有检查点/未解决义务及 DS 摘要维护剩余工作，尚未引入完整的任务依赖 DAG。

## 日期问题与有限恢复

83 cycle 134 的 Date fill confidence=0.96，输入 `2026-06-30`，Playwright receipt=ok，
之后 e5175 仍为空。fields_dict / refresh_field 异常在 fill 前已存在。
HRMS 镜像日期控件按用户显示格式 parse；历史观测未捕获输入格式，因此不能断言
是日期格式不匹配或 benchmark 环境缺陷，也不能把 Playwright ok 当作字段已接受。

本次观测新增 input_type、placeholder、validation_message，仅来自当前 DOM；不读取
应用内部模型或直接修改其数据库，不猜测或自动改写日期格式。

Jev 增加 not_applied 结果选项。只有 receipt=ok、同一文档/弹窗/控件、普通 editable
textbox 填入后稳定读回仍为空，且原来亦为空，controller 才能释放本地 fill pending；
每个字段只允许一次有限修复。不会记作成功或业务提交，不释放 Save/Submit、不释放
unknown dispatch、密码填入、非空的可能规范化值或换文档的控件。旧重复动作去重仍在。

## 验证与运行

远端相关检查通过后先 push，核验全部运行源码哈希，再检查 launcher、手动进程及 slot。
fresh run 计划为 `saas-longseq-business031-20261010-v11-jev-led-84/saas-bench-business_031`。
保留 v1.1/business_031、官方 DS deepseek-flash + Jev jev-latest、无恢复点、slot 0、
600 actions/600 cycles/1800 seconds、1000 feedback calls、4 planner calls、interval 12、
250 candidates、DS 1M token/Jev 48k token 和既有环境/评分器。interval 参数保持一致，
但本模式有意忽略其自动规划触发；这是实验差异。

本轮同时更改调度、Jev context 投影、补值交付与本地填入的有限恢复，是 pipeline 整体
试验，不能把结果提升独立归因于某个子改动。必须检查最终官方评分、严格成功、耗时、
请求量、stop reason、grading 和 cleanup；仅更少 DS 请求不能证明成功率提升。

macmini 相关回归 **429 passed**（45.70 秒），本轮源码/测试 lint 通过。
两项旧测试使用未声明 minimum_action_confidence 的 AsyncMock，补全模拟 policy 配置
及 .9 confidence，以测试其原本的验证拒绝/阶段权限分支；真实 Jev 的低/缺失置信度
拦截仍有独立回归。新增测试覆盖 DS 补值不 dispatch、新观测选择、依赖变化失效、
新弹窗执行、跳过普通规划、低置信度、有限字段修复、不释放提交及真实 Jev 请求字段。
启动前核验 **144 个源码/测试/配置文件**。

## 首次试跑 84：发现升级请求被覆盖

84 官方评分有效 **0/15，strict_success=false**，agent 291.660 秒，11 actions /
600 cycles，因 decision cycle budget reached 停止。604 次调用：DS 5 次（1 planning、
4 readback），Jev 599 次；无 transport 错误。setup 106.420 秒，verification 6.711 秒，
全流程 446.726 秒；finished、grade_final=true、completion_confirmed=true，cleanup_error=null。

cycle 65 在 People 页对 Employee 的 .42 候选被拦截，但下一轮 navigation_checkpoint
覆盖了 low_confidence，然后新模式跳过这个 checkpoint，使原升级请求也丢失。
同一 checkpoint 的存在还阻止 no_progress watchdog 触发。结果是在 People 页反复
选择且没有实际前进。此缺陷使 84 不能用于判断正确 Jev 主导方案的效果；其低 DS
调用数和较短时间不代表效率提升。完整源码在运行期间未修改，待它自然完成判分和
清理后再同步修复。

修复保留已经排队的显式 trigger，不被普通 checkpoint 覆盖；Jev-led 模式的
no_progress 不再因一个本会跳过的 checkpoint 而被排除。增加跨页面低置信度、
显式 NO ACTION 和跨页面无进展 watchdog 的端到端调度回归。
修复后 macmini 同组回归 **432 passed**（50.11 秒），lint 通过。

修复后的 fresh run 为 `saas-longseq-business031-20261010-v11-jev-led-85/saas-bench-business_031`，
保持 84/83 的任务、模型、预算、无 checkpoint 和环境版本，最终结果完成后补充。

## 第二轮 85：升级可达，但超时冷却空转

85 官方评分有效 **0/15，strict_success=false**，agent 725.156 秒（12.09 分钟），
23 actions / 600 cycles，decision cycle budget reached。294 次尝试：DS 28 次
（18 planning、9 readback、1 input），Jev 266 次；4 次 DS TimeoutError。setup 79.410 秒，
verification 1.965 秒，全流程 843.294 秒；finished、grade_final=true、
completion_confirmed=true、cleanup_error=null。

cycle 21/34 的 jev_requested 已实际触发 DS；cycle 193 的 Jev 实际请求为
`model-artifacts/9bf1d2c7920f4f89bc228e8f4ae56c65.request.json`，选项返回 request_replan
confidence=.79。但 DS 超时后的 planning_retry_after 仍有效，Jev-led 每轮清除
fresh_scope_required，越过了已有的冷却等待。结果是大量重复 NO ACTION 和降级规划，
消耗 cycles / Jev 请求，没有业务进展。85 同样不能用短于 83 的耗时宣称效率提升。

第三轮修复：冷却期间明确等待，不再调用 Jev/DS、不操作浏览器；截止后先重试已经
排队的 DS 干预，再恢复 Jev 选择。低置信度等规划超时在新模式下保持待处理范围状态。
新增等待不调用模型、冷却到期先干预及低置信度降级状态回归。

同时把新模式的 DS assistance schema 改为 next_goal、working_memory、inputs、
notes、evidence_requests、dependency_reviews，去除 stage_entry、stage_controls、
execution_groups 等 DOM 动作计划输出。实际当前能力仍供 DS 识别字段；计划输入只
能绑定到当次文档/弹窗内唯一匹配的 editable/selectable 控件，Jev 从新观测选择动作。
checkpoint 默认模式仍使用原 schema，最终复核/未知写入读回仍使用原证据要求。
这样避免新模式仍在生成和验证不会采用的旧动作计划。

fresh run 为 `saas-longseq-business031-20261010-v11-jev-led-86/saas-bench-business_031`。
这是冷却与 DS 输出简化的组合修复，不是单项消融；结果完成后补充。
macmini 最终相关回归 **448 passed**（41.86 秒），lint 通过；新增真实 DS schema/
计划值绑定的请求测试。默认 checkpoint 模式仍通过原反馈与阶段交付回归。

## 86 启动前：DS 官方余额阻断

实现先 push 为 `fb42552`，启动器通过全部 144 个源码/测试/配置哈希核验。
随后 DS 官方 `/user/balance` 返回 `is_available=false`，启动预检以
`DS official API insufficient balance` 阻断；`/tmp/jev-led-20261010-final-status.json`
记录 phase=blocked、arms=[]。**86 没有启动，没有动作、模型执行或官方评分结果**。
Studio launcher running=false、activities=[]；85 已完成正式 grading 与 cleanup。
没有改用其他 provider、改任务或加预算，没有启动重复试验。

余额阻断时，修复版只得到远端回归验证，尚未得到有效实跑验证。已完成的 84/85 均为 0/15，
严格未通过，且暴露了不同调度缺口；不能据其耗时减少声称 pipeline 提高效率或成功率。
需要 DS 官方余额恢复后，用已保存的 `/tmp/jev-led-20261010-final.py --check` 核验
模型/源码/slot，再以 `--run` 启动 fresh 86，继续跟踪到官方判分及清理。
如果源码随后变化，应重新测试、生成 hash 清单并 push，不能复用旧哈希宣称 exact source。

## 86：充值后继续验证

用户确认充值后，启动器重新确认 DS available=true、144 个运行文件哈希一致、
launcher/手动进程/slot 空闲，启动同一 fresh 86。2026-10-10 20:14:37（北京时间）
进入 setup，20:15:46 开始 agent；没有更换 provider/model、恢复点或预算。
与 83 的有界 compare 确认 task_equal=true，任务、模型、预算及 benchmark 环境
配置没有差异；差异为 tuning.feedback_mode、system_prompt_hash 和 code_hash。
以下是按需取得的历史证据，不以局部推进代替最终成绩。

- cycle 15 的 DS 求助指导实际应用后，Jev 点击 Ananya Reddy，求助闭环已可达。
- cycle 35 的快速创建弹窗存在 `fields_dict` / `refresh_field` 异常；cycle 54
  已到无前端错误的完整 Employee Separation 表单。不能据此认定历史日期失败的根因。
- cycle 59 的 Save 在 dispatch 前因 Company 必填项为空而被拒绝。cycle 61 的
  实际 DS 响应要求重新选 Employee 下拉选项触发 Company，而非直接填只读字段。
- cycle 65 的实际 Jev response：a4 / confidence=.36。a4 是 request_replan，
  不是浏览器动作。其实际 request 保留 planned_inputs 中 Employee=HR-EMP-00007；
  Employee 控件 e3719 亦显示该值、editable=true、popup_open=false。
  当时 NO ACTION、Save、Employee fill 的概率分别约 .37/.23/.24。
- 同一请求的 `untrusted_memory.loop` / `execution_feedback` 为 {}，
  guidance_matches_current_scope=true。源码在每轮前写入本地循环状态，但
  review 的 `memory.feedback = feedback.model_dump()` 会在随后选择前覆盖它；
  这是实际输入缺口，尚未通过消融实验证明其对分数的影响。
- `generate_dynamic` 不给 editable/selectable 控件生成 CLICK，Employee 有填入/
  清空路径，缺少直接打开其下拉的候选；这与 DS 的修复指导不匹配。它不证明整个
  表单不可操作，也不证明加入 CLICK 就必然解决 Company 派生问题。

当前运行源码保持不变。后续应分别验证：允许有当前控件身份和能力依据的 combobox
打开动作；在 DS 反馈替换后恢复当前本地循环/执行反馈；对重复 NO ACTION 建立有界
恢复及停止判据。每项仍须比较官方有效评分、整项成功和耗时，不能把不再报错或
求助可达当成成功率提升。

历史取证引用：cycle 61 DS response 为
`model-artifacts/5d3c7593867d4cfca675b8f58656550d.response.json`；cycle 65 Jev
request/response 为 `model-artifacts/32ce1538fcd84510ab6b6efefe55e7ea.*.json`。
有界 compare 保存于 macmini `/tmp/jev-led86-compare83.json`，原始产物保持在忽略的
run 目录中，不提交 Git。

### 86 最终结果

2026-10-10 20:46:21（北京时间）完成官方判分与清理，启动器 phase=finished，
grade_final=true、completion_confirmed=true、cleanup_error=null；启动器的最后
check_idle 通过，launcher/手动 benchmark/slot 0 无残留。
benchmark 子进程 exit_code=1 对应失败的任务结果，不代表漏做 grading 或 cleanup。

| 指标 | abstain-83 | jev-led-86 |
| --- | --- | --- |
| 官方有效评分 | 0/15 | 0/15 |
| strict_success | false | false |
| Agent 耗时 | 939.753 秒（15.66 分钟） | 1,804.140 秒（30.07 分钟） |
| DS 调用尝试 | 37 | 57（50 guidance、7 readback） |
| Jev 调用 | 81 | 95 |
| Actions / cycles | 59 / 139 | 36 / 100 |
| 停止原因 | readback unresolved; no resubmission | budget_exhausted / TimeoutError |

86 setup 65.718 秒、verification 3.062 秒、全环境流程 1,903.724 秒（31.73 分钟）。
152 次模型尝试中仅最后一次 DS guidance 出现 TimeoutError；151 份响应均已捕获，
DS 已完成响应的 finish_reason 全为 stop，无空响应，无 HTTP 余额不足错误。
cycle 100 的 planning_scope_wait 为等待冷却，未调用 Jev/DS，也没有重放写入；
因此 85 的冷却期间大量 Jev 空转现象没有在这个结尾重现。单个案例仍不能证明
所有超时路径均已修好。

全过程 16 份独立评分快照（含 baseline/final）都为 0/15；隐藏评分未反馈给 agent。
最终 cycle 96 的 Save 仍被本地前置条件阻断，诊断列出 Company 和一个名为
Open Link 的控件为空。后者是否被错误识别为必填字段需要单独核查其 role/DOM
归属，不能把该控件名称当成真实业务字段。表单未产生可计分记录。

结论：本轮完成了充值后修复版的有效实跑验证，但**没有提高分数或整项成功，
且比 83 更慢、模型调用更多**。这不是成功的效率改进，也不构成套件成功率估计。
NO ACTION 交给 DS 后仍允许重复规划同一状态，缺少下拉打开动作和干预后本地
context 缺口，应优先作单项修复/消融，而不是增加预算。上述后续项尚未修改或重跑。

## 87：停滞恢复与下拉交互实验

本节为 86 完成后的新实现，不改变上述历史结果。只在 `jev_led` 模式启用停滞
恢复：按当前路由、字段值及可见表格内容计算工作状态；新 DS 说明、证据 ID、
DOM handle 和下拉开合不算业务进展。返回旧状态仍保留计数，最多维护 128 个状态。
每个状态默认允许两轮常规 DS 干预，之后最多三次窄范围恢复请求；包括无绑定输入
导致的求助。存在未确认动作时保留 pending 并停止，不探测或重复提交。

恢复请求只让 DS 从当前可操作的有限候选中选一个或 stop，不再整阶段重规划。
允许打开命名下拉/菜单、滚动，以及选择与当前作用域计划值匹配且归属明确的选项。
排除 Save/Submit、任意输入/清空、导航和重复探测。模型返回后重新观察、核验候选
身份；页面已变则不执行旧提案。额度耗尽或没有有用候选时以明确 needs_attention
终止。工作状态改变仅刷新本地调度额度，不证明业务持久化；本机制不提供通用
树搜索、回滚或跨运行恢复的探索历史。

对于 NO ACTION / Save / Employee fill 约 .37/.23/.24 的分散概率，不适合依次执行
三个分支来试错。概率只作为歧义提示，不能绕过置信度、必填校验和写入确认。
更有价值的是先暴露缺失信息：打开 Employee 下拉、检查字段归属、选取任务已授权
的确切记录。需要写入的分支继续走原有执行/确认流程；低置信度 Jev 提案不直接执行。

同时修正以下缺口：

- editable combobox 增加 CLICK 打开候选；native SELECT 继续使用已有精确选项路径。
  通过新观察确认同一字段的 owned options 可见，只记录 combobox_opened_ui，
  不当作已选值、已派生 Company 或保存成功。选项确认允许留在同一活跃对话框。
- DS feedback 替换后保留当前本地 jev_loop / execution_feedback，不恢复旧 DS 授权。
- 必填元数据和保存前检查排除相邻按钮/链接，避免把 Open Link 当必填业务字段。
- 捕获合法候选的有限数值概率，保留真实候选含义；异常、未知或非有限概率丢弃。

尚未新增通用键盘打开适配或自定义非 editable listbox 展开路径；归属不清的选项
仍可能 NO ACTION/停止。多个改动合并在同一次试验，不能分别归因各自评分收益。

远端最终完整回归 **1,133 passed、3 skipped（89.78 秒）**，本轮源码/测试 ruff
通过。测试覆盖同状态反复干预、A/B 往返、pending 不重放、概率与实际候选对应、
fresh dropdown 归属/值/文档/异常校验，以及真实 Playwright 页面下拉打开和选项
填充 Company；局部 UI 测试成功不替代最终 benchmark 成绩。

启动前核验 macmini 的 146 个源文件/测试/配置哈希一致，DS 官方 API 余额可用，
Studio launcher、手动 benchmark 和 slot 0 空闲。新目录为
`saas-longseq-business031-20261010-v11-jev-recovery-87/saas-bench-business_031`。
保留 v1.1、business_031 原任务、官方 deepseek-flash、jev-latest、无恢复 checkpoint、
600 actions / 1,800 秒及其余原预算。最终结果待评分和清理后记录；目前不能声称
提高分数或整项成功。

### 87 最终结果与恢复解析修正

87 已完成官方评分和环境清理：**0/15、data_valid=true、strict_success=false**，
40 actions / 71 cycles，agent 550.715 秒（9.18 分钟），DS 28 / Jev 69。
setup 77.994 秒、verification 2.981 秒、全流程 671.716 秒；grade_final=true、
completion_confirmed=true、cleanup_error=null，启动器最终空闲检查通过。

cycle 68 的 Save 因真正的 Company 空值被拦截；没有再次列出 Open Link 假必填。
Activities 已有局部填写并不说明 Company 阻塞解除或记录已保存。cycle 71 在同一
工作状态累计两次 DS 干预后触发 jev_stall_detected，切换 dynamic_recovery_probe。
实际 DS 返回 choice=a29，候选为打开 Employee combobox；没有执行该动作，因为
reason 超过 schema 的 500 字符导致 ValidationError。模型说明中的“Save was
dispatched”与浏览器前置拦截证据冲突，不能把说明当作 dispatch 事实。

实际响应与候选证据为 `model-artifacts/74cb7663ffc6434e92a3455db77f6b50.*.json`。
cycle 32 另有一次 readback length/空 content，pending 保留后继续核查；本轮全部
97 次尝试都有请求/响应捕获，无 transport 错误。87 的较短耗时源于解析错误提前
结束，**不能声称有效率或成功率提升**，恢复动作在线效果还未得到验证。

后续仅修正恢复响应处理：reason 是非授权说明，解析时保留前 500 字符，完整原文
仍在 captured response；choice、额外字段、类型和候选范围继续严格验证。非法/
截断恢复响应有界地返回 needs_attention 并记录 recovery_probe_invalid，不派发、
不额外重试、不因日志说明错误把整个控制器变成未处理异常。模型 timeout/global
budget 仍走原有路径。远端针对性回归 23 passed，最终完整回归
**1,138 passed、3 skipped（125.34 秒）**，本轮 lint 通过。

修正后以全新目录 `saas-longseq-business031-20261010-v11-jev-recovery-88` 重跑，
任务、模型、无 checkpoint、预算及环境保持不变；最终评分和清理结果另行补充。
