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

__description__ = 'GaussMaster: Database field Q&A and LLMOps platform'
__version__ = '1.0.0'

import os

PORT_SUFFIX = "(:[0-9]{4,5}|)"

# confpath
GAUSSMASTER_PATH = os.path.dirname(os.path.realpath(__file__))
MISC_PATH = os.path.join(GAUSSMASTER_PATH, 'misc')

CONFILE_NAME = 'gaussmaster.conf'  # the name of configuration file
LLM_CONFIG_FILE_NAME = 'model_config.yaml'
MCP_CONFIG_FILE_NAME = 'mcp_servers.json'
DYNAMIC_CONFIG = 'dynamic_config.db'
ENCRYPTION_PART_B = 'encryption_part_b.bin'

KNOWLEDGE_BASE_PATH = 'knowledge_base'
DB_FILE_ZH = 'gauss_zh.db'
DB_FILE_EN = 'gauss_en.db'

LOGFILE_NAME = 'gaussmaster.log'
PIDFILE_NAME = 'gaussmaster.pid'

DBMIND = 'DBMIND'
SECTION_LOG = "LOG"
SECTION_VECTOR = "VECTOR"
SECTION_WEB_SERVICE = "WEB-SERVICE"
SSL = False

DISTINGUISHING_INSTANCE_LABEL = 'from_instance'
EXPORTER_INSTANCE_LABEL = 'instance'

LANGUAGES_SUPPORT_LIST = ["zh", "en"]
VERSION_SUPPORT_LIST = ["", "24.7.30.10", None]
SUFFIX_SUPPORT_LIST = ["md", "markdown", "docx"]
REPORT_SUPPORT_LIST = ["低俗色情", "账号违规", "敏感信息", "暴力恐怖", "造谣诽谤", "侮辱英烈", "谩骂攻击", "民族宗教",
                       "赌博诈骗", "危害未成年", "违法违禁品", "其他"]
KB_TYPE_SUPPORT_LIST = ["QA", "OM"]
CUSTOM_KB_PREFIX = "custom_knowledge_"
MAX_PROMPT_LENGTH = 4096 * 2
FILE_MAX_SIZE = 128 * 1024 * 1024

SECTION_SAFETY = "SAFETY"
STOP_WORDS = [" ", "&", "!", "！", "@", "#", "$", "￥", "*", "^", "%", "?", "？", "<", ">", "《", "》"]
SENSITIVE_WORD_PATH = os.path.join(GAUSSMASTER_PATH, 'common', 'safety', 'data')
