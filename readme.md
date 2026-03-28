# Purity

一个用于测试代理订阅节点纯净度与延迟的 Python 工具。

它会：

- 读取 Clash YAML 配置或订阅链接
- 自动过滤流量提示、到期提示、官网提示这类“假节点”
- 按地区自动分组，并把节点数较少的小地区合并成一个小分组
- 在测试前让你选择要测哪些分组，支持单选和多选
- 启动一个常驻 `mihomo` 运行时，通过 `external-controller` 逐个切换节点测试
- 调用 `IPPure` 获取出口 IP 信息和 `fraudScore`
- 测试 `github`、`youtube`、`cloudflare` 的延迟
- 持续把完整结果和排名结果写入 JSON 文件，运行中也可以查看

## 适用场景

- 你有一个 Clash/Shadowrocket 订阅，想批量筛节点
- 你想知道每个节点的出口 IP 纯净度
- 你想按“纯净度优先，延迟其次”的规则得到一个可排序结果

## 依赖

### 1. 创建 conda 环境

```bash
cd /Users/renzhongyuan/Documents/code/purity
conda env create -f environment.yml
conda activate purity
```

### 2. 安装 `mihomo`

推荐直接运行项目内置下载脚本，它会自动识别当前系统和架构，并把合适的 `mihomo` 下载到项目的 `bin/` 目录：

```bash
cd /Users/renzhongyuan/Documents/code/purity
conda activate purity
python download_mihomo.py
```

主脚本会自动优先查找：

- `./bin/mihomo`
- `./bin/clash-meta`
- `./bin/clash`

如果项目内没有，再继续查找系统 `PATH` 中的：

- `mihomo`
- `clash-meta`
- `clash`

如果运行 [analyze_subscription.py](/Users/renzhongyuan/Documents/code/purity/analyze_subscription.py) 时仍然没找到可用的 core，脚本也会在终端里直接询问你是否立即自动下载。

如果你想手动指定版本，也可以：

```bash
python download_mihomo.py --tag v1.19.21
```

如果已经有自己的安装路径，也可以手动指定：

```bash
python analyze_subscription.py --config SSRDOG.yaml --core-binary /path/to/mihomo
```

## 支持的输入

脚本支持两类输入：

- Clash YAML 配置文件
- 订阅链接

订阅链接既可以是 Clash YAML，也可以是常见的 `vmess://`、`vless://`、`trojan://`、`ss://` 订阅文本。

## 最常用的运行方式

### 方式 1：读取本地配置文件

```bash
cd /Users/renzhongyuan/Documents/code/purity
conda activate purity
python analyze_subscription.py --config SSRDOG.yaml
```

读取完配置后，脚本会先把分组列表输出到终端，再等待你手动选择，例如：

```text
按地区识别到以下测试分组：
  0. 全部节点 (66 个)
  1. 香港 (15 个)
  2. 台湾 (10 个)
  3. 新加坡 (10 个)
  4. 日本 (10 个)
  5. 美国 (10 个)
  6. 其他小分组 (11 个) | 包含: 加拿大(1)、土耳其(1)、德国(1)...
选择分组:
```

输入方式：

- 直接回车：测试全部
- 输入 `1`：只测一个分组
- 输入 `1,3,5`：同时测试多个分组
- 如果当前启用了小地区合并，终端会额外提示你可用 `--merge-small-groups-threshold 0` 查看更细分组

### 方式 2：直接传订阅链接

如果链接里有 `?`、`&` 等字符，请一定加引号：

```bash
python analyze_subscription.py '你的订阅链接'
```

### 方式 3：运行后再粘贴订阅链接

```bash
python analyze_subscription.py
```

## 常用参数

```bash
python analyze_subscription.py --config SSRDOG.yaml \
  --groups 香港,日本 \
  --output purity_results.json \
  --ranking-output purity_results_ranking.json \
  --workers 4 \
  --request-timeout 15 \
  --ippure-timeout 25 \
  --rank-latency-threshold-ms 1000
```

参数说明：

- `--config`
  指定本地 YAML 文件
- `--output`
  完整结果输出路径，默认 `purity_results.json`
- `--ranking-output`
  排名结果输出路径；不传则自动生成 `*_ranking.json`
- `--groups`
  直接指定要测试的地区分组，支持分组名称或序号，多个用逗号分隔；不传则在运行时交互选择
- `--merge-small-groups-threshold`
  某个地区的节点数小于等于该值时，会被合并进“其他小分组”，默认 `5`；如果不想合并，可以设为 `0`
