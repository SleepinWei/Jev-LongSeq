# SaaS-Bench 接入

执行机器为 `ssh macmini`，项目在 `/Users/octopusz/CodeProjects/Jev-LongSeq`。
上游 checkout 在 `/Users/octopusz/SaaS-Bench`，部署版本为
`48c22deb18b98c0ed78f81e7f3be82bc162de2c8`。

## 运行范围

共有 **106 项任务**：商业 15、医疗 16、软件工程 31、团队协作 12、农业 12、媒体 20。
逐项 ID、使用的应用和公开任务描述见 [完整任务目录](SAAS-BENCH-TASKS.md)。

Mac mini 为 ARM64、16 GB 内存。使用独立 Colima profile `saas-bench`，
4 CPU、8 GB 内存、120 GB 数据盘，通过 Rosetta 运行官方 `linux/amd64` 镜像。
每次只运行一个任务，默认试跑 `business_023`、`business_031`，两者都使用
HRMS、BigCapital、Twenty。每个任务都重建容器，判分后清理。

这是选定任务的 pilot，不是 106 任务全量部署或完整排行榜成绩。
其它文本任务可用 `--saas-task-ids` 选择，预检会列出缺少的镜像。
当前 DOM agent 不支持本地文件上传及多模态输入；声明了 `multimodal_input`
的任务会在模型调用前阻止运行。任务描述中未结构化声明的文件操作也可能无法完成。

## 修改后的自动复跑约定

修改当前 benchmark 的 harness、controller、观察器、memory/context、模型适配器或
评分器后，先在 macmini 上完成相关检查，再自动重跑受影响任务；不以单元测试通过代替
真实跑分，不等待用户再次提醒。源码先核对、同步；已有推送授权时保持先 push 再跑分。

复跑沿用已确认的模型/provider、原任务、安全恢复检查点和预算，使用全新的输出目录。
启动前同时检查 Studio launcher 和手动启动的 benchmark 进程；有任务运行时等待其结束，
避免同一 slot 并发或重复启动。跟进到官方判分和容器清理完成，再报告得分、停止原因和
配置差异；外部依赖阻塞时说明具体原因。密钥、浏览器配置及真实运行产物不提交到 Git。

该约定同时写入项目根目录的 [AGENTS.md](../AGENTS.md)，后续迭代应继续遵循。

输入辅助模型独立于阶段规划，只返回严格的 `{"value":"..."}`，输出上限为
8192 tokens；不接受额外字段、非字符串值或被截断的响应。格式或原生下拉选项校验
失败时，在浏览器动作之前最多修复一次，两次调用均计入反馈预算。修复保留原任务、
当前观察和记忆，仅增加错误类型诊断；不拼接或猜测残缺 JSON。仍失败则返回
`needs_attention`，该输入不会发送给浏览器。HTTP 账本记录结束原因和响应长度，
不记录原始响应；context 超限和网络故障不进入格式修复重试。

下拉候选项通过可见 ARIA 关系或唯一的同容器输入框绑定到具体控件和 grid/row。
成功点击后，在新观察中确认同一输入框的非空值完整匹配被点击的可见候选标签、下拉关闭，
只记为 `option_selected_ui`；关联记录解析、Save/Submit 和业务持久化仍需独立证据。
其它待确认动作使用 `dynamic_readback` 轻量复核：仅返回 outcome 与当前观察的证据 ID，
controller 从原观察取回精确引文，保留原目标、阶段指导及工作记忆；不生成完成声明。
轻量回读不重置完整阶段规划的动作预算，也不消费尚未总结的证据。Save、Submit、Publish
或业务确认完成后，先从新观察生成 `write_checkpoint` 阶段指导和记忆，再决定下一动作，
避免沿用保存前的指导重复创建记录。打开确认对话框本身不触发业务完成的阶段切换。
待确认动作优先使用自己的回读预算。非完成反馈中的非法非关键 note 可单独丢弃并记录
诊断；关键证据、控制字段、完成声明和回读证据继续严格校验。

观察器保留导航区域文字、Search 附近可见的键盘提示、下拉项所属字段，并从可见图标
标记识别 refresh/reload；隐藏图标不能提供名称。阶段规划和快速策略需区分全局命令搜索、
关联字段选项和报表刷新；输入显示文字不代表已选中关联记录。无名称、无链接目标且没有
具体 grid/row 上下文的点击不生成候选，执行入口也会拦截；已观察到的行编辑控件仍可操作。
这些语义都来自当前渲染 DOM，不读取应用内部状态，不以图标或点击回执证明业务完成。
装饰 SVG 的 `aria-hidden` 只表示不读给屏幕阅读器；若图标确实渲染，可提供资产名称提示，
但不会作为独立交互控件。`display:none`、透明、隐藏父级及不可见控件仍不提供名称。
键盘提示支持相邻 `kbd`、span 或按钮，须在无输入字段的小范围容器中唯一匹配快捷键文字。
到达新的路径、hash 路由或任务标签页后，先生成 `navigation_checkpoint` 阶段指导再选择
下一动作，避免把前一页的导航指令带到报表。仅 query filter 改变不触发新阶段；未知或
尚未确认的写入仍须先读回。某项验证未解决时，记忆中明确保留其状态；只有在用户要求的
顺序及依赖允许时才继续其它独立工作，不能把未验证项计为成功，也不能重复不确定写入。
紧急 context 压缩层把密集表格控件中重复的完整 grid ID 提取到 `control_defaults.grid_ref`。
其它表格及无表格控件显式覆盖该默认值，保留所有 ID、row、候选绑定、原任务和待确认写入；
这是无损线格式压缩，不删除当前页面控件，也不放宽请求字节上限。
点击已通过读回确认、且当前可见 menuitem 集合较点击前改变时，生成 `ui_checkpoint`
再决定下一步，避免沿用“点击菜单入口”的指导重复开关菜单。该检查点不替代动作读回，
不在未知写入期间触发，也不把菜单出现当成业务创建成功。

动作读回与阶段规划隔离：`dynamic_readback` 只接收原任务/约束、当前动作的局部确认条件、
点击前摘要及当前可见证据 ID，不再包含旧阶段目标、工作记忆或恢复历史。原任务仍经过
逐请求一致性校验，但只用于授权；创建菜单打开、菜单项选择、表单输入与保存分别确认。
显式菜单触发器通过可见 ARIA ownership/labelledby 或唯一局部触发器绑定菜单项；新观察
在同页面、同标签页看到原触发器展开及其新增可用菜单项，可确认 `menu_opened_ui` 并重规划。
未知回执、换页、对话框、加载、替换控件、无法确定归属或新增页面异常均不能使用此捷径。
无归属的菜单继续通过隔离后的模型读回，不从点击回执或菜单名称猜测业务成功。

`confirmed_actions_archive` 保存带环境 ID、动作 key、确认范围、观察出处和局部证明的
动作账本。当前环境的业务边界与最近动作独立于 advisory working_memory 提供，旧 startup
恢复警告只约束继承历史，不能抹掉当前运行的后来读回；环境重建生成新 ID，旧确认仅留作
审计，不作为新环境证据。局部确认仍不代表整个任务完成；矛盾的新观察需要重新检查。
阶段规划和策略优先采用当前页面直接匹配的操作入口，避免不必要地绕行全局创建菜单。
局部读回还接收可见控件的前后差异（忽略 DOM handle 变化、保留重复项），避免仅凭页面
开头摘要遗漏末尾浮层。Search/close 控件消失可用于判断关闭浮层；对话框或提交按钮消失
仍不能证明业务持久化。完整点击前控件快照保存在运行产物中，模型只接收差异。

## 运行保护

阶段指导增加 `inputs`（字段名、目标值、grid/row）和 `verification`（验证目标、独立后续目标）。
输入辅助收到所选字段的目标值，controller 校验响应是否一致；不匹配时最多修复一次，
仍不匹配则停止该输入，不把当前显示值误当成目标已满足。所有计划字段在当前页面匹配后，
只读验证在同一路由最多消耗 6 个动作或 120 秒；没有待确认写入且计划字段仍匹配时，
保存未解决项及当前观察出处，然后采用独立后续指导。
原任务、权限、依赖与最终完成校验继续有效，验证失败不计为成功。

API 阶段规划/动作回读/最终复核的单次逻辑调用最多 120 秒，其它辅助调用最多 60 秒，最多重试一次，
单个请求可完整使用剩余调用额度，不再切为两个强制短超时；并服从运行剩余时间、预留 5 秒收尾。
模型调用超时不能冒充总预算耗尽。没有待确认动作时，可选规划超时降级到同路由旧指导或原目标，
完整保留工作记忆，并对该路由的可选规划冷却 120 秒，避免每个阶段重复等待。
降级的简短指导引用原任务，不把完整长 prompt 复制到有长度上限的 `next_goal`；跨路由清除旧字段绑定；
输入、写入回读及最终复核仍是必需调用，超时报告 `needs_attention`，不派发未知输入、不确认或重放动作。
恢复、无进展恢复和新增行指导使用紧凑规划 schema。
checkpoint 在动作准备、回执、确认及外层取消时原子落盘，保留未知写入和确认记录；
模型取消或 trace 导出失败不能抹掉最终 memory。每次修改后仍沿用原 provider 和总预算自动复跑。

