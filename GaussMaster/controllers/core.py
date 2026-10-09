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

import asyncio
import logging
from typing import List, Optional

from fastapi import UploadFile
from pydantic import BaseModel

from GaussMaster.common.http import request_mapping
from GaussMaster.common.http import standardized_api_output
from GaussMaster.common.http._service_impl import standardized_event_stream_output
from GaussMaster.common.utils.checking import ParameterChecker
from GaussMaster import global_vars
from GaussMaster.multiagents.agents import dba
from GaussMaster.multiagents.mcp_integration import (
    get_mcp_server_status,
    reload_mcp_servers,
    serialize_server_status,
    ServerStatus,
)
from GaussMaster.multiagents.tools.dbmind_interface import retrieve_clusters_status
from GaussMaster.server.web import data_transformer
from GaussMaster.server.web.context_manager import switch_llm_context_decorator, current_llm
from GaussMaster.server.web.data_transformer import get_history_report, diagnostic_replay, get_all_models, switch_llm, \
    update_session_cluster

latest_version = 'v1'
api_prefix = '/%s/api' % latest_version


class Cluster(BaseModel):
    cluster_name: str
    host: str
    port: int
    username: str
    password: str


class PlanModel(BaseModel):
    query: str
    user_id: str
    session_id: str
    mode: str
    history_len: int = 1


@request_mapping(api_prefix + "/app/intelligent-interaction", method='POST', api=True)
@standardized_event_stream_output
@ParameterChecker.define_rules(
    query=ParameterChecker.QUERY_MODEL
)
@switch_llm_context_decorator
def intelligent_interaction_chat(query: PlanModel):
    plan_model = dict(query)
    plan_model.update({'model_name': current_llm.get()})
    return dba.interact(**plan_model)


@request_mapping(api_prefix + "/clusters", method='GET', api=True)
@standardized_api_output
def get_clusters():
    return retrieve_clusters_status()


@request_mapping(api_prefix + "/clusters", method='PUT', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    user_id=ParameterChecker.NAME,
    session_id=ParameterChecker.NAME,
    instance=ParameterChecker.IP_WITH_PORT
)
def put_clusters(user_id, session_id, instance):
    return update_session_cluster(user_id, session_id, instance)


@request_mapping(api_prefix + "/clusters/register", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    cluster=ParameterChecker.CLUSTER_MODEL
)
def register_cluster(cluster: Cluster):
    return data_transformer.register_cluster(cluster)


def _mcp_status_response(results) -> dict:
    """Build the unified MCP status response structure."""
    servers = serialize_server_status(results)
    connected_count = sum(
        1 for info in results.values() if info.status is ServerStatus.CONNECTED
    )
    return {
        "servers": servers,
        "connected_count": connected_count,
        "total_count": len(servers),
    }


@request_mapping(api_prefix + "/mcp/status", method='GET', api=True)
@standardized_api_output
def get_mcp_status():
    """Query MCP server connection status (read-only; does not trigger connection operations).

    Returns:
        dict: Status and statistics of each MCP server (connected_count / total_count)
    """
    return _mcp_status_response(get_mcp_server_status())


@request_mapping(api_prefix + "/mcp/reload", method='PUT', api=True)
@standardized_api_output
async def reload_mcp():
    """Reload the MCP configuration: disconnect existing connections and reconnect per mcp_servers.json.

    Returns:
        dict: Status of each MCP server after reconnection
    """
    return _mcp_status_response(await reload_mcp_servers(global_vars.confpath))


@request_mapping(api_prefix + "/summary/report/list", method='GET', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    instances=ParameterChecker.INSTANCE_LIST,
    current=ParameterChecker.PINT32_OPTIONAL,
    pagesize=ParameterChecker.PINT32_OPTIONAL,
    user_id=ParameterChecker.NAME,
    from_timestamp=ParameterChecker.TIMESTAMP,
    to_timestamp=ParameterChecker.TIMESTAMP,
    tz=ParameterChecker.TIMEZONE,
)
async def diagnose_list(instances: str, current: int = 1, pagesize: int = 20, user_id: str = None,
                        from_timestamp: int = None, to_timestamp: int = None, tz: str = None):
    kwargs = {
        'from_timestamp': from_timestamp,
        'to_timestamp': to_timestamp,
        'instances': instances,
        'tz': tz
    }
    return await get_history_report(current, pagesize, user_id, **kwargs)


@request_mapping(api_prefix + "/summary/report/content", method='GET', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    report_id=ParameterChecker.STRING
)
async def diagnose_report(report_id):
    return await diagnostic_replay(report_id)


