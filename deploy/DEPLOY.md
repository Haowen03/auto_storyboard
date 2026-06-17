# 新服务器部署指南

本文说明如何在**另一台 MetaX GPU 服务器**上，复现与当前 `whw` 容器等价的运行环境，使他人能独立跑通 `auto_storyboard` 全流程。

**可以复现**，但需要准备三类资源：

| 类别 | 内容 | 获取方式 |
|------|------|----------|
| Docker 镜像 | `vimax:v1`（CLI）、`ltx2:v1`（GPU 推理） | 从源服务器 `export` / 新服务器 `import` |
| 模型权重 | Qwen-Image-2512、Qwen-Image-Edit-2511、LTX-2.3 等 | 拷贝 `/mnt` 下权重，或自行下载到 `MODEL_ROOT` |
| 云端 API | Gitee / DashScope VLM Key | 各自申请，写入 `deploy/.env` |

本仓库 `seedance_fuke` 提供**工作流代码 + 部署脚本**；Qwen-Image / LTX 的**推理服务代码**目前在 `plin/` 目录（需一并拷贝或单独提供）。

---

## 部署架构（与现网一致）

```
新服务器
├── 容器 whw（vimax:v1）          ← 跑 auto_storyboard/run.py
├── 容器 qwen_image（ltx2:v1）    ← :9000 Qwen-Image / Edit（可合并到同一容器）
├── 容器 ltx2.3_0309（ltx2:v1）   ← :8000 LTX keyframe_interpolation
└── 云端 VLM API                  ← 分镜规划 / shot 规划
```

三者均使用 `--network=host`，工作流通过 `http://127.0.0.1:9000` 与 `http://127.0.0.1:8000` 调用本地推理服务。

---

## 第一步：源服务器导出镜像（一次性）

在**你当前正在用的服务器**上：

```bash
cd /path/to/seedance_fuke
bash deploy/export_images.sh /tmp/docker_images_export
```

将 `/tmp/docker_images_export/*.tar` 拷贝到新服务器（`scp`、`rsync`、移动硬盘均可）。  
单个镜像体积较大，传输时间取决于网络与磁盘。

---

## 第二步：新服务器导入镜像

```bash
cd /path/to/seedance_fuke
bash deploy/import_images.sh /path/to/docker_images_export
docker images   # 应能看到 vimax:v1、ltx2:v1
```

---

## 第三步：准备目录与模型

在新服务器上规划两个挂载点（可在 `deploy/.env` 中修改）：

| 变量 | 建议值 | 用途 |
|------|--------|------|
| `WORKSPACE_ROOT` | `/home/mx` | 代码、`whw/seedance_fuke`、`qwen/<case>/` 产物 |
| `MODEL_ROOT` | `/mnt` | 模型权重 |

需要拷贝或下载的权重（路径与 `deploy/env.example` 一致，可按实际修改）：

- `Qwen-Image-2512`
- `Qwen-Image-Edit-2511`
- LTX-2.3 checkpoint、distilled LoRA、spatial upsampler
- Gemma-3-12b-it（LTX 文本编码器）

同时拷贝推理服务代码（若新服务器没有 `plin/`）：

- `plin/Image/service/` → Qwen-Image 服务
- `plin/aigc_services/LTX-2_0318/service/` → LTX 服务

---

## 第四步：配置环境变量

```bash
cd seedance_fuke
cp deploy/env.example deploy/.env
vim deploy/.env
```

**必须修改的项：**

- `WORKSPACE_ROOT`、`MODEL_ROOT`（若不用默认 `/home/mx`、`/mnt`）
- 各 `*_MODEL_PATH` / `LTX_*` 路径
- `GITEE_AI_API_KEY` 或 `DASHSCOPE_API_KEY`

加载环境变量（后续命令前执行一次）：

```bash
set -a && source deploy/.env && set +a
```

---

## 第五步：创建 Docker 容器

### 5.1 工作流 CLI 容器（whw）