网格表头候选标明“列标题、非行输入”，保留真实排序/布局操作但不与同名字段混淆。
新增行若由通用回读确认（而非精确本地追加证明），仍触发新阶段指导，不沿用旧行的目标。
紧急 context 投影以共享列名和逐行值数组表示密集表格，保留所有当前行、精确值及控件引用；
只消除重复结构，不截断当前表格、待确认动作或业务确认记录。Jev 实际请求也标明表头候选的用途。
未解决验证记录保留完整目标、状态和出处，旧页面摘录按时间衰减；较旧确认记录只缩短页面摘录，
动作标识、目标、确认范围和业务确认标志继续保留。旧的长证明以 archive_ref 摘要表示，
brain 可通过 evidence_requests 取回完整证明；最近两条确认及其证明保留原样。
原始记录在 memory archive 中不变。已确认写入撤下旧阶段的验证指导及计时，要求新规划；
这不把其它任务义务计为完成。带空格样式的 Save/Submit 标签仍作为关键节点保留。
前三层投影仍超限时，最后一级以共享 control_columns 和逐控件值数组消除字段名重复；
全部控件 ID、值、能力、候选动作及表格行归属保持精确，默认值和空值有明确解码规则。
若无损投影后仍超限，controller 在发请求及执行动作前缩小候选页，保留 next_candidates 入口；
当前完整 observation、原始任务、待确认写入和记忆不变，未展示动作仍可翻页访问。
没有翻页权限或缩页后仍无法容纳受保护状态时继续安全停止，不丢弃 pending，也不重放写入。
已成功发送的 Close 在同页、同 tab 的新观察中弹窗及其原关闭控件消失时，可本地确认
dialog_closed_ui 并要求新规划。动画中的空弹窗仍等待，不重复关闭；未知发送、错误页或
待确认业务写入不适用此证明。关闭通知不证明保存、提交或整个任务完成。
必需的 outcome-only 模型回读在原 120 秒总限额内给首次请求最多约 60 秒，剩余时间
允许一次相同推理请求重试；不重放浏览器动作。规划仍可使用完整 120 秒，最终业务核验不降级。

2026-10-02 的 arcbench-23 实验（同一模型、恢复点及预算）：573 项远端测试通过，3 项跳过；
官方评分 4/15，离职单、三项活动和供应商通过，评分器无错误、环境清理完成。
通知关闭的 fresh_dialog_dismissal 本地回读在现场触发；供应商保存后的 49,606 字节请求
通过候选页 250→66→33 自动恢复。供应商 Save 仅成功发送一次，没有第二次保存。
本轮仍有三次可选规划超时，保存后曾再次进入供应商表单；新规划最终识别出已创建记录。
最终因显示名弹层的 outcome=unknown 停止（readback unresolved），而非 context 超限或
必需回读超时。剩余问题是阶段指导滞后和普通弹层回读缺口；本轮没有实测触发回读重试。

## 命令

在 Mac mini 项目目录执行；包装脚本使用项目 `.env`，不复制本机密钥或浏览器会话。
输出目录必须是全新目录。

LongSeq brain 可使用 OpenAI 兼容的 ArcBench 模型接口：在 macmini 的私密 env 文件中设置
`PLANNER_ENDPOINT=https://api.arc-bench.com/v1/chat/completions`、
`PLANNER_MODEL=deepseek-v4-flash` 和 `PLANNER_API_KEY`，保留原 `TYPESAFE_*` Jev 配置。
通过 `JEV_ENV_FILE` 选择该文件；不要提交密钥。
若从旧 brain 模型的检查点续跑，须显式添加
`--saas-resume-brain-model deepseek-v4-flash`。该选项仅允许迁移 brain，
校验 Jev 模型及 provider 不变，并在 `continuation.brain_migration` 中记录旧、新配置。
原任务、权限、工作记忆以及每次请求的原目标校验继续保留。
模型变化后的成绩应作为新的实验配置对比。

```bash
cd ~/CodeProjects/Jev-LongSeq

# 若重启后 Docker 未启动
/opt/homebrew/bin/colima start --profile saas-bench --activate=false

# 只检查 checkout、Docker、所需镜像和模型环境，不启动任务
bash scripts/saas-bench.sh benchmark --preflight-only \
  --output runs/saas-preflight-01

# 启动应用、调用官方判分器检查初始状态、清理容器；不调用模型
bash scripts/saas-bench.sh benchmark --environment-only \
  --saas-task-ids business_023 --output runs/saas-environment-01

# Jev + 已配置的大脑模型；默认两个任务串行运行
bash scripts/saas-bench.sh benchmark --max-actions 200 --max-seconds 600 \
  --live-preview --output runs/saas-pilot-01

# 原版 jev-ultrafast；默认源码 external/jev-ultrafast，可用 JEV_ULTRAFAST_ROOT 覆盖
bash scripts/saas-bench.sh benchmark --saas-agent jev-ultrafast \
  --saas-task-ids business_023 business_031 --max-seconds 600 \
  --output runs/saas-ultrafast-01

# 接入已有“基线 → 分析 → 候选 → 逐任务比较”循环
bash scripts/saas-bench.sh autoresearch --max-trials 2 \
  --max-actions 200 --max-seconds 600 --study-seconds 3600 \
  --max-model-attempts 300 --max-tokens 600000 \
  --live-preview --output runs/autoresearch-saas-01
```

Studio 的 Autoresearch 页面可选择 **SaaS-Bench · Business**，沿用既有研究预算。
Studio 仍监听 Mac mini 的 `127.0.0.1:8768`：

```bash
ssh -N -L 8768:127.0.0.1:8768 macmini
```

然后打开 <http://127.0.0.1:8768/research>。本地转发端口保持 8768，符合现有同源检查。
若更新 Studio 服务配置，使用：

```bash
.venv/bin/python scripts/install_studio_service.py --port 8768 \
  --env-file "$PWD/.env" --ultrafast-root "$PWD/external/jev-ultrafast" \
  --saas-root "$HOME/SaaS-Bench"
```

安装器会在有活动任务时拒绝重启。原 `~/LongSeq` 部署和 8767 端口不参与此次接入。

## 判分与观测

### LongSeq 未保存草稿续跑

`--saas-resume-from` 接收上次任务目录，例如：

```bash
bash scripts/saas-bench.sh benchmark --saas-agent longseq \
  --saas-task-ids business_031 --policy jev --brain api \
  --saas-resume-from runs/previous/saas-bench-business_031 \
  --candidates 250 --max-actions 600 --max-seconds 1800 \
  --max-feedback-calls 1000 --live-preview --output runs/continuation
```

当前恢复范围是已观察到的未保存、活动表格为空的草稿。任务结束后官方环境会删除
容器与数据卷，因此这是重建相同环境并恢复 UI 草稿的续跑，不是完整浏览器或数据库
checkpoint。恢复只重放已确认的登录、导航、草稿输入；未知动作保留为历史中断记录，
不重放、不记为成功。遇到可能已提交业务数据的动作会拒绝重建。

续跑校验原始 Task（包括完整 prompt 和权限）、镜像、fixture、上游版本与端口配置，
完整加载上次 working memory、证据档案和历史；多次续跑沿来源目录继承历史。
模型名称必须一致。每次模型请求前校验 `trusted_goal`，避免把阶段指导替代原始任务。
`resume-memory-initial.json` 保存首轮模型调用前的恢复记忆，`resume-bootstrap.jsonl`
记录 UI 重建；manifest 记录来源、prompt/memory hash 和重建范围。
随后 memory 按实际执行更新，脑模型首先用新页面核对历史状态。

若最新停止点已有未保存的活动行，可同时传
`--saas-resume-from runs/latest/saas-bench-business_031` 和
`--saas-resume-ui-from runs/earlier/saas-bench-business_031`：完整继承 latest 的
memory、证据和操作历史，UI 则回退到 earlier 的空草稿关键点。earlier 必须属于
同一续跑来源链，Task/环境仍需完全一致；成功派发或结果未知的 Save/Submit
不允许这样回退。manifest 明确记录 `ui_rewound` 和两份来源，脑模型重新核对
最新 memory 与回退后的页面，不把旧草稿活动行当作已恢复的数据。

候选动作按页提供；复杂表单可用 `--candidates 250`，避免小候选页只包含导航控件。
续跑仍执行官方判分，但报告的 `evidence_scope` 标记为重建草稿续跑，不能当作独立
从零运行的 benchmark 成绩。

- Agent 仅接收上游公开任务描述、登录信息、应用地址；多个应用以独立浏览器标签页提供。
- 官方 `verify.py` 在 agent 停止后、容器清理前执行；判分代码与数据库真值不进入 agent 上下文。
- `grade.score/earned/total/checks` 保留官方部分得分；`result.strict_success` 还要求控制器正常完成且无违规。
- 判分器缺失、异常、镜像或容器启动失败时，`data_valid=false`、`strict_success=null`，不生成有效成功率。
- 环境 smoke 的 `strict_success=null`，即使初始状态已有部分得分也不记为模型成绩。
- `report.json`、`result.json`、`manifest.json`、官方 `*_verify.json` 和既有轨迹/模型调用日志留在同一个任务目录。
- `end_to_end_s` / tokens / 成本沿用 agent 运行口径；容器部署与判分耗时另记在 `environment`。
- manifest 记录镜像 ID、任务及判分器 hash；重复对照使用同一环境指纹。
- `business_031` 使用版本化的 BigCapital 字段兼容补丁：不存在的
  `CONTACT_NORMAL_NAME` 改为 `LOWER(DISPLAY_NAME)`；布尔 `PUBLISHED` 改为
  `PUBLISHED_AT IS NOT NULL`。原有 8 项检查、15 分权重和成功标准不变。
  上游 checkout 保持原样，每次运行把修复版保存为 `verifier.compat.py`；manifest 的
  `upstream_verifier_hash` 记录原版，`verifier_hash` 记录实际执行版，
  `verifier_patch=business_031-bigcapital-schema-v1` 标识配置。补丁只接受已审计的
  固定评分器版本，版本变化时会在模型调用前拒绝运行，要求重新审核兼容性。
  新配置改变环境指纹；旧的无效评分保持原样，不能用重建的初始数据库冒充旧轨迹的最终状态。
- 默认容器前缀 `jevsaas`、基端口 31000；slot 0 的 Twenty / BigCapital / HRMS 分别为 31004 / 31005 / 31006。

## 原版 Ultrafast 基线

`--saas-agent jev-ultrafast` 直接运行原始 `jev_ultrafast.Agent`，不经过 LongSeq
planner/controller。原版动作空间、60 步上限与模型重试规则保持不变；`--max-actions`
不会覆盖其原生上限。外层仅限制 `--max-seconds`，记录轨迹，并在进程退出后执行官方判分。
原版只控制一个标签页，没有打开任意 URL 或切换标签页的动作；跨应用任务保留这一限制。
此选项不支持 LongSeq autoresearch 调参，避免把另一种控制器的实验冒充原版基线。

