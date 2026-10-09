# 自研 harness：全 DS 与 DS＋Jev 对照

本轮只比较自研 LongSeq harness 的两种 policy，不包含 browser-use。
目标是检查最终有效官方评分、整项成功和耗时，而非以请求更快或局部无报错代替成功。

## 实验条件

- 执行主机：macmini；任务：SaaS-Bench v1.1 business_031。
- 官方 DeepSeek `deepseek-flash` 规划；全 DS 组的动作 policy 同样使用该模型。
- 混合组动作 policy 为 `api.typesafe.ai` 的 `jev-latest`。
- 两组串行使用 slot 0，每组初始化全新环境，不使用历史 UI / memory checkpoint。
- 两组使用相同源码，启动前核验 139 个源码、静态资源、测试和配置文件哈希。
- 600 actions / 600 cycles / 1,800 秒；规划间隔 12；候选上限 250。
- DS context 1M tokens，Jev 48K tokens；原有 byte 参数仍是压缩软目标。
- observer-only 官方过程评分启用，过程分不提供给被测 agent。
- 第一组完成官方评分与环境清理后，才允许第二组启动；每次启动重新检查
  launcher、手动 benchmark 进程、slot 容器和源码一致性。

| 组 | run | 官方有效评分 | 整项成功 | agent 用时 | actions / cycles | 停止原因 |
|---|---|---|---|---|---|---|
| 全 DS | `saas-longseq-business031-20261009-v11-ds-pair-78/saas-bench-business_031` | 0/15 | 否 | 1,275.74 秒（21.26 分钟） | 68 / 84 | 清空输入两次与计划不符，未派发，needs_attention |
| DS＋Jev | `saas-longseq-business031-20261009-v11-hybrid-pair-79/saas-bench-business_031` | 0/15 | 否 | 1,803.40 秒（30.06 分钟） | 58 / 302 | DS 规划 TimeoutError，budget_exhausted |

两组 lifecycle 均为 finished，data_valid=true，cleanup_error=null。2026-10-09
08:13 UTC 后复核 launcher_running=false、无手动 benchmark 进程、slot 0 无遗留容器。
任务正文、版本、fixture、verifier、镜像、预算、恢复点、tuning、源码与规划系统指令
哈希、过程评分配置逐项相同；policy 及其模型、context 角色差异是本次比较变量。
两组过程评分从 baseline 到 final 都为 0/15，未出现有效净增分。

临时双组启动器、哈希清单及状态保存在 macmini `/tmp/jev-ds-jev-pair-20261009*`。
未修改被测 harness；未重新安装 browser-use。此前相关远端回归已通过，
本次只对已测试源码进行哈希核对及 benchmark preflight。

## hybrid-76 的已确认故障链

历史 run 已完成有效官方评分：0/15、整项失败、185.16 秒、32 actions / 40 cycles；
清理完成。最终停止为 DS 官方 API 的 HTTP 402，捕获响应明确为
`Insufficient Balance`。本轮启动前只读余额接口已恢复可用，未更换 provider/model。

402 之前另有独立错误，不能把全部失败归因于余额：

1. cycle 37 的规划文字要求填写 `Separation Begins On`，随后重新观察 Company；
   `inputs` 只绑定日期，但 `stage_controls` 同时授权日期、Company、Employee 的 fill。
   自然语言指导与可执行权限不一致。
2. cycle 37 已填日期；cycle 38 的新观察确认日期为 `2026-06-30`。
   Jev 却选择了 Company 控件 `e4312`。
3. 捕获的 DS 输入助手请求中，唯一保留的控件确实名为 `Company`，
   `selected_action` 也指向 `e4312`；没有 Company 的 `planned_input`。
   控件的 form context 同时包含 Employee、Company 与 Separation Begins On。
4. DS 返回 `{"value":"2026-06-30"}`。第一次派发因 stale 被拒绝，
   cycle 39 新观察后复用该值，成功将日期填入 Company。
5. cycle 40 出现 Company 的 `No results found`，随后的 DS 读回请求遇到 402。

这证明存在动作选择错误、输入生成错误以及缺少字段与值绑定校验。
相邻字段混入 context 是潜在诱因，但不能据此证明模型内部误判的原因。
观测中保留了正确控件名，并非完全没有必要观测。

