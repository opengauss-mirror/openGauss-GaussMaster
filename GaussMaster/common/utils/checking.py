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

import argparse
import datetime
import inspect
import json
import os
import re
import socket
import subprocess
from functools import wraps
from json import JSONDecodeError

from GaussMaster import constants, global_vars
from GaussMaster.common.utils import write_to_terminal
from GaussMaster.common.utils.base import adjust_timezone
from GaussMaster.constants import PORT_SUFFIX
from .base import ignore_exc
from ...llms.llm_utils import InteractionType

V4_EXP = r"(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}"
IPV4_PATTERN = re.compile(rf"^{V4_EXP}$")
V6_SEG = r"[0-9A-Fa-f]{1,4}"
V6_EXP = (
    rf"((({V6_SEG}:){{7}}({V6_SEG}|:))|"
    rf"(({V6_SEG}:){{6}}(((:{V6_SEG}){{1,1}})|({V4_EXP})|:))|"
    rf"(({V6_SEG}:){{5}}(((:{V6_SEG}){{1,2}})|:({V4_EXP})|:))|"
    rf"(({V6_SEG}:){{4}}(((:{V6_SEG}){{1,3}})|((:{V6_SEG}){{0,1}}:({V4_EXP}))|:))|"
    rf"(({V6_SEG}:){{3}}(((:{V6_SEG}){{1,4}})|((:{V6_SEG}){{0,2}}:({V4_EXP}))|:))|"
    rf"(({V6_SEG}:){{2}}(((:{V6_SEG}){{1,5}})|((:{V6_SEG}){{0,3}}:({V4_EXP}))|:))|"
    rf"(({V6_SEG}:){{1}}(((:{V6_SEG}){{1,6}})|((:{V6_SEG}){{0,4}}:({V4_EXP}))|:))|"
    rf"(:({V6_SEG}:){{0}}(((:{V6_SEG}){{1,7}})|((:{V6_SEG}){{0,5}}:({V4_EXP}))|:)))"
)
IPV6_PATTERN = re.compile(rf"(^\[{V6_EXP}(%.+)?]$)|(^{V6_EXP}(%.+)?$)")
BARE_IPV6_PATTERN = re.compile(rf"^{V6_EXP}(%.+)?$")
PORT_EXP = r"(102[4-9]|10[3-9]\d|1[1-9]\d{2}|[2-9]\d{3}|[1-5]\d{4}|6[0-4]\d{3}|65[0-4]\d{2}|655[0-2]\d|6553[0-5])"
INSTANCE_PATTERN = re.compile(rf"(^{V4_EXP}(:{PORT_EXP}|)$)|(^\[{V6_EXP}(%.+)?]:{PORT_EXP}$)|(^{V6_EXP}(%.+)?$)")
WITH_PORT = re.compile(rf"^{V4_EXP}:{PORT_EXP}$|^\[{V6_EXP}(%.+)?]:{PORT_EXP}$")
LOCAL_INSTANCE_PATTERN = re.compile(rf"(^0\.0\.0\.0:{PORT_EXP}$)|(^\[::(%.+)?]:{PORT_EXP}$)")
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{2,120}$")
MAX_STRING_LENGTH = 10240
# timestamp which has 13 digits
TIMESTAMPS_PATTERN = re.compile(r"^\d{13}$")
TIMEZONE_PATTERN = re.compile(r"UTC[-+]\d{1,2}(:\d{1,2})?$")
ILL_CHARACTER = [" ", "|", ";", "&", "$", "<", ">", "`", "\\", "'", "\"",
                 "{", "}", "(", ")", "[", "]", "~", "*", "?", "!", "\n"]


def uniform_ip(ip):
    """uniform ip"""

    ip = ip.replace("'", '').replace('"', '').strip()
    if not ip:
        return ip

    return socket.getaddrinfo(ip, port=None)[0][4][0]


def is_port_used(host, port):
    """check port is available"""

    if IPV6_PATTERN.match(host):
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    uniformed_ip = uniform_ip(host.strip())
    if uniformed_ip in ['0.0.0.0', '::']:
        try:
            # Align with the real server (uvicorn) which sets SO_REUSEADDR.
            # Without this, a lingering TIME_WAIT socket from a previous stop
            # would cause a false 'Address already in use' on the next start.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((uniformed_ip, port))
            return False
        except socket.error as e:
            return 'Address already in use' in str(e)
        finally:
            s.close()

    try:
        resp = s.connect_ex((host, port))
        return resp == 0

    except socket.error:
        return False
    finally:
        s.close()


