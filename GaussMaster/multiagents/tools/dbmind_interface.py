# Copyright (c) 2025 Huawei Technologies Co.,Ltd.
#
# openGauss is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#          http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""dbmind tools"""

import json
import logging
import traceback
from collections import defaultdict
from copy import copy
from datetime import datetime, timedelta
from typing import Optional, Union
from urllib.parse import urljoin

from GaussMaster import global_vars
from GaussMaster.common.exceptions import CustomToolException
from GaussMaster.common.http import create_requests_session
from GaussMaster.common.http.dbmind_request import dbmind_request
from GaussMaster.common.http.ssl import get_ssl_context
from GaussMaster.common.metadatabase.dao import clusters
from GaussMaster.common.plugins.param import Param
from GaussMaster.common.utils.base import validate_return_format, adjust_timezone
from GaussMaster.common.utils.checking import split_ip_port
from GaussMaster.constants import DBMIND
from GaussMaster.multiagents.config.agent_config import AgentRoles
from GaussMaster.multiagents.config.dbmind_config import (
    ANSWER_MAP,
    FEATURE_MAP,
    DBMIND_DETECTIONS,
    REASON_TRANSLATION,
    METRIC_2_INSPECTION_MAP,
    STATUS_MAP,
    METRIC_TRANSLATION,
    NODE_LEVEL_METRIC, METRIC_DIAGNOSIS_REASON_MAP
)
from GaussMaster.multiagents.tools import base_tools
from GaussMaster.multiagents.tools.utils import (
    get_data_source_flag,
    transfer_timestamp_2_date,
    transfer_date_2_timestamp,
    generate_title
)
from GaussMaster.server.web.context_manager import current_instance
from GaussMaster.utils.ui_output_util import (
    formatter_str,
    formatter_table,
    formatter_graph,
    formatter_alarm,
    formatter_empty_alarm,
    formatter_list,
    formatter_filter, formatter_title
)

HISTORY_ALARMS = {
    'case_history_alarms': [
        'history_alarm_id', 'metric_name', 'instance', 'alarm_type',
        'alarm_level', 'start_time', 'end_time', 'metric_filter',
        'alarm_content', 'alarm_cause', 'cluster_role',
        'cluster_feature', 'source', 'status'
    ]
}


def workload_collection_call(params, limit=None) -> list:
    """
     dbmind api return e.g. {"data":{"header":["user_name","db_name","schema_name",
     "application_name","unique_query_id", "start_time", "finish_time","duration",
     "n_returned_rows","n_tuples_fetched","n_tuples_returned", "n_tuples_inserted",
     "n_tuples_updated","n_tuples_deleted","n_blocks_fetched","n_blocks_hit","n_soft_parse",
     "n_hard_parse", "db_time","cpu_time","parse_time","plan_time","data_io_time","lock_wait_time",
     "lwlock_wait_time","query"], "rows":[["user1","db1","public"...]...]},"success":true}
    """
    url = urljoin(
        global_vars.configs.get(DBMIND, "api_prefix"),
        "app/workload-collection"
    )
    response = dbmind_request("get", url, params=params)
    target = []
    if response.status_code == 200:
        try:
            result = response.json()
            sql_list = result.get("data").get("rows")
            if sql_list:
                headers = result.get("data").get("header")
                if limit:
                    sql_list_sorted = sorted(sql_list, key=lambda row: row[headers.index('duration')], reverse=True)[
                                      :limit]
                    target.append(
                        formatter_str(f"检测到{len(sql_list)}条sql，耗时最长的前{len(sql_list_sorted)}条sql如下"))
                    target.append(formatter_table(headers, sql_list_sorted))
                    return target
                target.append(formatter_str({
                    "zh": f"检测到{len(sql_list)}条sql. ",
                    "en": f"detected {len(sql_list)} slow sqls. "
                }))
                target.append(formatter_table(headers, sql_list))
                return target
            text = f"工具获取的sql结果集为空，实例{current_instance.get()}无满足条件sql出现"
            target.append(formatter_str(text))
            return target
        except Exception as e:
            logging.error(traceback.format_exc())
            target.append(formatter_str(f"工具结果解析异常：{e}"))
            return target

    target.append(formatter_str(f"工具执行异常{response.status_code}"))
    return target


@base_tools(
    name="collect_stat_activity_workloads",
    description="collect_stat_activity_workloads工具的功能是获取当前正在进行中的SQL语句，"
                "collect_stat_activity_workloads工具没有必要参数。",
    params=[
        Param(name="database", description="非必要参数，通过SQL语句运行的数据库名对SQL语句进行筛选，未指定默认为None",
              param_type="str", required=False),
        Param(name="schema", description="非必要参数，通过SQL语句运行的数据库模式对SQL语句进行筛选，未指定默认为None",
              param_type="str", required=False)
    ]
)
@validate_return_format
def collect_stat_activity_workloads(database: str = None, schema: str = None) -> list:
    """Get the currently executing sql"""
    params = {
        "data_source": "pg_stat_activity",
        "databases": database,
        "schemas": schema,
        "duration": 0
    }
    return workload_collection_call(params)


@base_tools(
    name="collect_history_statement",
    description="collect_history_statement工具的功能是获取指定时间范围内的历史SQL列表，"
                "collect_history_statement工具有2个必要参数start_time和end_time。",
    params=[
        Param(name="start_time",
              description="必要参数，通过SQL语句的开始时间对SQL语句进行筛选，格式为%Y-%m-%d %H:%M:%S",
              param_type="str"),
        Param(name="end_time",
              description="必要参数，通过SQL语句的结束时间对SQL语句进行筛选，格式为%Y-%m-%d %H:%M:%S",
              param_type="str"),
        Param(name="database", description="非必要参数，通过SQL语句运行的数据库名对SQL语句进行筛选，未指定则默认None",
              param_type="str", required=False),
        Param(name="schema", description="非必要参数，通过SQL语句运行的模式对SQL语句进行筛选, 未指定则默认None",
              param_type="str", required=False)
    ]
)
@validate_return_format
def collect_history_statement(start_time: str,
                              end_time: str,
                              database: Optional[str] = None,
                              schema: Optional[str] = None) -> list:
    """Get history statement"""
    from_timestamp, to_timestamp = transfer_date_2_timestamp(start_time, end_time)
    params = {
        "data_source": "dbe_perf.statement_history",
        "schemas": schema,
        "databases": database,
        "start_time": from_timestamp,
        "end_time": to_timestamp
    }
    return workload_collection_call(params)


@base_tools(
    name="risk_analysis",
    description="risk_analysis工具的功能是预测指标的未来，预测指标的变化趋势进行预测和对指标进行风险分析，"
                "如果问题中涉及到指标的未来或者预测未来，则使用此工具。"
                "risk_analysis工具有2个必要参数metric和warning_hours。",
    params=[
        Param(name="metric", description="必要参数，用来指定需要被预测的指标名", param_type="str"),
        Param(name="warning_hours",
              description="必要参数，需要趋势预测的未来的长度，单位：小时",
              param_type="int")
    ]
)
@validate_return_format
def risk_analysis(metric, warning_hours):
    url = urljoin(
        global_vars.configs.get(DBMIND, "api_prefix"),
        "app/risk-analysis/{metric}".format(metric=metric)
    )
    if metric.startswith("os_") or metric in NODE_LEVEL_METRIC:
        instance = split_ip_port(current_instance.get())[0]
    else:
        instance = current_instance.get()
    params = {"instance": instance, "warning_hours": warning_hours, "tz": "UTC+8"}
    response = dbmind_request("get", url, params=params)

    target = []
    if response.status_code == 200 and "data" in response.json():
        try:
            result = json.loads(response.text)
            detail = result.get("data").get(metric)[0].get("abnormal_detail")
            timestamps = result.get("data").get(metric)[0].get("timestamps")
            values = result.get("data").get(metric)[0].get("values")
            time_strs = [datetime.fromtimestamp(int(ts) / 1000).strftime("%H:%M")
                         for ts in timestamps]
            target.append(
                formatter_str(
                    f"指标{metric}的状况为: 在接下来{warning_hours}小时内{detail}，当前状态如下图所示："
                )
            )
            target.append(formatter_graph(time_strs, values))
            return target
        except Exception:
            target.append(
                formatter_str(
                    f"不能获取到关于指标{metric}在接下来{warning_hours}小时内的状况，请确认指标名是否正确"
                )
            )
            return target

    target.append(
        formatter_str(
            f"不能获取到关于指标{metric}在接下来{warning_hours}小时内的状况"
        )
    )

    return target