模型配置沿用 `TYPESAFE_*` / `TEXT_MODEL_*`，不依赖 `PLANNER_*`。
每次 HTTP 尝试（含重试、失败和被终止的请求）都计入日志；未返回的 usage 和未知价格保持未知。
模型输入包含上游公开的测试账号和隔离容器页面，不读取判分器真值。

归一化结果在 `runs/<benchmark>/saas-bench-<task>/report.json`；原始截图、DOM 与决策
位于 `runs/ultrafast/<original_trace_id>/`，完成后可通过
`http://127.0.0.1:8768/ultrafast?run=<original_trace_id>` 回放。
`manifest.json` 保存源码 SHA-256、原生上限、模型名与原始轨迹位置。

## 镜像来源与校验

官方镜像下载地址：<https://huggingface.co/datasets/Marti844/SaaS-Bench-docker>。
首批三份 archive 的官方 SHA-256：

| Archive | SHA-256 |
| --- | --- |
| mw-twenty.tar | `ee2c0c6dcfaef89d721bc77aabcb029859556dc9cfce83844ee1337fa5cbdfa1` |
| mw-bigcapital.tar | `0d2aa61b22da276e65f93a99f6180594fbe223ccd9c966c4743feeb30bb04c22` |
| mw-hrms.tar | `aa48bfd941b878ca056b59b91ca7d1d488325fa5d4e6adea9acc50d7707f9267` |

用 `docker --context colima-saas-bench load -i <archive>` 导入。
Python 集成依赖可通过 `uv sync --locked --extra dev --extra chrome --extra saas` 安装，
无需安装上游的 browser-use 参考 agent。

## 测试

按仓库规则先同步改动，再执行：

```bash
bash scripts/test_macmini.sh -q tests/test_saas_benchmark.py \
  tests/test_research_benchmark.py tests/test_research_ui.py tests/test_autoresearch.py
```

覆盖执行/判分/清理顺序、部分启动失败、slot 互斥、无效判分、环境 smoke 隔离、
SaaS 字符串任务 ID 的安全路径、研究入口及既有 UI/自动研究回归。

## 2026-09-29 部署验证

- Mac mini 完整回归：**286 passed, 3 skipped**（跳过的是未安装 BrowserGym 的可选测试）。
  最后展示与退出码调整后的相关回归：**31 passed**。
- 三个官方 amd64 镜像均核对上述 SHA-256；在 Colima/Rosetta 下通过健康探测。
- `runs/saas-environment-20260929-01`：`business_023` 环境与判分器 smoke 通过，
  无模型调用，`graded_runs=0`，清理无异常。
- `runs/saas-environment-20260929-02`：`business_031` 的环境启动、判分进程与清理完成，
  无模型调用。后续原版试跑审计发现其判分脚本存在 SQL 字段错误；此前的“判分有效”判断
  不可靠。适配器已修正为将检查详情中的运行异常标为无效评分，详见下述基线报告。
- `runs/saas-agent-smoke-20260929-01`：真实 Jev/模型运行 6 个动作、10 次 HTTP 调用，
  全部成功返回，共记录 49,970 input tokens、2,379 output tokens；
  官方判分 **2/20 (0.1)**。因 6 动作预算停止，`strict_success=false`，不是完整任务成功。
  Agent 端到端 15.05 秒，容器准备 48.43 秒，判分 1.78 秒；容器清理无异常。
  未配置完整价格，费用保持未知。
- Studio 服务配置已备份到 `runs/service/studio-before-saas.plist`，
  更新前源码已备份到 `runs/service/saas-predeploy-source.tgz`。

通过已有 SSH 转发查看该次真实试跑：
<http://127.0.0.1:8768/?run=saas-agent-smoke-20260929-01%2Fsaas-bench-business_023>。

原版 Ultrafast 两项试跑及判分器问题见 [2026-09-29 基线报告](SAAS-BENCH-ULTRAFAST-20260929.md)。

## 2026-09-30 business_031 评分器修复验证

兼容补丁已接入 benchmark / autoresearch 共用的评分路径；原版 Ultrafast 也自动使用。
Mac mini 相关回归 **59 passed**，修复代码与验证脚本通过 Ruff 检查。

真实三应用数据库验证保存在 `runs/saas-verifier-validation-20260930-01`：

| 测试数据 | 得分 | 评分有效 | 全部通过 |
| --- | --- | --- | --- |
| 镜像初始状态 | 0/15 | 是 | 否 |
| 满足全部评分检查的测试记录 | 15/15 | 是 | 是 |
| 分录未发布 | 12/15 | 是 | 否 |
| Rent 借方金额错误 | 12/15 | 是 | 否 |
| 供应商邮箱错误 | 14/15 | 是 | 否 |

这是评分器验证：直接向独立 slot 98 的数据库写入合成测试数据，不运行 agent 或模型，
不计入 benchmark 成功率。验证后清理三个容器。复现命令（仅 Mac mini）：

```bash
export PATH=/opt/homebrew/bin:$PATH
export DOCKER_CONTEXT=colima-saas-bench DOCKER_DEFAULT_PLATFORM=linux/amd64
export PYTHONPATH="$PWD/src"
.venv/bin/python scripts/check_saas_business_031_verifier.py \
  --output runs/saas-verifier-validation-new
```

历史 `saas-ultrafast-20260929-01` 的 business_031 评分仍为无效。
原容器已清理，不能对旧轨迹恢复精确最终数据库状态；需新运行才能得到新配置的有效成绩。

## 操作历史 context 对照实验

使用 `--saas-history-context` 启用 `jev-ultrafast-history-context-v1`。
默认仍是原版；上游 Agent / Browser / model / snapshot 文件保持原样。
实验只给 Jev 每轮请求的 `state.execution_history` 加入实际操作结果、
累计观察到的字段值、页面跳转和连续无进展次数；近期结果保留 12 项，较早的字段和
导航效果继续保留。完成字段输入不等于业务任务完成，历史值不覆盖当前页面证据。
不新增模型调用，不更改输入辅助模型、候选动作、停滞保护或原生上限。

```bash
bash scripts/saas-bench.sh benchmark --saas-agent jev-ultrafast \
  --saas-history-context --saas-task-ids business_031 --max-seconds 600 \
  --output runs/saas-ultrafast-history-new
```

2026-09-30 单次对照：相关回归 22 项通过，静态检查通过。

| 指标 | 原版基线 | 历史 context |
| --- | --- | --- |
| 官方有效得分 | 0/15 | 0/15 |
| 原生状态 | blocked | blocked |
| 动作数 | 4 | 4 |
| Agent 运行时间 | 6.20 秒 | 6.66 秒 |
| HTTP 调用 | 8 | 8 |
| 输入 tokens | 36,434 | 37,858 |
| 输出 tokens | 600 | 600 |

基线为 `runs/saas-ultrafast-business031-20260930-01`，实验为
`runs/saas-ultrafast-history-business031-20260930-01`。两次任务 hash、fixture hash、
评分器 hash、原版源码 hash、模型、原生上限及 600 秒时限一致。
新增 context 已出现在每次 Jev 请求日志中，包含输入前后值、无变化结果和停滞计数；
模型仍执行四次 Email 填写，未点击 Show 或提交登录。
此次输入 tokens 增加约 3.9%，没有改善执行结果；单次、登录阶段的结果不能代表
更长任务中的记忆效果。密码候选过滤和跨应用动作缺失不在本次实验修复范围内。

实验 trace：<http://127.0.0.1:8768/ultrafast?run=saas-saas-bench-business_031-1790744542022637000>。

### 2026-10-02：以官方成功率为目标的逐项实验

目标是提高最终 `strict_success`，而非只让 readback、动作数或单元测试更好看。
采用顺序增量实验：每次只改变一个机制组，保持原 prompt、模型/provider、
恢复点、候选上限与运行预算一致，使用独立输出目录，完整执行官方评分和清理。
单次 rubric 得分改善不等于成功率改善；正式报告成功率需要多次独立试验并给出分母。
任何 scorer 金标准或数据库内部信息只用于事后评分，不能指导 agent。

实验 A（`arcbench-24`）：写入检查点及规划超时交接。控制器在已有新鲜读回的
Save/Submit 边界记录环境 ID、字段快照、原阶段、证据来源及确认范围；
`write_effect_confirmed` 不冒充业务最终验证。超时时清除旧填写绑定，限制候选为
已观测链接、返回、Quick find 及非表单操作，直到新的有效规划完成。
历史检查点在新环境只作为历史，较早字段详情可通过 archive_ref 取回。
这不是完整的逐义务任务 ledger；任务拆分和最终验证仍由 brain 与独立 grader 负责。

验证：macmini 定向回归 84 通过，全量 578 通过/3 跳过，Ruff 与 diff 检查通过。
使用 run23 cycle101 的真实保存前后 observation 做只读离线回放：保存字段快照 9 项，
超时后的 New Vendor 被排除，Quick find 仍可用，未调用模型或操作真实浏览器。
实验 A 的线上评分和后续阶段约束实验分别记录，避免把多项同时修改当成因果证据。

实验 A 实际结果：`arcbench-24` 官方 3/15、data_valid=true、strict_success=false，
42 动作/50 cycles，Agent 576.57 秒，整体 593.59 秒。离职单及三条活动正确；
尚未创建供应商。未触发规划超时保护，因此无法从此次试验判断交接保护的收益。
新增检查点重复长阶段说明/页面摘录，使最小候选页仍为 50,399 bytes，超过
48,000-byte 上限；pending 保留并停止。该版本存在上下文开销回归，不能宣称有效。

实验 A2（`arcbench-25`）：只调整检查点的请求投影。所有检查点保留状态、环境、
来源、最近字段及完整 archive_ref；长阶段说明/摘录在请求中限长，完整记录留在
memory archive，可只读检索。run24 cycle50 的真实 memory/observation 离线回放
将请求压到 47,095 bytes（level3），保留 pending，无真实模型或浏览器调用。
相关 macmini 回归 58 通过，Ruff/diff 通过。阶段范围约束尚未加入此实验。

