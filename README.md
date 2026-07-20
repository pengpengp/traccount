# Trae Account Manager (TAM)

Trae IDE（字节跳动 AI IDE）自动化账号管理系统：自动注册 + 一键切换 + 额度查询 + 本地 Web Dashboard。

- 直接调用 Bytedance Passport API 注册账号（无需浏览器）
- 通过 emailnator.com 的 Gmail dot-trick 获取 `@gmail.com` / `@googlemail.com` 邮箱，绕过 Trae 的临时邮箱域名屏蔽
- JWT、Cookie、密码使用 AES-256-GCM 加密后存入本地 SQLite
- 一键切换账号 = 轮换设备指纹 + 写入 cookies/license.dat + 重启 Trae
- CLI + 本地 FastAPI Dashboard（默认绑定 `127.0.0.1`，不对外暴露）

> ⚠️ **免责声明**：自动化注册可能违反 Trae 服务条款。本工具仅供学习与研究使用，使用本工具产生的一切后果由使用者自行承担。

---

## 目录

- [系统要求](#系统要求)
- [安装](#安装)
- [配置](#配置)
- [快速开始](#快速开始)
- [CLI 命令参考](#cli-命令参考)
- [Web Dashboard](#web-dashboard)
- [工作原理](#工作原理)
- [常见错误码](#常见错误码)
- [项目结构](#项目结构)

---

## 系统要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10/11 或 macOS（Trae IDE 仅在这两个系统运行；Linux 仅用于测试/CI） |
| Python | 3.10+（推荐 3.11/3.12） |
| 网络 | 需要能访问 `trae.ai`、`emailnator.com` 的 HTTP(S) 代理（默认 `http://127.0.0.1:10808`） |
| Trae IDE | 已安装（切换/捕获功能需要；纯注册可不需要） |
| Playwright | **可选** — 默认 emailnator 流程不需要浏览器；仅当需要 DeepMails fallback 邮箱源（Cloudflare Turnstile 保护的站点）时才安装 |

---

## 安装

```bash
# 1. 克隆仓库
git clone <your-repo-url> traeaccount
cd traeaccount

# 2. 创建并激活虚拟环境
python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows PowerShell:
# .\.venv\Scripts\Activate.ps1

# 3. 安装本包（含 CLI 入口 tam）— 默认依赖 ~30MB，无需下载浏览器
pip install -e .
```

安装完成后会有 `tam` 命令可用：

```bash
tam version
# tam 1.0.0
```

### 可选：启用 DeepMails 浏览器邮箱源

只有当默认 emailnator 失败、需要 fallback 到 deepmails.org（Cloudflare Turnstile 保护的临时邮箱）时才需要：

```bash
pip install -e ".[browser]"
playwright install chromium   # 下载 Chromium 浏览器（约 150MB）
```

不安装也不会影响默认流程；`DeepMailsProvider` 会在 `default_pool()` 中自动跳过。

---

## 配置

### 代理（重要）

Trae 的 `send_code` / `register_verify_login` / `Login` / `GetUserToken` 接口对中国大陆 IP 有 per-IP 限流（约 10 次/分钟），强烈建议通过境外代理访问。

```bash
# 默认代理地址（config.py）
http://127.0.0.1:10808

# 自定义：设置环境变量
export TAM_PROXY="http://127.0.0.1:7890"        # Linux/macOS
setx TAM_PROXY "http://127.0.0.1:7890"          # Windows（永久）
```

关闭代理：

```bash
export TAM_PROXY=none
```

### Trae 可执行文件路径

切换账号时会自动重启 Trae。如果自动扫描失败，手动指定：

```bash
# Windows
tam set-path "C:\Users\<you>\AppData\Local\Trae\Trae.exe"

# macOS
tam set-path "/Applications/Trae.app/Contents/MacOS/Trae"

# 查看
tam path
```

### 主加密密钥（可选）

`secrets_blob` 使用 AES-256-GCM 加密，密钥按以下顺序解析：

1. 环境变量 `TAM_MASTER_KEY`（32 字节 base64 或 hex）
2. 系统钥匙串（macOS Keychain / Windows Credential Manager / Linux SecretService）
3. 本地文件 `~/.trae_account_manager/master.key`（自动生成）

多机迁移时设置同一 `TAM_MASTER_KEY` 即可解密。

### 环境变量一览

| 变量 | 默认 | 说明 |
|---|---|---|
| `TAM_PROXY` | `http://127.0.0.1:10808` | 出站 HTTP 代理 |
| `TAM_DATA_DIR` | `platformdirs.user_data_dir("trae_account_manager", "tam")` | 数据库/日志/备份目录 |
| `TRAE_DATA_DIR` | 自动检测 | Trae IDE 的 user-data 目录（Linux 测试用） |
| `TAM_MASTER_KEY` | — | AES 主密钥，覆盖钥匙串 |

---

## 快速开始

```bash
# 0. 确认代理可用
curl -x "$TAM_PROXY" -I https://www.trae.ai/

# 1. 注册 1 个账号（默认 emailnator + googleMail 选项）
tam register 1

# 2. 查看账号列表（成功注册的会显示 status=active）
tam list

# 3. 查看当前账号
tam current

# 4. 查询额度（Free: 10 fast / 50 slow / 1000 advanced / 5000 autocomplete）
tam usage

# 5. 切换到指定账号（自动重启 Trae）
tam switch <account_id>

# 6. 启动 Web Dashboard
tam serve
# 浏览器打开 http://127.0.0.1:8765
```

---

## CLI 命令参考

所有命令以 `tam` 为前缀。完整帮助：`tam --help`。

### `tam register [COUNT]`

注册一个或多个新账号。

```bash
tam register 1                       # 注册 1 个
tam register 5 -c 3                  # 注册 5 个，最大并发 3
tam register 1 --headed              # 显示浏览器（调试用）
tam register 1 --no-persist          # 不写入数据库（仅测试）
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `COUNT` | `1` | 注册数量（1–50） |
| `-c, --concurrency` | `2` | 最大并发（1–10） |
| `--headed / --no-headed` | `--no-headed` | 显示浏览器窗口 |
| `--no-persist` | False | 不持久化到数据库 |

流程：
1. `emailnator.com` 生成 `@googlemail.com` 邮箱（dot-trick，~40% fresh rate）
2. `POST /passport/web/email/send_code/` 触发 OTP 邮件
3. 轮询 emailnator 收件箱（最多 180s），提取 6 位 OTP
4. `POST /passport/web/email/register_verify_login/` 创建账号
5. `POST /cloudide/api/v3/trae/Login` 获取 `X-Cloudide-Session` cookie
6. `POST /cloudide/api/v3/common/GetUserToken` 获取 JWT
7. AES 加密后写入 SQLite

### `tam list [--active]`

列出所有账号。`--active` 只显示 `status=active`。

```
*  76637484 tr.ick.18.1.98.8@googlemail.com  tr.ick  SG  Free   active   a1b2c3d4…
```

带 `*` 标记的是当前账号。

### `tam current`

显示当前驱动 Trae IDE 的账号详情（region / plan / user_id / machine_id / status）。

### `tam switch <ACCOUNT_ID> [--launch/--no-launch] [--reset-registry]`

切换 Trae 到指定账号。

```bash
tam switch 76637484                   # 切换并重启 Trae
tam switch 76637484 --no-launch       # 切换但不重启
tam switch 76637484 --reset-registry  # Windows 额外重置 MachineGuid（更强隔离）
```

`ACCOUNT_ID` 可以是完整 id、id 前缀或邮箱（大小写不敏感）。流程：

1. 杀掉运行中的 Trae 进程
2. 生成/恢复该账号的设备指纹（machineId、macMachineId、devDeviceId、sqmId）
3. 清理 Trae 运行时缓存
4. 写入 `storage.json` 的 telemetry 字段 + `iCubeAuthInfo`
5. Windows 可选：重置注册表 `MachineGuid`
6. 写入 `license.dat` 和 session cookies
7. 启动 Trae

### `tam capture [--name NAME] [--email EMAIL]`

把当前 Trae IDE 的 live session 导入为本地账号（用于手动登录后备份）。

```bash
tam capture --name "mywork" --email "me@gmail.com"
```

### `tam clear [--launch]`

把 Trae 重置到初始状态（登出）。`--launch` 切换后立即重启 Trae。

### `tam delete <ACCOUNT_ID>`

从本地数据库删除账号（不影响 Trae 服务端）。

### `tam add --email ... --token ...`

手动添加一个账号（例如从别处导出的 JWT）。

```bash
tam add \
  --email me@gmail.com \
  --token eyJ... \
  --refresh-token rt_... \
  --user-id 7663748457840935943 \
  --region SG \
  --name "manual"
```

### `tam usage [ACCOUNT_ID]`

查询 Trae 用量额度。不带参数时查询当前账号。

```
 metric            limit       used      left
 plan              Free
 fast requests     10          0.0       10.0
 slow requests     50          0.0       50.0
 advanced models   1000        0.0       1000.0
 autocomplete      5000        0.0       5000.0
```

### `tam set-path <PATH>`

设置 Trae 可执行文件路径（见上文配置）。

### `tam path`

显示当前 Trae 可执行文件路径。

### `tam info`

显示环境信息（版本、Trae 数据目录、Trae exe、数据库路径）。

### `tam serve [--host HOST] [--port PORT]`

启动本地 Web Dashboard。默认 `127.0.0.1:8765`。

### `tam version`

显示版本号。

---

## Web Dashboard

```bash
tam serve
# 启动后访问 http://127.0.0.1:8765
```

功能：
- 账号列表 / 详情 / 添加 / 删除
- 一键注册（后台运行，通过 WebSocket 推送进度日志）
- 一键切换、清理登录状态、捕获当前会话
- 查询用量额度
- 设置 Trae 路径

WebSocket 端点 `/ws` 推送事件：

| type | 说明 |
|---|---|
| `hello` | 连接建立 |
| `register_start` | 注册批次开始 |
| `register_result` | 单个账号结果 |
| `register_done` | 批次完成 |
| `register_error` | 批次异常 |
| `switch` | 切换结果 |

REST API 概览：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/accounts` | 列出账号 |
| GET | `/api/accounts/{id}` | 账号详情 |
| POST | `/api/accounts` | 手动添加 |
| DELETE | `/api/accounts/{id}` | 删除 |
| POST | `/api/switch/{id}` | 切换 |
| POST | `/api/capture` | 捕获当前会话 |
| POST | `/api/clear` | 清空登录 |
| POST | `/api/register` | 触发批量注册 |
| GET | `/api/usage/{id}` | 用量查询 |
| GET / POST | `/api/path` | 查看/设置 Trae 路径 |
| GET | `/api/info` | 环境信息 |

---

## 工作原理

### 注册流程（直接 API，无浏览器）

```
emailnator.com → @googlemail.com 邮箱
        │
        ▼
POST /passport/web/email/send_code/      → email_ticket
        │  (轮询 emailnator 收件箱，提取 OTP)
        ▼
POST /passport/web/email/register_verify_login/   → user_id + cookies
        │
        ▼
POST /cloudide/api/v3/trae/Login          → X-Cloudide-Session cookie
        │
        ▼
POST /cloudide/api/v3/common/GetUserToken  → JWT + RefreshToken + ExpiredAt
        │
        ▼  AES-256-GCM 加密
SQLite (accounts.db)
```

### 切换流程

```
kill Trae → 生成/恢复设备指纹 → 清理缓存
   → 写 storage.json (telemetry + iCubeAuthInfo)
   → 写 license.dat + cookies
   → (可选) 重置 Windows MachineGuid
   → launch Trae.exe
```

### 关键技术点

- **Gmail dot-trick**：`abc.def@gmail.com` 和 `a.bcdef@gmail.com` 路由到同一个真实 Gmail，但 Trae 当作不同注册地址 → emailnator 借此提供无限邮箱。
- **`@googlemail.com` 优于 `@gmail.com`**：Google 视为同一邮箱，Trae 当作不同 → fresh rate ~40%（vs `@gmail.com` 仅 0–10%）。
- **PascalCase 响应**：Trae API 返回 `{"Result": {"Token": ...}}`（不是 `{"result": {"token": ...}}`）。
- **X-Cloudide-Session cookie**：由 Trae Login 的 `Set-Cookie` 设置，是 GetUserToken 认证的关键。
- **设备指纹轮换**：Trae 对每台设备绑定账号数有限，切换前必须轮换 `machineId/macMachineId/devDeviceId/sqmId`。

---

## 常见错误码

| error_code | 含义 | 处理 |
|---|---|---|
| `1023` | Email is linked to another account | 邮箱已被注册，自动换新邮箱重试（最多 8 次） |
| `17` | Couldn't log in. Try again. | 风控/限流，短退避 2–10s |
| `1206` | Maximum number of attempts reached | send_code per-IP 限流，长退避 30s+ |
| `20116` | This mailbox domain is at risk | 临时邮箱域名被屏蔽（已用 Gmail dot-trick 绕过） |
| `20310` | get session empty | 缺少 X-Cloudide-Session cookie，需先调 Trae Login |

常见问题排查：

- **注册一直失败**：检查代理是否能访问 `https://www.trae.ai/` 和 `https://www.emailnator.com/`
- **`1023` 频繁**：emailnator 的 `@gmail.com` 池可能被其他 Trae 用户耗尽，改用 `googleMail` 选项（已在代码中默认）
- **`1206` 限流**：调低并发 `-c 1`，或换 IP/代理
- **GetUserToken 返回 401**：检查日志是否出现 `20310`，确认 Trae Login 成功
- **切换后 Trae 仍用旧账号**：确认 `tam current` 正确，必要时 `--reset-registry` 重置 Windows MachineGuid

---

## 项目结构

```
traeaccount/
├── trae_account_manager/
│   ├── __init__.py              # __version__
│   ├── cli.py                   # Typer CLI（14 个命令）
│   ├── config.py                # 路径/代理/区域 host
│   ├── models.py                # Account SQLModel
│   ├── db.py                    # SQLite 持久化
│   ├── vault.py                 # AES-256-GCM 加密
│   ├── machine.py               # 设备指纹 + iCube auth
│   ├── process_ctl.py           # Trae 进程控制（Win/macOS）
│   ├── switcher.py              # 账号切换 + capture + clear
│   ├── register.py              # 直接 API 注册流程
│   ├── trae_api.py              # Trae API 客户端（usage/token）
│   ├── mail/
│   │   ├── base.py              # EmailProvider 抽象
│   │   ├── emailnator.py        # ★ 默认 provider（Gmail dot-trick）
│   │   ├── mailtm.py            # mail.tm（fallback）
│   │   ├── tempmail.py          # tempmail.lol（fallback）
│   │   ├── tempmailplus.py      # TempMailPlus（fallback）
│   │   ├── deepmails.py         # deepmails（fallback）
│   │   └── pool.py              # 多源邮箱池 + failover
│   └── web/
│       ├── app.py               # FastAPI + WebSocket
│       └── static/index.html    # 单页 Dashboard
├── tests/                       # 62 个单元测试 + e2e
├── scripts/                     # 调试/探测脚本
├── pyproject.toml               # 包定义 + tam 入口
└── README.md
```

---

## 开发与测试

```bash
# 运行测试套件
pytest

# 指定模块
pytest tests/test_register.py -v
```

数据目录默认位置：

| OS | 路径 |
|---|---|
| Windows | `%APPDATA%\trae_account_manager\tam\` |
| macOS | `~/Library/Application Support/trae_account_manager/` |
| Linux | `~/.local/share/trae_account_manager/` |

包含：`accounts.db`、`logs/`、`backups/`、`master.key`（未设 `TAM_MASTER_KEY` 时）。

---

## License

MIT