@base_tools(
    name="cluster_diagnosis",
    description="cluster_diagnosis工具的功能是查询集群状态并进行诊断，"
                "cluster_diagnosis工具有1个非必要参数start_time。",
    params=[
        Param(name="start_time",
              description="非必要参数，进行集群诊断的时间点, 格式为%Y-%m-%d %H:%M:%S"
                          "默认值为当前时间",
              param_type="str",
              required=False)
    ]
)
@validate_return_format
def cluster_diagnosis(start_time: str = None):
    """diagnose cluster status """
    _, target = _diagnose_cluster_status(start_time=start_time)
    return target


def _cluster_diagnosis(role, timestamp, method: str = 'logical'):
    url = urljoin(
        global_vars.configs.get(DBMIND, "api_prefix"),
        "app/cluster-diagnosis"
    )
    instance = split_ip_port(current_instance.get())[0]
    params = {"instance": instance, "role": role, "timestamp": timestamp, "method": method}
    response = dbmind_request("get", url, params=params)

    flag = False
    target = []
    if response.status_code != 200 or "data" not in response.json():
        return False, [formatter_str({
            "zh": "集群诊断工具调用失败. ",
            "en": "Failed to call the cluster diagnostic tool. "
        })]

    result = response.json()
    try:
        features = result.get("data")[0]
        diagnosis_result = result.get("data")[1]
        diagnosis_result_zh = ANSWER_MAP[role].get(diagnosis_result, diagnosis_result)
        status_key = {"cn": "cn_status", "dn": "dn_status"}[role]
        status_map = STATUS_MAP.get(role).get(global_vars.LANGUAGE, STATUS_MAP.get(role).get('zh'))
        status = status_map.get(features[status_key], "Unknown")
        features.pop(status_key)

        target.append(formatter_str({
            "zh": f"数据库节点'{instance}'是一个'{role}', 其状态为'{status}', "
                  f"其诊断结果为：'{diagnosis_result_zh}'. ",
            "en": f"The database node {instance}'s role is '{role}', "
                  f"which is under condition '{status}'"
                  f"and has the diagnostic result: '{diagnosis_result}'. "
        }))
        if diagnosis_result != "Normal":
            flag = True
            abnormal_feature = []
            for feature_name, n in features.items():
                if n == 0:
                    continue

                if global_vars.LANGUAGE == "zh":
                    abnormal_feature.append(FEATURE_MAP.get(feature_name, feature_name))
                else:
                    abnormal_feature.append(feature_name)

            target.append(formatter_str({
                "zh": f"集群发生的异常包括{', '.join(abnormal_feature)}. ",
                "en": f"Related feature: {', '.join(abnormal_feature)}. "
            }, color='red'))

        return flag, target

    except Exception:
        flag = False
        target.append(formatter_str({
            "zh": f"解析集群诊断结果异常. ",
            "en": "Failed to parse cluster diagnostics. "
        }))

    return flag, target


def _diagnose_cluster_status(diagnosis_method: str = 'logical', cluster_role: str = None,
                             start_time: str = None, instance: str = None):
    """diagnose cluster status """
    tz = adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))
    if start_time is not None:
        to_timestamp = int(datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").timestamp()) * 1000
    else:
        to_timestamp = int(datetime.now(tz).timestamp() * 1000)

    if instance is None:
        instance = split_ip_port(current_instance.get())[0]

    if cluster_role is None:
        from_timestamp = to_timestamp - 10 * 60 * 1000
        url = urljoin(
            global_vars.configs.get(DBMIND, "api_prefix"),
            "summary/metrics/gaussdb_cluster_state"
        )
        params = {"from_timestamp": from_timestamp, "to_timestamp": to_timestamp,
                  "fetch_all": True, "regex": True}

        response = dbmind_request("get", url, params=params)
        if response.status_code != 200 or "data" not in response.json():
            return False, [formatter_str({
                "zh": "不能获取到关于集群的详细信息，集群诊断失败. ",
                "en": "Cluster diagnosis failed because details about "
                      "the cluster could not be obtained. "
            })]

        seqs = response.json().get("data")
        role = "dn"
        for seq in seqs:
            dn_detail_list = json.loads(seq.get("labels").get("dn_state", "[]"))
            cn_detail_list = json.loads(seq.get("labels").get("cn_state", "[]"))
            dn_list = [dn.get("ip") for dn in dn_detail_list] if dn_detail_list else []
            cn_list = [cn.get("ip") for cn in cn_detail_list] if cn_detail_list else []
            if instance in dn_list:
                role = "dn"
                break

            if instance in cn_list:
                role = "cn"
                break
    else:
        role = cluster_role

    target = [formatter_str({
        "zh": f"节点{instance}的角色为：{role}. ",
        "en": f"The role of node {instance} is: {role}. "
    })]

    flag, diagnosis_result = _cluster_diagnosis(role, to_timestamp, diagnosis_method)
    target.extend(diagnosis_result)
    return flag, target


@base_tools(
    name="index_recommendation",
    description="index_recommendation工具的功能是进行索引推荐，"
                "index_recommendation工具有2个必要参数sql和db_name。",
    params=[
        Param(name="sql", description="必要参数，需要进行索引推荐的查询语句SQL", param_type="str"),
        Param(name="db_name", description="必要参数，需要进行索引推荐的查询语句SQL所在的数据库名", param_type="str")
    ],
    roles=[AgentRoles.Repairer.name]
)
@validate_return_format
def index_recommendation(sql, db_name):
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "app/index-recommendation")
    params = {
        "database": db_name,
        "max_index_num": 10,
        "max_index_storage": 1000,
        "instance": current_instance.get()
    }
    body_params = [sql]
    response = dbmind_request("post", url, params=params, data=json.dumps(body_params))
    target = []
    if response.status_code == 200 and "data" in response.json():
        result = json.loads(response.text)
        try:
            advice = result.get("data")[0].get("advise_indexes")
            rows = [(item.get("index"), item.get("index_size"), item.get("improve_rate"))
                    for item in advice]
            headers = ["索引描述", "预计占用", "预计提升"]
            target.append(formatter_str("推荐的索引如下表所示："))
            target.append(formatter_table(headers, rows))
            return target

        except Exception:
            logging.error(traceback.format_exc())
    target.append(formatter_str("无推荐索引"))

    return target


@base_tools(
    name="slow_sql_rca",
    description="slow_sql_rca工具的功能是对慢SQL进行诊断和根因分析，"
                "slow_sql_rca工具有2个必要参数query和db_name。",
    params=[
        Param(name="query", description="必要参数，需要进行慢SQL根因分析的查询语句SQL", param_type="str"),
        Param(name="db_name", description="必要参数，需要进行慢SQL根因分析的查询语句SQL所在的数据库名", param_type="str")
    ]
)
@validate_return_format
def slow_sql_rca(query, db_name, **kwargs):
    params = {"db_name": db_name, "query": query}
    params.update(kwargs)

    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "app/slow-sql-rca")
    response = dbmind_request("get", url, params=params)
    target = []
    if response.status_code == 200 and "data" in response.json():
        result = response.json()
        try:
            roots = result.get("data")[-1][0][0]
            advice = result.get("data")[-1][1][0]
            if roots:
                target.append(formatter_str(f"慢SQL{query}的诊断结果如下："))
                target.append(formatter_table(["慢SQL根因", "诊断建议"], list(zip(roots, advice))))
                return target

            return target.append(f"慢SQL{query}未诊断到任何根因")
        except Exception as e:
            target.append(formatter_str(f"工具结果解析异常：{e}"))
            return target

    target.append(formatter_str(f"工具执行异常{response.status_code}"))

    return target