@request_mapping(api_prefix + "/llms", method='GET', api=True)
@standardized_api_output
def get_llms():
    """
    Retrieve the list of available large language models (LLMs).

    Returns:
    list: A list of available LLM identifiers.
    """
    return get_all_models()


@request_mapping(api_prefix + "/llms", method='PUT', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    name=ParameterChecker.STRING,
    user_id=ParameterChecker.NAME,
    session_id=ParameterChecker.NAME
)
def set_user_session_llm(name, user_id, session_id):
    """
    Set the large language model (LLM) for the current user and session.

    Parameters:
    user_id (str): The unique identifier for the user.
    session_id (str): The unique identifier for the session.
    name (str): The name of the large language model to be set for the user session.

    Returns:
    bool: True if the LLM was set successfully, False otherwise.
    """
    return switch_llm(name, user_id, session_id)


class SearchParams(BaseModel):
    question: str
    user_id: str
    session_id: str
    switch: bool = True
    vector_topk: int = 6
    text_topk: int = 6
    rerank_topk: int = 3
    kb_id: int = 0
    version: str = None
    model_name: str = "pangu_cloud_sigma_unify_plugin_38b"
    lang: str = "zh"
    history_len: int = 1
    model_config: dict = {}
    search_res: List[Optional[dict]] = []
    question_id: str = None