def check_path_valid(path):
    if path.strip() == '':
        return True

    for char in ILL_CHARACTER:
        if path.find(char) >= 0:
            return False

    return True


def check_ip_valid(value):
    if isinstance(value, str) and (IPV4_PATTERN.match(value) or IPV6_PATTERN.match(value)):
        return True

    return False


def prepare_ip(ip):
    if BARE_IPV6_PATTERN.match(ip):
        return f"[{ip}]"
    else:
        return ip


def split_ip_port(instance):
    if instance.endswith(PORT_SUFFIX):
        return instance.rsplit(PORT_SUFFIX, 1)[0].strip("[]"), PORT_SUFFIX
    elif WITH_PORT.match(instance):
        ip, port = instance.rsplit(":", 1)
        return ip.strip("[]"), port
    else:
        return instance.rsplit(":", 1)[0].strip("[]"),


def check_ip_port_valid(value):
    if isinstance(value, str):
        values = split_ip_port(value)
        if len(values) != 2:
            return False

        if check_ip_valid(values[0]) and check_port_valid(values[1]):
            return True

        return False

    return False


def check_port_valid(value):
    if isinstance(value, str):
        return str.isdigit(value) and 1023 < int(value) <= 65535
    elif isinstance(value, int):
        return 1023 < value <= 65535
    else:
        return False


def check_instance_valid(value):
    if INSTANCE_PATTERN.match(value):
        return True

    return LOCAL_INSTANCE_PATTERN.match(value)


def is_more_permissive(filepath, max_permissions=0o600):
    return (os.stat(filepath).st_mode & 0o777) > max_permissions


def check_ssl_file_permission(certfile, keyfile, ca_file):
    if keyfile and is_more_permissive(keyfile, 0o400):
        result_msg = "WARNING: the permission of ssl key file %s is greater than 400." % keyfile
        write_to_terminal(result_msg, color="yellow")

    if certfile and is_more_permissive(certfile, 0o400):
        result_msg = "WARNING: the permission of ssl certificate file %s is greater than 400." % certfile
        write_to_terminal(result_msg, color="yellow")

    if ca_file and is_more_permissive(ca_file, 0o400):
        result_msg = "WARNING: the permission of ssl ca file %s is greater than 400." % ca_file
        write_to_terminal(result_msg, color="yellow")


@ignore_exc
def check_ssl_certificate_remaining_days(certfile, expired_threshold=90):
    """
    Check whether the certificate is expired or invalid.
    :param expired_threshold: how many days to warn.
    :param certfile: path of certificate.
    :certificate_warn_threshold: the warning days for certificate_remaining_days
    output: dict, check result which include 'check status' and 'check information'.
    """
    if not certfile:
        return
    gmt_format = '%b %d %H:%M:%S %Y GMT'
    child = subprocess.Popen(['openssl', 'x509', '-in', certfile, '-noout', '-dates'],
                             shell=False, stdout=subprocess.PIPE, stdin=subprocess.PIPE)
    sub_chan = child.communicate()
    tz = adjust_timezone(global_vars.configs.get('TIMEZONE', 'tz'))
    if sub_chan[0]:
        not_after = sub_chan[0].decode('utf-8').split('\n')[1].split('=')[1].strip()
        end_time = datetime.datetime.strptime(not_after, gmt_format)
        certificate_remaining_days = (end_time - datetime.datetime.now(tz)).days
        if 0 < certificate_remaining_days < expired_threshold:
            result_msg = "WARNING: the certificate '{certificate}' has the remaining " \
                         "{certificate_remaining_days} days before out of date." \
                .format(certificate=certfile,
                        certificate_remaining_days=certificate_remaining_days)
            write_to_terminal(result_msg, color="yellow")
        elif certificate_remaining_days <= 0:
            result_msg = "WARNING: the certificate '{certificate}' is out of date." \
                .format(certificate=certfile)
            write_to_terminal(result_msg, color="yellow")


def warn_ssl_certificate(certfile, keyfile, ca_file):
    check_ssl_file_permission(certfile, keyfile, ca_file)
    check_ssl_certificate_remaining_days(certfile)