@base_tools(
    name="get_metric_range_sequence",
    description="get_metric_range_sequence工具的功能是查询指定指标在特定时间范围内的数据信息或者获取指标的状态，"
                "get_metric_range_sequence工具有3个必要参数metric_name，start_time和end_time。",
    params=[
        Param(name="metric_name", description="必要参数，要查询的指标的指标名", param_type="str"),
        Param(name="start_time", description="必要参数，要查询的指标的开始时间，格式为%Y-%m-%d %H:%M:%S",
              param_type="str"),
        Param(name="end_time", description="必要参数，要查询的指标的结束时间，格式为%Y-%m-%d %H:%M:%S", param_type="str")
    ]
)
@validate_return_format
def get_metric_range_sequence(metric_name, start_time: str, end_time: str):
    """get all metric sequences of the cluster instances"""
    instance_list = get_cluster_list().get(current_instance.get())
    final_output = []
    for instance in instance_list:
        output = get_metric_by_instance_or_labels(metric_name, start_time, end_time, instance)
        final_output.extend(output)
    return final_output


def get_metric_by_instance_or_labels(metric_name, start_time: str, end_time: str, instance: str = None,
                                     labels: str = None):
    if instance is None and labels is None:
        return [
            formatter_str({
                "zh": f"{instance}: 获取指标数据失败. ",
                "en": f"{instance}: Failed to fetch metric values. "
            })
        ]
    from_timestamp, to_timestamp = transfer_date_2_timestamp(start_time, end_time)
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"),
                  "summary/metrics/{name}".format(name=metric_name))
    params = {
        "instance": instance,
        "name": metric_name,
        "from_timestamp": from_timestamp,
        "to_timestamp": to_timestamp,
        "fetch_all": True,
        "regex": True,
        "labels": labels
    }
    response = dbmind_request("get", url, params=params)
    target = []
    if response.status_code == 200 and "data" in response.json():
        result = response.json()
        if result["data"]:
            target.append(formatter_str({
                "zh": f"{instance}: 指标{METRIC_TRANSLATION.get(metric_name, metric_name)}"
                      f"在时间范围{start_time}到{end_time}的数据如下所示: ",
                "en": f"{instance}: The data of {metric_name} in the time range "
                      f"from {start_time} to {end_time} is as follows: "
            }))
            for data in result["data"]:
                if isinstance(data["labels"], dict):
                    title = generate_title(data["name"], data["labels"])
                else:
                    title = str(data["labels"])

                target.append(
                    formatter_graph(
                        data["timestamps"],
                        data["values"],
                        title=title
                    )
                )
        else:
            target.append(formatter_str({
                "zh": f"{instance}: 指标{METRIC_TRANSLATION.get(metric_name, metric_name)}"
                      f"在时间范围{start_time}到{end_time}的数据为空. ",
                "en": f"{instance}: The {metric_name} metric is empty in the time range "
                      f"{start_time} to {end_time}. "
            }))

        return target

    target.append(
        formatter_str({
            "zh": f"{instance}: 获取指标数据失败. ",
            "en": f"{instance}: Failed to fetch metric values. "
        }))

    return target


def _get_related_sqls(url, headers, tips):
    """
    common method for get specific sqls
    :param url: related url
    :param headers: columns
    :param tips: description of sqls
    """
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), url)
    response = dbmind_request("get", url)
    target = []
    if response.status_code == 200:
        result = json.loads(response.text)
        header = result.get("data").get("header")
        filtered_result = []
        if header is not None:
            rows = result.get("data").get("rows")
            filtered_result = [
                                  [
                                      item[header.index(column)]
                                      for column in headers
                                  ]
                                  for item in rows
                                  if item[header.index("query")]
                              ][:10]

        target.append(formatter_str(tips))
        target.append(formatter_table(headers, filtered_result))

        return target

    target.append(formatter_str({
        "zh": "获取失败. ",
        "en": "Failed to get related sql. "
    }))

    return target


@base_tools(
    name="get_top_sqls",
    description="get_top_sqls工具的功能是查询所有查询语句中运行时间最长的那些查询语句Top SQLs，"
                "get_top_sqls工具没有参数，不需要进行参数提取。"
)
@validate_return_format
def get_top_sqls():
    """get top sqls"""
    url = "summary/sql/top"
    headers = ["query", "n_calls"]
    tips = {"zh": "当前top SQL信息为: ",
            "en": "Top sqls are as follows: "}
    return _get_related_sqls(url, headers, tips)


@base_tools(
    name="get_locking_sql",
    description="get_locking_sql工具的功能是查询所有查询语句中被锁定的查询语句，"
                "get_locking_sql工具没有参数，不需要进行参数提取。"
)
def get_locking_sql():
    """get locking sqls"""
    url = "summary/sql/locking"
    headers = ["datname", "query", "sessionid", "query_start"]
    tips = {"zh": "当前被锁阻塞SQL信息为: ",
            "en": "Locking sqls are as follows: "}
    return _get_related_sqls(url, headers, tips)


@base_tools(
    name="get_database_info",
    description="查询当前实例数据库列表，"
                "get_database_info工具没有参数，不需要进行参数提取。"
)
def get_database_info():
    """get database info"""
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "summary/database-list")
    response = dbmind_request("get", url)
    target = []
    if response.status_code == 200:
        result = json.loads(response.text)
        target.append(formatter_str({
            "zh": "当前实例数据库信息如下: ",
            "en": "All databases are as follows: "
        }))
        headers = ["index", "name"]
        rows = [[index + 1, item] for index, item in enumerate(result.get("data", []))]
        target.append(formatter_table(headers, rows))
        return target

    target.append(formatter_str({
        "zh": "存在异常，无法获取信息. ",
        "en": "Failed to get databases information. "
    }))

    return target


@base_tools(
    name="get_knob_warning",
    description="查询是否存在不合适的指标配置，"
                "get_knob_warning工具没有参数，不需要进行参数提取。"
)
def get_knob_warning():
    """get knob warning"""
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"),
                  "summary/knob-recommendation/warnings")
    params = {"pagesize": 1000}
    response = dbmind_request("get", url, params=params)
    target = []
    if response.status_code == 200:
        result = response.json()
        values = result.get("data").get("rows", [])
        if not values:
            target.append(formatter_str("当前数据库参数无报警信息"))
            return target

        target.append(formatter_str("当前数据参数报警信息为:"))
        target.append(formatter_table(headers=result["data"]["header"], rows=values))
        return target

    target.append(formatter_str({
        "zh": "获取指标告警失败. ",
        "en": "Failed to get the warning knob config. "
    }))
    return target


@base_tools(
    name="get_instance_status",
    description="get_instance_status工具的功能是查询当前数据库的实例信息和状态，"
                "get_instance_status工具没有参数，不需要进行参数提取。"
)
@validate_return_format
def get_instance_status():
    """get instance status"""
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "status/instances")
    response = dbmind_request("get", url)
    target = []
    if response.status_code == 200:
        result = json.loads(response.text)
        rows = result["data"]["rows"]
        for row in rows:
            row[2] = "True" if row[2] else "False"

        target.append(formatter_table(headers=["instance", "role", "state"], rows=rows))
        return target
    target.append(formatter_str("存在异常，无法获取信息"))
    return target


def get_cluster_list():
    """get cluster list"""
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "agents")
    with create_requests_session(ssl_context=get_ssl_context()) as session:
        response = session.get(url, timeout=2)
        response.raise_for_status()

    if response.status_code == 200 and 'data' in response.json():
        current_dbmind_clusters = response.json().get("data")
        return current_dbmind_clusters
    return {}


def retrieve_clusters_status():
    """retrieve clusters status"""
    agents = get_cluster_list()
    if agents:
        return retrieve_cluster_status(agents)
    return {}


