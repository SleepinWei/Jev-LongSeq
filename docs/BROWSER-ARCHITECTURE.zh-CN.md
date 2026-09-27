# 浏览器长任务目标架构（设计规格，非全部已实现）

## 一、研究目标

给定相同网页观察权限、动作空间和预算，事件触发的强语义规划器＋Jev 局部策略，是否能：
提高长任务严格成功率，并相对每一步使用强模型降低每次成功的总成本？

长任务不只等于更多点击。分别控制参考动作长度 H、跨页面依赖跨度 D、页面干扰量 L、站点数与副作用风险。

## 二、三层架构

1. **任务层**：生成式 LLM；页面视觉信息必要时使用 VLM。维护目标、硬约束、子任务依赖和待探索页面队列。
2. **局部决策层**：Jev 从已绑定的动作候选中选择。输入当前子任务、相关事实、当前页面片段、少量近期事件。
3. **浏览器执行层**：Playwright / Browser Use / BrowserGym backend。负责元素映射、动作可执行性、等待、页面变化与证据采集。

研究执行后端优先 BrowserGym，便于统一 benchmark；实际应用可接 Browser Use 或直接 Playwright。不要依赖未经验证的库私有 API。
Browser Use 的默认 Agent 是一个完整智能体，不应该把它和 Jev 控制循环无意叠加；也不能假设 Jev Choice API 能直接替换生成式 LLM 接口。

当前起步工程直接使用 Playwright，以便隔离最小控制闭环，尚未接 BrowserGym/Browser Use。

## 三、页面观察协议

观察建议包含：
- `observation_id`, `tab_id`, `frame_id`, `document_version`。
- 当前 URL、页面标题、可见对话框、浏览器最近异常。
- 可交互控件：元素 ID、role、accessible name、当前值、enabled、所属表格行/表单、链接目的地。
- 任务相关可见文本、表格行及证据指针。
- 视觉模式下的截图和候选框；截图不能直接传给仅接文本的决策适配器。

元素 ID 是特定文档/快照中的短期引用，不是持久记忆键。导航/重渲染后重新 grounding；持久记忆保存实体 ID、URL、事实来源。
同名 Edit 按钮必须携带对应实体的行级上下文，不能仅靠按钮名称选择。

首版只用 DOM/可访问性信息，后续视觉 fallback 独立评估。视觉 fallback 的成本、次数与失败率必须记录。

## 四、子任务契约

语义子任务以可验证的业务进展为粒度，不固定等于一张页面或一个点击。例如：
- 根据条件遍历目录，找出全部候选。
- 查看一个实体的详情并保存带来源的字段。
- 在另一站点核验已经观察到的实体属性。
- 在沙盒内将通过条件的实体加入清单。

建议契约字段：
`id`, `objective`, `entity_refs`, `depends_on`, `allowed_operations`, `bindings`, `success_predicates`, `invariants`, `max_actions`, `recovery`。
成功条件通过白名单 DSL 编译为检查器，不执行模型生成的任意 Python/JavaScript。

初始允许每子任务约 3–8 步，但通过开发集调参；记录实际粒度分布，不将这个范围当作已验证最佳值。

## 五、候选动作与参数

动作是完整元组 `(operation, tab, frame, element_ref, bound_value)`。
首批操作：click、fill、select、scroll、wait、back、switch_tab、open_observed_url、extract_visible、request_finish、request_replan。
是否允许 goto/back/extract 等必须与 benchmark 的动作协议一致。

候选从当前控件与授权的历史观察构造；表单值来自用户给定值、已观察实体字段或高层一次性生成的内容。
Jev 不负责发明选择器、未知 URL 或任意文本。选项缺失时保留 `NO_MATCH/REPLAN`，不得强选。
初始候选数量 K 可扫描 16/32/64；这是预算参数，不是 API 限制。检索不能裁掉必要的“返回、下一页、关闭弹窗”等导航动作。

## 六、控制权交接

- 子任务可验证完成：控制器推进已有 DAG，无需再次调用模型。
- 页面尚在加载：有限等待、针对预期状态的检查；不盲目重规划。
- 元素引用过期：重观察并重新选择，不复用旧点击。
- 候选不足、局部无进展、条件失效、子任务预算耗尽：调用高层修订子任务。
- 付款/删除/发送等：独立权限策略和用户批准，不交给置信度阈值单独决定。

Jev 的 Choice confidence 不等于实际动作成功率；在开发轨迹上校准升级门控并画风险—覆盖率曲线。

第一版每次只执行一个状态改变动作，再观察；稳定后才测试 action chunking。导航会使后续元素引用失效，不默认预执行整批点击。

## 七、记忆与信息处理

区分约束、任务图、实体事实、页面访问账本、动作/结果回执。字段附来源 URL、快照版本、采集时间，允许失效和冲突。
算术/日期比较/精确过滤在代码里做，但输入必须来自允许的页面观察。语义筛选由模型做。
“已经访问”不等于“已经验证”；分页需要覆盖状态，保存操作需要读回状态。
跨标签保存 tab/entity 映射，而不是把整个浏览器历史拼成长 prompt。

## 八、安全与评测边界

网页内容仅作为数据，不能修改高层目标、扩大域名/工具权限或索取密钥。提示注入、超时、错误完成声明、重复提交都设专门测试。
不绕过 CAPTCHA 或访问限制；这些事件分类记录并交由用户处理。
直接调网站业务 API、读取隐藏 DOM 状态或数据库会改变任务定义；GUI-only 主实验不得使用。评测判分器可读取隐藏终态，但不能把真值反馈给策略。

对真实网站的写入不存在普遍可用的幂等键：点击后结果未知时先查询/读回，不重复点击。不可逆动作只在授权范围内进行。

## 九、实现阶段

M0：DOM点击正确性、日志、只读/本地环境。
M1：真实 Jev flat、相同记忆/候选的强 LLM 对照。
M2：事件规划器、契约验证、结构化长期记忆和参数绑定。
M3：WebArena smoke + WebChoreArena 主实验、任务分层与消融。
M4：视觉、多标签和真实网站只读试点；评估新的故障来源。

当前只完成 M0 的一部分及 Jev HTTP 适配器；不是 M2/M3 完成声明。

## 主要参考（2026-09-27 查阅）

- BrowserGym: https://github.com/ServiceNow/BrowserGym
- Browser Use agent configuration: https://docs.browser-use.com/open-source/customize/agent/all-parameters
- Playwright actionability: https://playwright.dev/python/docs/actionability
- Playwright locators: https://playwright.dev/python/docs/locators
- TypeSafe API: https://docs.typesafe.ai/api
- 社区 Jev 浏览器演示及限制: https://github.com/TypeSafeAI/typesafe-playground/blob/main/docs/jev-browser-agent.md
- WebDART: https://arxiv.org/abs/2510.06587
