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

API 阶段规划/最终复核的单次逻辑调用最多 120 秒，其它辅助调用最多 60 秒，最多重试一次，
单个请求可完整使用剩余调用额度，不再切为两个强制短超时；并服从运行剩余时间、预留 5 秒收尾。
模型调用超时不能冒充总预算耗尽。没有待确认动作时，可选规划超时降级到同路由旧指导或原目标，
完整保留工作记忆，并对该路由的可选规划冷却 120 秒，避免每个阶段重复等待。
降级的简短指导引用原任务，不把完整长 prompt 复制到有长度上限的 `next_goal`；跨路由清除旧字段绑定；
输入、写入回读及最终复核仍是必需调用，超时报告 `needs_attention`，不派发未知输入、不确认或重放动作。
恢复、无进展恢复和新增行指导使用紧凑规划 schema。
checkpoint 在动作准备、回执、确认及外层取消时原子落盘，保留未知写入和确认记录；
模型取消或 trace 导出失败不能抹掉最终 memory。每次修改后仍沿用原 provider 和总预算自动复跑。

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