A2 实际结果：`arcbench-25` 官方 3/15、data_valid=true、strict_success=false，
77 动作/90 cycles、Agent 971.47 秒，清理无错误。已通过上轮上下文失败位置，
但停在空的 New Vendor 表单：cycle80–89 连续 WAIT，cycle85 规划恢复后仍等待。
HR 密集页面把全局候选页降到 8；这项限制延续到新路由，使初始候选页主要是
控制动作和另一个应用的切换，表单字段位于后续页。不能把压缩错误修复视为成功率提升。

实验 B（`arcbench-26`）：阶段范围和候选分页。brain 提供当前可见控件 ID/操作；
harness 绑定控件角色、名称、表格/行及环境/路由，过滤阶段外操作，保留观测导航、
读回与 replan。新出现的下拉选项仅在归属明确且属于本阶段字段时可选；
读回不会丢掉阶段范围，显式的新阶段计划可重新授权重复工作，不设永久对象黑名单。
先过滤再分页，避免目标控件被无关动作挤到后续页。有效页大小改为按路由缓存，
新路由从原配置 250 开始尝试，不继承 HR 的 8。原 provider、prompt/恢复点和预算不变。

两项分别做机械回放：run23 cycle104，在手工提供与原规划一致的可见范围后，
邮箱和显示名菜单不再可选，Quick find 可选；run25 cycle80，四个输入字段和 Save
都在首个 15-candidate 页面。回放不调用真实模型/浏览器，因此还不能证明 brain
会正确生成范围。macmini 全量 588 通过/3 跳过，最新提示文本相关 42 通过，Ruff/diff
通过。线上 B 验证组合效果；若得分提升，单项对成功率的贡献仍需进一步消融及重复试验。

B 实际结果：`arcbench-26` 官方 4/15、data_valid=true、strict_success=false，
102 动作/121 cycles、46 feedback calls、Agent 1297.09 秒，清理无错误。
离职已提交，三条活动及负责人正确，供应商最终创建正确；journal/payment/Twenty
未完成。第一次供应商 Save 因缺少 Display Name 被 UI 拒绝：brain 将菜单项提供为
select，但真实 menuitem 只支持 click，范围过滤丢掉选项却仍保留 Save。
后续完成显示名再 Save，pending 读回时最小请求为 49,268 bytes，超过 48,000。
相对参考 run23 的 4/15，最终严格成功率尚无提升。

实验 C（`arcbench-27`）：针对 B 的已观测机制分别增加机械验证，再检验组合的
最终成绩。阶段控件 ID 变化只重绑同环境/路由/dialog、同角色/名称/表格/行的唯一
新鲜控件；歧义或不同作用域需要新规划。grid input 读回依据明确格子身份和精确值，
允许邻近下拉文本变化，仍需已知 receipt、fresh observation 和相同 URL/tab/dialog，
只证明输入已填，不证明业务持久化。menuitem/option 的 select 意图规范为已观测
click，排除无业务标签的纯图标菜单项。较早 readback/关键点的长详情留 archive，
请求保留来源锚点、范围及 archive_ref，最近事实和当前 pending 不舍弃。fast policy
最小请求仍超限且有 pending 时，转交已有必需 action_readback；只读刷新和等待次数
有上限，禁止重提未知写入，无 pending 时仍安全停止。

C 验证：macmini 全量 608 通过/3 跳过，Ruff/diff 通过。真实 run26 cycle15→16
回放中 Activity Name 值精确匹配、邻近 context 从 Begin typing 变为 7 results，
本地读回成立且 business_commit_confirmed=false；使用原记录 resume stage_controls
回放 date 后 Add row 从 e2866→e2870，仍在候选中，无额外模型调用。menu select
规范、错误行/歧义/环境拒绝、pending overflow 不重提及等待上限均有定向回归。
run26 cycle121 真实 memory/observation 最小页回放为 46,892 bytes（level4），
pending 保留，无真实模型调用/浏览器动作。这些证明机械机制有效，不能据此推断
最终成功率提高；C 线上试验检验组合结果，单项成功率贡献仍需消融和重复试验。

C 实际结果：`arcbench-27` 官方仍为 4/15、data_valid=true、strict_success=false，
91 动作/106 cycles、43 feedback calls、Agent 1218.53 秒，清理无错误。11 次阶段
控件重绑定；date 后 Add row 在 cycle3 执行（B 为 cycle13），三条活动及负责人
由官方评分确认正确。供应商显示名此次 brain 直接使用 click，首次 Save 即成功，
所以 SELECT→CLICK 规范化未触发，不能单独归因。cycle105 Quick find 的 pending
最小请求为 48,115 bytes，必需读回 fallback 完成确认，未重提未知业务写入。
但新 Search... 输入不具有 dialog/menuitem 标记，旧计划/作用域仍只授权 Quick find；
cycle106 再点被浮层遮挡的 opener，Playwright 超时 unknown，停止。压缩/读回修复
跨过 B 的停止点，但未提高严格成功率。财务分录、付款及 Twenty 仍未完成。

实验 D（`arcbench-28`）：只补新输入揭示后的阶段交接。在已有合法 confirmed
click 读回后，同 URL/tab 上出现此前没有的可编辑/可选择控件身份时，设置
ui_checkpoint，在下一动作前要求新阶段规划。按 role/name/grid/row 比较，忽略
DOM ID/value/邻近文本变化；禁用、只读或未知 receipt 不产生此交接。它不授权新
字段，不新增业务成功声明，不豁免未知写入重提限制。目标是修复 C 的 Search
palette 已打开却仍使用 opener 计划的问题，不硬编码 BigCapital 路径或任务答案。

D 机械验证：152 项定向回归通过，Ruff/diff 通过，真实 C cycle104→105 observation/action/receipt
只读回放中出现 e762 Search... 后 ui_review_due=true，无真实模型或浏览器调用。
端到端模拟验证旧 action head 的二次 Quick find 被丢弃，ui_checkpoint 后才填写
Search；已有输入改值/改 ID、disabled/readonly 和未知回执的负例通过。
全量首轮发现通用交接覆盖专用 draft_row_added 的优先级冲突；修正为 grid_append
继续使用专用交接，包含真实浏览器表格增行回归的上述 152 项复验通过。

D 实际结果：`arcbench-28` 官方 0/15、data_valid=true、strict_success=false，
16 动作/29 cycles、11 feedback calls、Agent 486.63 秒，清理正常。新输入交接
未触发；第一行 User 选择后，brain 的新作用域为空，只要求重新观察。fast policy
却选择默认豁免的行内 Open Link，经 stale 刷新跳到 /desk/user/Rajesh%20Kumar，
显示找不到记录，再去 Home 后重复状态停止。所有工作仍未保存，因而得分为零。
不能把这次新交接未触发的偏航直接归因于 D；也不能据单次机械回放宣称它已提高
线上成功率。此试验说明默认放行所有 href 可破坏未保存阶段。

实验 E（`arcbench-29`）：仅将具有 grid_ref/row_ref 的链接从通用导航豁免中移除。
它们需要当前可见 stage_controls 的显式授权，仍受同环境/路由/身份检查；普通
全局链接与返回仍可用。后续的新阶段规划可以授权同一行内链接，不设永久黑名单。
132 项最新定向回归及 Ruff/diff 通过。D cycle16 原 Open Link 动作与原 brain
空作用域回放中，该动作被排除且 dispatch 前拒绝，无真实模型或浏览器动作。
线上 E 保留 D 的输入揭示交接，检验此次单项导航范围改变的组合最终表现。

E 实际结果：`arcbench-29` 官方仍为 0/15、data_valid=true、strict_success=false，
26 动作/28 cycles、13 feedback calls、Agent 427.08 秒，清理正常。没有再跳出草稿，
但第一行 User 连续填写/选择三次，之后同值 input 被正确抑制，旧 stage 仍要求
填选该 User，连续 WAIT 到 repeated state after brain recovery。候选日志中字段
位于完整 23/24-candidate 列表，故本次不是候选分页饥饿。导航限制阻止了偏航，
未改善最终成功率；最新两次新增交接均未触发，仍无 D 的在线收益证据。

实验 F（`arcbench-30`）：只在精确的 scoped option UI 读回成立后设置 ui_checkpoint，
在下一动作前重新规划已选择的标识与后续字段。仍要求 fresh observation、已知
receipt、同 URL/tab/grid/row、唯一 owner、值与实际选项匹配且 popup_closed；
不把选择/关闭等同链接后台解析或业务持久化，不解除未知写入保护。
E cycle7→8 的真实选项点击回放中，owner e2898 值为 rajesh.kumar@techvista.com
且 dropdown 关闭，ui_review_due=true，无真实浏览器/模型调用。目标是避免旧的
“输入搜索标签”计划再次覆盖已选择的标识，或在同值 input 抑制后原地等待。
macmini 全量 617 通过/3 跳过，Ruff/diff 通过；F 线上成绩另记，不以回归测试推断成功率。

F 实际结果：`arcbench-30` 官方 4/15、data_valid=true、strict_success=false，
98 动作/112 cycles、49 feedback calls、Agent 1787.35 秒，达到原定时间上限，
评分与环境清理正常。三条活动及负责人和供应商由官方检查确认正确，分录、付款、
Twenty 未完成。精确选项交接在线触发 4 次：第一行选择后推进到第二行，未重现
E 的第一行重填/WAIT 循环；Save 位于 cycle28（C 为 cycle37），供应商保存位于
cycle85（C 为 cycle104）。不同规划和菜单路径仍有波动，单次进度不能估计成功率。
输入揭示交接未触发，本轮无法补充 D 的在线收益证据；菜单 SELECT 规范和超限
fallback 也未触发。提交后的可选规划超时交接保护触发 2 次，没有填写旧 HR 表单。