def retrieve_cluster_status(agents):
    """
    Retrieve the status of each cluster.

    Parameters:
    agents (dict): A dictionary containing the primary node and all nodes of each cluster.

    Returns:
    cluster_status (dict): A dictionary containing the status of each cluster.

    """
    cluster_status = dict()
    for primary, all_nodes in agents.items():
        managed = False
        cluster_name = ''
        for node in all_nodes:
            ip, port = split_ip_port(node)
            res = clusters.select_managed_cluster(ip, port)
            if res:
                cluster_name = res.get('cluster_name')
                managed = True
        cluster_status[primary] = {"cluster_name": cluster_name, "managed": managed,
                                   "instances": all_nodes}
    return cluster_status


@base_tools(
    name="get_all_cluster",
    description="get_all_cluster工具的功能是获取纳管的所有数据库集群信息，"
                "get_all_cluster工具没有参数，不需要进行参数提取。"
)
def get_all_cluster():
    """get all cluster"""
    result = get_cluster_list()
    target = []
    if result:
        target.append(formatter_str({
            "zh": "所有数据库集群信息如下: ",
            "en": "All database cluster info is as follows: "
        }))
        headers = ["agent", "instances"]
        rows = [[key, ", ".join(value)] for key, value in result.items()]
        target.append(formatter_table(headers, rows))
    else:
        target.append(formatter_str({
            "zh": "集群信息为空. ",
            "en": "The database cluster info is None. "
        }))

    return target


def update_cluster_list():
    """update cluster list"""
    params = {"force": False}
    headers = {"Content-Type": "application/json"}
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "agent/status")
    with create_requests_session(ssl_context=get_ssl_context()) as session:
        response = session.put(url, headers=headers, data=json.dumps(params))

    if response.status_code == 200:
        return response.json().get("data")

    return False


@base_tools(
    name="get_guc_parameter",
    description="get_guc_parameter工具的功能是查询数据库的特定GUC参数的当前值，"
                "get_guc_parameter工具有1个必要参数name。",
    params=[Param(name="name",
                  description="必要参数，需要查询的GUC参数的名称，"
                              "问题里出现的字母和下划线组合一般为该参数",
                  param_type="str")]
)
@validate_return_format
def get_guc_parameter(name):
    """get value of the guc parameter """
    url = urljoin(
        global_vars.configs.get(DBMIND, "api_prefix"),
        "summary/metrics/pg_settings_setting"
    )
    params = {
        "labels": f"name={name}",
        "fetch_all": False,
        "latest_minutes": 1,
        "from_instance": current_instance.get()
    }
    response = dbmind_request("get", url, params=params)
    target = []
    if response.status_code == 200:
        result = json.loads(response.text)
        if len(result["data"]) >= 1 and len(result["data"][0].get('values', [])) > 0:
            value = result.get("data")[0].get("values", [-1])[-1]
            vartype = result.get("data")[0].get("labels").get("vartype")
            target.append(formatter_str({
                "zh": f"GUC参数{name}当前值:{value}，类型为{vartype}. ",
                "en": f"The current value of {name} is {value}, whose type is {vartype}. "
            }))

            return target

    target.append(formatter_str({
        "zh": "查询GUC参数失败，请确认GUC参数名是否正确。",
        "en": "Failed to query GUC parameter. Please confirm that the GUC parameter name is correct."
    }))

    return target


def _filter_alarm(alarm: dict) -> dict:
    """filter alarm"""
    alarm["source"] = "dbmind"  # alarm source
    alarm["status"] = 0  # alarm is not cleared
    return {
        col: alarm[col]
        for col in HISTORY_ALARMS["case_history_alarms"]
        if col in alarm
    }


def _alarm_uniform(alarm: dict):
    """
    raw cluster alarm columns: diagnosis_id | instance | timestamp | cluster_role | diagnosis_method |
    cluster_feature | diagnosis_result | status_code | alarm_type | alarm_level

    final alarm columns: history_alarm_id | metric_name | start_time | cluster_role | diagnosis_method |
    cluster_feature | diagnosis_result
    """
    alarm["history_alarm_id"] = "c_" + str(alarm["diagnosis_id"])
    alarm["metric_name"] = "gaussdb_cluster_state"
    alarm["start_time"] = alarm["timestamp"]
    alarm["alarm_content"] = (
        f"{alarm['instance']}({alarm['cluster_role']}): "
        f"{alarm['cluster_feature']} {alarm['diagnosis_result']}"
    )
    alarm['metric_filter'] = {
        'diagnosis_method': alarm["diagnosis_method"],
        'diagnosis_result': alarm["diagnosis_result"]
    }

    del alarm["diagnosis_id"]
    del alarm["timestamp"]
    del alarm["status_code"]

    alarm = _filter_alarm(alarm)
    return alarm


