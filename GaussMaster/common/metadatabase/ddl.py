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

"""Data Definition Language"""
import sqlalchemy
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from GaussMaster.common.exceptions import DuplicateTableError, SQLExecutionError
from GaussMaster.common.metadatabase.base import DynamicConfigDbBase, ResultDbBase
from GaussMaster.common.metadatabase.dao.dynamic_config import Table
from GaussMaster.common.metadatabase.result_db_session import update_session_clz_from_configs, session_clz, get_session
from GaussMaster.common.metadatabase.schema import load_all_schema_models
from GaussMaster.common.metadatabase.utils import create_dsn
from GaussMaster.constants import DYNAMIC_CONFIG


def create_dynamic_config_schema():
    """create"""
    engine = create_engine(create_dsn('sqlite', DYNAMIC_CONFIG))
    DynamicConfigDbBase.metadata.create_all(engine)

    # Batch insert default values into config tables.
    Session = sessionmaker(bind=engine, autoflush=True)
    with Session() as session:
        try:
            session.bulk_save_objects(Table.default_values())
            session.commit()
        except sqlalchemy.exc.IntegrityError as e:
            session.rollback()
            # May be duplicate, ignore it.
            raise DuplicateTableError(e) from e


def create_metadatabase_schema(check_first=True):
    """create_metadatabase_schema"""

    update_session_clz_from_configs(is_terminal=True)
    load_all_schema_models()
    try:
        ResultDbBase.metadata.create_all(
            session_clz.get('engine'),
            checkfirst=check_first
        )
    except Exception as e:
        if 'DuplicateTable' in str(e):
            raise DuplicateTableError(e) from e
        raise SQLExecutionError(e) from e


def destroy_metadatabase():
    """destroy_metadatabase"""

    update_session_clz_from_configs(is_terminal=True)
    load_all_schema_models()
    try:
        ResultDbBase.metadata.drop_all(
            session_clz.get('engine')
        )
    except Exception as e:
        if 'DuplicateTable' in str(e):
            raise DuplicateTableError(e) from e
        raise SQLExecutionError(e) from e


def truncate_table(table_name):
    """truncate table"""
    with get_session() as session:
        if session_clz.get('db_type') == 'sqlite':
            sql_prefix = 'DELETE FROM '
        else:
            sql_prefix = 'TRUNCATE TABLE '
        session.execute(text(sql_prefix + table_name))
