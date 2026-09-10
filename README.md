# AudiobookStudio

一个 Web 应用,在一个地方完成中文有声书的完整制作流程:**文本排版 → 分册切割 → 文本解析 → 角色配音 → 音频合成 → 音频合并 → 音频分集**,外加开始(工作空间管理)与设置。

## 功能

- **开始** — 设置 / 清除工作空间;未设置工作空间时流水线锁定(后端写端点返回 409,入口禁用)
- **文本排版** — 对原始 TXT 做确定性排版(空白 / 段落 / 标点 / 章节,10 个可配置开关),结果内容保持、可重复排版
- **分册切割** — 按章节边界把长文均衡切分为若干分册,只在章节处切割、不重编号,拼回所有分册可精确复现原文
- **文本解析** — 「LLM → JSON」管线:从 `02_split_text/` 勾选一个或多个文本文件(可多选),每个文件作为独立任务并发调用 LLM(并发数可配),逐段生成逐行标注脚本 `03_parsed_json/<文件基名>.json`(角色 / 类型 / 演绎指令 / 停顿);每文件独立成败、互不影响
- **角色配音** — 选择一个解析 JSON(或选「全部文件」一次性合并整本书所有角色),为角色生成声音画像(`voice_config.json` + 试听样本);支持一键全部、仅处理新增、或单角色按提示词重生成
- **音频合成** — 选择一个解析 JSON 后批量合成:单个长时任务驱动 `.venv-tts` 子进程,模型只加载一次,按 JSON 顺序合成全部行;单行失败只记录、不打断整批;每个 JSON 合成成一个「音频包」(子文件夹 `05_audio_chunk/<JSON 基名>/`,含逐行音频与 `manifest.json`)
- **音频合并** — 从「音频包」列表(05_audio_chunk/ 的各子文件夹)选中一个包,按该包清单把逐行音频无损合并为 `06_audio_merge/<包名>.mp3`,支持换人 / 同人停顿
- **音频分集** — 选择一个已合并的有声书(06_audio_merge/),无损切分为若干集(`-c copy` 不重编码),输出到 `07_output/<源名>/`;支持按停顿位置智能对齐边界
- **长时任务系统** — 进度 / 日志 / 暂停 / 恢复 / 取消 / 重试;各流程页内联展示所启动任务的实时状态;失败任务自动隔离,不影响整个应用
- **设置** — 工作空间、各模块参数(排版开关 / 每册字数 / 分集 / TTS 模型与停顿)、ffmpeg 路径、界面主题、日志级别,持久化保存、重启自动恢复

## 技术栈

- Frontend: Vue 3 + TypeScript + Vite + Tailwind CSS + Pinia + vue-router(瘦客户端,只渲染 UI)
- Backend: Python FastAPI + Uvicorn + Pydantic(监听 `127.0.0.1:8642`,**所有处理逻辑都在这里**)
- TTS: 本地 Qwen3-TTS — 独立 `.venv-tts` 环境(Python 3.10 + torch (CUDA) + qwen-tts),一次性子进程编排,ML 依赖与 3.14 后端完全隔离

## 环境要求

- Windows 10/11(也支持 macOS / Linux)
- Python 3.10+(本仓库 `.venv` 为 3.14)
- Node.js 18+
- ffmpeg / ffprobe(「音频分集」与「音频合并」需要;在 `PATH` 中,或在设置里填绝对路径)
- TTS 合成 / 合并(可选):独立 `.venv-tts`(Python 3.10 + torch (CUDA) + qwen-tts,数 GB),由 `install_tts_env.ps1` 重建;其余模块不需要

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

# TTS 独立环境(可选,仅「音频合成 / 音频合并」需要;下载数 GB 的 torch)
powershell -ExecutionPolicy Bypass -File install_tts_env.ps1
```

### 3. 启动

先启动后端(两种方式都需要):

```powershell
.venv\Scripts\python -m backend.main    # → http://127.0.0.1:8642
```

再任选一种方式打开界面:

| 方式 | 命令 | 打开地址 |
| --- | --- | --- |
| 浏览器开发态 | `npm run dev` | `http://localhost:5173` |
| 浏览器 + 后端托管(最简) | 先 `npm run build`,再开后端 | `http://127.0.0.1:8642` |

macOS / Linux 将 `.venv\Scripts\python` 换为 `.venv/bin/python` 即可。