def test_scene(scene, final_alarms, real_alarms, mock_list):
    """Only used for demo demonstration, to be deleted"""
    tz = adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))
    if scene not in real_alarms and scene in mock_list:
        end_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        start_time = (datetime.now(tz) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        if scene == "os_cpu_user_usage":
            alarm_detail = {
                "history_alarm_id": 5000,
                "instance": f"{split_ip_port(current_instance.get())[0]}",
                "metric_name": "os_cpu_user_usage",
                "metric_filter": f"from_instance={split_ip_port(current_instance.get())[0]}",
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": "CPU使用率超过了告警上限: 80%"
            }
            final_alarms["os_cpu_user_usage"] = {
                "metric_name": "os_cpu_user_usage",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：CPU使用率超过了告警上限: 80%",
                "detail": alarm_detail
            }
        elif scene == "disk_usage":
            alarm_detail = {
                "history_alarm_id": 5001,
                "instance": "*.*.*.*",
                "metric_name": "os_disk_usage",
                "metric_filter": "",
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": "磁盘使用率超过了告警上限: 70%"
            }
            final_alarms["disk_usage"] = {
                "metric_name": "os_disk_usage",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：磁盘使用率超过了告警上限: 70%",
                "detail": alarm_detail
            }
        elif scene == "os_mem_usage":
            alarm_detail = {
                "history_alarm_id": 5002,
                "instance": "*.*.*.*",
                "metric_name": "os_mem_usage",
                "metric_filter": "",
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": "内存使用率超过了告警上限: 70%"
            }
            final_alarms["os_mem_usage"] = {
                "metric_name": "os_mem_usage",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：内存使用率超过了告警上限: 70%",
                "detail": alarm_detail
            }
        elif scene == "gaussdb_cluster_state":
            alarm_detail = {
                "history_alarm_id": 5003,
                "instance": f"{current_instance.get()}",
                "metric_name": "gaussdb_cluster_state",
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": "集群状态异常",
                "cluster_feature": json.dumps({
                    "ping": 0,
                    "dn_status": 0,
                    "bind_ip_failed": 0,
                    "dn_ping_standby": 0,
                    "ffic_updated": 1,
                    "cms_phonydead_restart": 0,
                    "cms_restart_pending": 0,
                    "dn_read_only": 1,
                    "dn_manual_stop": 0,
                    "dn_disk_damage": 0,
                    "dn_nic_down": 0,
                    "dn_port_conflict": 0,
                    "dn_writable": 0
                })
            }
            final_alarms["gaussdb_cluster_state"] = {
                "metric_name": "gaussdb_cluster_state",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：集群状态异常",
                "detail": alarm_detail
            }
        elif scene == "os_disk_io_exception":
            alarm_detail = {
                "history_alarm_id": 5004,
                "instance": "*.*.*.*",
                "metric_name": "os_disk_await",
                "metric_filter": f"from_instance={split_ip_port(current_instance.get())[0]}",
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": "磁盘I/O等待"
            }
            final_alarms["os_disk_io_exception"] = {
                "metric_name": "os_disk_await",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：磁盘I/O等待",
                "detail": alarm_detail
            }
        elif scene == "self_security_exception":
            alarm_detail = {
                "history_alarm_id": 5005,
                "instance": f"{current_instance.get()}",
                "metric_name": "scanning_attack",
                "metric_filter": "",
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": (
                    "Between 2024-06-04 13:59:00 and 2024-06-04 14:29:00 found the following "
                    "anomalies: had 3 anomalies in metric:gaussdb_log_errors_rate, "
                    "had 0 anomalies in metric:gaussdb_user_violation_rate."
                )
            }
            final_alarms["self_security_exception"] = {
                "metric_name": "scanning_attack",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：扫描攻击",
                "detail": alarm_detail
            }
        elif scene == "gaussdb_ping_lag":
            alarm_detail = {
                "history_alarm_id": 5007,
                "instance": "*.*.*.*",
                "metric_name": "gaussdb_ping_lag",
                "metric_filter": 'to_primary_dn=True',
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": "节点网络异常"
            }
            final_alarms["gaussdb_ping_lag"] = {
                "metric_name": "gaussdb_ping_lag",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：节点网络异常",
                "detail": alarm_detail
            }
        elif scene == "P80_P95":
            alarm_detail = {
                "history_alarm_id": 5008,
                "instance": f"{split_ip_port(current_instance.get())[0]}",
                "metric_name": "statement_responsetime_percentile_p95",
                "metric_filter": "",
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": "P80/P95异常"
            }
            final_alarms["P80_P95"] = {
                "metric_name": "statement_responsetime_percentile_p95",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：P80/P95异常",
                "detail": alarm_detail
            }
        elif scene == "core_dump":
            alarm_detail = {
                "history_alarm_id": 5009,
                "instance": "*.*.*.*",
                "metric_name": "gaussdb_log_ffic",
                "metric_filter": "instance=10.90/56/174:9183,unique_sql_id=111,debug_query_id=222",
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": "出现Core Dump"
            }
            final_alarms["core_dump"] = {
                "metric_name": "gaussdb_log_ffic",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：数据库内核异常",
                "detail": alarm_detail
            }
        elif scene == "pg_long_transaction_count":
            alarm_detail = {
                "history_alarm_id": 5010,
                "instance": "*.*.*.*",
                "metric_name": "pg_long_transaction_count",
                "metric_filter": "",
                "start_time": start_time,
                "end_time": end_time,
                "alarm_content": "存在长事务"
            }
            final_alarms["pg_long_transaction_count"] = {
                "metric_name": "pg_long_transaction_count",
                "from_timestamp": 0,
                "content": f"{start_time}到{end_time}期间出现如下异常：存在长事务",
                "detail": alarm_detail
            }
        elif scene == "slow_sql":
            alarm_detail = {
                "history_alarm_id": 5010,
                "start_time": start_time,
                'metric_name': 'slow_sql',
                'db_name': 'test_db',
                'schema_name': '"$user",public',
                'query_id': 1946399464033034044,
                'n_returned_rows': 1,
                'n_tuples_fetched': 1,
                'n_tuples_returned': 1,
                'n_tuples_inserted': 0,
                'n_tuples_updated': 0,
                'n_tuples_deleted': 0,
                'n_blocks_fetched': 20,
                'n_blocks_hit': 20,
                'n_soft_parse': 1,
                'n_hard_parse': 1,
                'cpu_time': 887,
                'parse_time': 15,
                'plan_time': 178,
                'data_io_time': 0,
                'lock_wait_time': 198988488,
                'lwlock_wait_time': 0,
                'client_addr': '192.168.0.1',
                'query': 'SELECT * FROM test_table WHERE id = ? FOR UPDATE;',
                'query_plan': (
                    "Datanode Name: dn_6001\n"
                    "LockRows  (cost=0.00..8.28 rows=1 width=42)\n"
                    "  ->  Index Scan using test_table_pkey on test_table  (cost=0.00..8.27 rows=1 width=42)\n"
                    "        Index Cond: (id = '***')\n"
                    "\n"
                ),
                'alarm_content': f"节点: 192.168.0.1在{start_time}有慢sql: "
                                 f"SELECT * FROM test_table WHERE id = ? FOR UPDATE;"
            }
            final_alarms["slow_sql"] = {
                "metric_name": "slow_sql",
                "from_timestamp": 0,
                "content": f"节点: 192.168.0.1在{start_time}有慢sql: {alarm_detail['query']}",
                "detail": alarm_detail
            }


def supply_inspection(real_alarms: dict, mock_list: list) -> dict:
    """assemble alarms"""
    final_alarms = real_alarms.copy()

    for k, v in DBMIND_DETECTIONS.items():
        test_scene(k, final_alarms, real_alarms, mock_list)
        if k not in real_alarms and k not in mock_list:
            final_alarms[k] = {"content": v.get("anomaly_name"), "detail": {}}

    return final_alarms


def get_alarm_from_history(from_timestamp: int, to_timestamp: int, ip_list: list):
    """
    fetch alarm from tb_history_alarms
    """
    detector_alarms = defaultdict(list)
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "summary/alarms")
    params = {"start_at": from_timestamp, "end_at": to_timestamp, "pagesize": 1000}
    response = dbmind_request("get", url, params=params)
    if response.status_code != 200 or "data" not in response.json():
        raise CustomToolException({
            "zh": "工具执行异常. ",
            "en": "An exception occurred in the tool call. "
        })

    try:
        alarms = response.json().get("data").get("rows")
        if not alarms:
            return detector_alarms

        header = response.json().get("data").get("header")
        idx_instance = header.index("instance")
        idx_metric_name = header.index("metric_name")
        idx_start_at = header.index("start_at")
        idx_end_at = header.index("end_at")
        idx_alarm_content = header.index("alarm_content")
        header[idx_start_at] = "start_time"
        header[idx_end_at] = "end_time"
        for alarm in alarms:
            ip = split_ip_port(alarm[idx_instance].strip())[0]
            if ip not in ip_list:
                continue

            metric_name = alarm[idx_metric_name].strip()
            from_timestamp = alarm[idx_start_at]
            alarm[idx_start_at] = transfer_timestamp_2_date(alarm[idx_start_at])
            alarm[idx_end_at] = transfer_timestamp_2_date(alarm[idx_end_at])
            alarm_detail = _filter_alarm(dict(zip(header, alarm)))
            content = (f"{alarm[idx_start_at]}到{alarm[idx_end_at]}期间出现如下异常: "
                       f"{alarm[idx_alarm_content]}")

            if metric_name not in METRIC_2_INSPECTION_MAP:
                continue

            detector_alarms[ip].append({
                "metric_name": metric_name,
                "from_timestamp": from_timestamp,
                "content": content,
                "detail": alarm_detail
            })

    except Exception:
        raise CustomToolException({
            "zh": "告警解析异常. ",
            "en": "An exception occurred in the parsing alarms. "
        }) from None

    return detector_alarms


def get_summary_cluster_diagnosis(from_timestamp: int, to_timestamp: int,
                                  is_normal: bool, ip_list: list):
    """
    fetch the summary of cluster_diagnosis

    :param from_timestamp: from_timestamp
    :param to_timestamp: to_timestamp
    :param is_normal: filter the record which is normal
    :param ip_list: current ip list
    :return: [{"metric_name": "", "from_timestamp": "", "content": "", "detail": {xxx}}]
    """
    url = urljoin(
        global_vars.configs.get(DBMIND, "api_prefix"),
        "summary/cluster-diagnosis"
    )
    params = {"start_at": from_timestamp, "end_at": to_timestamp,
              "pagesize": 10000, "is_normal": is_normal}
    response = dbmind_request("get", url, params=params)
    cluster_alarms = defaultdict(list)
    if response.status_code != 200 or "data" not in response.json():
        raise CustomToolException({
            "zh": "工具执行异常. ",
            "en": "An exception occurred in the tool call. "
        })

    try:
        alarms = response.json().get("data").get("rows")
        if not alarms:
            return cluster_alarms

        header = response.json().get("data").get("header")
        idx_instance = header.index("instance")
        idx_timestamp = header.index("timestamp")
        idx_cluster_role = header.index("cluster_role")
        for alarm in alarms:
            ip = split_ip_port(alarm[idx_instance].strip())[0]
            if ip not in ip_list:
                continue

            alarm_from_timestamp = alarm[idx_timestamp]
            alarm[idx_timestamp] = transfer_timestamp_2_date(alarm[idx_timestamp])
            content = (f"{alarm[idx_cluster_role].strip()}节点: "
                       f"{alarm[idx_instance].strip()}在{alarm[idx_timestamp]}状态异常")
            alarm_detail = _alarm_uniform(dict(zip(header, alarm)))
            cluster_alarm = {
                "metric_name": "gaussdb_cluster_state",
                "from_timestamp": alarm_from_timestamp,
                "content": content,
                "detail": alarm_detail
            }
            if cluster_alarms[ip]:
                if alarm_from_timestamp > cluster_alarms[ip][0].get("from_timestamp"):
                    cluster_alarms[ip] = [cluster_alarm]
            else:
                cluster_alarms[ip].append(cluster_alarm)

    except Exception:
        raise CustomToolException({
            "zh": "告警解析异常. ",
            "en": "An exception occurred in the parsing alarms. "
        }) from None

    return cluster_alarms


