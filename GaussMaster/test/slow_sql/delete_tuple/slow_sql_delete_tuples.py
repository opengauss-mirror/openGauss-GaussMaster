import subprocess
import sys
import paramiko
import json
import time
import os
import psutil, psycopg2

#config
config = json.load(open("config.json"))
CMD_TIMEOUT = 120

def ssh_exec(cmd : str, node : str, user : str):
    SSH_HOST = config['nodes'][node]['ip']
    SSH_PORT = config['nodes'][node]['port']
    SSH_USER = config['nodes'][node][user]['username']
    SSH_PWD = config['nodes'][node][user]['password']
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(SSH_HOST, SSH_PORT, SSH_USER, password=SSH_PWD, timeout=CMD_TIMEOUT)
        stdin, stdout, stderr = client.exec_command(cmd, timeout=CMD_TIMEOUT)
        raw = stdout.read()
        return raw.decode('utf-8', errors='replace').strip().replace('\n', '')
    finally:
        client.close()

def large_aggregation_cost_setup():
    cmd1 = f"source {config['opengauss_bashrc_path']}; cm_ctl stop && cm_ctl start"
    print(cmd1)
    ssh_exec(cmd1, "primary_node", 'normal_users')
    time.sleep(10)

def large_aggregation_cost():
    host = config['db_users']['admin_users']['ip']
    port = config['db_users']['admin_users']['port']
    database = config['db_users']['admin_users']['database']
    user = config['db_users']['admin_users']['username']
    password = config['db_users']['admin_users']['password']

    conn = psycopg2.connect(host=host, port=port, database=database, user=user, password=password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS t1;")
    cur.execute("""CREATE TABLE t1 AS
        SELECT i AS id,
        (random()*1000)::int AS x,
        (random()*1000)::int AS y,
        md5(random()::text) AS info
        FROM generate_series(1, 2000000) i;""")
    print("执行慢sql")
    cur.execute("delete from t1 where x < 900;")
    print("执行完毕")
    cur.close()
    conn.commit()

if __name__ == "__main__":
    large_aggregation_cost_setup()
    large_aggregation_cost()