## 使用方法

1. **开始**:选择一个文件夹作为工作空间(未设置时流水线锁定)→ 工程配置、日志与所有产物都落在该工作空间的固定子目录(`config/` · `logs/` · `00_temp` … `07_output`)
2. **文本排版**:选择 TXT 文件 → 勾选排版选项(句断、对话分行、章节检测、标点规范等)→ 点「开始排版」→ 预览结果,可下载,或一键「前往下一步(分册切割)」
3. **分册切割**:选择已排版的文本(或任意 TXT)→ 设置每册目标字数(默认 10 万字)→ 点「开始分册」→ 预览各册字数,可下载 zip
4. **文本解析**:在 `02_split_text/` 勾选要解析的文本文件(可多选)→ 配置 LLM(base_url / api_key / 模型)、生成参数(含并发数)与 Prompt → 点「开始解析」,每个文件作为独立任务并发逐段调用 LLM,分别生成 `03_parsed_json/<文件基名>.json`;每文件独立状态(待处理 / 解析中 / 已完成 / 失败),一个失败不影响其他
5. **角色配音**:选择一个解析 JSON(03_parsed_json/,或选「全部文件」合并整本书所有角色)→ 一键为角色生成声音画像(voice_config.json + 试听样本),也可仅处理新增角色或单角色重生成
6. **音频合成**:选择一个解析 JSON(03_parsed_json/)→ 启动批量合成(长时任务,进度 / 日志内联显示在页面)→ 在 `05_audio_chunk/` 产出逐行音频与 `manifest.json`
7. **音频合并**:按清单把逐行音频合并为 `06_audio_merge/cloned_audiobook.mp3`(换人 / 同人停顿可配)
8. **音频分集**:选择音频文件(通常是合并成品)→ 设置目标单集时长(默认 10:00)与命名格式 → 启动「停顿检测 + 切割」(长时任务,进度 / 日志内联显示在页面)→ 完成后可下载 zip 或输出到源音频同级「分集」文件夹
9. **设置**:配置工作空间、各模块参数(排版开关 / 每册字数 / 分集 / TTS 模型与停顿)、ffmpeg / ffprobe 路径、界面主题(跟随系统 / 亮 / 暗)、日志级别

所有产物按模块落在工作空间的固定子目录下:`01_input/`(排版文本)· `02_split_text/`(分册)· `03_parsed_json/`(解析 JSON)· `04_voice_profiles/`(角色声音)· `05_audio_chunk/`(合成片段)· `06_audio_merge/`(合并成品)· `07_output/`(最终分集);产物通过页面上的下载链接获取。

## 项目结构

```text
项目目录/
├── backend/                 # Python 后端(所有处理逻辑)
│   ├── main.py              # 入口:路由、CORS、/api/health、静态托管 dist/
│   ├── api/                 # 瘦路由:text / book / audio / tts + workspace / tasks / config / files
│   ├── engines/             # 核心算法:文本排版 / 分册 / 音频切割 / TTS(批量合成 / 合并 / 角色配音)
│   ├── core/                # 目录布局、持久化配置、任务系统、日志
│   ├── tests/               # pytest 测试套件(164 个测试)
│   └── requirements.txt     # 精简依赖:fastapi / uvicorn / pydantic / python-multipart
├── src/                     # Vue 3 前端(瘦客户端)
│   ├── api/                 # HTTP 客户端(固定指向 127.0.0.1:8642)
│   ├── views/               # 九个模块:开始 / 文本排版 / 分册切割 / 文本解析 / 角色配音 / 音频合成 / 音频合并 / 音频分集 / 设置
│   ├── stores/              # Pinia 状态
│   ├── components/          # 布局 + 原子组件
│   └── utils/               # 文件选择 / 下载(浏览器 input + 上传)
├── tts-engine/              # TTS 工作进程(运行在独立 .venv-tts,一次性子进程)
├── install_tts_env.ps1      # 重建 .venv-tts 独立环境(Python 3.10 + torch,数 GB)
├── app.json                 # 运行时配置(gitignore):通用配置模板 + 当前工作空间指针;工程配置 / 日志 / 产物都在用户工作空间内
├── dist/                    # 前端构建产物(gitignore),后端可直接托管
├── package.json
├── vite.config.ts
└── README.md
```
