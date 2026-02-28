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
import atexit
import glob
import logging
import os
import shutil
import signal
import sys
import time

import yaml


try:
    from GaussMaster import constants
except ImportError:
    curr_path = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    sys.path.append(curr_path)
from GaussMaster import constants
from GaussMaster import controllers
from GaussMaster import global_vars
from GaussMaster.common import utils
from GaussMaster.common.cert_checker import CertCheckerHandler
from GaussMaster.common.configs.config_utils import load_sys_configs, save_config_password, has_config_password
from GaussMaster.common.configs.configurators import UpdateConfig
from GaussMaster.common.exceptions import SetupError, ConfigSettingError, DuplicateTableError, SQLExecutionError
from GaussMaster.common.http import HttpService
from GaussMaster.common.http.ssl import configure_psycopg2_ssl
from GaussMaster.common.metadatabase.ddl import create_metadatabase_schema, destroy_metadatabase, \
    create_dynamic_config_schema
from GaussMaster.common.metadatabase.result_db_session import update_session_clz_from_configs
from GaussMaster.common.safety.word_detect import get_detector
from GaussMaster.common.security import check_password_strength
from GaussMaster.common.utils import MultiProcessingRFHandler, split, dbmind_assert
from GaussMaster.common.utils.checking import is_port_used
from GaussMaster.common.utils.cli import get_config_option_from_stdin, write_to_terminal, read_pid_file, \
    get_json_from_stdin
from GaussMaster.common.configs import kb_config
from GaussMaster.constants import __description__, __version__, LLM_CONFIG_FILE_NAME, \
    SECTION_LOG, SECTION_WEB_SERVICE, SECTION_VECTOR, MISC_PATH, DBMIND, SECTION_SAFETY, GAUSSMASTER_PATH, \
    KNOWLEDGE_BASE_PATH, DB_FILE_ZH, DB_FILE_EN
from GaussMaster.common.metadatabase.utils import get_db_instance
from GaussMaster.multiagents.tools import base_tools
from GaussMaster.server.multicluster import ClusterProxy
from GaussMaster.server.web.data_transformer import update_llm, get_all_available_online_llms, \
    initialize_embedding_model
from GaussMaster.utils.case_to_db import upload_ops_knowledge_base
from GaussMaster.utils.doc_util import load_knowledge_base_local

gauss_master_should_exit = False
_http_service = HttpService()


def load_env_files_from_conf_dir(confpath: str) -> None:
    """Load KEY=VALUE from conf/.env or conf/local.env into os.environ (setdefault, does not override)."""
    for filename in ('.env', 'local.env'):
        path = os.path.join(confpath, filename)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding='utf-8') as ef:
                for line in ef:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, _, val = line.partition('=')
                    key, val = key.strip(), val.strip()
                    if not key:
                        continue
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in '"\'':
                        val = val[1:-1]
                    os.environ.setdefault(key, val)
        except OSError:
            pass


def init_global_configs(confpath, need_check):
    """
    - param confpath: the path of gaussmaster.conf
    - param need_check: Whether to check the password. Don't need to check when starting the service.
    """
    check_config(confpath, need_check)
    global_vars.confpath = confpath

    configs = load_sys_configs(os.path.join(confpath, constants.CONFILE_NAME))
    global_vars.configs = configs

    validate_ssl_config(SECTION_VECTOR)

    dbmind_api_prefix = (global_vars.configs.get(DBMIND, 'api_prefix') or '').strip()
    if dbmind_api_prefix.startswith('http://'):
        utils.cli.write_to_terminal(
            "FATAL: DBMIND service is using an insecure protocol.",
            color='red')
        sys.exit(0)
    if dbmind_api_prefix and not dbmind_api_prefix.startswith('https://'):
        utils.cli.write_to_terminal(
            "WARN: DBMind api_prefix is not https:// (placeholder or invalid); DBMind integration disabled until "
            "you set a valid https URL.",
            color='yellow')
    validate_ssl_config(DBMIND)

    validate_ssl_config(SECTION_WEB_SERVICE)
    # initialize llm config
    llm_config_file_path = os.path.join(confpath, LLM_CONFIG_FILE_NAME)
    with open(llm_config_file_path, 'r', encoding='utf-8') as f:
        llm_config = yaml.safe_load(f)
        global_vars.llm_config = llm_config

    llm_config['device_map'] = device_selection(llm_config)

    # configure psycopg2 with ssl
    configure_psycopg2_ssl()