def existing_special_char(word):
    if word is not None and any(ill_char in word for ill_char in ILL_CHARACTER):
        return True

    return False


def path_type(path):
    realpath = os.path.realpath(path)
    if os.path.exists(realpath):
        return realpath

    raise argparse.ArgumentTypeError('%s is not a valid path.' % path)


def http_scheme_type(param):
    param = param.lower()
    if param in ('http', 'https'):
        return param

    raise argparse.ArgumentTypeError('%s is not valid.' % param)


def positive_int_type(integer: str):
    if not integer.isdigit():
        raise argparse.ArgumentTypeError('Invalid value %s.' % integer)

    try:
        integer = int(integer)
    except ValueError:
        raise argparse.ArgumentTypeError('Invalid value %s.' % integer)

    if integer == 0:
        raise argparse.ArgumentTypeError('Invalid value 0.')

    return integer


def not_negative_int_type(integer: str):
    if not integer.isdigit():
        raise argparse.ArgumentTypeError('Invalid value %s.' % integer)

    try:
        integer = int(integer)
    except ValueError:
        raise argparse.ArgumentTypeError('Invalid value %s.' % integer)

    return integer


def date_type(date_str):
    """Cast date or timestamp string to
    a 13-bit timestamp integer."""
    if date_str.isdigit():
        # We can't know whether the timestamp users give is valid.
        # So, we only cast the string to integer and return it.
        return int(date_str)

    # If the date string users give is not a timestamp string, we
    # will regard it as the date time format.
    try:
        d = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        return int(d.timestamp() * 1000)  # unit: ms
    except ValueError:
        pass

    raise argparse.ArgumentTypeError('Invalid value %s.' % date_str)


def check_datetime_legality(time_string):
    try:
        datetime.datetime.strptime(time_string, "%Y-%m-%d %H:%M:%S")
        return True
    except ValueError:
        return False


def check_timestamp_legality(timestamp):
    """make sure the time unit is 'ms' which has 13 digits"""
    if not timestamp:
        return False

    return TIMESTAMPS_PATTERN.match(timestamp)


def check_name_valid(value):
    """avoid security issues such as injection based on whitelist"""
    if not value:
        return False

    return NAME_PATTERN.match(value)


def check_string_valid(value):
    """currently used to limit string length"""
    if isinstance(value, str):
        if not value:
            return False

        return len(value) <= MAX_STRING_LENGTH

    return False


def check_timezone_valid(value):
    """
    currently used to check format of timezone
    note: now only support 'UTC'
    """
    if not value:
        return False

    return TIMEZONE_PATTERN.match(value)


def check_query_model(value):
    """verify the parameters of intelligent interaction api"""
    query = dict(value)
    for item in ['query', 'user_id', 'session_id', 'mode']:
        if item not in query or not check_string_valid(query.get(item)):
            return False, item
    if not check_name_valid(query.get('user_id')):
        return False, 'user_id'
    if not check_name_valid(query.get('session_id')):
        return False, 'session_id'
    if query.get('mode') not in [InteractionType.FAULT_DIAGNOSTIC, InteractionType.TOOL_INTERACTION]:
        return False, 'mode'
    history_len = query.get('history_len', None)
    if history_len is not None and (not isinstance(history_len, int) or not 0 < history_len <= 3):
        return False, 'history_len'
    return True, None


def check_cluster_model(value):
    """verify the parameters of register_cluster api"""
    query = dict(value)
    for item in ['cluster_name', 'username', 'password']:
        if item not in query or not check_string_valid(query.get(item)):
            return False, item
    if 'host' not in query or not check_instance_valid(query.get('host')):
        return False, 'host'
    port = query.get('port', 0)
    if port is not None and (not isinstance(port, int) or not 1 <= port <= 65535):
        return False, 'port'
    return True, None


def check_instance_list(param, value):
    """verify whether the instance in list is valid"""
    try:
        instance_list = json.loads(value)
    except JSONDecodeError:
        instance_list = value.split(',')
    if not instance_list or not isinstance(instance_list, list):
        return False, param
    for instance in instance_list:
        if not check_instance_valid(instance):
            return False, param
    return True, None