F 的 26 次 dynamic_feedback 调用累计 1537.03 秒，约占 Agent 时间 86%；
Jev 105 次调用累计 60.48 秒，input 12 次累计 35.11 秒，readback 11 次累计
110.34 秒。供应商之后在 HR 报告与 BigCapital 间往返：计划要求 Accounting /
Manual Journal，但财务页观察中有 Accounting 文本，没有任何名含 Accounting /
Journal 的可执行 element。尚未捕获对应实时 DOM，不能据此断定具体 HTML 缺陷。
截至时间上限没有进入分录表单；上下文并未造成本轮终止。

本批结论（C/D/E/F 四次均未严格成功）：检查点压缩、唯一身份重绑定、输入值读回、
pending 超限必需读回有机械/在线证据；选项后交接有消除局部循环的在线证据。
行内链接限制阻止了 D 的偏航，但 E 仍失败；输入揭示交接仅有回放证据。
所有局部收益均不能证明最终成功率提高，当前最好官方结果仍为 4/15。
下一批优先级为：捕获财务 sidebar 的渲染 DOM，修复可见入口缺少可执行候选的
真实原因；降低阶段规划的重复输出和调用开销；用明确的未完成义务与阶段退出条件
减少跨应用往返。仍保持原任务、模型/provider、恢复点和预算，分别消融、评分，
在出现严格成功后才用相同配置重复试验估计该任务成功率；不能推广到整个任务集。

实验 G（`arcbench-31`）：仅修复跨阶段菜单入口去重。F cycle56 的 Quick new
与 cycle85 的页面语义/action_key 完全相同，旧 consumed key 将新阶段明确授权的
入口排除；真实 observation 离线回放中，移除该单个 key 才出现候选。
控制器现在记录已知 ok 且经过新鲜读回的菜单展开：同页面/tab、没有新弹窗或输入变化，
出现此前没有的启用 menuitem，且不是 Save/Submit/Publish/Approve、表格行操作或
业务提交。只有菜单项已消失、没有 pending/未知写入、环境/路由一致，并且更新的
有效规划再次显式授权同一控件 click，才临时放行这个菜单 key。consumed 原记录不删除；
新 dispatch 立即消耗此次权限，未知结果仍停止并禁止重提。本轮不修改导航约束、
观察抽取、规划频率或 provider，以隔离此次机制收益。

G 机械验证：119 项定向回归通过；F 的真实 cycle56→61→85 观察回放记录一个菜单
展开，并在 cycle85 恢复 Quick new 授权候选，consumed 仍保留原 key；没有真实
模型/浏览器调用。新测试覆盖同阶段/局部读回不重新授权、菜单未关闭、范围/环境/
路由改变、pending、未知结果，以及业务提交始终不放行。线上评分另记。
macmini 全量回归 630 通过/3 跳过，Ruff/diff 检查通过。

G 实际结果：`arcbench-31` 官方仍为 4/15、data_valid=true、strict_success=false，
95 动作/109 cycles、44 feedback calls、Agent 1458.30 秒，评分及清理正常。
菜单展开记录在线出现 2 次，但重用尚未触发，不能给出线上收益结论。本轮先做 HR
报告验证，verification_deferred 触发后才去财务，顺序变化发生在重用之前，不归因于 G。
供应商已由官方确认创建正确；保存后两次必需 readback 返回不属于当前观察的 evidence_ids。
JsonFeedback.readback 在返回 Feedback 对象前抛 UngroundedFeedback，controller.review
第二次异常处理却访问 feedback.complete；feedback 此时为 None，继而 AttributeError。
这是读回异常路径的独立缺陷，不是菜单被重新点击、context 超限或业务保存失败。
21 次阶段规划累计 1227.97 秒，昂贵规划的问题仍在。

实验 H（`arcbench-32`）：仅修复上述证据引用合同与异常路径。读回 schema 的
evidence_ids.items 增加当前观察引用的 enum；提示模型逐字复制当前 ID，禁止沿用旧
观察 ID。无效引用的 repair diagnostic 明确列出 invalid_refs，仍拒绝非当前引用。
两次修复失败且没有 Feedback 对象时，必需读回安全转为 ReadbackUnresolved：保留
pending/未知状态，禁止重提；finish 校验失败仍按完成审查错误处理，不弱化为局部读回。
不从官方评分、历史引用或无效 ID 推断已保存。原 provider、prompt/恢复点、预算及 G
菜单机制不变，自动使用新目录重跑；本轮未加入导航/观察/规划频率调整。
H 验证：164 项定向回归通过，macmini 全量 633 通过/3 跳过，Ruff/diff 通过。
真实 G cycle109 observation 仍包含供应商成功提示，因此此次无效引用不能解释为
提示已经消失；新的合同与定向 repair 需要线上验证。测试覆盖首次错误引用后用
当前 ID 修复成功、两次失败保留 pending 并 needs_attention、不重提，以及 finish
无 Feedback 对象时仍保持严格校验。

H 实际结果：`arcbench-32` 官方 0/15、data_valid=true、strict_success=false，
30 动作/38 cycles、8 feedback calls、Agent 419.08 秒，评分及清理正常。
没有进入供应商保存读回，不能判断 H 的线上收益。cycle11 的 ui_checkpoint
阶段规划超时 120 秒，之后 8 次退让/冷却沿用旧阶段的 Add row 控件与搜索输入，
草稿最后有 8 行，随后 repeated state after brain recovery 停止。没有保存，故评分为零。
这暴露了可选规划超时后继续复用旧表单范围的独立错误；不是 H 放行无效证据或重复业务提交。

实验 I（`arcbench-33`）：仅修复 UI 交接后的规划退让。已有 scoped 阶段在
ui_checkpoint/draft_row_added/stale_target_changed 无法取得新规划时，保留原始任务、
working_memory 和证据，但清除旧输入与可变控件范围，标记 fresh_scope_required。
冷却窗口内只观察并等待，不调用 fast policy、不派发浏览器动作，不因相同页面提前
触发 no_progress；冷却结束后重新请求 ui_checkpoint 规划。只有新的有效显式 stage
controls 才解除冻结，局部读回与旧授权不能解除。仍受原 cycles/时间预算约束，不
修改 120 秒请求上限、120 秒冷却或 provider；已确认业务保存后的 navigation handoff
保持原规则。原 H 证据引用合同和 G 菜单机制保留，线上成绩另记。
I 验证：156 项定向回归通过，全量 macmini 636 通过/3 跳过，Ruff/diff 通过。
真实 H cycle11 的规划与 observation 离线回放中，模拟已记录的规划超时后，
fresh_scope_required=true，仅 WAIT/REPLAN 可选，旧 Add row 不可选；原始 task.objective
和 working_memory 字节内容保持一致。没有真实模型/浏览器调用。定向测试也确认
冷却期间没有 policy.choose 或 backend.execute，新的有效范围才能解除等待。

I 实际结果：`arcbench-33` 官方 0/15、data_valid=true、strict_success=false，
12 动作/15 cycles、8 feedback calls、Agent 335.39 秒，评分无 verifier_errors，
环境 cleanup_error=null，进程已结束。第二条离职活动填写阶段的 dynamic_input
请求在 cycle15 收到 Arcbench HTTP 402 Payment Required，约 0.08 秒返回，
并非上下文超限或规划超时。当前模型调用日志没有错误响应正文，因此只能确认
provider 的付款/额度类拒绝，不能据此断定余额已经耗尽。没有保存业务数据，故
官方得分为零；该外部中断不能用于判断 I 是否提高或降低最终成功率。
本轮 option_selection_handoff 触发 1 次，planning_degraded 和 planning_scope_wait
均未触发，I 的冷却恢复在线行为仍待验证。保留 Arcbench/deepseek-v4-flash、原始
任务、恢复点及预算；在服务可用性恢复前不重复启动相同付费请求或擅自切换 provider。

G/H/I 三轮最终严格成功均为 false：G 为 4/15，H 为 0/15，I 因 HTTP 402
中断后为 0/15。全量回归 636 通过/3 跳过和真实故障点离线回放证明相应机制的
局部行为，不构成最终成功率提升证据。外部依赖恢复后，先验证 I 的规划超时恢复，
再分别评估阶段导航约束、规划控件可执行性检查、Accounting 入口观察抽取、
规划耗时缩减与结构化未完成义务记录；这些后续机制尚未实施。

官方 API 对照（`deepseek-34`）：用户要求改用 DeepSeek 官方 API 后，使用 macmini
已有 api.env，brain endpoint 为 api.deepseek.com/v1/chat/completions，model 为
deepseek-flash（官方 /models 返回此名称及 deepseek-v4-pro）。只切换 provider 及其
官方 flash 模型名称，不修改 harness；保留 Jev、原始任务、memory/UI 恢复点、
48000/96000 bytes 上下文上限以及 600 actions/1800 秒/1000 feedback calls 预算。
沿用 I 已验证的源码，远端 controller SHA256 与本地一致。模型列表查询成功只证明
鉴权与该读取接口可用，不代表生成请求或最终 benchmark 一定成功；成绩另记。

官方 API 实际结果：`deepseek-34` 官方 3/15（20%）、data_valid=true、
strict_success=false。离职单 docstatus=1 与三条活动/负责人通过，其余未通过。
84 动作/103 cycles、50 feedback calls、Agent 709.22 秒；停止原因为 repeated
state after brain recovery，评分无错误、cleanup_error=null。50 次 brain 请求
均使用 api.deepseek.com/deepseek-flash，100 次策略请求仍使用 Jev；未出现 HTTP
402 或其他模型请求错误。25 次阶段规划累计 575.33 秒（约 81% Agent 时间），
readback 28.00 秒、input 18.34 秒、Jev 53.52 秒。规划仍是主要时间开销。
一次 no_progress 规划返回额外 JSON 文本而解析失败，修复后继续；未触发规划
退让/冻结，因此不能补充 I 的在线恢复收益证据。此单次成绩不代表任务成功率。