def initialize_agent_components():
    """initialize components associated with agent"""
    global_vars.tools_registry = {**base_tools.all}


def device_selection(configs):
    """select device when default is auto"""
    device = configs.get('model_device')
    if device == 'auto':
        try:
            import torch_npu
            device = configs.get('npu_device')
        except ImportError:
            try:
                import torch
                if torch.cuda.is_available():
                    device = configs.get('cuda_device')
                elif torch.backends.mps.is_available():
                    device = configs.get('mps_device')
                else:
                    device = "cpu"
            except ImportError:
                device = "cpu"
    return device


def init_logger_with_config():
    log_directory = global_vars.configs.get(SECTION_LOG, 'log_directory', fallback='logs')
    log_directory = os.path.realpath(log_directory)
    os.makedirs(log_directory, exist_ok=True)
    max_bytes = global_vars.configs.getint(SECTION_LOG, 'maxbytes')
    backup_count = global_vars.configs.getint(SECTION_LOG, 'backupcount')
    logging_handler = MultiProcessingRFHandler(filename=os.path.join(log_directory, constants.LOGFILE_NAME),
                                               maxBytes=max_bytes,
                                               backupCount=backup_count)
    logging_handler.setFormatter(
        logging.Formatter("[%(asctime)s %(levelname)s][%(process)d-%(thread)d][%(name)s]: %(message)s"))
    logger = logging.getLogger()
    logger.handlers.clear()
    logger.name = 'GaussMaster'
    # delete debug
    console_handler = logging.StreamHandler()
    console_handler.setLevel(global_vars.configs.get(SECTION_LOG, 'level', fallback='INFO').upper())
    logger.addHandler(logging_handler)
    logger.setLevel(global_vars.configs.get(SECTION_LOG, 'level').upper())
    return logging_handler


def init_cluster_info():
    global_vars.cluster_proxy = ClusterProxy()
    global_vars.cluster_proxy.autodiscover()


def build_parser():
    actions = ['setup', 'start', 'stop']
    parser = argparse.ArgumentParser(description=__description__)
    parser.add_argument('-v', '--version', action='version', version=__version__)

    # Add sub-commands:
    subparsers = parser.add_subparsers(title='available subcommands',
                                       help="type '<subcommand> -h' for help on a specific subcommand",
                                       dest='subcommand')
    # Create the parser for the "service" command.
    parser_service = subparsers.add_parser('service', help='send a command to GaussMaster to change the status of '
                                                           'the service')
    parser_service.add_argument('action', choices=actions, help='perform an action for service')
    parser_service.add_argument('-c', '--conf', type=os.path.realpath, metavar='DIRECTORY', required=True,
                                help='set the directory of configuration files')
    parser_service.add_argument('--initialize', action='store_true', help='initialize config and database')
    parser_service.add_argument('--initialize_vector_db', action='store_true',
                                help='initialize vector database, must use with --initialize')
    parser_service.add_argument('--initialize_meta_db', action='store_true',
                                help='initialize meta database, must use with --initialize')
    return parser