@base_tools(
    name="summary_alarms",
    description="summary_alarms工具的功能是获取指定时间范围内的告警信息，"
                "summary_alarms工具有2个必要参数start_time和end_time，如果没有日期的信息，默认日期为今天。",
    params=[
        Param(name="start_time",
              description="必要参数，用来筛选告警的时间范围的开始时间，格式为%Y-%m-%d %H:%M:%S",
              param_type="str"),
        Param(name="end_time",
              description="必要参数，用来筛选告警的时间范围的结束时间，格式为%Y-%m-%d %H:%M:%S",
              param_type="str")
    ]
)
@validate_return_format
def summary_alarms(start_time, end_time):
    """get all alarms"""
    start_at, end_at = transfer_date_2_timestamp(start_time, end_time)

    instance_list = get_cluster_list().get(current_instance.get())
    ip_list = [split_ip_port(instance)[0] for instance in instance_list]

    final_alarms = fetch_all_alarms(start_at, end_at, ip_list)

    target = generate_alarm_output(final_alarms, ip_list)
    return target


def get_slow_sqls(from_timestamp: int, to_timestamp: int):
    """
    fetch the slow sqls

    :param from_timestamp: from_timestamp
    :param to_timestamp: to_timestamp
    :return: list of slow sqls
    """

    params = {
        "data_source": "dbe_perf.statement_history",
        "start_time": from_timestamp,
        "end_time": to_timestamp,
        "duration": 0,
    }
    url = urljoin(
        global_vars.configs.get(DBMIND, "api_prefix"),
        "app/workload-collection"
    )
    response = dbmind_request("get", url, params=params)
    if response.status_code != 200 or "data" not in response.json():
        raise CustomToolException({
            "zh": "工具执行异常. ",
            "en": "An exception occurred in the tool call. "
        })

    try:
        slow_sql_list = defaultdict(list)
        resp = response.json()
        rows = resp["data"]["rows"]
        if not rows:
            return slow_sql_list

        header = resp["data"]["header"]
        workload_collection = list()
        for row in rows:
            workload_collection.append(dict(zip(header, row)))

        rca_headers = [
            'db_name', 'schema_name', 'query_id', 'n_returned_rows', 'n_tuples_fetched',
            'n_tuples_returned', 'n_tuples_inserted', 'n_tuples_updated', 'n_tuples_deleted',
            'n_blocks_fetched', 'n_blocks_hit', 'n_soft_parse', 'n_hard_parse', 'cpu_time',
            'parse_time', 'plan_time', 'data_io_time', 'lock_wait_time', 'lwlock_wait_time',
            'client_addr', 'query', 'query_plan'
        ]
        for sql in workload_collection:
            ip = split_ip_port(current_instance.get())[0]
            query = sql.get('query')
            if not (isinstance(query, str) and query):
                continue

            slow_sql = {k: sql.get(k) for k in rca_headers}
            start_datetime = transfer_timestamp_2_date(from_timestamp)
            end_datetime = transfer_timestamp_2_date(to_timestamp)
            alarm_timestamp = datetime.strptime(sql['start_time'], "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
            alarm_timestamp = int(alarm_timestamp * 1000)  # ms
            alarm_datetime = transfer_timestamp_2_date(alarm_timestamp)
            content = f"节点: {ip}在{alarm_datetime}有慢sql: {query}"
            slow_sql["metric_name"] = "slow_sql"
            slow_sql["start_time"] = start_datetime
            slow_sql["end_time"] = end_datetime
            slow_sql["alarm_content"] = content
            slow_sql_list[ip].append({
                "metric_name": "slow_sql",
                "from_timestamp": alarm_timestamp,
                "content": content,
                "detail": slow_sql
            })

    except Exception:
        raise CustomToolException({
            "zh": "告警解析异常. ",
            "en": "An exception occurred in the parsing alarms. "
        })

    return slow_sql_list


def fetch_all_alarms(start_at: int, end_at: int, ip_list: list):
    """
    get all alarms by calling the related apis of DBMind
    """
    detector_alarms = get_alarm_from_history(start_at, end_at, ip_list)
    cluster_alarms = get_summary_cluster_diagnosis(start_at, end_at, False, ip_list)
    slow_sql_alarms = get_slow_sqls(start_at, end_at)

    # 将检测器的告警与集群诊断结果放到一起
    for ip in ip_list:
        detector_alarms[ip].extend(cluster_alarms[ip])
        detector_alarms[ip].extend(slow_sql_alarms[ip])

    # 对告警进行聚合，只选取同类的告警中发生时间最晚的告警
    final_alarms, all_self_security_alarms = filter_latest_alarms(
        detector_alarms, ip_list
    )
    # 将多个自安全异常场景的引发的多个告警整合成一个, 并添加到final_alarms中
    integrate_self_security_alarms(final_alarms, ip_list, all_self_security_alarms)

    # 如果需要mock告警，mock_list的值改为list(DBMIND_DETECTIONS.keys())即可
    mock_list = []
    for ip in ip_list:
        final_alarms[ip] = supply_inspection(final_alarms.get(ip), mock_list)
    return final_alarms


def generate_alarm_output(final_alarms, instance_list):
    """
    generate the formatted output

    :param final_alarms: all alarms
    :param instance_list: output the alarms which occurred in the given instance
    :return: list
    """
    abnormal_instances = []
    normal_instances = []
    for instance in instance_list:
        target_alarms = final_alarms.get(instance)
        if not isinstance(target_alarms, dict):
            continue

        alarms = []
        has_alert = False
        for inspection_key, alarm in target_alarms.items():
            if not alarm.get("detail"):
                alarms.append(formatter_empty_alarm(alarm.get("content"), 1))
            else:
                has_alert = True
                alarms.append(
                    formatter_alarm(
                        metric=DBMIND_DETECTIONS.get(inspection_key).get("name"),
                        status=0,
                        content=alarm.get("content"),
                        event=json.dumps(alarm.get("detail")),
                        instance=instance
                    )
                )

        if alarms and has_alert:
            # record the instance with the alarm
            abnormal_instances.append(formatter_list(instance, alarms))
        elif alarms:
            # record the instance with no alarms
            normal_instances.append(formatter_list(instance, alarms))

    if abnormal_instances:
        target = [
            formatter_str({
                "zh": f"巡检感知到异常节点{len(abnormal_instances)}个， 详细信息如下: ",
                "en": f"The patrol inspection has detected {len(abnormal_instances)} "
                      f"abnormal nodes, with the following details: "
            }),
            formatter_filter(
                "name",
                {"zh": "请输入要查询的ip. ",
                 "en": f"Please enter the ip. "},
                abnormal_instances
            )
        ]
        target.extend(abnormal_instances)
    else:
        target = [formatter_str({
            "zh": f"可巡检{len(instance_list)}个节点，未发现异常节点. ",
            "en": f"A total of {len(instance_list)} nodes were monitored "
                  f"in this inspection, and no abnormal nodes were found. "
        })]

    return target


def generate_abnormal_alarm_output(final_alarms, instance):
    """
    generate the formatted output

    :param final_alarms: all alarms
    :param instance: output the alarms which occurred in the given instance
    :return: list
    """
    target_alarms = final_alarms.get(instance)

    alarms = []
    for inspection_key, alarm in target_alarms.items():
        if not alarm.get("detail"):
            continue
        alarms.append(
            formatter_alarm(
                metric=DBMIND_DETECTIONS.get(inspection_key).get("name"),
                status=0,
                content=alarm.get("content"),
                event=json.dumps(alarm.get("detail")),
                instance=instance
            )
        )

    return alarms


def filter_latest_alarms(all_alarms, ip_list):
    """
    filter out the latest alarms
    :param all_alarms: detector alarms and cluster_diagnosis summary
    :param ip_list: current ip list
    :return: filtered all_alarms, filtered self_security_alarms
    """
    final_alarms = {}
    all_self_security_alarms = {}
    for ip in ip_list:
        filtered_alarms = {}
        self_security_alarms = {}
        for alarm in all_alarms.get(ip):
            inspection_key = METRIC_2_INSPECTION_MAP.get(alarm.get("metric_name"))
            if inspection_key is None:
                continue
            if inspection_key == "self_security_exception":
                security_metric_name = alarm.get("metric_name")
                # 自安全异常场景，过滤出告警时间最晚的异常场景
                if (
                        security_metric_name not in self_security_alarms or
                        (
                                security_metric_name in self_security_alarms and
                                alarm.get("from_timestamp") >
                                self_security_alarms.get(security_metric_name).get("from_timestamp")
                        )
                ):
                    self_security_alarms[security_metric_name] = alarm

                continue

            if (
                    inspection_key not in filtered_alarms or
                    (
                            inspection_key in filtered_alarms and
                            alarm.get("from_timestamp") >
                            filtered_alarms.get(inspection_key).get("from_timestamp")
                    )
            ):
                filtered_alarms[inspection_key] = alarm

        final_alarms[ip] = filtered_alarms
        all_self_security_alarms[ip] = self_security_alarms

    return final_alarms, all_self_security_alarms


def integrate_self_security_alarms(final_alarms, ip_list, all_self_security_alarms):
    """
    merge scanning_attack, brute_force_login_attack, user_violation_attack to one alarm
    """
    for ip in ip_list:
        self_security_alarms = all_self_security_alarms.get(ip, None)
        if self_security_alarms:
            # 指标名用逗号连接
            integrated_metric_name = ",".join(list(self_security_alarms.keys()))
            content = integrated_metric_name
            from_timestamp = min([
                item.get("from_timestamp")
                for item in list(self_security_alarms.values())
            ])

            # 告警详情中的alarm_content用|连接
            alarm_content = "|".join([
                item.get("detail").get("alarm_content")
                for item in list(self_security_alarms.values())
            ])
            temp_alarm_detail = copy(list(self_security_alarms.values())[0].get("detail"))
            temp_alarm_detail["alarm_content"] = alarm_content
            temp_alarm_detail["metric_name"] = integrated_metric_name
            temp_alarm_detail['instance'] = ip
            integrated_alarm = {
                "metric_name": integrated_metric_name,
                "from_timestamp": from_timestamp,
                "content": (
                        f"{temp_alarm_detail.get('start_time')}到"
                        f"{temp_alarm_detail.get('end_time')}"
                        f"期间出现如下异常：" + content
                ),
                "detail": temp_alarm_detail
            }
            final_alarms.get(ip)["self_security_exception"] = integrated_alarm


def metric_diagnosis_raw(metric_name: str, start_time: int, end_time: int,
                         metric_filter: str = None, alarm_cause: list = None,
                         reason_name: list = None):
    """call the metric_diagnosis tool and return raw result"""
    if not metric_filter:
        source_flag = get_data_source_flag(metric_name)
        if metric_name.startswith("os_") or metric_name in NODE_LEVEL_METRIC:
            instance = split_ip_port(current_instance.get())[0]
        else:
            instance = current_instance.get()
        metric_filter = json.dumps({source_flag: f"{instance}"})

    params = {
        "metric_name": metric_name,
        "metric_filter": metric_filter,
        "start": start_time,
        "end": end_time
    }
    if alarm_cause and reason_name:
        raise ValueError('The parameter alarm_cause and reason_name are exclusive')

    if alarm_cause:
        params['alarm_cause'] = json.dumps(alarm_cause)

    if reason_name:
        params['reason_name'] = json.dumps(reason_name)

    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"),
                  "app/metric-diagnosis-detail")
    response = dbmind_request("get", url, params=params)
    if response.status_code != 200 or "data" not in response.json():
        return False, []

    result_tuple = response.json().get("data")
    if result_tuple:
        return result_tuple[1] == "Unknown", result_tuple

    return False, []