## 批量执行及重复验证

hybrid-76 有 3 次 `execution_groups_armed`、3 次 cancelled、0 次 completed。
登录组中密码输入成功后观测值为 `[redacted]`，组校验继续将该值与计划中的
明文比较，触发 `confirmed group value changed`。这是隐藏值表示与验证契约不一致，
不应将其当作密码实际改变，也不能据此确认密码字面值。

同一 run 选择 Employee 后，cycle 28 首次出现页面脚本错误
`fields_dict` / `refresh_field`。cycle 28–31、33–36 多轮等待静态页面。
这些错误与 Company 未自动填充在时间上相关，但现有捕获仅含错误消息，
缺少调用栈，尚不能证明具体应用回调的因果关系。

新全 DS 组也出现 UI 关闭操作的多轮 pending 读回。cycle 25 的动作选择
耗时 43.94 秒，输出 8,189 tokens，其中 reasoning 8,177；cycle 29 的规划
耗时 61.82 秒，输出 11,277 tokens，其中 reasoning 9,891。
这是实际供应商 usage，不能解释为 context 达到 1M 上限。

cycle 44 的 DOM 仍是旧搜索结果，同 observation_id 的回放帧在约 0.42 秒后
已显示 Employee Separation 新结果；cycle 45 的新 DOM 也包含这些结果。
截图为异步采样，不是原子前后快照。此例证明存在异步结果窗口，未证明持续观测缺失。

## 两组共同卡点：Company 真实必填，观测与恢复契约不完整

cycle 78 的真实观察中，Company 为 `role=status`、空值、`read_only=true`、
`required=false`；当前页面没有脚本异常。cycle 81 的实际捕获系统指令要求：
任何空的只读字段必须有非空值或字段专属的 optional 标签，否则阻止 Save/Submit。
DS 随后明确解释，没有 optional 标签就不能将 Company 标为 not_applicable，
于是再次查询 Employee，试图修复派生字段。

在 macmini 用真实 cycle 78 观察做无浏览器、无模型的只读复现，即使使用空 Memory、
不加入任何历史 UI 必填拒绝，`write_prerequisite_diagnostics` 仍返回
`write_derived_field_unresolved: Company`。这隔离了无条件 guard，不代表重建了
完整历史 memory。限定证据保存在 `/tmp/jev-ds78-derived-guard-partial.json`。

这项阻塞不能归咎于 DS 自行发明规则。`workflow_dependencies` 将空派生字段
称为 inspection hints，并声明空值本身不证明必填；规划系统指令及执行 guard
却要求所有空只读字段必须解决，存在一般契约不一致。

但进一步只读检查本次 HRMS 容器的 `employee_separation.json` 后发现，Company
实际定义为 `fieldtype=Link, reqd=1, fetch_from=employee.company`。这是实验分析侧
证据，没有提供给被测 agent。**不能把这次 Company 卡点认定为可选字段误阻塞，
也不能承诺解除 Save guard 即可成功。** 源字段定义仍不覆盖动态 property setter、
运行时定制和最终服务器验证，尚未执行绕过 guard 的保存试验。

实际 trace 的 Company 为只读 display/status，required=false。当前 DOM 提取器只
读取原生 required、aria-required、标签末尾星号及标签 ::after 星号；字段定义中
reqd=1 不等于页面有可见必填标记。因此这里首先是观测中 required=false 的语义
不完整：它不能证明可选。尚不能单凭 schema 指认提取器漏掉了当时可见的星号。

全 DS cycle 80、83 两次重选同一 Employee 后 Company 仍空；cycle 84 的实际
规划也指出同值重选可能没有触发真正 value change，提出先 clear 再重选。
混合组 cycle 54 已填完三条活动，cycle 235 再次查询、选择同一 Employee，最终
cycle 302 仍然 Company 空白、Not Saved。两组缺少有效的依赖修复完成条件：
反复查询同值不能保证 fetch 已重触发，也没有及时转向 Employee 来源检查或保留
草稿后推进独立任务。Company 为何未取到值，现有 trace 尚不足以区分应用回调
失败、源记录缺值与前端显示缺失；旧 run 的脚本异常不是新两组的确定根因。

## 全 DS 最终停止点与效率