def check_config(conf_path, need_check):
    """
    check configs and encrypt password by using AES256-CBC.
    - param need_check: Whether to check the password. Don't need to check when starting the service.
    """
    if not os.path.exists(conf_path):
        raise SetupError(f"Not found the directory {conf_path}")

    for file_name in glob.glob(os.path.join(MISC_PATH, '*.*')):
        if not os.path.exists(file_name):
            raise SetupError(f"Missing config files, please use setup command without initialize first.")

    gauss_master_config_path = os.path.join(conf_path, constants.CONFILE_NAME)
    create_dynamic_config_schema()

    with UpdateConfig(gauss_master_config_path) as config:
        stdin_dict = get_json_from_stdin()
        dbmind_api_prefix = (config.get(DBMIND, 'api_prefix')[0] or '').strip()
        if dbmind_api_prefix.startswith('http://'):
            utils.cli.write_to_terminal(
                "FATAL: DBMind service is using an insecure protocol, exit...",
                color='red')
            sys.exit(0)
        if dbmind_api_prefix and not dbmind_api_prefix.startswith('https://'):
            utils.cli.write_to_terminal(
                "WARN: DBMind api_prefix is not https:// (placeholder or invalid); DBMind integration disabled until "
                "you set a valid https URL.",
                color='yellow')
        for section, option in [(SECTION_VECTOR, 'password'), (SECTION_WEB_SERVICE, 'ssl_keyfile_password'),
                                (DBMIND, 'ssl_keyfile_password')]:
            password = get_config_option_from_stdin(section, option, stdin_dict)
            if option == 'ssl_keyfile_password':
                keyfile = (config.get(section, 'ssl_keyfile')[0] or '').strip()
                if section == SECTION_WEB_SERVICE and not constants.SSL:
                    pass
                elif not keyfile or keyfile.upper() == '(NULL)':
                    pass
                else:
                    if need_check and password and not check_password_strength(password):
                        utils.cli.write_to_terminal(
                            "FATAL: %s_ssl_keyfile_password is not strong enough, exit..." % section,
                            color='red')
                        sys.exit(0)
                    if not os.path.exists(keyfile):
                        utils.cli.write_to_terminal(
                            "FATAL: %s service is using an insecure protocol, exit..." % section,
                            color='red')
                        sys.exit(0)
                    if not constants.SSL or 'ENCRYPTED' not in open(keyfile, encoding='utf-8', errors='ignore').read():
                        utils.cli.write_to_terminal(
                            "FATAL: %s service is using insecure SSL configuration, exit..." % section,
                            color='red')
                        sys.exit(0)
            if password:
                save_config_password(config, section, option, password)
            elif need_check and not has_config_password(config, section, option):
                raise ValueError(f'You should pass the value of \'{section}_{option}\' through '
                                 f'the json, exit...')


def setup_directory(confpath):
    """create customized conf directory and copy config files to the customized conf directory"""
    # Determine whether the directory is empty.
    if os.path.exists(confpath):
        raise SetupError("Given setup directory '%s' already exists." % confpath)

    utils.cli.write_to_terminal(
        "Please modify configurations manually.\n"
        "The file you need to modify is '%s' and '%s'.\n"
        "After configuring, you should continue to set up and initialize "
        "the directory with --initialize option, e.g.,\n "
        "'... service setup -c %s --initialize'"
        % (
            os.path.join(confpath, constants.CONFILE_NAME),
            os.path.join(confpath, constants.LLM_CONFIG_FILE_NAME),
            confpath),
        color='yellow')

    # Make the confpath directory and copy all files
    # (basically all files are config files) from MISC directory.
    shutil.copytree(
        src=constants.MISC_PATH,
        dst=confpath
    )
    utils.base.chmod_r(confpath, 0o700, 0o600)
    utils.cli.write_to_terminal("Configuration directory '%s' has been created successfully." % confpath,
                                color='green')


def validate_ssl_config(section):
    """
    Function to validate the SSL configuration for a given section.

    Parameters:
    section (str): The name of the section in the configuration.

    Raises:
    ValueError: If the SSL configuration is invalid.
    """
    api_prefix = global_vars.configs.get(section, 'api_prefix')
    if api_prefix and api_prefix.startswith('https://'):
        use_ssl_raw = 'true'
    else:
        use_ssl_raw = global_vars.configs.get(section, 'ssl')
        if section == SECTION_WEB_SERVICE:
            use_ssl_raw = 'true' if constants.SSL else 'false'
    ssl_certfile = global_vars.configs.get(section, 'ssl_certfile')
    ssl_keyfile = global_vars.configs.get(section, 'ssl_keyfile')
    ssl_ca_file = global_vars.configs.get(section, 'ssl_ca_file')
    if not use_ssl_raw:
        return
    if use_ssl_raw and use_ssl_raw.strip().upper() == 'TRUE':
        if '' in (ssl_certfile, ssl_keyfile, ssl_ca_file) or \
                None in (ssl_certfile, ssl_keyfile, ssl_ca_file) or \
                '(NULL)' in (ssl_certfile.upper(), ssl_keyfile.upper(), ssl_ca_file.upper()):
            if not api_prefix:
                raise ValueError(f"When 'ssl' of {section} is True, "
                                 f"all of 'ssl_certfile', 'ssl_keyfile', "
                                 f"'ssl_ca_file' must be provided.")
            raise ValueError(f"When 'api_prefix' of {section} starts with 'https://', "
                             f"all of 'ssl_certfile', 'ssl_keyfile', "
                             f"'ssl_ca_file' must be provided.")
        if not CertCheckerHandler.is_valid_cert(ca_name=ssl_ca_file, crt_name=ssl_certfile):
            raise ValueError('ca is invalid.')
    elif use_ssl_raw.strip().upper() not in ('FALSE', '(NULL)'):
        raise ValueError(f'ERROR: The parameter of ssl for {section} '
                         f'must be either True or False.')


