# Jev 不执行与低置信度升级验证

company-82 的事件 215 在 confidence=0.29 时执行 Open Link，丢失离职草稿。
已有 request_replan 候选，但默认 confidence_threshold=null，没有启用动作拦截。

本次将 request_replan 明确描述为 NO ACTION：不操作浏览器，请 DS 重新思考。
动态 Jev 默认动作门槛为 0.5；显式 Budget.confidence_threshold 优先。低于门槛
或外部选择缺少 confidence 的浏览器动作，在 input helper、关闭弹窗、切换 tab
及实际 dispatch 之前拦截。内部基于当前语义身份的 stale-target 恢复不伪造模型
置信度。WAIT、翻候选页、完成复核和 request_replan 不受动作门槛阻挡。

policy_abstained 记录实际候选、confidence、threshold、observation_id、pending
保留状态；下次 DS 规划请求的 execution_feedback 包含该记录。pending readback
使用更窄的输入，先核对原有写入效果，不包含这份 execution_feedback。明确请求重新规划
也保留尚未确认的写入，不将“不执行”解释为上次操作成功。门槛和升级语义写入
manifest.policy_decision_gate，便于对照实际运行配置。

macmini 相关回归 **172 passed**，变更源码与测试 lint 通过。测试覆盖低置信度
click/fill、缺失 confidence、显式 no action、门槛边界、自定义门槛、连续放弃
的预算边界、未确认写入保留，以及现有执行组、阶段交付和模型调用统计。

自动重跑使用 fresh 输出目录 `saas-longseq-business031-20261010-v11-hybrid-abstain-83`。
保留 company-82 的 SaaS-Bench v1.1、business_031、DS 官方 deepseek-flash +
jev-latest、slot 0、600 actions / 600 cycles / 1800 seconds、1000 feedback calls、
brain_interval=12、250 candidates、context token 上限和无恢复 checkpoint。
本次仅验证动作放弃与置信度升级，不同时扩大执行组、导航去重或业务写入权限。
置信度不是正确率；company-82 的另一次错误选择 confidence=0.55，不能宣称
0.5 门槛会解决所有语义误绑定。结论仍须依据官方有效评分、严格成功和耗时。

## 最终验证结果

run：`saas-longseq-business031-20261010-v11-hybrid-abstain-83/saas-bench-business_031`。
源码提交 `c060b4e` 已在启动前推送，macmini 启动前核对了 142 个源码、测试和配置
文件。实验 code hash 为 `941baab8a44d23b14a0b06aabf6d687829e82537b3c2f0cdd5db85bfefcd9c64`。

| 指标 | company-82 | abstain-83 |
| --- | ---: | ---: |
| 官方有效评分 | 0/15 | 0/15 |
| 严格整项成功 | false | false |
| Agent 耗时 | 1003.323 秒（16.72 分钟） | 939.753 秒（15.66 分钟） |
| 浏览器动作 / cycles | 78 / 99 | 59 / 139 |
| 模型请求总数 | 134 | 118 |
| DS / Jev 请求 | 39 / 95 | 37 / 81 |
| TimeoutError | 1 | 2 |
| 停止原因 | invalid feedback exhausted | readback unresolved; no resubmission |

83 的 lifecycle 为 finished，completion_confirmed=true、grade_final=true，
官方 verify.py 返回 data_valid=true，cleanup_error=null；launcher 已空闲，slot 0
容器已移除。环境 setup 69.688 秒，最终评分 2.367 秒，完整工作流 1047.278 秒。

两次实验的 task、provider/model、预算、checkpoint 和 fixture/image/verifier 相同。
对照工具列出的差异为 code_hash 和 system_prompt_hash；该工具目前未比较新增的
policy_decision_gate，因此实际门槛变化另在此记录：82 未启用默认拦截，83 的 Jev
有效门槛为 0.5。单次实验耗时、请求减少不能单独归因于优化；停止位置不同，且 83
有两次 DS 超时。**本次证明了动作拦截和主动升级生效，没有证明最终成功率提升。**

## 不执行和升级的在线证据

共 17 条 policy_abstained：11 条低置信度浏览器候选被拦截，6 条为 Jev 主动
request_replan；全部 browser_action_dispatched=false。被拦截置信度为
0.48、0.37、0.42、0.38、0.32、0.34、0.43、0.44、0.41、0.33、0.31。
低置信度不等于候选必然错误，不能将所有被拦截的导航都计作避免了错误。

cycle 5 拦截 confidence=0.48 的 “Verifying…” click 并保留 pending；cycle 6
Jev 以 confidence=0.32 主动选择 NO ACTION。实际 DS 规划请求
`model-artifacts/f71ae81394bc4430b9c15c32ff4f96ad.request.json` 的
`/data/messages/1/content/untrusted_memory/execution_feedback/policy_abstention`
记录 jev_requested、request_replan、0.32、门槛 0.5 和未派发浏览器动作。
cycle 6 的 readback 请求 `136c1c5d69bc4a1599d5a6b7d7f79aad.request.json`
不含 untrusted_memory；这是不同请求类型的输入边界，不能声称所有 DS 调用
都包含放弃动作记录。confidence=0.29 的不执行要求另有回归测试覆盖。

## 仍未解决的阻塞

cycle 139 停在员工页面的 New Employee Separation 弹窗。待确认动作是填入
Separation Begins On=`2026-06-30`（实际 Jev 请求
`model-artifacts/3541934f00da4c0dadaf715a9a7bf1da.request.json`，指针
`/data/state/untrusted_memory/pending_writes/0/action`）。DS readback 表示 Company
已有 TechVista Solutions Pvt. Ltd.、Employee 为 HR-EMP-00007，但日期仍为空。
trajectory line 570 的 last_outcome=unknown、planning_result=not_applied；重新读取
后仍未确认写入，控制器保留 pending、禁止重放并停止。

DS 的 next_goal 要求确认日期后再 Edit Full Form，并禁止在活动行尚未填完时
点击 Open Link 或 Save。本次没有生成已提交的离职记录。现有证据可定位为日期
填入后的 readback 未确认，尚不能断言具体是原生日期控件、blur 或其他前端问题。

macmini 上保存了有限报告 `/tmp/jev-abstain83-final-step139.json`、
`/tmp/jev-abstain83-vs82-partial.json`、`/tmp/jev-abstain83-events-partial.json`。
step139 报告受 12 KB 限制，仅有 8 条记录，next_cursor=`1:117`，不是完整 cycle；
后续调查应按 cursor 和上述实际请求指针继续取证，避免整份轨迹输入上下文。
