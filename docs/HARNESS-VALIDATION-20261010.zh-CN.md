# Jev 不执行与低置信度升级验证

company-82 的事件 215 在 confidence=0.29 时执行 Open Link，丢失离职草稿。
已有 request_replan 候选，但默认 confidence_threshold=null，没有启用动作拦截。

本次将 request_replan 明确描述为 NO ACTION：不操作浏览器，请 DS 重新思考。
动态 Jev 默认动作门槛为 0.5；显式 Budget.confidence_threshold 优先。低于门槛
或外部选择缺少 confidence 的浏览器动作，在 input helper、关闭弹窗、切换 tab
及实际 dispatch 之前拦截。内部基于当前语义身份的 stale-target 恢复不伪造模型
置信度。WAIT、翻候选页、完成复核和 request_replan 不受动作门槛阻挡。

policy_abstained 记录实际候选、confidence、threshold、observation_id、pending
保留状态；下次 DS 输入的 execution_feedback 包含该记录。明确请求重新规划
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

新实验的官方评分及 cleanup 结果待运行完成后填写。