```bash
bash deploy/docker_run.sh
# 或显式指定：bash deploy/docker_run.sh vimax:v1 whw
```

### 5.2 Qwen-Image 服务容器（可选独立容器）

若希望与现网一样单独起 `qwen_image` 容器：

```bash
bash deploy/docker_run.sh ltx2:v1 qwen_image
```

### 5.3 LTX 服务容器（可选独立容器）

```bash
bash deploy/docker_run.sh ltx2:v1 ltx2.3_0309
```

> 也可以**只用一个** `ltx2:v1` 容器，在里面同时启动 :9000 与 :8000 两个服务，省资源；只要端口不冲突即可。

---

## 第六步：启动推理服务

进入对应容器后启动服务（路径以 `deploy/.env` 为准）。

**Qwen-Image（:9000）**

```bash
docker exec -it qwen_image bash
cd /home/mx/plin/Image/service
# 按 deploy/.env 修改 launch.sh 中的模型路径与 GPU_DEVICES 后：
bash launch.sh
```

**LTX 2.3（:8000）**

```bash
docker exec -it ltx2.3_0309 bash
cd /home/mx/plin/aigc_services/LTX-2_0318/service
bash launch.sh
```

验证：

```bash
source deploy/.env
bash deploy/check_services.sh
```

---

## 第七步：在 whw 容器内跑工作流

```bash
docker exec -it whw bash
set -a && source /home/mx/whw/seedance_fuke/deploy/.env && set +a

cd /home/mx/whw/seedance_fuke
python auto_storyboard/run.py \
  --idea "御剑飞行穿越竹林与云海" \
  --case-name case_demo \
  --grid 6 \
  --pipeline frames
```

`config.py` 会读取环境变量 `QWEN_IMAGE_BASE_URL`、`LTX_BASE_URL`；在 `deploy/.env` 中设好即可，无需改代码。

---

## 快速检查清单

- [ ] MetaX 驱动正常，`/dev/dri`、`/dev/mxcd` 存在
- [ ] `vimax:v1`、`ltx2:v1` 镜像已 import
- [ ] 模型权重路径正确且可读
- [ ] `:9000`、`:8000` 服务已启动（`deploy/check_services.sh` 通过）
- [ ] VLM API Key 已写入 `deploy/.env`
- [ ] `whw` 容器已创建，能 `docker exec` 进入
- [ ] `python auto_storyboard/run.py --help` 正常

---

## 常见问题

### 新服务器没有 MetaX GPU？

当前推理服务与 `docker_run.sh` 针对 **MetaX MACA**（`/dev/mxcd`）编写。若为 NVIDIA GPU，需：

- 替换为支持 CUDA 的 Docker 镜像
- 修改 `docker_run.sh` 中的 `--device` 参数（如 `--gpus all`）
- 使用对应 GPU 版 Qwen-Image / LTX 推理代码

工作流 CLI 本身只依赖 HTTP API，**只要能访问 :9000 和 :8000，换硬件也能跑**。

### 不想拆三个容器？

推荐最简方案：**一个 `ltx2:v1` 容器**内启动 Qwen + LTX 两个服务，**一个 `vimax:v1` 容器**跑 CLI。共两个容器即可。

### 镜像太大传不动？

- 使用内网 `rsync` 或共享 NAS
- 或在新服务器用相同 Dockerfile / 基础镜像重新构建（需自行维护构建文档）

### API Key 能提交到 Git 吗？

不能。只提交 `deploy/env.example`，真实 Key 放在 `deploy/.env`（加入 `.gitignore`）。

---

## 相关文件

| 文件 | 说明 |
|------|------|
| `deploy/env.example` | 环境变量模板 |
| `deploy/docker_run.sh` | 创建容器 |
| `deploy/export_images.sh` | 源服务器导出镜像 |
| `deploy/import_images.sh` | 新服务器导入镜像 |
| `deploy/check_services.sh` | 检查服务与 Key |
| `auto_storyboard/README.md` | 工作流功能与参数说明 |
