# AudiobookStudio

一个桌面应用,在一个地方完成中文有声书的完整制作流程:**文本排版 → 分册切割 → TTS 合成 → 音频分集**,外加任务中心与设置。

## 功能

- **文本排版** — 对原始 TXT 做确定性排版(空白 / 段落 / 标点 / 章节,10 个可配置开关),结果内容保持、可重复排版
- **分册切割** — 按章节边界把长文均衡切分为若干分册,只在章节处切割、不重编号,拼回所有分册可精确复现原文
- **TTS 合成** — 流水线占位模块:导航 / 配置 / 接口已就位,引擎待接入(当前返回"即将推出")
- **音频分集** — 把长音频无损切分为若干集(`-c copy` 不重编码),支持按停顿位置智能对齐边界
- **任务中心** — 长时任务的实时进度 / 日志 / 暂停 / 恢复 / 取消 / 重试;失败任务自动隔离,不影响整个应用
- **设置** — 工作目录、ffmpeg 路径、界面主题、日志级别,持久化保存、重启自动恢复

## 技术栈

- Frontend: Vue 3 + TypeScript + Vite + Tailwind CSS + Pinia + vue-router(瘦客户端,只渲染 UI)
- Backend: Python FastAPI + Uvicorn + Pydantic(监听 `127.0.0.1:8642`,**所有处理逻辑都在这里**)
- Desktop: Rust + Tauri 2(原生窗口 + 文件对话框 `plugin-dialog` + 打开目录 `plugin-shell`;可省略,用浏览器运行)
- TTS: 占位接口(尚未实装,预留引擎接缝,可增量接入)

## 环境要求

- Windows 10/11(也支持 macOS / Linux)
- Python 3.10+(本仓库 `.venv` 为 3.14)
- Node.js 18+
- ffmpeg / ffprobe(仅「音频分集」需要;在 `PATH` 中,或在设置里填绝对路径)
- Rust(rustup + MSVC,可选 — 仅构建桌面壳需要,浏览器方式完全不需要)

## 桌面壳的构建代价(用 Rust 前请阅读)

`npm run tauri dev` 会触发一次 **Rust 全量调试编译**,在 `src-tauri/target/` 下生成约 **4–5 GB** 的构建缓存。这是 Tauri / Rust 在 Windows 上的正常现象,而非本项目的冗余:

- **为什么这么大** — Cargo 会把 Tauri 依赖的数百个 crate 全部编译并缓存(主要在 `target/debug/deps/`),debug 模式还嵌入完整调试符号;Windows 的 PDB 文件尤其占空间。
- **它不会随仓库 / 发布包扩散** — 已 git 忽略,不提交、不分发。克隆仓库只拿到几 MB 源码,每个开发者各自在本地生成,可随时删除、自动重建。
- **不用 Rust 就完全不会产生** — 走「浏览器开发态」或「浏览器 + 后端托管」启动时不编译 Rust,`target/` 不会出现。日常开发推荐这两种方式。
- **想缩小它** — 在 `src-tauri/Cargo.toml` 里把 debug 符号降级或关掉(见下方配置);代价是失去在 Rust 层打断点的能力,而本项目 Rust 代码极薄,通常无影响。
- **回收磁盘** — 删除 `src-tauri/target/`(或运行 `cargo clean`)即可,下次构建自动重建,只是首次会慢几分钟。

在 `src-tauri/Cargo.toml` 追加以下配置,可显著减小 `target/` 体积且不影响运行:

```toml
[profile.dev]
debug = "line-tables-only"   # 追求更小体积可改为 false
```

> **对终端用户没有影响**:正式分发用 `tauri build` 产出的安装包(几十 MB)加打包后的 Python 后端,与这个本地构建缓存无关。

## 安装

### 1. 克隆项目

```powershell
git clone <仓库地址> audiobookstudio
cd audiobookstudio
```

### 2. 安装依赖

```powershell
# Python 后端(仓库自带 .venv;没有则 python -m venv .venv 重建)
.venv\Scripts\pip install -r backend\requirements.txt

# 前端
npm install
```

### 3. 启动

先启动后端(三种方式都需要):

```powershell
.venv\Scripts\python -m backend.main    # → http://127.0.0.1:8642
```

再任选一种方式打开界面:

| 方式 | 命令 | 打开地址 | 需要 Rust? |
| --- | --- | --- | --- |
| ① 桌面应用 | `npm run tauri dev` | 自动弹出桌面窗口 | 是 |
| ② 浏览器开发态 | `npm run dev` | `http://localhost:5173` | 否 |
| ③ 浏览器 + 后端托管(最简) | 先 `npm run build`,再开后端 | `http://127.0.0.1:8642` | 否 |

macOS / Linux 将 `.venv\Scripts\python` 换为 `.venv/bin/python` 即可。

## 使用方法

1. **文本排版**:选择 TXT 文件 → 勾选排版选项(句断、对话分行、章节检测、标点规范等)→ 点「开始排版」→ 预览结果,可下载,或一键「前往下一步(分册切割)」
2. **分册切割**:选择已排版的文本(或任意 TXT)→ 设置每册目标字数(默认 10 万字)→ 点「开始分册」→ 预览各册字数,可下载 zip
3. **TTS 合成**:占位模块,界面显示「即将推出」徽标,暂不可用
4. **音频分集**:选择音频文件 → 设置目标单集时长(默认 10:00)与命名格式 → 启动「停顿检测 + 切割」(长时任务,进度 / 日志见任务中心)→ 完成后可下载 zip 或输出到源音频同级「分集」文件夹
5. **任务**:集中查看所有长时任务,支持暂停 / 恢复 / 取消 / 重试,实时查看进度条与日志
6. **设置**:配置工作目录、ffmpeg / ffprobe 路径、界面主题(跟随系统 / 亮 / 暗)、日志级别

所有产物按模块落在工作目录(默认 `workspace/`)的 `output/{text,books,tts,audio}/` 下;桌面版可直接打开所在文件夹,浏览器版走下载链接。

## 项目结构

```text
项目目录/
├── backend/                 # Python 后端(所有处理逻辑)
│   ├── main.py              # 入口:路由、CORS、/api/health、静态托管 dist/
│   ├── api/                 # 瘦路由:text / book / audio / tts + tasks / config / files
│   ├── engines/             # 核心算法:文本排版 / 分册 / 音频切割 / TTS(占位)
│   ├── core/                # 目录布局、持久化配置、任务系统、日志
│   ├── tests/               # pytest 测试套件(68 个测试)
│   └── requirements.txt     # 精简依赖:fastapi / uvicorn / pydantic / python-multipart
├── src/                     # Vue 3 前端(瘦客户端)
│   ├── api/                 # HTTP 客户端(固定指向 127.0.0.1:8642)
│   ├── views/               # 七个模块:概览 / 文本排版 / 分册切割 / TTS / 音频分集 / 任务 / 设置
│   ├── stores/              # Pinia 状态
│   ├── components/          # 布局 + 原子组件
│   └── utils/               # Tauri 桥接 + 浏览器兜底
├── src-tauri/               # Tauri 2 桌面壳(可选,详见其 README)
│   └── target/              # Rust 构建缓存(gitignore;约 4–5 GB,可删,详见「桌面壳的构建代价」)
├── docs/                    # 截图等文档素材
├── workspace/               # 运行时数据(gitignore):input / output / temp / logs / config
├── dist/                    # 前端构建产物(gitignore),后端可直接托管
├── package.json
├── vite.config.ts
└── README.md
```