全 DS 已完成有效官方评分和清理：0/15、strict_success=false；68 actions / 84 cycles，
agent 1,275.74 秒，cleanup_error=null。三条退出活动在草稿内填写，未保存；
其他应用任务未开始。这解释了局部操作推进却没有官方分数。

cycle 84 在处理上述 Company 阻塞时，规划要求先清空 Employee，再重查来源。
实际输入助手请求的 `planned_input.value` 为 `""`，schema 允许空字符串。
两次 DS 返回均为 `HR-EMP-00007`，触发两次 `planned_input_mismatch`；
停止为 `input helper output invalid after one repair; no input dispatched`。
因此最后这项确有模型未遵守明确绑定的证据，不是协议禁止空字符串。
同时，已明确的清空操作仍经由未绑定 fill 候选重新请求模型求值，暴露重复语义决策。
限定证据在 `/tmp/jev-ds78-clear-input-partial.json`，应验证直接选择现有显式 Clear
候选或复用校验后的绑定能否减少此类错误，不能任意放行不一致的输入值。

| 请求类型 | 次数 | 累计请求耗时（秒） |
|---|---:|---:|
| DS 规划 | 25 | 648.23 |
| DS 动作选择 | 62 | 531.67 |
| DS 输入辅助 | 17 | 41.26 |
| DS 必要读回 | 5 | 11.38 |
| 合计 | 109 | 1,232.55 |

累计请求耗时约占 agent 用时 96.6%；规划与动作选择占请求耗时约 95.7%。
日志记录 23 个 wait，22 个直接 stage_entry（跳过 policy）。3 个执行组启动、
1 个组完成、2 个取消；组完成不等于持久化成功。已收到的 109 个响应均为 stop，
没有 transport 异常、空正文或 length 截断；单次 context 达到上限不是本轮停止原因。

另外，cycle 59 的全 DS 实际动作选择了 User=Guest，cycle 60 的新观察确认错误，
随后重新解析为 Rajesh 的邮箱。这项错误最终修复，没有直接触发终止，但说明
只开放正确字段的候选仍不够：link option 还需要校验本阶段目标实体。
第三行活动在最终 text 中未展开，但 cycle 84 的控件 value 明确包含
`Conduct exit interview` 与 Pooja 邮箱；不能只看 text 就认定第三行丢失。

## 混合组最终停止点与效率

混合组 cycle 54 已完成三条活动的草稿输入，随后围绕 Company 修复反复规划。
cycle 54、110、166、242 的每次逻辑规划各经历约 90.5 秒＋29.5 秒的两次 timeout；
cycle 299 最后一次调用只剩约 18.25 秒。合计 9 个 TimeoutError，均在 DS 规划。
规划失效后冻结旧阶段写操作是合理边界，但 120 秒重试额度加 120 秒 cooldown
使 224 个 cycle 只产生 planning_scope_wait，不能解释为业务执行了 302 步。

cycle 235 还有一次可恢复规划错误：DS 对 Employee 请求 click，而捕获的当前
能力只有 fill，stage_entry 也要求 click；运行时以 control_operation_unavailable /
stage_entry_not_executable 拒绝，无浏览器派发。第二次反馈改为 fill 后继续。

| 请求类型 | 次数 | 累计请求耗时（秒） |
|---|---:|---:|
| DS 规划 | 39 | 1,213.25 |
| Jev 动作选择 | 70 | 23.82 |
| DS 输入辅助 | 10 | 17.16 |
| DS 必要读回 | 4 | 16.97 |
| 合计 | 123 | 1,271.20 |

Jev 动作选择平均 0.340 秒，已显著快于全 DS 的 8.576 秒；但 DS 规划占混合组
请求耗时 95.4%、agent 用时 67.3%，规划等待又占用约 448 秒。混合组总体更慢，
两组仍零分，因此还不能证明混合方案提高了任务效率或成功率。
混合组启动 8 个执行组、取消 8 个、完成 0 个；登录密码的 `[redacted]` 比较错误
在 cycle 4 再现，其余多数在 checkpoint / 新观察错误时取消。多数已启动组仅有
一个动作，尚未实现用户希望的“DS 规划几组，Jev 连续执行几组”。

