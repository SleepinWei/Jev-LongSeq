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
