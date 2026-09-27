# 本项目实际验证记录

日期：2026-09-27。平台：macOS arm64，Python 3.13.13，Playwright 1.63.0，Chromium 153.0.8010.12；viewport 1280 × 900。

本次从空目录实现并验证。用户提供的历史记录另存于 `REFERENCE-VERIFICATION.md`，其 Python/Chromium 版本、12 项测试和 94 步轨迹均不作为本次结果。

## 自动测试

`python -m pytest -q`：**35 passed in 22.10s**。其中 16 个真实 Chromium 用例，19 个协议/HTTP mock/指标用例。完整输出见 [test-report.txt](test-report.txt)。

覆盖：flat / once / event；32 条长目录；弹窗与页面提示注入；排序重排；延迟保存；DOM 版本失效；相同 HTML 替换后的句柄失效；隐藏、透明和视口外内容排除；未知写入禁止重提；虚假完成；完成时重新观察；停滞与预算恢复；CAPTCHA 升级；绑定输入与下拉；DAG/DSL 拒绝无效输入；来源冲突；Jev 和生成式 HTTP 协议；失败调用计费留存；配对统计与校准。

`ruff check src tests` 与 `ruff format --check src tests` 均通过。

## 最终浏览器矩阵

运行命令：

```bash
python -m jev_browser matrix --sizes 4 12 32 --popup --injection --output runs/final-matrix
```

所有运行使用规则局部策略；once / event 使用规则规划器。没有调用真实 Jev、LLM 或 VLM。全部开启初始弹窗和页面提示注入。判分在策略停止后读取隔离的沙盒终态。

| 模式 | 记录数 | 原子动作 | 决策循环 | 规划调用 | 严格成功 |
|---|---:|---:|---:|---:|---|
| flat | 4 | 18 | 18 | 0 | 是 |
| once | 4 | 20 | 19 | 1 | 是 |
| event | 4 | 20 | 19 | 1 | 是 |
| flat | 12 | 50 | 48 | 0 | 是 |
| once | 12 | 50 | 48 | 1 | 是 |
| event | 12 | 50 | 48 | 1 | 是 |
| flat | 32 | 130 | 124 | 0 | 是 |
| once | 32 | 130 | 124 | 1 | 是 |
| event | 32 | 130 | 124 | 1 | 是 |

9/9 严格成功；每条轨迹详情访问覆盖 100%；无违规、无重复写入、无错误完成请求。含启动/收尾的端到端延迟 p50 2.606 秒、p95 5.877 秒。等待时序会影响原子动作数，不能据此推断调度策略优劣。

原子动作包含抽取、等待和写入读回；一个控制循环可能执行抽取后等待，因此循环数不保证大于动作数。每次仅执行一个改变页面状态的动作，然后重新观察。

故障无关的参考动作数分别为 16 / 44 / 116，来自固定正确解的点击、显式抽取和读回计数；实际等待和恢复不计入参考长度。这个沙盒主要改变 H，不能代表已经独立控制 D/L 的研究任务。

产物位于 `runs/final-matrix/<mode>-<records>-0/`，包含完整 JSONL 轨迹、记忆、来源、模型/环境 manifest、终态截图与 `trace.zip`。汇总另见 [matrix-report.txt](matrix-report.txt)。最终源代码 hash：

```
620def15548b533425c6d557a10435bce72bb7b33922c695fed08aa4df6e6396
```

## 未知写入故障

```bash
python -m jev_browser demo --records 4 --lost-ack --output runs/final-unknown-write
```

结果为预期的 `needs_attention`，原因 `unknown write outcome; no resubmission`；12 次原子动作。判分器确认第一条记录已经保存，但页面没有提供新的成功读回证据。控制器保留 pending 状态并停止，重复提交为 0，违规为 0；整体严格成功为 false，未将安全停止算成任务成功。

## 验证边界

- Jev、生成式规划器和生成式局部策略只完成 HTTP mock 契约测试，真实模型服务未请求。
- BrowserGym、WebArena、WebChoreArena 和其他公共 benchmark 没有部署或运行。
- 没有 VLM、多站点、多标签长任务或复杂 frame/shadow/canvas 系统测试。
- 定价和浏览器资源单价未配置，所有总成本与每次成功成本为 `null`；不能宣称成本优势。
- CI 配置已提供，本次没有远程 CI 执行结果。
- Chromium 在默认 macOS 执行沙盒下因 MachPort 权限无法启动；经工具授权的本地进程执行完成验证。未更改系统或浏览器管理策略。

这些结果证明本地工程控制闭环和规定故障保护通过了当前测试，不能证明 Jev 的长任务能力或事件分层相对其他模型的研究收益。
