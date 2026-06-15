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

import json
import logging
import multiprocessing
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps

import requests
import select

from GaussMaster.common import utils
from GaussMaster.common.http import create_requests_session
from GaussMaster.common.platform import LINUX
from GaussMaster.common.process import Process
from GaussMaster.common.utils import dbmind_assert
from GaussMaster.common.utils.base import WHITE_FMT, RED_FMT, GREEN_FMT, YELLOW_FMT


def read_pid_file(filepath):
    """Return the running process's pid from file.
    If the acquisition fails, return 0.

    Note
    ~~~~~~~~

    The func only can read the pid file for GaussMaster due to specific/fine-grained verification.
    """
    try:
        if not os.path.exists(filepath):
            return 0
        with open(filepath, mode='r') as fp:
            pid = int(fp.readline().strip())
        proc = Process(pid)

        if proc.alive and os.path.samefile(os.path.dirname(filepath), proc.cwd):
            return pid
        return 0
    except PermissionError:
        return 0
    except ValueError:
        return 0
    except FileNotFoundError:
        return 0


def write_to_terminal(
        message,
        level='info',
        color=None
):
    """
    Specify the message level and color based on the written information
    """
    levels = ('info', 'error')
    colors = ('white', 'red', 'green', 'yellow', None)
    dbmind_assert(color in colors and level in levels)

    if not isinstance(message, str):
        message = str(message)

    # coloring.
    if color == 'white':
        out_message = WHITE_FMT.format(message)
    elif color == 'red':
        out_message = RED_FMT.format(message)
    elif color == 'green':
        out_message = GREEN_FMT.format(message)
    elif color == 'yellow':
        out_message = YELLOW_FMT.format(message)
    else:
        out_message = message

    # choosing a streaming.
    try:
        if level == 'error':
            sys.stderr.write(out_message)
            sys.stderr.write(os.linesep)
            sys.stderr.flush()
        else:
            sys.stdout.write(out_message)
            sys.stdout.write(os.linesep)
            sys.stdout.flush()
    except BrokenPipeError as e:
        logging.error("BrokenPipeError occurred: {}".format(str(e)))
    except OSError as os_error:
        if "Input/output error" not in str(os_error):
            raise os_error


def read_input_from_pipe():
    """
    Read stdin input if there is "echo 'str1 str2' | python xx.py", return the input string.
    """
    # Windows: select() on stdin is unreliable; read piped JSON when stdin is not a TTY.
    if sys.platform == 'win32':
        if not sys.stdin.isatty():
            try:
                return sys.stdin.read().strip()
            except Exception:
                return ""
        return ""

    if not LINUX:
        return ""

    input_str = ""
    r_handle, _, _ = select.select([sys.stdin], [], [], 0)
    if not r_handle:
        return ""

    for item in r_handle:
        if item == sys.stdin and not sys.stdin.line_buffering:
            input_str = sys.stdin.readline().strip()
    return input_str


def get_json_from_stdin():
    """
    Convert JSON strings to Python dictionary objects
    """
    json_str = read_input_from_pipe()
    try:
        if json_str:
            return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError('Invalid JSON input') from e

    return dict()


def raise_fatal_and_exit(
        message,
        exitcode=1,
        use_logging=True,
        only_print_at_main_process=False
):
    """
    Determine whether to exit the program based on the main process
    """
    if use_logging:
        logging.fatal(message, exc_info=True)
    is_main_process = multiprocessing.current_process().name == 'MainProcess'
    if not only_print_at_main_process:
        write_to_terminal(message, level='error', color='red')
    elif is_main_process:
        write_to_terminal(message, level='error', color='red')
    if is_main_process:
        exit(exitcode)
    else:
        os.kill(signal.SIGQUIT, os.getppid())


def check_connectivity_parallely(service_params_list, terminal_output=False):
    """
    check connectivity of all services parallely
    service_params_list is a list of tuples:
      (url, ssl_context, model_name) or
      (url, ssl_context, model_name, api_key, model_config)
    """
    available_services = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {executor.submit(check_url_connectivity, *task): task for task in service_params_list}
        for future in as_completed(future_to_url):
            result = future.result()
            if result[0]:
                available_services.append(result[1])
            if terminal_output:
                if not result[0]:
                    utils.cli.write_to_terminal(result[2], color='yellow')
            logging.info(result[2])
    return available_services