@request_mapping(api_prefix + "/search", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(search_params=ParameterChecker.SEARCH_PARAM)
async def search(search_params: SearchParams = None):
    """Function search."""
    if not search_params:
        raise Exception(f'The search_params is None.')
    response = await data_transformer.search(search_params.question, search_params.user_id, search_params.session_id,
                                             search_params.vector_topk, search_params.text_topk,
                                             search_params.rerank_topk,
                                             search_params.kb_id, search_params.version, search_params.lang,
                                             search_params.history_len)
    return response


@request_mapping(api_prefix + "/infer", method='POST', api=True)
@standardized_event_stream_output
@ParameterChecker.define_rules(search_params=ParameterChecker.SEARCH_PARAM)
def infer(search_params: SearchParams = None):
    """Function infer."""
    if not search_params:
        raise Exception(f'The search_params is None.')
    return data_transformer.infer(search_params.question, search_params.question_id, search_params.user_id,
                                  search_params.session_id, search_params.switch, search_params.model_name,
                                  search_params.lang, search_params.history_len, search_params.model_config,
                                  search_params.search_res)


@request_mapping(api_prefix + "/ask_gauss", method='POST', api=True)
@standardized_event_stream_output
@ParameterChecker.define_rules(search_params=ParameterChecker.SEARCH_PARAM)
def ask_gauss(search_params: SearchParams = None):
    """Function ask gauss."""
    if not search_params:
        raise Exception(f'The search_params is None.')

    return data_transformer.ask_gauss(search_params.question, search_params.user_id, search_params.session_id,
                                      search_params.switch, search_params.vector_topk, search_params.text_topk,
                                      search_params.rerank_topk, search_params.kb_id, search_params.version,
                                      search_params.model_name, search_params.lang, search_params.history_len,
                                      search_params.model_config)


class KLUpdateParams(BaseModel):
    kb_id: int
    name: str
    user_id: str
    description: str = ""
    context: str = ""


@request_mapping(api_prefix + "/serve/knowledge_base/add", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    name=ParameterChecker.STRING,
    user_id=ParameterChecker.NAME,
    kb_type=ParameterChecker.STRING,
    description=ParameterChecker.NSTRING,
    context=ParameterChecker.NSTRING
)
async def add_knowledge_base(name: str, user_id: str, file: List[UploadFile], kb_type: str = 'QA',
                             description: str = "", context: str = ""):
    """Function add knowledge base."""
    response = await data_transformer.add_knowledge(name, user_id, file, kb_type, description, context)
    return response


@request_mapping(api_prefix + "/serve/knowledge_base/update", method='PUT', api=True)
@standardized_api_output
@ParameterChecker.define_rules(kl_update_params=ParameterChecker.KL_UPDATE_PARAM)
def update_knowledge_base(kl_update_params: KLUpdateParams = None):
    """Function update knowledge base."""
    return data_transformer.update_knowledge(kl_update_params.kb_id, kl_update_params.name, kl_update_params.user_id,
                                             kl_update_params.description, kl_update_params.context)


@request_mapping(api_prefix + "/serve/knowledge_base/get", method='GET', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    kb_id=ParameterChecker.INT32,
    user_id=ParameterChecker.NAME
)
def get_knowledge_base(kb_id: int, user_id: str):
    """Function get knowledge base."""
    return data_transformer.get_knowledge_info(kb_id, user_id)


@request_mapping(api_prefix + "/serve/knowledge_base/delete", method='DELETE', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    kb_id=ParameterChecker.INT32,
    user_id=ParameterChecker.NAME
)
def delete_knowledge_base(kb_id: int, user_id: str):
    """Function delete knowledge base."""
    return data_transformer.delete_knowledge(kb_id, user_id)


@request_mapping(api_prefix + "/serve/knowledge_base/list", method='GET', api=True)
@standardized_api_output
@ParameterChecker.define_rules(user_id=ParameterChecker.NAME)
def list_knowledge_base(user_id: str):
    """Function listing knowledge base."""
    return data_transformer.list_knowledge(user_id)


@request_mapping(api_prefix + "/serve/knowledge_base/batch_delete", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(user_id=ParameterChecker.NAME)
def batch_delete_knowledge_base(kb_id_list: List[int], user_id: str):
    """Function batch delete knowledge base."""
    return data_transformer.batch_delete_knowledge(kb_id_list, user_id)


class DSUpdateParams(BaseModel):
    ds_id: int
    name: str
    related_kb_id: int
    description: str = ""


@request_mapping(api_prefix + "/serve/datasource/add", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    related_kb_id=ParameterChecker.INT32,
    name=ParameterChecker.NSTRING,
    description=ParameterChecker.NSTRING
)
async def add_datasource(related_kb_id: int, file: List[UploadFile], name: str = "", description: str = ""):
    """Function add datasource."""
    response = await data_transformer.add_datasource(related_kb_id, file, name, description)
    return response


@request_mapping(api_prefix + "/serve/datasource/update", method='PUT', api=True)
@standardized_api_output
@ParameterChecker.define_rules(ds_update_params=ParameterChecker.DS_UPDATE_PARAM)
def update_datasource(ds_update_params: DSUpdateParams = None):
    """Function update datasource."""
    return data_transformer.update_datasource(ds_update_params.ds_id, ds_update_params.name,
                                              ds_update_params.related_kb_id, ds_update_params.description)


@request_mapping(api_prefix + "/serve/datasource/get", method='GET', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    ds_id=ParameterChecker.INT32,
    related_kb_id=ParameterChecker.INT32
)
def get_datasource(ds_id: int = None, related_kb_id: int = None):
    """Function get datasource."""
    return data_transformer.get_datasource_info(ds_id, related_kb_id)


@request_mapping(api_prefix + "/serve/datasource/delete", method='DELETE', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    ds_id=ParameterChecker.INT32,
    related_kb_id=ParameterChecker.INT32
)
def delete_datasource(ds_id: int = None, related_kb_id: int = None):
    """Function delete datasource."""
    return data_transformer.delete_datasource(ds_id, related_kb_id)


@request_mapping(api_prefix + "/serve/datasource/list", method='GET', api=True)
@standardized_api_output
@ParameterChecker.define_rules(related_kb_id=ParameterChecker.INT32)
def list_datasource(related_kb_id: int):
    """Function listing datasource."""
    return data_transformer.list_datasource(related_kb_id)


@request_mapping(api_prefix + "/serve/datasource/batch_delete", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(related_kb_id=ParameterChecker.INT32)
def batch_delete_datasource(ds_id_list: List[int], related_kb_id: int):
    """Function batch delete datasource."""
    return data_transformer.batch_delete_datasource(ds_id_list, related_kb_id)


@request_mapping(api_prefix + "/feedback/like", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(answer_id=ParameterChecker.STRING)
def like(answer_id: str = None):
    """Function feedback like."""
    return data_transformer.update_like_info(answer_id)


@request_mapping(api_prefix + "/feedback/hate", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(answer_id=ParameterChecker.STRING)
def hate(answer_id: str = None):
    """Function feedback hate."""
    return data_transformer.update_hate_info(answer_id)


@request_mapping(api_prefix + "/feedback", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    answer_id=ParameterChecker.STRING,
    feedback_info=ParameterChecker.STRING
)
def feedback(answer_id: str = None, feedback_info: str = None):
    """Function feedback."""
    return data_transformer.update_feedback_info(answer_id, feedback_info)


@request_mapping(api_prefix + "/feedback/report", method='POST', api=True)
@standardized_api_output
@ParameterChecker.define_rules(
    answer_id=ParameterChecker.STRING,
    report_type=ParameterChecker.STRING,
    report_info=ParameterChecker.STRING
)
def report(answer_id: str = None, report_type: str = None, report_info: str = None):
    """Function feedback report."""
    return data_transformer.update_report_info(answer_id, report_type, report_info)
