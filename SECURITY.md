# Security Policy / 安全政策

## 支持的版本 / Supported Versions

| 版本 | 支持状态 |
|---|---|
| v1.1.x（latest） | 积极维护 |

## 报告漏洞 / Reporting a Vulnerability

发现安全漏洞或隐私风险时，请按以下方式报告：

1. **首选**：GitHub 私有漏洞报告（仓库页面 → `Security` → `Report a vulnerability`）
2. 备选：在 [Issues](https://github.com/baizi51676-source/astrbot_plugin_deepseek_peak_switch/issues) 提交，**请勿在正文中包含任何 API Key / Token / 账号信息**
3. 报告请包含：
   - AstrBot 版本、运行平台（Windows / Linux）
   - 复现步骤与现象
   - 日志中 `[deepseek_peak_switch]` 开头的相关行

我们会在 **7 天内**确认并评估，修复后发布补丁版本。

## 安全注意事项 / Security Notes

- 本插件**不收集、不存储、不上传**任何模型 API Key、账号凭据或聊天内容；模型调用均由 AstrBot 的「模型提供商」配置完成，本插件只负责选择使用哪一个。
- 插件仅会向 DeepSeek 官方定价文档地址发起**只读 GET 请求**（用于解析高峰/低峰时段），地址可在插件配置中修改；关闭「定时拉取官方文档」后不再发起任何网络请求。
- 插件会在本地缓存时段数据（`data/plugin_data/astrbot_plugin_deepseek_peak_switch/`），不包含任何敏感信息。
- 建议为 AstrBot 配置**最小权限**的凭据，并定期轮换。