混合组最大超时请求估计约 19,939 input tokens，远低于配置的 DS 1M。
已捕获 timeout 请求只有 model、messages、response_format，没有实际发送
max_tokens / max_completion_tokens / thinking / reasoning_effort；16,384 的 output
reserve 是 context 计数参数，不能当作供应商输出硬限制。响应头很快返回，但
超时没有完整响应正文及 usage，不能断言是长 reasoning、服务延迟或模型违反
指令。也没有本轮 HTTP 402 或 context overflow 的证据。

两组官方失败检查一致：离职单不存在及三条活动不存在共 3 分；Vendor、Journal、
Payment 共 6 分；Twenty tasks 与 note 共 6 分，总计 15 分。评分检查持久化状态，
未保存草稿不计分。v1.1 第一项标签是 Employee Separation exists；不要额外把
该项解释为必须 submitted。其他应用尚未推进，故后续检查也全部失败。

## 后续需逐项验证的改动

1. 先验证 Company 取值与依赖恢复：保留必填/可选/未知的证据来源，不能将
   required=false 当作可选，也不能把全部空只读字段永远列作硬阻塞。验证
   clear → 查询 → 选择新鲜目标 → 新观察的真正 value change 路径，以及用户
   可见 Employee 来源检查。限定恢复次数；仍失败就保留草稿，推进依赖图中
   独立任务。不能用私有 DB、应用 schema 或评分答案指导被测 agent。
2. 填值动作要求当前字段的明确 binding，或显式授权的查询解析意图。
   单个字段缺少绑定时应重新规划；不能因值出现在任务正文中，就允许填到任意字段。
   保持一般字段规则，不写入 benchmark 的私有答案。
3. 将规划指导编译为有限执行组，区分只观察字段与授权改写字段；
   减少无意开放 Company 等无目标值控件；精确绑定输入后跳过重复的模型求值，
   link option 同时约束字段、行和目标实体，避免 Guest 一类合法候选误选。
4. 记录观察到的密码/隐藏值类型，修复初始空密码进入批次以及后续明文比较的矛盾。
   使用有限的填入证据，保持“不确认隐藏字面值”的边界。
5. 对静态 UI 的局部效果读回使用紧凑契约和有界重观察，减少重复动作选择请求。
   持久化 Save/Submit 的未知结果继续禁止重放；不能通过忽略 unknown 提高表面完成率。
6. 快速创建表单遇到应用脚本异常或依赖无法解析时，要求规划观察可见恢复入口，
   例如本轮 DS 在 cycle 53 通过 Edit Full Form 进入完整离职单并继续填写活动。
   此路径只说明有可行恢复，不证明整体成功。
7. 规划服务连续超时转为有界的恢复/停止，不长时间反复冷却同一个静态状态。
   分别验证紧凑局部修复请求、实际 API 输出/推理参数及更少规划调用，保持
   原模型、预算和明确的授权边界。恢复后再执行、未知写入不重放。

两组对照期间保持源码不变。上述改动应在对照完成后分别实现、远端验证并自动重跑，
用有效最终评分、整项成功和耗时判断收益。每组单次 business_031 运行不构成
全 benchmark 成功率估计。

当前 JsonPolicy 支持由验证后的 `stage_entry` 直接执行入口、跳过 policy 请求，
JevPolicy 没有相同的适配器标志。全 DS 22 次跳过，混合组 0 次；须分别统计，
不能将两种完整方法的结果当作只替换同一路径内模型的纯模型能力实验。

## 按需取证与结论范围

macmini `/tmp/jev-ds-jev-pair-final-compare.json` 是 5,077 字节的标准 compare 报告；
`/tmp/jev-ds-jev-pair-final-analysis.json` 保存筛选后的调用统计、超时尝试、官方
失败检查和最后表单字段。后者仅作按需取证源，应继续按字段拆分读取，不能默认
全塞入 Codex context。原始请求、response、完整 memory/trajectory 留在 remote runs，
没有提交 Git。本轮只增加本分析文档，没有改变被测 harness，因此不启动第三轮。

结论是：两组失败存在共同的依赖恢复问题、执行组契约问题与重复决策开销，也
存在已捕获的模型错误；Jev 的动作选择延迟优势已测得，但没有转化为官方成功。
不能由单任务各一次零分推导全 benchmark 成功率，也不能判定更换 DS 就能解决。