def check_url_connectivity(url: str, ssl_context, model_name=None, api_key=None, model_config=None):
    """
    Function to check the connectivity of a given URL.

    Parameters:
    url (str): The URL to be checked.
    ssl_context: ssl_context
    model_name: optional service label (embedding_model / reranker_model / llm id)
    api_key: optional Bearer token
    model_config: optional dict with model_name for request body

    returns:
    whether the url is reachable or not, llm name, message
    """

    def _generate_msg(output, service_name=None):
        if model_name:
            return f"{service_name}: {output}"
        return output

    if not url or not str(url).strip():
        msg = "URL is empty or not configured."
        return False, model_name, _generate_msg(msg, model_name)

    headers = {}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    try:
        with create_requests_session(ssl_context=ssl_context) as session:
            if 'embeddings' in url.lower():
                model = model_config.get('model_name', 'BAAI/bge-large-zh-v1.5') if model_config else 'BAAI/bge-large-zh-v1.5'
                test_data = {"input": "test", "model": model}
                response = session.post(url, json=test_data, headers=headers, timeout=10)
            elif 'rerank' in url.lower():
                model = model_config.get('model_name', 'BAAI/bge-reranker-v2-m3') if model_config else 'BAAI/bge-reranker-v2-m3'
                test_data = {"query": "test", "documents": ["test"], "model": model}
                response = session.post(url, json=test_data, headers=headers, timeout=10)
            else:
                test_model = model_config.get('model_name', 'Qwen/Qwen2.5-7B-Instruct') if model_config else 'Qwen/Qwen2.5-7B-Instruct'
                test_data = {"model": test_model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
                response = session.post(url, json=test_data, headers=headers, timeout=10)
    except requests.exceptions.ConnectTimeout:
        msg = f"Timeout connecting to the service {url}."
        return False, model_name, _generate_msg(msg, model_name)
    except Exception as e:
        return False, model_name, _generate_msg(str(e), model_name)
    if response.status_code not in (200, 405, 422):
        msg = f"The service '{url}' returned status code: {response.status_code}. Response: {response.text[:200]}"
        return False, model_name, _generate_msg(msg, model_name)
    msg = f"The service '{url}' is available."
    return True, model_name, _generate_msg(msg, model_name)


def get_model_ssl_config(model_name, url, model_config):
    """
    Function to check if the URL starts with 'https' and retrieve the SSL configuration for the model.

    Parameters:
    model_name (str): The name of the model.
    url (str): The URL to be checked.
    model_config (dict): A dictionary containing the configuration for the model.

    Returns:
    dict: A dictionary containing the SSL configuration.

    Raises:
    ValueError: If the model configuration does not include all necessary SSL files when the URL starts with 'https'.
    """
    ssl_config = dict()
    if url.startswith('https'):
        if not (model_config.get(model_name).get('ssl_keyfile')
                and model_config.get(model_name).get('ssl_ca_file')
                and model_config.get(model_name).get('ssl_certfile')):
            raise ValueError(f"When {model_name} startswith 'https', "
                             f"all of 'ssl_certfile', 'ssl_keyfile', "
                             f" and 'ssl_ca_file' must be provided.")
        ssl_keyfile = model_config.get(model_name).get('ssl_keyfile')
        ssl_ca_file = model_config.get(model_name).get('ssl_ca_file')
        ssl_certfile = model_config.get(model_name).get('ssl_certfile')
        ssl_config = {'verify': ssl_ca_file,
                      'cert': (ssl_certfile, ssl_keyfile)}
    return ssl_config


def get_config_option_from_stdin(section, option, stdin_dict):
    """
    Function to read configuration options from standard input.

    Parameters:
    section (str): The section name in the configuration.
    option (str): The option name in the configuration.
    stdin_dict (str): The user's input

    Returns:
    str: The value of the specified configuration option.
    """
    if f'{section}_{option}' not in stdin_dict:
        return None
    return stdin_dict[f'{section}_{option}']


global_flag = True


def print_progress(information):
    """print the cost of a function in time"""

    def timer(start, info):
        global global_flag
        while global_flag:
            dur = time.perf_counter() - start
            sys.stdout.write('\r{} {:.2f}s'.format(info, dur))
            sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write('\n')
        sys.stdout.flush()

    def decorate(f):
        @wraps(f)
        def inner(*args, **kwargs):
            start = time.perf_counter()
            timer_thread = threading.Thread(target=timer, kwargs={'start': start, 'info': information})
            timer_thread.start()
            try:
                f(*args, **kwargs)
            except Exception as e:
                sys.stdout.write('\n')
                sys.stdout.flush()
                logging.exception(e)
            global global_flag
            global_flag = False
            timer_thread.join()
            global_flag = True

        return inner

    return decorate