class Main:

    def __init__(self, parser):
        os.umask(0o0077)
        self.parser = parser
        self.args = self.parser.parse_args()
        self.confpath = os.path.realpath(self.args.conf)
        self.pid_file = os.path.realpath(os.path.join(self.confpath, constants.PIDFILE_NAME))

    def run(self):
        os.umask(0o0077)
        exitcode = 0
        try:
            if self.args.subcommand == 'service':
                if self.args.action == 'setup':
                    if self.args.initialize:
                        self.check_config_and_initialize_kb()
                    else:
                        setup_directory(self.confpath)
                elif self.args.action == 'start':
                    self.start()
                elif self.args.action == 'stop':
                    self.stop()
                else:
                    self.parser.print_usage()
        except (SetupError, ConfigSettingError) as e:
            utils.write_to_terminal(f"{e}", color="red")
            exitcode = 2
        exit(exitcode)

    def check_config_and_initialize_kb(self):
        """check config and initialize knowledge base"""
        os.chdir(self.confpath)

        pid = read_pid_file(self.pid_file)
        if pid > 0:
            utils.write_to_terminal(
                'The initialization procedure Can not be executed when GaussMaster process is running.\n', color='red')
            return

        init_global_configs(self.confpath, need_check=True)

        # Initialize embedding model
        initialize_embedding_model()

        # Initialize vector database
        if self.args.initialize_vector_db:
            write_to_terminal('[1/2] Loading gaussdb_zh knowledge base.')
            gaussdb_zh = get_db_instance(kb_config.KT_TABLE_NAME + '_zh', kb_config.KT_TABLE_CONFIG)
            load_knowledge_base_local(os.path.join(GAUSSMASTER_PATH, KNOWLEDGE_BASE_PATH, DB_FILE_ZH), gaussdb_zh)
            write_to_terminal('[2/2] Loading gaussdb_en knowledge base.')
            gaussdb_en = get_db_instance(kb_config.KT_TABLE_NAME + '_en', kb_config.KT_TABLE_CONFIG)
            load_knowledge_base_local(os.path.join(GAUSSMASTER_PATH, KNOWLEDGE_BASE_PATH, DB_FILE_EN), gaussdb_en)
            kb_db = get_db_instance(kb_config.KB_TABLE_NAME, kb_config.KB_TABLE_CONFIG)
            ds_db = get_db_instance(kb_config.DS_TABLE_NAME, kb_config.DS_TABLE_CONFIG)
            qa_db = get_db_instance(kb_config.QA_TABLE_NAME, kb_config.QA_TABLE_CONFIG)
            kb_db.load_table()
            ds_db.load_table()
            qa_db.load_table()
            utils.cli.write_to_terminal('The vector database initialization is completed.', color='green')

        # Initialize metadatabase
        if self.args.initialize_meta_db:
            try:
                create_metadatabase_schema(check_first=False)
            except (DuplicateTableError, SQLExecutionError) as e:
                if 'already exist' not in str(e):
                    utils.cli.write_to_terminal('Failed to link metadatabase due to unknown error (%s), '
                                                'please check the database and its configuration.' % e,
                                                color='red')
                    return
                utils.cli.write_to_terminal('Starting to drop existent tables in meta-database...',
                                            color='yellow')
                destroy_metadatabase()
                create_metadatabase_schema(check_first=True)
            utils.cli.write_to_terminal('The metadatabase initialization is completed.', color='green')

    def start(self):
        """start GaussMaster main server"""
        os.chdir(self.confpath)
        pid = read_pid_file(self.pid_file)
        if pid > 0:
            utils.write_to_terminal('GaussMaster process is already running.\n')
            return

        if os.sys.platform != 'win32':
            if os.fork() > 0:
                sys.exit(0)
        atexit.register(
            lambda: os.path.exists(self.pid_file) and os.remove(self.pid_file)
        )
        with open(self.pid_file, 'w+') as fp:
            fp.write('%d\n' % os.getpid())

        # Set global variables.
        init_global_configs(self.confpath, need_check=False)
        load_env_files_from_conf_dir(self.confpath)
        # Set logger.
        logging_handler = init_logger_with_config()
        for p in split(global_vars.configs.get(SECTION_VECTOR, 'password')):
            logging_handler.add_sensitive_word(p)

        initialize_embedding_model()
        available_llms = get_all_available_online_llms(terminal_output=True)
        if not available_llms:
            utils.cli.write_to_terminal('There is no available llm service. Please check the model config.',
                                        color='red')
        model_name = global_vars.llm_config.get('model_name')
        update_llm(model_name)

        # initialize agent
        initialize_agent_components()

        # Initialize global cluster info
        try:
            init_cluster_info()
        except Exception:
            utils.cli.write_to_terminal('FATAL: Failed to establish to DBMind, please check related configuration',
                                        color='red')

        update_session_clz_from_configs(is_terminal=True)

        # Build the sensitive word detector
        if global_vars.configs.get(SECTION_SAFETY, 'safety_check').strip().upper() == 'TRUE':
            global_vars.DFA_DETECTOR = get_detector()

        # Start to create a web service.
        web_service_host = global_vars.configs.get(SECTION_WEB_SERVICE, 'host')
        web_service_port = global_vars.configs.getint(SECTION_WEB_SERVICE, 'port')
        web_service_ssl_config = dict()
        if constants.SSL:
            web_service_ssl_config = {'ssl_certfile': global_vars.configs.get(SECTION_WEB_SERVICE, 'ssl_certfile'),
                                      'ssl_keyfile': global_vars.configs.get(SECTION_WEB_SERVICE, 'ssl_keyfile'),
                                      'ssl_keyfile_password': global_vars.configs.get(SECTION_WEB_SERVICE,
                                                                                      'ssl_keyfile_password'),
                                      'ssl_ca_file': global_vars.configs.get(SECTION_WEB_SERVICE, 'ssl_ca_file')}

        if is_port_used(web_service_host, web_service_port):
            utils.cli.write_to_terminal('FATAL: GaussMaster web service port conflicts, exiting...', color='red')
            return

        # Attach rules for web service.
        for c in controllers.get_dbmind_controller():
            _http_service.register_controller_module(c)

        utils.write_to_terminal(
            f"GaussMaster Server is starting on "
            f"{'https' if constants.SSL else 'http'}://{web_service_host}:{web_service_port}",
            color="green")
        _http_service.start_listen(web_service_host,
                                   web_service_port,
                                   **web_service_ssl_config)

    def stop(self, level='low'):
        """stop GaussMaster process"""

        sigquit = getattr(signal, 'SIGQUIT', signal.SIGTERM)
        sigkill = getattr(signal, 'SIGKILL', signal.SIGTERM)
        level_mapper = {'low': signal.SIGTERM, 'mid': sigquit, 'high': sigkill}

        """Stop the daemon process"""
        pid = read_pid_file(self.pid_file)
        if pid <= 0:
            utils.write_to_terminal('GaussMaster process is not running.\n')
            return

        def kill_process_group(sig):
            if sig in (signal.SIGTERM, sigquit):
                os.kill(pid, sig)
            elif sig == sigkill:
                os.kill(pid, sig)
            else:
                dbmind_assert(False)

        # If the pid is valid, try to kill the daemon process.
        try:
            send_count = 0
            while True:
                # retry to kill
                write_to_terminal('Waiting for process to exit...')
                kill_process_group(level_mapper.get(level, level_mapper['low']))
                send_count += 1
                time.sleep(1)
                # if quitting is timeout, signal will upgrade.
                if level == 'mid' and send_count >= 5:
                    level = 'high'

        except OSError as e:
            if 'No such process' in e.strerror and os.path.exists(self.pid_file):
                os.remove(self.pid_file)


if __name__ == "__main__":
    main_process = Main(build_parser())
    main_process.run()