def generate_metric_diagnosis_output(result, alarm_cause):
    """generate metric_diagnosis report according the raw result"""
    target = []
    features = result[0]
    conclusions = result[1].split(",")
    advice_list = result[2].split(" Or ")
    normal_seqs = result[3]
    abnormal_seqs = result[4]

    for i, item in enumerate(features):
        is_normal = features[item] == 0
        target.append(formatter_title({
            "zh": f"{chr(i + 97)}. 排查风险项 '{REASON_TRANSLATION.get(item, item)}'，状态: "
                  f"{'正常' if is_normal else '异常'}. ",
            "en": f"{chr(i + 97)}. check risk item '{REASON_TRANSLATION.get(item, item)}': "
                  f"{'Normal' if is_normal else 'Abnormal'}. "
        }, level=4, color='black' if is_normal else 'red'))
        if item in conclusions:
            advice = advice_list[conclusions.index(item)]
            target.append(formatter_str({
                "zh": f"建议：{advice}. ",
                "en": f"Advice: {advice}. "
            }))
        for seq in abnormal_seqs.get(item, [])[:10]:
            target.append(formatter_graph(
                seq.get("timestamps"),
                seq.get("values"),
                title=generate_title(seq.get("name"), seq.get("labels")),
                color='red'
            ))
        for seq in normal_seqs.get(item, [])[:10]:
            target.append(formatter_graph(
                seq.get("timestamps"),
                seq.get("values"),
                title=generate_title(seq.get("name"), seq.get("labels"))
            ))

    abnormal_features = [key for key, value in features.items() if value != 0]
    alarm_cause_alias = [REASON_TRANSLATION.get(item, item) for item in alarm_cause]
    conclusions_alias = [REASON_TRANSLATION.get(item, item) for item in abnormal_features]
    if conclusions[0] == "Unknown":
        target.append(formatter_str({
            "zh": f"经诊断，与{', '.join(alarm_cause_alias)}相关的各项风险项都正常. ",
            "en": f"All indicators related to {', '.join(alarm_cause_alias)} "
                  f"are normal after diagnosis. "
        }))
        return target

    target.append(formatter_str({
        "zh": f"经诊断, {', '.join(alarm_cause_alias)}"
              f"可能是由{', '.join(conclusions_alias)}导致的. ",
        "en": f"It is diagnosed that {', '.join(alarm_cause)} "
              f"may be caused by {', '.join(abnormal_features)}. "
    }, color='red'))
    return target


@base_tools(
    name="metric_diagnosis",
    description="metric_diagnosis工具的功能是对告警的指标进行根因的分析诊断，"
                "metric_diagnosis工具有4个必要参数metric_name，alarm_cause，start_time，end_time,1个非必要参数metric_filter。",
    params=[
        Param(name="metric_name", description="必要参数，指标名", param_type="str"),
        Param(name="alarm_cause", description="必要参数，告警原因", param_type="str"),
        Param(name="start_time", description="必要参数，开始时间，格式为%Y-%m-%d %H:%M:%S", param_type="str"),
        Param(name="end_time", description="必要参数，结束时间，格式为%Y-%m-%d %H:%M:%S", param_type="str"),
        Param(name="metric_filter", description="非必要参数，指标过滤条件，要求格式为：key1=value1,key2=value2",
              param_type="str", required=False)
    ]
)
@validate_return_format
def metric_diagnosis(metric_name: str, alarm_cause: Union[list, str],
                     start_time: str, end_time: str,
                     metric_filter: str = None):
    """agent_tool: metric_diagnosis"""
    start_timestamp, end_timestamp = transfer_date_2_timestamp(start_time, end_time)
    if isinstance(alarm_cause, str):
        alarm_cause = [alarm_cause]
    if metric_name not in METRIC_DIAGNOSIS_REASON_MAP:
        return [
            formatter_str({
                "zh": f"指标名暂不支持，请从{','.join(list(METRIC_DIAGNOSIS_REASON_MAP.keys()))}中选择",
                "en": f"The metric inputted is not supported, please select from below list: "
                      f"{','.join(list(METRIC_DIAGNOSIS_REASON_MAP.keys()))}"
            })
        ]
    target_alarm_cause_list = METRIC_DIAGNOSIS_REASON_MAP.get(metric_name, [])
    for cause in alarm_cause:
        if cause not in target_alarm_cause_list:
            return [
                formatter_str({
                    "zh": f"告警项暂不支持，请从{','.join(list(target_alarm_cause_list))}中选择",
                    "en": f"The alarm_cause inputted is not supported, please select from below list: "
                          f"{','.join(target_alarm_cause_list)}"
                })
            ]
    _, raw_result = metric_diagnosis_raw(metric_name, start_timestamp, end_timestamp, metric_filter, alarm_cause)
    if raw_result:
        return generate_metric_diagnosis_output(raw_result, alarm_cause)
    return [formatter_str({
        "zh": f"在{start_time}到{end_time}期间未诊断出任何异常. ",
        "en": f"Nothing unusual was found between {start_time} and {end_time}. "
    })]