本轮在供应商显示名处暴露了菜单状态与计划不一致：cycle87 填 Last Name 后，
cycle88 的新鲜观察已含正确 menuitem e292（名称被抽取为重复的 Ananya Reddy），
但下一动作仍点击 Select display name as，随后 cycle93 起选项从观察中消失。
brain 后续反复要求查找/点击正确选项、且仅在关闭时重开，却未形成可执行的新
选项范围；同状态 WAIT 后停止，供应商未保存。现有 G 菜单重授权未触发，具体
被哪项前置条件阻止仍需定向回放。后续应在菜单已展开时防止不必要的 opener
切换，及时重绑定因输入变化而更新的选项范围，并检查恢复计划是否包含当前
可执行动作；不能把页面曾经显示过的选项当成现在仍可执行。

实验 J（`deepseek-35`，2026-10-03）：仅增加已观察控件的阶段操作能力校验。
brain 请求提供 current_control_capabilities，来自实际 candidate generator，
与 task.allowed_operations、enabled/read_only、editable/selectable 等约束一致，
在阶段范围、去重、输入抑制和分页之前计算；因此这一步只验证固有能力，不声称
该动作已获阶段授权或现在允许重提。非局部规划若请求不存在的操作（如按钮 fill），
在覆盖 memory/作用域之前拒绝，并把 element_ref、role、name、请求操作、当前
可用操作传回既有的一次 repair。两次仍失败则按既有错误机制停止，不派发动作。
保留 menuitem/option 的 select→click 规范；局部读回、完成审查和旧引用处理不变。
空范围导航仍有效，本轮不修改菜单交接、导航约束、模型或预算，以隔离验证。
真实 deepseek-34 cycle83 观察/规划离线回放精确返回 e129 button 仅支持 click，
不支持 fill，无模型或浏览器调用。61 项定向测试通过；线上结果另记。
macmini 全量 644 通过/3 跳过，Ruff/diff 通过；仍需官方评分验证最终结果。

J 实际结果：`deepseek-35` 官方 4/15、data_valid=true、strict_success=false，
离职单/三条活动/供应商通过；75 动作/149 cycles、45 feedback calls、Agent
831.76 秒，评分无 verifier_errors，cleanup_error=null。阶段能力拒绝未触发，
本轮没有按钮 fill，但只能说明提示下的路径不同，不能单独证明 repair 的在线收益。
供应商阶段 ui_checkpoint 规划耗尽 120 秒，冻结机制在线触发：59 个冷却等待
cycle 没有调用 Jev 或派发浏览器动作，新合法作用域恢复后选对姓名并保存供应商。
规划累计 603.91 秒。随后供应商列表页面最小请求为 49,103 bytes，超过原定
48,000 bytes，已没有 pending，停止；此前 50,261 bytes 的必需读回 fallback
已确认保存。故当前明确阻塞是 context，不是重复业务提交或供应商未创建。
真实 cycle149 原始请求离线重建精确复现 49,103 bytes；未调用浏览器/模型。

实验 K（`deepseek-36`）：仅补已确认输入后的菜单交接。已知 ok 的 fill/select
得到新鲜读回，且同页面/tab、无加载/弹窗/新运行时错误、存在有效菜单项，菜单
语义发生变化时，设置 ui_checkpoint，在下一动作前重新规划。只发交接信号，
不授权选项、不修改任务/记忆、不把输入值或菜单变化当成保存成功。目标是修复
deepseek-34 Last Name 填写后正确选项出现但仍执行旧 opener 指令的问题。
仍保留 J、DeepSeek 官方 API 和原预算；context 调整另行验证，避免混合归因。
K 验证：64 项定向、macmini 全量 653 通过/3 跳过，Ruff/diff 通过。真实
deepseek-34 cycle87→88 原观察与 Last Name fill 动作离线回放中，输入值确认后
ui_checkpoint=true，产生一个 input_menu_handoff，无业务保存声明或真实模型/浏览器调用。

K 实际结果：`deepseek-36` 官方 3/15、data_valid=true、strict_success=false，
96 动作/115 cycles、38 feedback calls、Agent 400.44 秒，评分无错误、清理正常。
仅离职单和三条活动通过，没有进入供应商阶段；input_menu_handoff 未在线触发，
不能据此判断 K 的线上收益。Employee Exits 报告反复 reload/readback；既有两次
verification_deferred 之后，brain 又恢复报告检查，未转入财务。cycle115 的 brain
stage_budget 请求最小为 104,310 bytes，超过 96,000 上限；其中自上次完整规划
以来的新观察证据为 55,213 bytes（45 个不同 url/quote 记录，存在大量共同文本），
不是原始 task 变长或业务 pending 无法解除。核验退出条件仍是独立的未解决问题。

实验 L（`deepseek-37`）：只减少 context 中重复表示的开销，不调整预算或丢弃
更多证据。在第 4 层投影共享 memory 内重复 URL 与 readback/checkpoint/page
记录字段结构；pending_writes 不参与改写，全部已投影字段可完整还原。在 brain
规划第 3/4 层投影中，将重复历史观察行编码为 url/line catalog，保留全部记录、
顺序、每个 quote 的全部字符和换行；局部必需读回、当前精确证据、retrieved
quotes、原任务/schema、完成审查不改变。只在加上解码说明后仍节省字节才启用。
真实 J cycle149 请求从 49,103 降为 47,509 bytes，满足原 48,000 上限；真实
K cycle115 brain 请求从 104,310 降为 57,744 bytes，45 个 quote 均逐字还原，
满足原 96,000 上限。两次离线回放无真实模型或浏览器调用。48 项定向测试通过，
实际最终成功率与核验循环是否改善仍需本轮官方评分，不能由压缩结果推断。
L 验证：macmini 全量 659 通过/3 跳过，Ruff/diff 通过。新增回归验证 checkpoint
字段和最近证据精确还原、pending/当前证据/schema 不变、Unicode/CRLF/空行与
顺序保持、原始 notebook 不被修改，以及小请求不引入无收益的编码开销。

L 实际结果：`deepseek-37` 官方 4/15、data_valid=true、strict_success=false，
离职单/三条活动/供应商通过，分录、付款和 Twenty 未完成。88 动作/162 cycles，
48 feedback calls、Agent 810.57 秒；评分无错误、cleanup_error=null。供应商阶段
规划耗尽 120 秒，冻结机制冷却等待 59 cycles 后恢复。K 的 input_menu_handoff
本轮首次在线触发两次（First/Last Name 改变菜单选项）；新规划明确授权正确
e292，完成显示名选择和供应商保存，未重现 deepseek-34 的旧 opener 循环。
J 的阶段能力拒绝未触发，本轮不能补充其 repair 在线收益证据。

Jev 使用第 4 层投影，但供应商保存后下一阶段请求仍从新的历史记录增长到
48,363 bytes（cycle162，原始 145,528 bytes），超过不变的 48,000 上限。
此时 pending_preserved=false；停止原因里的 pending retained 是通用措辞，
本轮并没有尚未确认的业务写入。context 无损共享解决了两个已记录的离线失败点，
却未解决后续状态增长；不能宣称 context 问题已完全修复。brain 历史行编码的
真实 K 故障点仍仅有精确离线证明，本轮未重现同等证据堆积，不能单独估计线上收益。

本批 J/K/L 最终得分为 4/15、3/15、4/15，整体均未严格成功。定向/全量回归与
菜单交接在线进展不能替代最终成功率。接下来优先设计按当前可用字节分配的记忆
投影（保留原任务、当前控件、pending、最近证据与关键节点检索锚点），避免只跨过
单个固定阈值；其次建立稳定的核验义务与有条件重访，禁止同一空报告耗尽预算后
通过新措辞或新页面重新获得无限查询额度；同时验证计划入口实际存在于候选中，
区分缺控件、空阶段授权和被去重等原因。以上后续机制尚未实现，本轮不擅自提高
字节/时间预算或切换模型，不把被基础设施或接口阻塞的结果解释为模型能力上限。

实验 M（2026-10-03）：针对 L 的三个阻塞机制增加回归与联合在线验证。
保持 DeepSeek 官方 deepseek-flash、Jev、原任务、resume-03 memory / resume-02 UI
恢复点和所有预算，不以提高字节上限跨过单个失败点。

- 上下文：普通四档不足时，按实际序列化字节尝试三档历史缩减。旧核验目标、
  旧写入证据和历史节点保留状态、身份与 archive_ref 检索锚点；最近四个关键节点、
  最近两个写入证据/检查点、当前控件、任务、pending 和执行作用域保持不变。
  原始档案不修改，核验完整目标可经已有 evidence_requests 获取，完成审查仍读取原证据。
  精确重建 L cycle162：原始 145,528、旧 level4 48,363、新 pressure3 45,403 bytes；
  当前控件、近期关键节点、pending 一致，0 模型调用 / 0 浏览器动作。
- 阶段：新的 API 规划 schema 要求 stage_entry，明确意图与首个可执行操作。
  入口必须存在于当前真实候选、已获阶段授权且没有被消费；导航阶段不能同时挂旧核验。
  错误通过已有一次 repair 返回给模型，在接受计划前完成检查；局部读回继承原入口。
  候选分页优先放入入口，当前页面阶段禁止切往无关标签页，不重新授权未观察控件。
- 核验：按环境 ID 和页面路由保存 verification_ledger；更换标签页、参数或措辞
  不重置额度，SPA 不同路由保持独立。同一目标只保留一条未解决义务；仅目标页面
  上新确认的写入允许重新核验，其他应用的保存不会使旧报告重试额度复活。
  仍不把 defer、空报告或输入值当成业务完成；新环境的旧 ledger 不是当前证据。

此批是三个机制的联合实验；定向回归分别验证机制，单次联合得分不能单独归因于
某一项修改。按仓库约定先通过 macmini 检查并 push，再用新输出目录自动重跑，
跟到官方评分和环境清理。最终得分另记，离线压缩成功不代表任务成功率提升。

M 实际结果（`deepseek-38`）：macmini 全量 671 passed / 3 skipped，改动文件 Ruff
通过；官方仍为 4/15、data_valid=true、strict_success=false，评分无错误、清理正常。
100 动作 / 118 cycles、56 feedback calls、Agent 810.14 秒。25 个请求使用自适应
历史投影，本轮未因 context 终止；Employee Exits 只保留一条未解决义务并转入财务。
这说明机制在本次路径中工作，但不能证明最终成功率提高。

