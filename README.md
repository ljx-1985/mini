# GLaDOS 自动签到

零第三方依赖的 GLaDOS（`glados.cloud`）每日自动签到脚本，支持**本地 Windows 计划任务**与**GitHub Actions 云端定时**两种运行方式。

> 关键契约：GLaDOS 已迁移到 `glados.cloud`，签到请求 `token` 字段必须为 `glados.cloud`（旧脚本填 `glados.one` 会被服务端拒绝）。本脚本已按此实现。成功响应 `code=1`（非旧资料的「已签到」语义），真实每日积分来自响应 `list[].change`。

## 目录结构

```
glados-checkin/
├── glados_checkin.py          # 主脚本（纯标准库，无需 pip install）
├── config.example.json        # 配置模板（Cookie + 推送通道）
├── config.json                # 本地配置（含真实 Cookie，已被 .gitignore 排除，勿提交）
├── run_checkin.bat            # Windows 启动器，双击即可
├── register_task.ps1          # 注册 Windows 每日计划任务
├── .github/workflows/glados-checkin.yml  # GitHub Actions 云端定时
└── README.md
```

## 本地使用

```bash
# 1. 复制配置模板
cp config.example.json config.json

# 2. 填入 Cookie（见下「获取 Cookie」）
# 3. 先干跑验证连通性（只查状态，不签到）
python glados_checkin.py --dry-run

# 4. 正式签到
python glados_checkin.py
```

### 获取 Cookie

1. 浏览器登录 https://glados.cloud
2. `F12` → Network（网络）→ 刷新签到页 → 任意请求 → Headers → Request Headers → `Cookie:` 整行复制
3. 粘到 `config.json` 的 `cookies` 字段（多账号用 `&` 隔开）

### Windows 每日计划任务（可选，本地保险）

```powershell
powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -Time 10:00
```

任务默认每天 10:00 运行，开启「错过补跑」（`StartWhenAvailable`）：当天晚些开机也会自动补跑。
查看 / 立即运行 / 删除：

```bash
schtasks /Query /TN GLaDOS-Daily-Checkin /V /FO LIST
schtasks /Run   /TN GLaDOS-Daily-Checkin
schtasks /Delete /TN GLaDOS-Daily-Checkin /F
```

## GitHub Actions 云端定时（推荐，关机/出差都不漏）

云端运行不依赖你的电脑，GitHub 服务器每天北京时间 **09:30 与 21:30** 各跑一次。

### 部署步骤

1. **新建仓库**：在 GitHub 新建一个仓库（如 `glados-checkin`），公开私有均可。
2. **推送本目录**（见下「推送到 GitHub」）。
3. **配置 Secrets**（仓库 `Settings → Secrets and variables → Actions → New repository secret`）：
   | Name | Value |
   |---|---|
   | `COOKIES` | 你的完整 Cookie 字符串（`koa:sess=...; koa:sess.sig=...`） |
   | `PUSHPLUS` | （可选）PushPlus token，用于微信推送签到结果 |
4. **启用 Actions**：仓库 `Actions` 标签 → 左侧 `GLaDOS Auto Checkin` → `Enable workflow`。
5. **手动触发验证**：`Actions → GLaDOS Auto Checkin → Run workflow`。

> Cookie 失效后（登录过期）只需更新 `COOKIES` Secret，无需改代码。

### 推送命令

```bash
# 在 glados-checkin/ 目录下
git init
git add .
git commit -m "GLaDOS auto checkin"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

> `config.json` 已在 `.gitignore` 中排除，不会误提交真实 Cookie。

## 命令行参数

| 参数 | 作用 |
|---|---|
| `--config PATH` | 指定配置文件，默认 `./config.json` |
| `--dry-run` | 只查询账号状态，不发送签到请求 |
| `--selftest` | 离线自检（不联网，验证逻辑） |
| `--no-notify` | 本次不推送 |
| `--quiet` | 仅在日志文件记录 INFO（CI 友好） |
| `--notify-test` | 用占位结果测试推送通道 |

## 推送通道（可选）

`config.json` 的 `notify` 支持 PushPlus / Server酱 / 企业微信 / Telegram 四类，留空即关闭；也可通过环境变量 `PUSHPLUS` / `SERVERCHAN_KEY` / `WECOM_WEBHOOK` / `TG_BOT_TOKEN` / `TG_CHAT_ID` 注入（CI 场景）。推送失败不影响签到结果。

## 契约说明

- 签到：`POST /api/user/checkin`，body `{"token": "glados.cloud"}`
- 状态：`GET /api/user/status`（邮箱、剩余天数）
- 积分：`GET /api/user/points`（真实日增减在签到响应的 `list[].change`）
- 鉴权：仅 `Cookie` 头，无 Token
- `code ∈ {0, 1}` 视为成功；`code=1` 为实际成功语义（非「已签到」）
