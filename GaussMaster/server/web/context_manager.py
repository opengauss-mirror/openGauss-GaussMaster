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

import contextvars
import functools
import logging
import threading

from GaussMaster import global_vars

_access_context = threading.local()


class ACCESS_CONTEXT_NAME:
    CLUSTER_IP_WITH_PORT_LIST = 'cluster_ip_with_port_list'
    CLUSTER_IP_LIST = 'cluster_ip_list'
    INSTANCE_IP_WITH_PORT = 'instance_ip_with_port'
    INSTANCE_IP = 'instance_ip'


def set_access_context(**kwargs):
    """
    Since the web front-end login user can specify
    a cope, we should also pay attention
    to this context when returning data to the user.
    Through this function, set the effective visible field.
    """
    for k, v in kwargs.items():
        setattr(_access_context, k, v)


def get_access_context(name):
    return getattr(_access_context, name, None)


current_instance = contextvars.ContextVar("current_instance")
current_llm = contextvars.ContextVar('current_llm')


def set_current_instance(user_id, session_id):
    """
    Function to set the current instance for a user's session.

    The instance must be bound via the (user_id, session_id) pair. Session-level
    isolation is enforced: a session that has not been explicitly bound will not
    silently fall back to the user-level instance.

    Parameters:
    user_id (str): The unique identifier for the user.
    session_id (str): The unique identifier for the session.

    Raises:
    ValueError: If no instance has been bound for the given (user_id, session_id).
    """
    instance = global_vars.user_session_instance[user_id].get(session_id)
    if not instance:
        raise ValueError(f'You must set the instance for user_id: {user_id} '
                         f'and session_id: {session_id} first.')
    current_instance.set(instance)


def set_user_current_instance(user_id, instance):
    """
    Set the instance for the user.
    """
    global_vars.user_session_instance[user_id]['current_instance'] = instance


def get_user_current_instance(user_id):
    """
    Get the instance for the user.
    """
    return global_vars.user_session_instance[user_id]['current_instance']


def switch_to_user_session_llm_context(user_id, session_id):
    """
    Switch to the LLM context for the specified user and session.

    Parameters:
    user_id (str): The unique identifier for the user.
    session_id (str): The unique identifier for the session.
    """
    if not global_vars.user_session_llm[user_id]:
        default_llm = global_vars.user_session_llm['default_userid']['default_sessionid']
        logging.info('The current user does not specify a llm, and the default llm %s will be used.', default_llm)
        global_vars.user_session_llm[user_id][session_id] = default_llm
        set_user_current_llm(user_id, default_llm)
    elif not global_vars.user_session_llm[user_id].get(session_id):
        last_llm = global_vars.user_session_llm[user_id]['current_llm']
        logging.info('There is no bound llm in the current session %s, and the last used model %s is used by default.',
                     session_id, last_llm)
        global_vars.user_session_llm[user_id][session_id] = last_llm
    llm = global_vars.user_session_llm[user_id][session_id]
    current_llm.set(llm)


def set_default_llm(model_name):
    """
    Set the global default large language model (LLM).

    Parameters:
    llm (str): The large language model to be set as the global default.
    """
    global_vars.user_session_llm['default_userid']['default_sessionid'] = model_name


def get_default_llm():
    """
    Retrieve the default large language model (LLM).

    Returns:
    str: The default LLM.
    """
    return global_vars.user_session_llm['default_userid']['default_sessionid']


def set_user_current_llm(user_id, model_name):
    """
    Set the global large language model (LLM) for the user.
    """
    global_vars.user_session_llm[user_id]['current_llm'] = model_name


def get_user_current_llm(user_id):
    """
    Get the global large language model (LLM) for the user.
    """
    return global_vars.user_session_llm[user_id]['current_llm']


def switch_llm_context_decorator(func):
    """
    A decorator to switch to the LLM context for a specific user and session
    before executing the decorated function.

    This decorator reads `user_id` and `session_id` from the function's
    parameters and calls `switch_to_user_session_llm_context` to set the
    appropriate LLM context.

    Parameters:
    func (callable): The function to be decorated.

    Returns:
    callable: The wrapped function with LLM context switching.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # for planmodel
        kwargs_copy = kwargs.copy()
        for param in kwargs.values():
            if hasattr(param, 'user_id') and hasattr(param, 'session_id'):
                kwargs_copy.update({'user_id': getattr(param, 'user_id'),
                                    'session_id': getattr(param, 'session_id')})

        if 'user_id' in kwargs_copy and 'session_id' in kwargs_copy:
            try:
                set_current_instance(kwargs_copy['user_id'], kwargs_copy['session_id'])
            except ValueError:
                return f'You must set the instance for user_id: {kwargs_copy["user_id"]} ' \
                       f'and session_id: {kwargs_copy["session_id"]} first.'
            switch_to_user_session_llm_context(kwargs_copy['user_id'], kwargs_copy['session_id'])
            return func(*args, **kwargs)

    return wrapper
