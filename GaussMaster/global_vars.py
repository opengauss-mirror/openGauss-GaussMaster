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

from collections import defaultdict

LANGUAGE = 'zh'
confpath = ''
cluster_proxy = None
configs: "ReadonlyConfig" = None
embedding_model = None
reranker_model = None
MEMORY = None
llm_config = {}
local_llm = None
# the qa history of current session
SESSION_QA_HISTORY = {}
# the intention of current session
SESSION_TOOL_HISTORY = defaultdict(dict)
user_session_instance = defaultdict(dict)
user_session_llm = defaultdict(dict)

# the dfa safety detector
DFA_DETECTOR = None
