# GaussMaster

- [什么是GaussMaster](#什么是GaussMaster)
- [系统架构](#系统架构)
- [安装启动](#安装启动)
  - [前置条件](#前置条件)
  - [启动方式概览](#启动方式概览)
  - [conf配置](#conf配置)
  - [大语言模型选择](#大语言模型选择)
  - [向量模型配置](#向量模型配置)
  - [前台服务部署](#前台服务部署)
- [快速上手](#快速上手)
- [接入MCP Server工具](#接入MCP-Server工具)
  - [功能简介](#功能简介)
  - [配置mcp_servers.json](#配置mcp_serversjson)
  - [启动加载](#启动加载)
  - [状态查询与热重载](#状态查询与热重载)
- [贡献](#贡献)
- [许可证](#许可证)

## 什么是GaussMaster
GaussMaster是一款开源的数据库智能运维Copilot。基于LLM和AI等技术，GaussMaster旨在提供一站式、交互式的AI原生运维系统。用户可以通过文字命令，与GaussMaster平台自由对话，完成智能问答，根因诊断，故障巡检，预测调优等等任务，覆盖数据库开发运维全生命周期。

<img src="doc/overview.png" alt="GaussMaster概览" width="1000"/>

GaussMaster的设计目标具有如下能力：

- **全栈感知能力**。支持多源数据（指标、日志、业务负载）实时采集、关联分析与预测。
- **分布式诊断能力**。端到端链路追踪、跨节点依赖分析，支持复杂分布式系统的故障定位。
- **智能决策能力**。基于大模型（盘古，DeepSeek等）、领域模型、业务模型等各层级智能决策系统，实现自诊断、自恢复、自调优，减少人工干预。
- **开放集成能力**。可插拔架构，兼容多云 / 混合云环境，支持第三方工具接入和自定义扩展，支持二次开发低成本能力扩充。
- **持续进化能力**。通过用户反馈和机器学习模型迭代，动态适应新负载、新架构。

作为一个开源的数据库运维工具，GaussMaster期望与广泛的开发者构建一个开放多元的数据库运维生态。对接不同的数据库，运维工具，大模型底座和领域模型。

## 系统架构

<img src="doc/deployment-architecture.png" alt="系统架构" width="600"/>

GaussMaster运行的系统架构如上图所示。GaussMaster的主服务依赖于以下模块运行：
- **DBMind服务**。GaussMaster通过[DBMind](https://gitcode.com/opengauss/openGauss-DBMind)服务纳管运行中的openGauss运行实例。
- **向量检索**。GaussMaster调用用第三方的向量数据库存储知识片段，并在用户在线提问的时候进行向量检索。
- **向量模型**。GaussMaster调用第三方的Embedding Service，将用户的提问向量化，用以向量检索。
- **重排序模型**。GaussMaster调用第三方的Reranking Service将检索到的知识片段进行排序。
- **大模型推理**。GaussMaster调用第三方的大模型服务进行推理，完成智能问答智能运维等各项任务。

## 安装启动

### 前置条件

确保DBMind服务，向量数据库，向量模型服务，重排序模型服务和大模型推理服务都正在运行，并且GaussMaster服务所在网络环境可以连接到上述各服务的端口。

### 启动方式概览
1. 执行`python startup.py service setup -c conf`命名，
   该命令会在conf目录下创建GaussMaster配置文件`gauss_master.conf`和大模型配置文件`model_config.yaml`
   在该命令执行后，用户需要完善配置信息
2. 执行`echo '{"VECTOR_password": "x", "DBMIND_password": "x"}' | python startup.py service setup -c conf --initialize --initialize_meta_db`命令，
   该命令会对配置文件中的信息进行检查并加密存储，然后将智能运维需要用到的本地知识向量化到向量数据库中
3. 执行`python startup.py service start -c conf`命令，（在执行该命令前，需要先执行第5步，对UI进行编译）
   该命令会启动GaussMaster服务，用户可以通过前台进行交互。

### conf配置
在conf文件夹中找到`gauss_master.conf`配置文件，进行如下配置：
1. `[VECTOR]`章节的内容为向量数据库的实例地址，数据库名，用户名和密码
2. `[DBMIND]`章节的内容为对接DBMind暴露的对外接口，当前仅支持http协议
3. `[WEB-SERVICE]`章节的内容为GaussMaster web服务的地址，当前仅支持http协议，所以ssl值为false

### 大语言模型选择
当前支持调用如下模型服务：
- 盘古智子
- Chatglm3
- Baichuan2
- Llama3
- Qwen
- DeepSeek

#### 在线大模型配置
在conf文件夹中找到`model_config.yaml`配置文件，进行如下配置，以盘古智子模型为例。
```yaml
# default llm
model_name: pangu_sigma_unify_plugin_71b

# 配置url信息
online_llm:
  pangu_sigma_unify_plugin_71b:
    enable: True
    model_name: pangu_sigma_unify_plugin_71b
    api_type: Pangu
    api_url: http://*.*.*.*:*/chat/completions
    recommended_config:
      temperature: 0.7
      top_p: 1.0
```

### 向量模型配置
修改`model_config.yaml`中如下内容，添加api_url和完整model_path
```yaml
# embedding model
embedding_model:
  api_url: xxx
  model_path: xxx
```

### 前台服务部署
第一步：修改`ui\.env.development'文件，配置后台服务的URL，如下
```
# 后端服务
VITE_BASE_PROXY_URL = http://x.x.x.x:x
```

第二步：修改`ui\vite.config.ts`文件，配置前台服务的ip和host


第三步：如果是第一次编译前台代码，需要执行`npm install`命令，否则只需要执行`npm run dev`命令即可
```shell
cd ui
npm install
npm run dev
```

## 快速上手

### 智能问答

在部署完成之后，通过RESTful API调用`ask_gauss`接口向GaussMaster提问。

```shell
curl -X 'POST' "<gaussmaster_endpoint>/v1/api/ask_gauss" \
      -H 'accept: text/event-stream'  \
      -H "Content-Type: application/json" \
      -d '{ \
         "user_id": "123", \
         "session_id": "123", \
         "model_name":"pangu_cloud_sigma_unify_plugin_38b", \
         "lang":"zh", \
         "history_len":3, \
         "question":"GaussMaster是什么"\
      }'
```

请注意将`<gaussmaster_endpoint>`替换成上文中部署的GaussMaster服务的endpoint。

### 智能运维

首先需要通过DBMind，将openGauss实例纳管起来。在确保DBMind已经纳管的openGauss实例之后，使用下述命令在GaussMaster中注册。

```shell
curl -X 'POST' 'http://<gaussmaster_endpoint>/v1/api/clusters/register' \
      -H 'Content-Type: application/json'  \
      -d '{ \
         "cluster_name": <cluster_name>, \
         "host": <opengauss_host>, \
         "port": <opengauss_port>, \
         "username": <opengauss_username>, \
         "password": <opengauss_password>\
      }' 
```

请注意填入`<cluster_name>`, `<opengauss_host>`, `<opengauss_port>`, `<opengauss_username>`, `<opengauss_password>`参数，填入openGauss实例的认证信息。

在GaussMaster服务中成功注册之后，调用如下命令进行交互式运维。

```shell
curl -X 'POST' 'http://<gaussmaster_endpoint>/v1/api/app/intelligent-interaction' \
      -H 'accept: application/json' \
      -H 'Content-Type: application/json' \
      -d '{ \
         "query": "帮我查询当前数据库集群状态", \
         "mode": "tool_interaction", \
         "user_id": <user_id>, \
         "session_id": <session_id> \
      }' 
```

其中`<user_id>`和`<session_id>`分别为本次对话的用户和session id的唯一标识符。

## 接入MCP Server工具

### 功能简介

GaussMaster支持通过[MCP（Model Context Protocol）](https://modelcontextprotocol.io/)协议接入外部MCP Server，将其提供的工具动态扩展到智能运维工具集中。接入后，MCP工具与GaussMaster内置工具在交互式运维中统一使用：大模型会根据用户提问自动完成工具匹配与参数推断，无需额外开发即可扩充运维能力。

当前支持两种传输方式的MCP Server：
- **stdio**：GaussMaster以子进程方式启动并通信，适用于本地工具进程
- **Streamable HTTP**：GaussMaster通过HTTP连接远程MCP Server

### 配置mcp_servers.json

在`conf`目录下创建（或修改）`mcp_servers.json`配置文件。该文件为JSON格式，顶层键为`mcpServers`，每个MCP Server以名称为键。stdio方式通过`command`字段识别，HTTP方式通过`url`字段识别，两者互斥，不可同时配置。

```json
{
  "mcpServers": {
    "server-filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
      "env": {}
    },
    "server-remote-tools": {
      "url": "http://*.*.*.*:8000/mcp",
      "headers": {},
      "timeout": 30.0
    }
  }
}
```

字段说明：
- stdio方式：`command`为启动命令，`args`为命令参数列表，`env`为环境变量（可选）
- HTTP方式：`url`为MCP Server地址，`headers`为请求头（可选），`timeout`为连接超时时间（秒，默认30.0）
- Server名称不能重复；配置文件中不允许出现重复的JSON键

### 启动加载

执行`python startup.py service start -c conf`启动服务时，GaussMaster会自动读取`mcp_servers.json`并连接其中定义的所有MCP Server。连接结果会输出到终端：
- 连接成功：`MCP Server '<名称>' connected`
- 连接失败：`MCP Server '<名称>' connection failed: <原因>`

配置文件不存在或内容非法时**不会阻塞服务启动**，仅记录告警日志并跳过MCP初始化。

连接成功后，MCP工具会以`<server名称>_<工具名>`的形式自动加入工具集，与内置工具一起参与智能运维交互。若MCP工具与内置工具同名，内置工具优先生效。

服务运行期间，GaussMaster会周期性（默认30秒）对MCP Server进行健康巡检，连续多次不可达时会自动断开旧连接并在下一轮巡检中自动重连。

### 状态查询与热重载

**查询连接状态**（只读，不触发连接操作）：

```shell
curl -X 'GET' 'http://<gaussmaster_endpoint>/v1/api/mcp/status'
```

返回示例（字段以实际返回为准）：

```json
{
  "servers": {
    "filesystem": {
      "status": "connected",
      "status_label": "Connected",
      "transport": "stdio",
      "message": ""
    }
  },
  "connected_count": 1,
  "total_count": 1
}
```

**热重载配置**：修改`mcp_servers.json`后，无需重启服务，调用如下接口即可断开现有连接并按新配置重新连接：

```shell
curl -X 'PUT' 'http://<gaussmaster_endpoint>/v1/api/mcp/reload'
```

重载失败时GaussMaster会保留旧连接，不影响现有功能。

## 贡献

欢迎大家来参与贡献。详情请参阅我们的[社区贡献](https://opengauss.org/zh/contribution/)。

## 许可证

[MulanPSL-2.0](http://license.coscl.org.cn/MulanPSL2/)