- `--workers`
  预留并发参数；当前主流程使用单个常驻 `mihomo` 逐个切换节点测试
- `--request-timeout`
  普通请求超时，默认 `15` 秒
- `--ippure-timeout`
  `IPPure` 请求超时，默认 `25` 秒
- `--rank-latency-threshold-ms`
  平均延迟高于该值的节点不参与排名，默认 `1000ms`
- `--core-binary`
  手动指定 `mihomo` 路径
- `--keep-temp`
  保留运行时临时配置和日志，方便排查问题

## 输出文件

脚本会生成两个文件：

### 1. 完整结果文件

默认文件名：

```bash
purity_results.json
```

内容包括：

- 当前运行阶段：`running` 或 `completed`
- 已测试数量、剩余数量
- 跳过了哪些提示类节点
- 本次选择了哪些地区分组
- 每个节点的完整测试结果
- `IPPure` 返回的 IP 信息
- 各测试站点的延迟
- 是否符合排名条件

### 2. 排名结果文件

默认文件名：

```bash
purity_results_ranking.json
```

内容包括：

- 当前运行阶段：`running` 或 `completed`
- 当前已进入排名的节点
- 每个节点的排名名次
- 被排除在排名之外的节点数量

这两个文件都会在运行过程中持续刷新，所以不需要等脚本结束才能看结果。

## 排名规则

节点排序规则如下：

1. 先按 `fraudScore` 档位排序，越低越好

档位规则：

- `0`
- `15`
- `25`
- `40`
- `50`
- `60`
- `70`
- `100`

2. 再按平均延迟排序，越低越好

3. 平均延迟高于 `1000ms` 的节点不参与排名

默认情况下，排名文件只保留“满足延迟阈值要求”的节点。

## 终端状态说明

脚本运行时，每个节点前面会出现一个状态标记：

- `OK`
  `IPPure` 成功，并且至少有一个延迟站点测试成功
- `WARN`
  部分成功，例如 `IPPure` 失败但延迟成功，或者反过来
- `ERR`
  该节点本轮没有拿到有效测试结果

## 运行中的结果查看

如果脚本还没跑完，你也可以直接打开 JSON 文件看当前进度：

- 完整结果：查看 `purity_results.json`
- 排名结果：查看 `purity_results_ranking.json`

其中：

- `phase = running` 表示仍在进行中
- `tested` 表示已完成数量
- `pending` 表示剩余数量

## 已做的兼容处理

为了让结果更贴近真实使用，这个脚本已经做了这些处理：

- 自动跳过流量提示、到期提示、官网提示节点
- 自动按地区分组，并把小地区合并为一个分组
- 支持测试前按分组单选或多选
- 使用单个常驻 `mihomo` 进程，而不是每个节点重启一次
- 通过 `external-controller` 切换节点，减少启动开销
- 即使 `IPPure` 失败，也会尽量继续测延迟
- 排名时自动过滤高延迟节点

## 常见问题

### 1. `zsh: no matches found`

这是 shell 把订阅链接里的 `?` 当成通配符了。给链接加引号：

```bash
python analyze_subscription.py 'https://example.com/sub?token=xxx'
```

### 2. `代理端口 xxx 在 8.0s 内未就绪`

通常说明 `mihomo` 运行时没有成功启动。可以这样排查：

```bash
python analyze_subscription.py --config SSRDOG.yaml --keep-temp
```

这样会保留临时配置和日志，方便查看 `mihomo` 启动失败原因。

### 3. 为什么有些节点能用，但脚本里是 `WARN` 或 `ERR`

这不一定表示节点完全不可用。常见原因有：

- `IPPure` 本身响应慢
- 某些测试站点对该出口不稳定
- 节点能上网，但不适合访问当前测试目标

建议结合完整结果里的：

- `ip_error`
- `latency_errors`
- `average_latency_ms`

一起判断。

## 文件说明

- [analyze_subscription.py](/Users/renzhongyuan/Documents/code/purity/analyze_subscription.py)
  主脚本
- [environment.yml](/Users/renzhongyuan/Documents/code/purity/environment.yml)
  conda 环境定义
- [example.yaml](/Users/renzhongyuan/Documents/code/purity/example.yaml)
  示例配置
- [SSRDOG.yaml](/Users/renzhongyuan/Documents/code/purity/SSRDOG.yaml)
  本地测试用配置样例
