# astrbot_plugin_song_request

AstrBot 点歌机插件：给一个主题，它把词写成歌、渲染成 mp3，用文件发给对方。

它不是点播别人的歌，是**生成**一首新的歌。

---

## 它做什么

| 能力 | 说明 |
|---|---|
| `create_song` 工具 | 给主题就给歌。参数：主题、歌词、风格提示、语言、是否高质量 |
| `/点歌 <主题>` 命令 | 命令入口，自己调当前模型写词，再走同一条渲染链 |
| 分支路由 | 中文/英文默认走轻量分支；明确要高质量或使用中英之外的语言，走重量分支 |
| 名额与排队 | 交给底层脚本管（默认每群每天 3 首，管理员免限） |
| 失败如实返回 | 不编结果，失败就把原因原样交回给调用方 |

## 两条分支

| 分支 | 引擎 | 语言 | 特点 |
|---|---|---|---|
| 轻量 | DiffRhythm | 中文、英文 | 快，显存占用低，日常首选 |
| 重量 | ACE-Step 1.5 | 19 种（en zh ja ko es fr de it pt ru bn hi ar th vi id tr nl pl） | 质量更高，中英之外的语言只能走这条 |

路由判断只有一句：

```python
want_heavy = bool(high_quality) or (language in ACE_LANGS)
```

也就是说：**没提要求就用轻的，提了高质量或换语言才用重的。**

---

## 依赖（重要，缺一样就跑不起来）

### 必需

| 项目 | 要求 | 说明 |
|---|---|---|
| AstrBot | >= 4.26 | 需要 `filter.llm_tool`；直听/直写词的路径还要求当前模型支持工具调用 |
| Python | >= 3.10 | 插件跑在 AstrBot 自带环境里 |
| 生成流水线 | 轻量：`sing_queue.py`；重量：`sing_queue_ace.py` | **本仓库不含模型与流水线**，见下节 |
| ffmpeg | 在 PATH 里 | 重量分支要把 flac/wav 转 mp3 |
| 磁盘 | 至少几十 GB | 模型权重很大 |

### 重量分支额外需要

| 项目 | 要求 |
|---|---|
| ACE-Step 1.5 | 一套推理框架 + `checkpoints` 权重（acestep-v15-turbo、5Hz-lm） |
| transformers | >= 4.51,< 4.58。**与轻量分支的 4.49/4.50 冲突**，建议用 `pip install --target` 旁挂，运行时 `PYTHONPATH` 优先它 |
| vector-quantize-pytorch | 主模型加载必需 |
| pyworld、toml | ACE-Step 运行需要 |
| 环境变量 | `PYTHONUTF8=1`（它按系统默认编码读 TOML，中文日文会崩） |
| instruction.txt | 入口每次运行前要自己写好，否则它会停下来等人按回车 |

### 本仓库不含

- 模型权重
- 推理框架本体
- 任何歌曲素材

只包含插件代码与配置描述。

---

## 安装

1. 把本目录放进 AstrBot 的插件目录：

   ```
   <astrbot>/data/plugins/astrbot_plugin_song_request/
   ```

2. 重启 AstrBot（插件改配置或代码后都要重启，配置才会重新生成）

3. 打开面板，找到「点歌机」，填这几项：

| 配置键 | 默认 | 说明 |
|---|---|---|
| `enabled_groups` | 空 | 允许点歌的群。留空 = 所有群 |
| `style_prompt` | uplifting chinese pop rock... | 用户没说风格时用这条 |
| `queue_python` | `D:\FurinaSinger\.venv\Scripts\python.exe` | 跑流水线的解释器 |
| `queue_script` | `D:\FurinaSinger\sing_queue.py` | 轻量分支入口 |
| `ace_script` | `D:\FurinaSinger\sing_queue_ace.py` | 重量分支入口 |
| `timeout_seconds` | 300 | 轻量分支最长等待 |
| `ace_timeout_seconds` | 1800 | 重量分支最长等待（首次要加载模型） |
| `daily_quota` | 3 | 每群每天名额（实际生效在底层脚本里） |

4. 让底层脚本可被调用。约定它们输出**最后一行 JSON**：

```json
{"ok": true, "file": "D:\\...\\歌名.mp3", "seconds": 53.9, "duration": 150}
{"ok": false, "reason": "今天的名额用完了（3/3）"}
```

插件只认这一行，认不出就当作失败如实返回。

---

## 用法

### 让 AI 调工具

模型会这样调用：

```python
create_song(
    topic="写给一个刚重启的夜晚",
    lyrics="[verse]\n...\n[chorus]\n...",
    style="warm chinese pop ballad, piano, emotional male vocal",
    language="zh",          # 可选
    high_quality=False      # 可选，True 走重量分支
)
```

返回：

```json
{"ok": true, "file": "D:\\path\\歌名.mp3", "topic": "...", "branch": "light", "提示": "..."}
```

### 命令

```
/点歌 关于早八
```

命令会先让当前 Provider 写词，再走同一条渲染链。

---

## 两个坑，写在这里省得你也踩

### 1. 成品必须用文件发，不要用语音

AstrBot 的 aiocqhttp 适配器会把语音转成 `base64://` 塞进单条 WebSocket 帧。
一首 6 MB 的歌变成约 8 MB 一帧，超过 NapCat 的收包上限，连接直接断，插件等不到回执。

实测：446 KB 的 m4a 当语音正常；6.19 MB 必崩。

本插件因此只返回 mp3 路径，由调用方用 **file** 通道发送。

### 2. 发 file 时，`text` 字段是文件名

`send_message_to_user` 的 file 类型里，`name` 取自 `text`，取不到才用 basename。
把一句说明写进 `text`，对方收到的文件就真的叫那句说明，**连扩展名都没有**。

正确做法：`text` 只写 `歌名.mp3`，要说的话另发一条 `plain`。

---

## 已知限制

- 强依赖外部脚本路径，换机器要改配置
- 重量分支首次调用要加载分词器与权重，约 35 秒起步
- 本插件不判断内容合规；红线与限额由底层脚本与使用方负责
- 只在 aiocqhttp（QQ）上验证过

---

## 目录

```
astrbot_plugin_song_request/
├── main.py            插件主体：工具、命令、分支路由
├── metadata.yaml      插件元信息
├── _conf_schema.json  面板配置项
├── README.md
└── .gitignore
```

## 许可

MIT
