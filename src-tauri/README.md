# src-tauri — 原生桌面壳（Tauri 2）

AudiobookStudio 的桌面外壳。窗口里加载 Vue 3 前端，前端再调用本地 Python 后端
（`http://127.0.0.1:8642`）。真正的处理逻辑都在 Python 后端；这个 Rust 工程只负责
提供一个原生窗口，以及原生文件对话框 / 打开目录的能力。

> ⚠️ 构建这个桌面壳需要 **Rust 工具链**（`rustup` + 平台工具集）。
> 如果你的机器没有装 Rust，**完全不需要装**——用下面的「无 Rust 运行方式」即可跑完整应用。

## 结构

```
src-tauri/
├── tauri.conf.json        # 窗口 / 构建 / 打包 / CSP 配置
├── Cargo.toml             # Rust 依赖（tauri 2 + dialog + shell 插件）
├── build.rs
├── src/
│   ├── main.rs            # 入口（调用 lib 的 run()）
│   └── lib.rs             # 注册 dialog / shell 插件
├── capabilities/
│   └── default.json       # 权限（原生文件对话框 + 打开文件/目录）
├── icons/                 # 应用图标（占位渐变图标，可替换）
└── app-icon.png           # 1024×1024 图标源（供 `tauri icon` 生成全套）
```

## 前提（仅构建/运行桌面壳时需要）

- **Rust**：`winget install rustup` 或 <https://rustup.rs>，装完执行 `rustup default stable`。
- **Windows 链接器**：Visual Studio Build Tools（MSVC），或 VS 里的
  “Desktop development with C++” 工作负载。
- **Node.js**：项目根已装好（`node_modules`）。
- 首次构建会自动拉取 Rust 依赖（`tauri` 等 crate），需要联网。

## 开发运行

先起后端，再起壳（两个终端）：

```powershell
# 终端 1：Python 后端
.venv\Scripts\python -m backend.main        # → http://127.0.0.1:8642

# 终端 2：桌面壳（会自动 `npm run dev` 起 Vite 5173，再开窗口）
npm run tauri dev
```

壳启动后轮询 `GET /api/health`，就绪即加载界面。

## 打包发布

```powershell
npm run tauri build
```

产物在 `src-tauri/target/release/bundle/`（Windows 为 `nsis/*.exe` 安装程序与 `msi`）。

> 打包版默认**不包含** Python 后端——发布/运行机器仍需按上面的命令单独运行后端，
> 或后续把后端打成 sidecar 可执行（如 PyInstaller）随应用一起启动。那是「正式分发」
> 的下一步，不影响你现在开发 / 自用。

## 图标

`icons/` 里是一套占位渐变图标（含 Windows 用的 `icon.ico`），开箱即可构建。
想换成自己的图标：

1. 准备一张 1024×1024 的方图，放到 `src-tauri/app-icon.png`（已有一个占位源）。
2. 运行 `npm run tauri icon src-tauri/app-icon.png` 重新生成全套（含 macOS `.icns`）。

> 在 **macOS** 上构建前必须先跑这一步（本仓库未附带 `.icns`）。

## 无 Rust 运行方式（推荐自用 / 快速验证）

完全不需要 Rust 工具链，两条路：

**A. 浏览器 + Vite（开发态）**

```powershell
.venv\Scripts\python -m backend.main   # 后端 8642
npm run dev                            # Vite 5173，浏览器打开 http://localhost:5173
```

**B. 浏览器 + 后端托管（生产态，最简）**

```powershell
npm run build                          # 前端 → dist/
.venv\Scripts\python -m backend.main   # 后端同时托管 dist/
# 浏览器打开 http://127.0.0.1:8642 即是完整应用
```

后端已内置静态托管：真实资源（`/assets/…`）从 `dist/` 读取，其余路径回退 `index.html`
交给前端路由，`/api/*` 永远优先——已验证 `/`、`/text`、`/book` 均返回 SPA 入口，
`/api/health` 仍返回 JSON。

## 常见问题

- **窗口打开但界面空白 / 一直转圈**：多半是后端没起。先确认
  `http://127.0.0.1:8642/api/health` 返回 `{"ok":true,…}`。
- **`npm run tauri` 报找不到 cargo / rustc**：装 Rust（见「前提」），重开终端让 `PATH` 生效。
- **文件选择 / 打开目录没反应**：确认 `capabilities/default.json` 保留了
  `dialog:default` 与 `shell:allow-open` 两项权限。
- **浏览器里选文件 / 下载输出**：非桌面环境自动走「上传到 `input/` + 链接下载」的
  兜底逻辑，功能完整。
