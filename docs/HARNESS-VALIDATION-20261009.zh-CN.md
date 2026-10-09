# 输入绑定与 Company 恢复验证

验证目标是 SaaS-Bench v1.1 `business_031` 的最终有效官方分、整项成功和耗时。
一次局部错误消失、单元测试通过或请求变少，都不能替代这些结果。

## 第一项：精确输入绑定

提交 `ed47f5f` 已推送；macmini 完整回归 **1074 passed, 3 skipped**，本轮源码及
测试 lint 通过。运行前核对 140 个文件 SHA-256。仅 runtime 的 bind_input 新增
直接使用明确阶段输入的路径；规划指令、保存前置 guard、provider/model、任务、
环境初始化、memory 模式和预算保持原样。

只有同一环境、阶段 generation、location、dialogs、唯一字段/行身份、当前 Action
observation/document/tab/frame 和授权操作相符，且无 pending/unknown write、
无 fresh scope 要求，才能使用精确值。SELECT 仍必须匹配当前选项；InputValue 的
类型和长度限制仍检查。空字符串是明确的清空意图，不能用 truthiness 丢弃。
这条路径不确认业务保存，不自动增加动作，不改变 Jev 的动作选择角色。
不具备完整绑定的输入仍走原 helper 路径；这是替换重复求值，不是全面解决错字段。

历史 all-DS-78 cycle 84 的真实 Observation、反馈和 captured request 中
planned_input.value="" 被用于隔离阶段契约复现，结果精确为空字符串、0 模型
调用、0 浏览器派发、无新增 pending。没有假称重建了完整历史 memory。
限定报告：macmini `/tmp/jev-ds78-clear-binding-validation.json`。

| 组 | run | 状态 |
|---|---|---|
| 全 DS | `saas-longseq-business031-20261009-v11-ds-binding-80/saas-bench-business_031` | 有效 0/15；strict_success=false；1,803.73 秒；64 actions / 215 cycles；budget_exhausted / TimeoutError；清理正常 |
| DS＋Jev | `saas-longseq-business031-20261009-v11-hybrid-binding-81/saas-bench-business_031` | 有效 0/15；strict_success=false；1,803.67 秒；73 actions / 268 cycles；budget_exhausted / TimeoutError；清理正常 |

两组每次从新环境开始，无 UI/memory checkpoint；600 actions / 600 cycles /
1800 秒，brain_interval=12，DS 官方 deepseek-flash，Jev jev-latest，分别对应
上轮 pair-78 / pair-79。启动器为 macmini `/tmp/jev-stage-input-20261009.py`；
启动器每组前重新检查源码、launcher、手动进程、slot 及 API 可用性。
配对运行源码 hash 为 `97538cb3234efefecf1319d4c1b552c59be4712ceea791457cccb0a5cbdb3c3b`。

全 DS 的新日志记录 10 次 validated_stage_input、6 次 execution-group 输入、1 次
显式 clear、1 次 helper 路径。dynamic_input 从上轮 17 次降到本轮 1 次；全部
104 个模型尝试中 DS 规划 37、动作 policy 60、读回 6、输入 1，出现 6 个规划
TimeoutError。Company 修复未完成；随后保留草稿转向 BigCapital，但供应商表单
也未保存。官方最终仍零分，因此只证明输入求值减少，没有证明成功率提升。
总用时相比旧 run 更长；旧 run 提前输入报错，新 run 用完整 30 分钟预算，不能
把继续运行或多走一个应用解释为完成效率提升。限定 compare 报告为 macmini
`/tmp/jev-binding80-vs78-partial.json`。

混合组共 130 个模型尝试：DS 规划 35、Jev 89、DS 读回 6、输入 helper 0；
8 次 TimeoutError。执行组绑定 2 次、精确阶段绑定 9 次、观测 clear 1 次。
相较 pair-79，dynamic_input 10 → 0，规划尝试 39 → 35；Jev 70 → 89，
总模型尝试 123 → 130，累计模型调用耗时 1,271.20 → 1,396.87 秒。因此
消除输入 helper 也没有证明总请求、耗时或成本下降；不能只报一个好看的子项。
Company 始终空白，所有官方检查未通过；相较 pair-79，73 vs 58 actions 并未
产生有效业务分，耗时同样约 30 分钟。两组都没有证明最终成功率提升。
限定 compare：macmini `/tmp/jev-binding81-vs79-partial.json`；清空 cycle 204
限定证据：`/tmp/jev-binding81-clear204.json`。

## 第二项：Company 依赖机制的隔离验证

只读检查相同 fixture 的运行时数据库，Employee `HR-EMP-00007` 的 Company 非空；
运行时 Company 字段 reqd=1、read_only=0、fetch_from=employee.company、
fetch_if_empty=0。该证据仅供实验分析，没有提供给被测 agent。
不能据此放行未知派生字段、从数据库代填答案或把后台记录注入规划。