供应商保存后的 cycle93 暴露新入口检查与旧菜单重授权的顺序冲突：Quick new
曾有当前环境的确认展开记录，菜单已关闭，新阶段提出再次授权，但入口预检先按
旧阶段作用域计算 consumed 例外，拒绝了合法的新提议。repair 转入供应商列表核验，
随后详情抽屉打开时 Quick new 点击返回 Playwright action timed out / unknown，安全停止，
没有重放未知动作。没有模型接口超时。详情抽屉是否遮挡按钮是现场路径推断，
receipt 只证明浏览器动作超时，不能据此宣称业务写入已发生或完全没有发生。

实验 N：仅修复上述授权预检顺序。用拟议 scope 和下一 generation 对已确认菜单
展开进行纯预检；不安装该 scope、不清空 consumed、不派发浏览器动作。整个提议
验证成功后才替换作用域。真实派发仍复用原菜单安全条件：同环境/页面、菜单已关闭、
显式新授权、无 pending、无未知结果；Save/Submit/Publish/Approve 不参与例外。
失败提议保留旧 memory 与 scope，补充合法重用、拒绝未知/打开/待确认/业务按钮的回归。
N 保留 M 的其他机制及全部配置，通过 macmini 检查并 push 后自动重跑，得分另记。

N 验证：macmini 全量 676 passed / 3 skipped、改动文件 Ruff 通过。以 M cycle93
真实观察和 cycle69 确认的菜单记录离线重建：旧作用域拒绝入口、新拟议作用域接受，
consumed 未清空，候选保留 Quick new；0 模型调用 / 0 浏览器动作。授权 generation
在此回放中只重建新旧关系，不声称完整重放运行时状态。

N 实际结果（`deepseek-39`）：官方 3/15、data_valid=true、strict_success=false，
48 动作 / 56 cycles、24 feedback calls、Agent 232.62 秒；评分与清理正常。尚未进入
供应商阶段，不能提供菜单修复的在线收益证据。报告 cycle45 第一次刷新返回 ok，
cycle50 本地语义读回 confirmed，并显示 Nothing to show / 查询执行时间；cycle51
却再次刷新，cycle56 读回 unknown 后安全停止。任务的 Search=Ananya Reddy 规划
输入始终未匹配当前控件，verification_inputs_ready=false，使整个六动作核验额度
从未启动（ledger 为空）。这不是报告加载无限等待，也不是未知业务写入被重放。

实验 O：只修复核验额度启动条件。显式 stage_entry.intent=verify 表示核验尝试已经
开始，查询准备也计入原六动作 / 120 秒额度；不存在或错配的 advisory inputs 不再
关闭额度计时。旧协议没有显式入口时仍按已匹配输入启动。未确认/未知 pending
继续阻止 defer；查询或准备动作已有当前读回确认后，额度耗尽才保留未解决义务，
重新规划独立任务，不再重复查询。没有把空结果改成成功，也没有提高预算或放松
未知写入保护。O 通过远端检查并 push 后自动重跑，配置与恢复点保持一致，得分另记。

O 验证：macmini 全量 677 passed / 3 skipped、改动文件 Ruff 通过。以 N cycle50
真实观察和反馈回放：原 input 匹配仍为 false，但新额度可以启动，原六动作后能
记录未解决义务并转入 fallback；不添加业务确认记录、0 模型调用 / 0 浏览器动作。

O 实际结果（`deepseek-40`）：官方 0/15、data_valid=true、strict_success=false，
15 动作 / 31 cycles、24 feedback calls、Agent 181.36 秒，评分和清理正常。
尚未保存离职单或进入报告，无法提供核验额度修改的在线效果证据。
cycle17 第三行 Pooja 选项点击在派发前返回 stale；pending 已清除，但 last_transition
仍为 resolved=false。cycle19 stale_target_changed、cycles20–30 jev_requested 和
cycle26 no_progress 全被 JsonFeedback 分流到 dynamic_readback，继承旧 stage_goal、
memory 和 scope generation=6。此时活动名已经填好、User 弹窗只剩 Create a new User /
Advanced Search，旧规划却仍要求填空行。最终 repeated state after brain recovery 停止。
这是确定未派发动作被错误保留为读回义务，导致新规划无法执行；没有证据说明
请求超过上下文上限，也不应把它归为模型无法恢复或已填字段必须重复输入。

实验 P：只修复未派发动作的读回分流。stale/rejected receipt 使 last_transition
成为已结束的尝试，保留原 receipt、原始事件和 grounding rejection；不写入确认档案，
不把动作放入 consumed。下一次规划阶段使用完整 StageGuidance，可更新 memory 和
当前控件授权。ok 写入仍通过 pending 进入读回；unknown 仍未解决、保留 pending / consumed，
禁止重放。resolved 在此表示该尝试无后续读回义务，不表示业务成功。
回归覆盖 stale/rejected/ok/unknown 与 stale_target_changed/jev_requested/no_progress
三种触发组合，检查真实 controller → JsonFeedback 请求 schema、memory、scope 和档案。
P 保持原提供方、模型、恢复点、任务和预算；远端验证并 push 后自动重跑，官方结果另记。

P 验证：macmini 全量 689 passed / 3 skipped，改动文件 Ruff 通过。以 O cycle17
真实 Pooja stale receipt、点击前观察和 cycle19 新观察回放，controller 清除 pending，
随后发出 dynamic_feedback / StageGuidance，scope generation 从 6 到 7，memory 更新。
保留 receipt=stale，不增加 consumed 或业务确认；0 模型调用 / 0 浏览器动作。
回放使用模拟新计划验证分流与授权替换，不声称 DeepSeek 必然生成同一恢复计划。

P 在线重跑（`deepseek-41`）无有效分数：三个应用启动正常，但原恢复 recipe 的
derived-reselect 点击 Employee 选项返回 unknown，detail 为 Element is not attached
to the DOM。恢复没有重放，0 controller actions / 0 cycles / 0 模型请求；随后
run_trial 的异常报告调用 controller.result，因 run() 尚未初始化 started 抛出
AttributeError，掩盖原恢复异常并跳过官方评分。data_valid=false，清理正常。
这不是 P 的模型规划在线验证，也不能计作 0/15 或最终成功率变化。

补充修复 Q：构造控制器时初始化报告计时，run() 开始时仍重置原预算计时，
确保恢复阶段失败能报告原异常并继续官方评分与清理。回归用真实 unknown 类型
模拟原选项点击，验证只派发一次、报告原 ValueError/no replay: unknown、无成功记录；
不放宽 unknown 重放规则。保留 P 修改及原实验配置，用新环境和目录重新尝试。

Q 验证：macmini 定向 111 passed；全量 690 passed / 3 skipped，改动文件 Ruff 通过。

P/Q 联合重跑（`deepseek-42`）：官方 4/15（26.7%）、data_valid=true、
strict_success=false。离职单提交、三条正确活动和供应商通过；分录、付款、Twenty
未完成。106 动作 / 127 cycles、56 feedback calls、Agent 660.02 秒，端到端 Agent
683.16 秒（含恢复等）、环境总计 787.44 秒；评分无错误，cleanup_error=null。
提供方仍为 api.deepseek.com / deepseek-flash 和 api.typesafe.ai / jev-latest，
任务 hash、恢复 78 条动作及原始 working-memory hash 不变。

本轮未重现 O 的 Pooja stale 选项消失：stale 发生在 Add row、User fill、Save 等
操作，原有安全重新定位继续生效，没有 stale_target_changed 规划现场。因此 P
修复该分流的证据仍是实际失败点离线回放与回归，不能把本轮越过第三条活动全部
归因于 P。Q 的正常恢复路径没有再次抛出异常，错误报告修复同样只有定向证据。
本轮空 Employee Exits 报告只有一条 ledger/unresolved obligation，耗尽后转入财务，
提供 O 的额度机制在线证据；无终止性 context 超限。总分仍与 deepseek-37/38 的
4/15 持平，单次恢复实验不能证明最终成功率提升。

新停止点：cycle120 填 Posting date=06/30/2026，cycle121 页面规范化为 6/30/2026
并读回确认；Reference # 在 cycle122 也已确认。cycle122 填第一行 Account Search
e198=Rent 返回 ok，但 cycles123–127 同一控件 value 始终为空，未出现对应科目结果。
cycle127 局部读回 unknown 后，一次只读刷新仍无变化，保留 pending，停止原因为
readback unresolved; no resubmission。日期、引用都不是未解决动作；残留日历和
Quick new 菜单可见，但是否造成重渲染/遮挡并无确定因果证据。下一步需区分输入
控件的可见失败、已派发未知状态与业务提交未知状态，再决定安全的 UI 修复路径；
不能只凭 receipt=ok 重试、把空 Account 当成功或提高等待预算掩盖问题。

实验 R（全 DeepSeek baseline）：按用户要求，让所有新增模型操作均使用官方
api.deepseek.com / deepseek-flash：JsonPolicy 做候选动作选择，JsonFeedback 做
阶段规划、输入值生成、局部读回、记忆压缩和完成审查。保持当前浏览器/harness，
仍由浏览器 backend 执行操作，由官方 verifier 判分；不使用 Jev 作新增动作选择。
使用相同 business_031 原任务、resume-03 memory / resume-02 UI 恢复点、600 actions /
600 cycles / 1800 秒 / 1000 feedback calls / 250 candidates，动作上下文 48,000 bytes、
brain 96,000 bytes。继承的 78 条历史仍来自原运行，不改写为 DS 产生；这是一轮
同恢复点对照，不是从零开始的独立完整 episode。

新增 --saas-resume-policy-model 显式迁移开关，记录原策略 model/provider/call kind
及新策略和 brain 的 model/provider；未声明的策略或 brain 变化仍拒绝。新增
JsonPolicy 上下文投影/预算，复用已有共享历史投影并保留候选、任务、pending 与
当前观察；避免 baseline 因忽略上下文限制而获得额外预算。策略请求格式为生成
JSON，与 Jev 分类接口不同；baseline 成绩衡量当前有限候选 harness 中模型替换，
不代表 DeepSeek 完全自主操纵浏览器的上限。本轮先固定浏览器行为，暂停修改
输入自动 Tab 等疑似机制，以免同时更换模型和执行语义；官方结果另记。

