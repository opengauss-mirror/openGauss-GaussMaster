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

"""llm utils and constants"""
from functools import wraps


class LLMType:
    """llm type"""
    PANGU = 'pangu'
    PANGUCLOUD = 'pangu_cloud'
    CHATGLM = 'chatglm'
    BAICHUAN = 'baichuan'
    LLAMA3 = 'llama3'
    OPENAI_COMPATIBLE = 'openai_compatible'


class InteractionType:
    """interaction type"""
    TOOL_INTERACTION = 'tool_interaction'
    FAULT_DIAGNOSTIC = 'fault_diagnostic'
    INTELLIGENT_INSPECTION = 'intelligent_inspection'


class LLMRole:
    """llm role definition"""
    ASSISTANT = "assistant"
    FUNCTION = "function"
    SYSTEM = "system"
    USER = "user"

    ALLOWED_LIST = [ASSISTANT, FUNCTION, SYSTEM, USER]


class LLMMsgKey:
    """llm message key definition"""
    ROLE = "role"
    CONTENT = "content"
    FUNCTION_CALL = 'function'

    ALLOWED_LIST = [ROLE, CONTENT, FUNCTION_CALL]


WEEK_ZH = "一二三四五六日"
WEEK_CN = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def check_message(f):
    """ check whether the message format is correct"""

    @wraps(f)
    def decorate(*args, **kwargs):
        if len(args) > 1:
            messages = args[1]
        elif 'prompt' in kwargs:
            messages = kwargs.get('prompt')
        else:
            raise ValueError(f"The prompt parameter is not provided.")

        if not isinstance(messages, (list, str)):
            raise TypeError(f"typeError: excepted 'list' or 'str', but got '{type(messages).__name__}'.")
        for message in messages:
            for key, content in message.items():
                if key not in LLMMsgKey.ALLOWED_LIST:
                    raise ValueError(f"The key of message should in {LLMMsgKey.ALLOWED_LIST} but got '{key}'.")
                if key == LLMMsgKey.ROLE and content not in LLMRole.ALLOWED_LIST:
                    raise ValueError(f"The role of message should in {LLMRole.ALLOWED_LIST} but got '{content}'.")
        return f(*args, **kwargs)

    return decorate