1. **已确认的必填标记遗漏。** all-DS-78 cycle 84 的回放帧显示 Company、Employee
   和日期旁的星号；Frappe `common/controls.scss` 的实际 content=" *" 带前导空格。
   当前 snapshot 将去掉引号后的内容与 "*" 精确比较，没有 trim。
   macmini 隔离渲染比较原实现和 in-process trim 变体：Company/Employee required
   均由 false 变 true，可选字段仍 false，隐藏字段仍不出现。仓库运行源码未修改，
   0 模型调用，0 真实 benchmark 浏览器变更。限定报告
   `/tmp/jev-required-marker-validation.json`；这证明标记提取问题，不证明最终得分。
2. **待真实实验验证的链接清空提交。** 当前 FILL 对 combobox 保留焦点；应用
   Link 的 input 处理仅查询建议，blur 在值与 last_value 不同才提交到 model。
   option 选择再调用 validate_and_set_in_model。全 DS binding-80 cycle 39 实际
   清空 Employee、之后重选相同员工，Company 仍空；这没有证明当时 model 的值。
   隔离联动表单中，clear → 查询 → 同值 option 的原路径保留旧 model、Company
   不更新；clear 后加入一次当前 DOM 绑定的原生 Tab，model 才清空，随后新鲜
   option 使 Company 出现。模型调用为 0，不触碰真实实验浏览器。限定报告
   `/tmp/jev-link-clear-validation.json`。这是事件机制验证，尚未重建历史 Frappe
   内部状态，不能把模拟页面成功当作 benchmark 成功。

配对实验期间保持远端源码冻结。必填标记 trim 和明确空 combobox 的原生
提交修正当时暂存于开发 checkout；在配对完成、核对远端改动、同步并通过远端
检查后才可用于新的真实实验。非空 link 查询继续保留焦点；clear 的 receipt 不
确认链接解析或持久化；原生 Tab 若失败必须返回 unknown，不能自动重试。

第二项源码已在 macmini 临时副本 `/tmp/jev-company-native-check-20261009` 同步
检查：150 passed，lint 通过；包含新旧 backend 的恢复兼容检查，新 backend
显式 clear 已发送原生 Tab 时续跑逻辑不再重复发送。该副本使用项目测试脚本的
JEV_REMOTE_ROOT 参数；当时正在运行的正式 checkout 没有改动。隔离检查阶段
尚未完成在线 rerun，不能把隔离测试成功写成真实 benchmark 成功。

第一项配对完成评分和 cleanup 后，已检查正式 checkout 待修改文件与冻结
基线一致，并同步上述五个源码/测试文件，核对 141 个文件哈希无差异。
源码包首次因 macOS tar 附加元数据被白名单保护拒绝，未覆盖文件；误启动的
旧源码 pytest 已停止，其 897 passed 不计入本轮修复回归。纯源码包随后通过
同步检查，正式完整回归重新执行。本轮修改文件 lint 已通过。
正式 checkout 完整回归结果为 **1081 passed, 3 skipped**；这些结果才是第二项
修复的正式测试证据。随后按既有流程提交推送，以 fresh environment 启动
`saas-longseq-business031-20261009-v11-hybrid-company-82/saas-bench-business_031`。
其 provider/model、任务、slot、无恢复 checkpoint、memory 模式和全部预算
与 hybrid-binding-81 相同；只改变必填标记提取和空链接输入提交/续跑兼容路径。
修复提交 `df81e60` 已推送。

### 第二项在线取证与最终结果

hybrid-company-82 的员工源页面以及快速录入表单均出现 required=true；
Company、Employee、日期的带空格 CSS 星号已在线正确提取。
cycle 38 的 DS 原始响应明确把 TVS 当 Company 搜索词，并要求再观察选项。
cycle 42 的真实 Jev 请求 `/data/state/untrusted_memory/brain_guidance` 保留
该指导；`/data/questions/action/criteria/a6` 仍包含 click e3315（Open Link）。
真实响应选择 a6，实际进入不存在的 `/desk/company/TVS`，没有选中有效公司。
cycle 43 规划又把 Company=TVS 当作应保留的字段值；搜索串非空不能证明
关联解析成功。观察修复不能解决候选导航过宽或搜索/已解析链接状态的混淆。
相关捕获请求/响应 attempt 为 `f983f6cc6bcc4192a7f210e71c87bfca`，
DS 规划 attempt 为 `450e231cb0294d4e8ac848ec2e34d7c7`；只读局部调用报告
为 macmini `/tmp/jev-company82-step42-calls.json`，不使用最新 memory 代替历史输入。

hybrid-company-82 已完成官方评分及 cleanup：**0/15，data_valid=true，
strict_success=false**；78 actions / 99 cycles，1,003.32 秒（16.72 分钟）。
停止原因为 `feedback failed schema/evidence checks after one repair; no action
replayed`，不是耗尽 1,800 秒预算；cleanup_error=null，slot 已释放。
运行源码 hash 为 `fd0541e63ace3df474d0d9001a530e2c96286e6cd1f5a6fcebfbc369cf8c33bf`。