def metric_diagnosis_insight(metric_name: str, alarm_cause: list,
                             start_time: int, end_time: int,
                             metric_filter: str):
    """metric_diagnosis_insight tool"""
    if not metric_filter:
        source_flag = get_data_source_flag(metric_name)
        if metric_name.startswith("os_") or metric_name in NODE_LEVEL_METRIC:
            instance = split_ip_port(current_instance.get())[0]
        else:
            instance = current_instance.get()
        metric_filter = json.dumps({source_flag: f"{instance}"})

    params = {
        "metric_name": metric_name,
        "metric_filter": metric_filter,
        "alarm_cause": json.dumps(alarm_cause),
        "start": start_time,
        "end": end_time
    }
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"),
                  "app/metric-diagnosis-insight")
    response = dbmind_request("get", url, params=params)
    if response.status_code == 200 and "data" in response.json():
        return response.json().get("data")

    return {}


@base_tools(
    name="data_directory",
    description="data_directory工具的功能是查看数据库某数据目录的状态，"
                "data_directory工具有1个必要参数instance。",
    params=[
        Param(name="instance", description="必要参数，需要查询的数据库的数据节点的ip和port")
    ]
)
def data_directory(instance: str):
    """
    get the status of database data directory
    """
    params = {
        "instance": instance
    }
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "status/data-directory")
    response = dbmind_request("get", url, params=params)
    target = []
    if response.status_code == 200 and "data" in response.json():
        data = response.json().get("data")
        headers = ["free_space", "total_space", "usage_rate", "used_space"]
        headers_unit = ["free_space/GB", "total_space/GB", "usage_rate/%", "used_space/GB"]
        rows = [[data.get(item, -1) for item in headers]]
        target.append(formatter_str({
            "zh": "数据目录情况如下所示: ",
            "en": "The status of data directory is as follows. "
        }))
        target.append(formatter_table(headers_unit, rows))
        return target

    target.append(formatter_str({
        "zh": "查询数据库目录情况失败. ",
        "en": "Failed to get the status of data directory. "
    }))

    return target


@base_tools(
    name="knob_recommendation_details",
    description="knob_recommendation_details工具的功能是获取参数推荐功能的结果列表及详情，"
                "knob_recommendation_details工具没有参数，不需要进行参数提取。",
)
def knob_recommendation_details():
    """
    get the detail of knob recommendation
    """
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"),
                  "summary/knob-recommendation/details")
    response = dbmind_request("get", url, params={"pagesize": 1000})
    target = []
    if response.status_code == 200 and "data" in response.json():
        data = response.json().get("data")
        target.append(formatter_table(data.get("header"), data.get("rows")))
        return target
    target.append(formatter_str({
        "zh": "获取参数推荐列表失败. ",
        "en": "Failed to get the detail of knob recommendation. "
    }))

    return target


@base_tools(
    name="knob_recommendation_xtuner",
    description="knob_recommendation_xtuner工具的功能是通过xtuner执行参数推荐，并将结果保存到数据库中，"
                "knob_recommendation_xtuner工具没有参数，不需要进行参数提取。",
)
@validate_return_format
def knob_recommendation_xtuner():
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"),
                  "app/knob-recommendation/xtuner")
    params = {
        "instance": current_instance.get(),
    }
    response = dbmind_request("post", url, params=params)
    target = []
    if response.status_code == 200 and "data" in response.json():
        data = response.json().get("data")
        target.append(formatter_str({
            "zh": "参数推荐执行成功，结果如下：",
            "en": "Knob recommendation executed successfully, results are as follows:"
        }))
        target.append(formatter_table(data.get("header"), data.get("rows")))
        return target
    error_msg = ""
    if response.status_code == 200 and "msg" in response.json():
        error_msg = response.json().get("msg", "")
    target.append(formatter_str({
        "zh": f"参数推荐执行失败. {error_msg}",
        "en": f"Failed to execute knob recommendation. {error_msg}"
    }))

    return target


@base_tools(
    name="knob_recommendation_snapshots",
    description="knob_recommendation_snapshots工具的功能是获取参数推荐的快照，"
                "knob_recommendation_snapshots工具没有参数，不需要进行参数提取。"
)
def knob_recommendation_snapshots():
    """
    get the detail of knob recommendation
    """
    params = {"pagesize": 1000}
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"),
                  "summary/knob-recommendation/snapshots")
    response = dbmind_request("get", url, params=params)
    target = []
    if response.status_code == 200 and "data" in response.json():
        data = response.json().get("data")
        target.append(formatter_table(data.get("header"), data.get("rows")))
        return target

    target.append(formatter_str({
        "zh": "获取指标快照失败. ",
        "en": "Failed to get the snapshots of knob. "
    }))

    return target


@base_tools(
    name="memory_check",
    description="memory_check工具的功能是获取数据库进程的内存使用的细节，找出其中的异常并进行分析"
                "memory_check工具有1个非必要参数latest_hours。",
    params=[
        Param(name="latest_hours",
              description="非必要参数，对内存进行趋势预测的预测时长，默认值为4小时",
              param_type="int",
              required=False)
    ]
)
def memory_check(latest_hours: int = None):
    """
    get the detail of memory
    """
    params = {"latest_hours": latest_hours} if latest_hours else None
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "toolkit/memory_check")
    response = dbmind_request("get", url, params=params)
    target = []

    def get_memory_details(name, full_data):
        detail = full_data.get(name).get("data")
        if detail.get("values"):
            return formatter_graph(
                detail.get("timestamps"),
                detail.get("values"),
                title=f"{name}: {full_data.get(name).get('status')}"
            )
        return {}

    if response.status_code == 200 and "data" in response.json():
        data = response.json().get("data")

        memory_list = [
            "dynamic_used_memory_continuous_increase",
            "dynamic_used_shrctx_continuous_increase",
            "os_mem_usage_continuous_increase",
            "other_used_memory_continuous_increase",
            "process_used_memory_continuous_increase",
        ]
        for item in memory_list:
            memory_detail = get_memory_details(item, data)
            if memory_detail:
                target.append(memory_detail)

        return target

    target.append(formatter_str({
        "zh": "获取内存占用情况失败. ",
        "en": "Failed to get the detail of memory usage. "
    }))

    return target


@base_tools(
    name="status_overview",
    description="status_overview工具的功能是获取数据库总体状态和的概况等信息"
                "status_overview工具没有参数，不需要进行参数提取。",
)
def status_overview():
    """status overview"""
    url = urljoin(global_vars.configs.get(DBMIND, "api_prefix"), "status/overview")
    response = dbmind_request("get", url)
    if response.status_code == 200 and "data" in response.json():
        data = response.json().get("data")
        headers = ["item", "value"]
        rows = [[key, value] for key, value in data.items()]
        return [formatter_table(headers, rows)]
    return [formatter_str({
        "zh": "获取概览信息失败. ",
        "en": "Failed to get the overview of database. "
    })]
