# Staycation 运行连接失败排查

## 结论

此次可证实的故障点是 Clash/Mihomo 的 DeepSeek 直连出口：分流命中 `GeoIP(cn)`，选择 `全球直连[DIRECT]`，上游 TCP 连接超时。应用看到的 `SSLEOFError` 是连接被中断后的表象，并不能据此认定证书或模型协议有问题。

2026-09-27 20:10 左右复查时，原路由已能建立连接；未改系统代理、DNS、证书校验、API 地址或模型。后续三次实际模型 POST 均返回 HTTP 200。此次没有通过改代码“修好”网络，恢复原因尚不能确定。

## 证据链

1. 系统解析 `api.deepseek.com` 得到 `198.18.0.210`，属于 Clash fake-IP 地址范围。应用到本机 TUN 的 TCP 建连通常只需 3–11 毫秒，这并不代表已经连通 DeepSeek 上游。
2. 应用在 `start_tls` 等待约 5 秒后收到 `ConnectError → ConnectError → EndOfStream → SSLEOFError`。
3. 对应时间的 Clash 日志明确记录上游 TCP 超时。例如北京时间 19:53:35.431 的 `python3.13` 请求命中 `GeoIP/cn`，对 `119.36.225.121:443` 和 `123.148.116.184:443` 的拨号均 `i/o timeout`，与应用 19:53:30.290 发起、约五秒后失败的请求对应。
4. 19:59:10 的两条日志分别来自 `127.0.0.1` 显式代理和 `198.18.0.1` TUN，二者都选择同一个 `DIRECT` 出口并超时。因此此前比较“默认路径”和“本机代理”没有真正换出口；设置 `trust_env=False` 也不能绕过系统 TUN。
5. 纯 DS 成功轮在 19:42:53 也有一次同型 TLS 失败，重试后成功。Jev 首轮既有一次 Jev API TLS 失败，也有 DeepSeek 失败；不能将这些网络异常解释为某个模型不会执行任务。

首次 Jev + DS 中还有一条不同的失败：DeepSeek `dynamic_input` 已发送请求体，等待响应头约 40.62 秒后出现 `RemoteProtocolError`。由于较早的代理日志已滚动，无法将该次中断精确定位到同一个上游原因。它也说明失败请求的费用不能一律当作零。

## 恢复验证

- 原环境、`trust_env=False`、显式 `http://127.0.0.1:7897` 三条访问路径均获得 DeepSeek HTTP 401；这是未带凭据 GET 的预期响应，验证 TLS 和 HTTP 连通。
- 使用项目原有 `ModelTransport`、原模型 `deepseek-flash`、原凭据，关闭请求重试进行三次极短 POST：全部 HTTP 200，用时约 2.689、0.434、0.524 秒；后两次复用连接。
- 三次诊断请求仅用于连接验证，输出上限为 8 token，全部消耗在推理 token，未生成可见答案；不能将其算作任务成功或内容质量测试。
- 诊断调用用量独立保留，不混入视频中的任务 API 费用。
- 完整小红书任务使用新目录 `jev-ds-verified` 复跑成功：端到端 59.1808 秒，12 次模型请求全部 HTTP 200，无连接重试，已知 API 等价费用 $0.015478554。执行了搜索并输出总结；“成功”仍是项目语义复核通过，不是独立事实核验。结果以该目录的 `report.json` 为准。

## 后续处理

当前网络恢复，不需要改模型策略或放宽证书校验。若再次复现，应先核对 Clash 的实际出站规则；需要更换出口时，只针对 `api.deepseek.com` 指向已经验证可用的线路，再验证实际模型 POST。仅设置 `HTTPS_PROXY` 指向当前 Clash 端口不会改变其 `DIRECT` 分流。

提高 Python 超时时间无法解除代理侧约 5 秒的上游拨号失败。现有有界重试及连接复用对偶发失败有帮助，但不能替代出口连通性。现有日志不足以进一步区分本地网络、运营商链路和上游 CDN 节点，也不能证明代理软件本身有 bug。

## 原始资料

- [DeepSeek 专用代理日志摘录](../runs/staycation-ds-vs-jev-20260927/diagnostic-route-evidence.json)
- [三次真实 POST 的阶段计时与用量](../runs/staycation-ds-vs-jev-20260927/diagnostic-posts.json)
- [首次 Jev + DS](../runs/staycation-ds-vs-jev-20260927/jev-ds/report.json)
- [重试](../runs/staycation-ds-vs-jev-20260927/jev-ds-retry/report.json)
- [再次尝试](../runs/staycation-ds-vs-jev-20260927/jev-ds-recovery/report.json)
- [恢复后的完整任务验证](../runs/staycation-ds-vs-jev-20260927/jev-ds-verified/report.json)