| 验证 | 官方有效分 | 整项成功 | agent 耗时 | 结论 |
|---|---:|---|---:|---|
| 全 DS 精确输入 binding-80 | 0/15 | 否 | 30.06 分钟 | 输入 helper 减少，未提升成绩 |
| DS＋Jev 精确输入 binding-81 | 0/15 | 否 | 30.06 分钟 | helper 10→0，但总尝试增加，未提升成绩 |
| DS＋Jev 必填/清空提交 company-82 | 0/15 | 否 | 16.72 分钟 | 必填提取生效；清空路径未触发；反馈校验失败提前结束 |

本轮 134 个模型尝试：DS 规划 32、Jev 95、输入 1、读回 6；1 个 TimeoutError，
累计调用耗时 969.83 秒，134 份请求/133 份响应已捕获，无空响应。
精确阶段输入绑定 10 次、helper 1 次；明确空 combobox native Tab **0 次**，
不能用这轮评分评价未触发路径的真实修复收益。Company 搜索流程绕行后仍未
保存离职记录，三个应用任务都未获得官方有效分。

最终 cycle 99：trajectory line 514 和 516 两次拒绝均为
`stage_entry_not_executable`，element_ref=e438、operation=click；统一 reason
为 missing control / missing stage authorization / consumed action。一次修复
未改成可执行入口，随后停止，没有派发这两个被拒动作。这条统一诊断尚不能
独立区分上述三种具体原因，不能把它直接说成 DOM ID 陈旧或模型没有看见页面。
限定终态报告：macmini `/tmp/jev-company82-final-step99.json`，其初始页有 cursor；
已按 `0:516` 续读最终拒绝与 result。限定对照：`/tmp/jev-company82-vs81-partial.json`，
task_equal=true，配置差异只有 code_hash，两个官方 grade_final 均 true。

这次只验证到观察质量与局部绑定能力，**没有证明成功率、完成效率或成本收益**。
16.72 分钟是提前失败耗时，不能和成功完成耗时混淆。下一项应优先控制未完成
阶段的导航候选、区分链接搜索串与已选记录，并让执行组降级保留前置依赖。
先独立复现并限定修改，再用相同条件 fresh run 的官方终分、整项成功和耗时
验证；不能通过放宽保存 guard、注入数据库答案或继续加预算宣布成功。

## 已发现、尚未纳入本轮修复的顺序问题

hybrid-binding-81 cycle 71 的结构化反馈明确先填写日期，再点击 Edit Full Form。
trajectory line 365 的 stage_entry 是日期 FILL，execution_groups 也按此顺序；
line 364 因 dialog 存在而放弃 execution window，但宽泛 stage_controls 仍保留。
line 369 的 Jev 动作先点击 Edit Full Form，cycle 72/73 日期仍空。
这是执行组降级后丢失依赖顺序的证据；精确值绑定、必填标记和 native blur 都不
解决它。后续应让降级候选保留当前步骤依赖，或明确重新规划，而不是继续允许
后续导航绕过未完成的前置输入。为避免混淆本轮对照，本次不同时修改该机制。

cycle 62 另有 Company/Employee 不可用操作以及 group grounding 错误；cycle 71
首次模型 JSON 有非法控制字符，修复后成功解析。模型输出问题与 harness 的
降级顺序问题需分别统计，不能统称为模型能力不足。

## 回放进度条验证

修复仅涉及观察网页 `static/app.js`，不改变被测 agent、harness 执行或评分。
原来每个 slider input 同步渲染，重复扫描事件前缀、重建官方评分列表、更新
所有历史行，并立刻更换截图 URL。连续拖动造成大量过期截图请求。

现在以 requestAnimationFrame 合并输入，松手立即处理最终位置；同一事件的
状态复用，官方评分只在结果或过程采样边界变化时重绘，历史行只更新选中项。
截图请求合并、可取消且绑定选择 generation；过期响应和错误不覆盖当前画面，
近期六帧缓存有界并在切换 run 时释放。自动播放不使用手动拖动的延后窗口。

macmini 相关回归 **24 passed, 1 skipped**，lint 通过。测试覆盖慢截图、最终
指令/动作/DOM 对齐、过期成功和错误响应、缓存回退、run reset，以及原有
Studio 导航/CSP/DOM 标注。首次新增测试因 Shadow DOM 定位错误失败，修正
测试定位后通过；不把首次失败报告成产品行为。

对同一真实 `hybrid-company-82`（520 事件）执行 61 次 slider input 的只读
浏览器对照：旧版截图请求 **56 次**，修复版 **1 次**；两组最终均停在事件
215，显示对应 Open Link 动作及原阶段指令。输入循环耗时 360.4 vs 367.2 ms，
包含人工 3 ms 间隔；没有据此声称主线程耗时提升。该对照证明请求拥塞减少，
不代表 benchmark 成功率或所有网络条件下的播放流畅度已验证。
限定对照报告及截图保存在 macmini 的忽略目录
`output/playwright/replay-scrub-comparison-20261009.json` 和
`output/playwright/replay-scrub-fixed-20261009.png`。

## 比较边界

过程分始终为 observer-only。请求节省和字段修复单独记录，但最终结论依据官方
grade.data_valid=true、strict_success、earned/total、停止原因、耗时和 cleanup。
一次新环境试验不能估计全 benchmark 成功率；旧异步截图也不是原子动作快照。