R 检查：macmini 定向 86 passed，全量 699 passed / 3 skipped，改动文件 Ruff 通过。

R 初次 baseline（`ds-baseline-43`）：官方 3/15（20%）、data_valid=true、
strict_success=false。提交离职单及三条活动通过，尚未进入财务/Twenty。
26 动作 / 41 cycles、21 feedback calls、Agent 689.47 秒，64 次 HTTP 尝试均为
api.deepseek.com / deepseek-flash，包括 llm_policy / dynamic_*；评分无错误，清理正常。
cycle41 的 llm_policy 连续三次约 30 秒超时，没有 HTTP 响应；此时总预算尚未用完。
transport 将最后一次 asyncio TimeoutError 原样抛出，Controller 把它误报为
wall-clock deadline reached。不能按该 stop reason 断言 1800 秒耗尽，也不能把
这轮受 API 等待限制的分数当 DeepSeek 能力上限。

R 补跑：只将 POLICY_TIMEOUT_SECONDS 从 30 调为 90，与 DS brain 的请求等待一致，
整体 1800 秒、动作/反馈/候选/context 预算和恢复点保持不变。另修复 llm_policy
超时分类：保留原重试次数、原请求和记录，耗尽后抛 ModelCallTimeout，报告模型
调用超时而不是整轮 deadline；不增加浏览器动作、不清除 pending、不重放。
这是明确不同等待上限的第二个配置，分别保存成绩，不混算为同配置重复成功率。

R 补跑检查：macmini 定向 47 passed，全量 701 passed / 3 skipped，Ruff 通过。

R 补跑结果（`ds-baseline-44`）：官方 4/15（26.7%）、data_valid=true、
strict_success=false。离职单 docstatus=1、三条正确活动、供应商显示名/邮箱通过；
分录、付款和 Twenty 均未完成。50 动作 / 70 cycles、44 feedback calls、Agent
1415.09 秒，端到端 Agent 1434.47 秒，环境总计 1514.22 秒。评分无错误，
cleanup_error=null；进程退出、Studio launcher 空闲、slot 0 容器全部清理。

109 次 HTTP 尝试全部为 api.deepseek.com / deepseek-flash：llm_policy 65、
dynamic_feedback 28、dynamic_input 11、dynamic_readback 5。两个 llm_policy
请求各约 90 秒超时，均重试恢复，没有成为最终停止原因。策略请求中位耗时
4.792 秒；混合 `deepseek-42` 的 Jev 请求中位耗时 0.460 秒。已知 token 用量
为输入 1,564,434 / 输出 256,285，两个超时请求用量未知，费用不可据此完整估算。
运行报告与 memory.json 均记录显式 policy/brain migration；任务 hash、恢复
78 条动作 / 107 条证据、原始 working-memory hash 和 600 动作 / 1800 秒预算不变。

具体终止机制：供应商 Save 已成功。cycle69 策略 context 超限时仍有 pending，
进入局部 dynamic_readback；第一次响应 finish_reason=length、内容为空，校验失败，
修复请求后确认保存。cycle70 brain 将下一阶段设为只读验证供应商列表；此时
pending 已清除。JsonPolicy 请求原始 163,072 bytes，历史投影至 level4 /
memory_pressure3，候选由 9 缩至 8 后仍为 51,082 bytes > 48,000。主要内容为
memory 22,831、observation 10,372、task 5,229、candidates 3,130 bytes，另有
系统指令、Chat 封装及转义开销。没有派发该次 HTTP 或浏览器操作，最终停止为
protected context cannot fit; no pending action; no request or resubmission。
尚余约 385 秒总预算，并非 DS 服务返回 context 超限，也未进入 Journal 科目输入。
这是当前 JsonPolicy 请求构造与预算恢复机制的限制；不能用本轮证明 DS 对科目
控件会产生与 Jev 相同的错误，也不能将其归为 DS 的任务推理能力上限。

| 同恢复点对照 | 官方加权得分 | 严格整任务成功 | 停止原因 | Agent 秒 |
| --- | --- | --- | --- | --- |
| Jev + DS / deepseek-42 | 4/15（26.7%） | 否 | Rent 输入读回未解决 | 660.02 |
| 全 DS / ds-baseline-43（30 秒请求等待） | 3/15（20%） | 否 | 动作 API 三次超时，旧报告误分类 | 689.47 |
| 全 DS / ds-baseline-44（90 秒请求等待） | 4/15（26.7%） | 否 | 本地策略 context 预算无法容纳 | 1415.09 |

结论：当前 harness 下全 DS 的本次参考得分为 26.7%，没有高于混合方案；
严格成功为 0/1。这是 business_031 的单次同恢复点对照，不是全套 benchmark
成功率或目标能力上限。43/44 等待配置不同，不合并样本。下一步应优先验证
JsonPolicy 的紧凑候选/Chat 请求构造，以及无 pending 时压缩 context 后继续规划
的恢复路径；保留原任务、当前控件、执行授权和关键写入证据，并在相同预算下
重新对照。Journal autocomplete 的 fill/自动 Tab 问题仍需独立验证。

实验 S（修复全 DS 策略 context）：不提高 48,000 bytes / 96,000 bytes 预算。
动态 JsonPolicy 与 Jev 共享模型侧候选表示，保留 ID、operation、target、精确
绑定值（包括空字符串）及表格列头警告；观测版本、tab/frame、receipt 等执行
元数据仍留在原 Action，由 controller 原样执行，不重复发送给模型。

无 pending 时，JsonPolicy 的控件视图仅保留当前候选页的目标、递归弹窗归属及
所涉表格行的其他控件。保留完整页面文字和所有可见表格，再按既有时间衰减机制
投影旧记忆。候选分页、执行授权、controller/browser 的完整观察与原始记忆均
不改；其他控件仍可通过 next_candidates / request_replan 访问。有 pending 或
候选含未知目标时不缩小控件视图；原 outcome 读回、unknown 不重放及真正保护
内容超限时的停止机制不放宽。静态 JsonPolicy 格式与 Jev 选择语义保持原样。

macmini 离线用 R cycle70 的最终记忆、观察和 8 个候选精确重建原失败请求，
确认旧实现投影后 51,082 bytes；S 后为 47,737 bytes，在 level3 /
memory_pressure0 即可放入 48,000 bytes。该候选页只有 1 个目标控件，129 个
观察控件中的 128 个不进入本次选择请求；任务、原始记忆、完整表格和候选 ID
均不变。无模型调用、无浏览器动作，真实 payload 仅留在远端 /tmp。
回归检查弹窗 owner 递归、同行关联、全表格证据、候选空字符串与列头警告、
pending / 未知目标不裁剪、原 Action 不修改和 HTTP 实际预算。

S 检查：macmini 定向 110 passed，全量 708 passed / 3 skipped，改动文件 Ruff
通过；在线重跑结果另记。在线使用全 DS
deepseek-flash / api.deepseek.com、90 秒动作请求等待、原 resume-03 memory /
resume-02 UI、600 动作 / 600 cycles / 1800 秒，在新目录 ds-baseline-45 重跑。

S 在线结果（`ds-baseline-45`）：官方 3/15（20%）、data_valid=true、
strict_success=false。离职单 docstatus=1 与三条正确活动通过；供应商未保存，
分录、付款和 Twenty 均未进入。52 动作 / 173 cycles、34 feedback calls、Agent
1676.15 秒，端到端 Agent 1698.10 秒，环境总计 1793.50 秒。评分无错误，
cleanup_error=null；手动进程退出、launcher 空闲、slot 0 容器全部清理。
启动核验 104 个源码/测试文件哈希；108 次请求全部为 api.deepseek.com /
deepseek-flash（llm_policy 71、dynamic_feedback 25、dynamic_readback 1、
dynamic_input 11）。原任务、
恢复记忆 hash、78 条动作 / 107 条证据、模型和预算均不变。

本轮没有终止性 context 超限，也没有 policy overflow readback fallback。
但未到达 R 保存供应商后的 cycle70 场景，因此线上未验证越过那个具体失败点；
精确失败点能通过原预算的证据仍是离线回放与回归。官方得分比 R 少 1 分，
不能声称最终成功率提高或仅凭此单次样本断言修复造成退化。

新的失败链有两部分：

1. cycle41、111、165 的 dynamic_feedback 各两次请求超时（约 90+29.5 秒），
   cycle164 的 llm_policy 首次请求约 90.5 秒超时、随后重试成功。7 次超时
   合计 450.52 秒；前两次规划冷却另产生 98 个 planning_scope_wait cycle
   （显式等待合计 194.88 秒）。
   这些是实际模型请求等待超时，不是 API 返回 context 长度错误。
2. cycle162 规划要求先选择显示名 'Ananya Reddy - Ex Employee'，并预期之后
   出现可编辑的 Display Name。cycle164 选择已读回，但显示名仍不符合目标
   'Ananya Reddy'；策略提出填 First Name e109，被原执行授权拒绝，未派发。
   cycle165 的新规划超时后，degraded_planning 沿用旧指导且进入 120 秒冷却。
   cycles166–172 的重新规划均只返回冷却回退；cycle168 的 no_progress 也未
   得到新模型计划，却先将该页面 signature 放入 recovered_states。
   cycle173 因同页再次无进展停止为 repeated state after brain recovery。
   停止时无 pending，尚余约 124 秒总预算，未保存错误显示名或重复提交。

后续应分开验证：只有成功应用新的有效计划才能消耗该页面的“恢复机会”；
冷却、超时、schema/evidence 修复失败不能计作成功恢复，也不应让同一旧指导
在冷却期间反复触发 action policy。另需改进派生字段的规划：未观察到可编辑
Display Name 时不能假设选择后会出现；应观察生成下拉选项的姓名字段并重新
授权输入。S 没有更改这些恢复和规划机制，避免将多个策略同时混入一次对照。