class ParameterChecker:
    UINT2 = 'uint2 (0 ~ 65535)'
    INT2 = 'int2 (-32768 ~ 32767)'
    INT2_OPTIONAL = 'Default None, or int2 (-32768 ~ 32767)'
    TIMESTAMP = 'timestamp (ms)'
    PINT32 = 'Postive Interger (1 ~ 4294967295)'
    INT32 = 'Interger (0 ~ 4294967295)'
    PINT32_OPTIONAL = 'Default None, or Postive Interger (1 ~ 4294967295)'
    PERCENTAGE = '0 ~ 1'
    BOOL = 'Bool'
    IP_WITHOUT_PORT = 'IP Without Port'
    IP_WITH_PORT = 'IP With Port'
    INSTANCE = 'IP With or Without Port'
    INSTANCE_LIST = 'LIST of IP With or Without Port'
    NAME = '2-120 Letters, Digits and Underlines'
    DIGIT = 'Digits String'
    STRING = 'String type, 1-10240 letters'
    TIMEZONE = 'Timezone type, UTC format, example: UTC-8, UTC+8:35'
    QUERY_MODEL = 'the model used for intelligent_interaction'
    CLUSTER_MODEL = 'the cluster model used for registering'
    VERSION = 'supported version'
    LANG = 'supported language'
    DICT = 'dict'
    SEARCH_RES = 'list of dictionaries'
    SEARCH_PARAM = 'class SearchParams parameters for searching'
    KL_UPDATE_PARAM = 'class KLUpdateParams parameters for updating'
    DS_UPDATE_PARAM = 'class DSUpdateParams parameters for updating'
    NSTRING = 'String type, 0-10240 letters'

    @staticmethod
    def define_rules(**rules):
        """Used to determine whether the input parameters are legal."""
        customized_rules = {}
        inspect_params_dict = {}

        def can_pass_inspection(parameter_pairs, rules=rules):
            for parameter, value in parameter_pairs.items():
                if parameter not in rules:
                    continue

                if rules[parameter] == ParameterChecker.UINT2:
                    if value is not None and (not isinstance(value, int) or not 0 <= value <= 65535):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.INT2:
                    if not isinstance(value, int) or not -32768 <= value <= 32767:
                        return False, parameter
                elif rules[parameter] == ParameterChecker.INT2_OPTIONAL:
                    if value is not None and (not isinstance(value, int) or not -32768 <= value <= 32767):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.PINT32:
                    if not isinstance(value, int) or not 0 < value <= 4294967295:
                        return False, parameter
                elif rules[parameter] == ParameterChecker.INT32:
                    if not isinstance(value, int) or not 0 <= value <= 4294967295:
                        return False, parameter
                elif rules[parameter] == ParameterChecker.PINT32_OPTIONAL:
                    if value is not None and (not isinstance(value, int) or not 0 < value <= 4294967295):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.PERCENTAGE:
                    if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                        return False, parameter
                elif rules[parameter] == ParameterChecker.BOOL:
                    if not isinstance(value, bool):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.IP_WITHOUT_PORT:
                    if not check_ip_valid(value):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.IP_WITH_PORT:
                    if value is not None and not check_ip_port_valid(value):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.INSTANCE:
                    if value is not None and not check_instance_valid(value):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.NAME:
                    if value is not None and not check_name_valid(value):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.DIGIT:
                    if value is not None and not value.isdigit():
                        return False, parameter
                elif rules[parameter] == ParameterChecker.TIMESTAMP:
                    if value is not None and not check_timestamp_legality(str(value)):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.STRING:
                    if value is not None and not check_string_valid(value):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.TIMEZONE:
                    if value is not None and not check_timezone_valid(value):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.QUERY_MODEL:
                    success, target_parameter = check_query_model(value)
                    if not success:
                        return False, target_parameter
                elif rules[parameter] == ParameterChecker.CLUSTER_MODEL:
                    success, target_parameter = check_cluster_model(value)
                    if not success:
                        return False, target_parameter
                elif rules[parameter] == ParameterChecker.INSTANCE_LIST:
                    success, target_parameter = check_instance_list(parameter, value)
                    if not success:
                        return False, target_parameter
                elif rules[parameter] == ParameterChecker.VERSION:
                    if value not in constants.VERSION_SUPPORT_LIST:
                        return False, parameter
                elif rules[parameter] == ParameterChecker.LANG:
                    if value not in constants.LANGUAGES_SUPPORT_LIST:
                        return False, parameter
                elif rules[parameter] == ParameterChecker.DICT:
                    if not isinstance(value, dict):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.SEARCH_RES:
                    if not isinstance(value, list) or not all(isinstance(i, dict) for i in value):
                        return False, parameter
                elif rules[parameter] == ParameterChecker.SEARCH_PARAM:
                    success, cur_parameter = inspect_search_params(parameter_pairs)
                    if not success:
                        return False, cur_parameter
                elif rules[parameter] == ParameterChecker.KL_UPDATE_PARAM:
                    success, cur_parameter = inspect_kl_update_params(parameter_pairs)
                    if not success:
                        return False, cur_parameter
                elif rules[parameter] == ParameterChecker.DS_UPDATE_PARAM:
                    success, cur_parameter = inspect_ds_update_params(parameter_pairs)
                    if not success:
                        return False, cur_parameter
                elif rules[parameter] == ParameterChecker.NSTRING:
                    if value and not check_string_valid(value):
                        return False, parameter
            return True, None

        def value_filter(parameter_pairs):
            for parameter, value in parameter_pairs.items():
                if parameter not in rules:
                    continue

            return parameter_pairs

        def set_rule(param_list, inspect_rule):
            nonlocal customized_rules
            nonlocal inspect_params_dict
            for param in param_list:
                if param in inspect_params_dict:
                    customized_rules[param] = inspect_rule

        def inspect_search_params(param_pairs):
            """inspect search parameters in class SearchParams"""
            nonlocal inspect_params_dict

            search_params = param_pairs.get("search_params")
            if search_params is None:
                raise Exception('The search_params is None.')
            inspect_params_dict = dict(search_params)

            set_rule(['question', 'model_name', 'question_id'], ParameterChecker.STRING)
            set_rule(['user_id', 'session_id'], ParameterChecker.NAME)
            set_rule(['switch'], ParameterChecker.BOOL)
            set_rule(['vector_topk', 'text_topk', 'rerank_topk', 'kb_id'], ParameterChecker.INT32)
            set_rule(['version'], ParameterChecker.VERSION)
            set_rule(['lang'], ParameterChecker.LANG)
            set_rule(['history_len'], ParameterChecker.UINT2)
            set_rule(['model_config'], ParameterChecker.DICT)
            set_rule(['search_res'], ParameterChecker.SEARCH_RES)
            return can_pass_inspection(inspect_params_dict, customized_rules)

        def inspect_kl_update_params(param_pairs):
            """inspect search parameters in class KLUpdateParams"""
            nonlocal inspect_params_dict

            search_params = param_pairs.get("kl_update_params")
            if search_params is None:
                raise Exception('The kl_update_params is None.')
            inspect_params_dict = dict(search_params)

            set_rule(['kb_id'], ParameterChecker.INT32)
            set_rule(['user_id'], ParameterChecker.NAME)
            set_rule(['name'], ParameterChecker.STRING)
            set_rule(['description', 'context'], ParameterChecker.NSTRING)
            return can_pass_inspection(inspect_params_dict, customized_rules)

        def inspect_ds_update_params(param_pairs):
            """inspect search parameters in class DSUpdateParams"""
            nonlocal inspect_params_dict

            search_params = param_pairs.get("ds_update_params")
            if search_params is None:
                raise Exception('The ds_update_params is None.')
            inspect_params_dict = dict(search_params)

            set_rule(['ds_id', 'related_kb_id'], ParameterChecker.INT32)
            set_rule(['user_id'], ParameterChecker.NAME)
            set_rule(['name'], ParameterChecker.STRING)
            set_rule(['description'], ParameterChecker.NSTRING)
            return can_pass_inspection(inspect_params_dict, customized_rules)

        def decorator(f):

            @wraps(f)
            def wrapper(*args, **kwargs):
                success, parameter = can_pass_inspection(kwargs)
                if not success:
                    raise ValueError(f'Incorrect value for parameter {parameter}.')

                return f(*args, **value_filter(kwargs))

            @wraps(f)
            async def async_wrapper(*args, **kwargs):
                success, parameter = can_pass_inspection(kwargs)
                if not success:
                    raise ValueError(f'Incorrect value for parameter {parameter}.')

                return await f(*args, **value_filter(kwargs))

            if inspect.iscoroutinefunction(f):
                return async_wrapper
            return wrapper

        return decorator
