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

def redundant_index_setup():
    cmd1 = f"source {config['opengauss_bashrc_path']}; cm_ctl stop && cm_ctl start"
    print(cmd1)
    ssh_exec(cmd1, "primary_node", 'normal_users')
    time.sleep(10)

def redundant_index():
    host = config['db_users']['admin_users']['ip']
    port = config['db_users']['admin_users']['port']
    database = config['db_users']['admin_users']['database']
    user = config['db_users']['admin_users']['username']
    password = config['db_users']['admin_users']['password']

    conn = psycopg2.connect(host=host, port=port, database=database, user=user, password=password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS t1;")
    cur.execute("DROP TABLE IF EXISTS t2;")
    cur.execute("""CREATE TABLE t1 (
                id int primary key,
                x int,
                y int,
                info varchar(200)
                );""")
    cur.execute("""ALTER TABLE t1 SET (
                autovacuum_analyze_threshold = 100000000,
                autovacuum_analyze_scale_factor = 0
                );""")
    
    cur.execute("""CREATE TABLE t2 (
                id int primary key,
                z int,
                info varchar(200)
                );""")
    
    cur.execute("""DROP TABLE IF EXISTS t2;
                CREATE TEMP TABLE vars AS
                SELECT 2000000 AS N1, 1000000 AS N2;""")
    
    cur.execute("""INSERT INTO t1 (id, x, y, info)
                SELECT
                generate_series(1, SELECT N1 FROM vrs) AS id,
                CASE WHEN (random() < 0.999 THEN 1 ELSE (random()* (SELECT N1 FROM vrs))::int END AS x,
                CASE WHEN (random() < 0.001 THEN 1 ELSE (random()* (SELECT N1 FROM vrs))::int END AS y,
                md5(random()::text) AS info;""")
    
    cur.execute("""INSERT INTO t2 (id, z, info)
                SELECT
                generate_series(1, (SELECT N2 FROM vrs)),
                random()* 10::int,
                md5(random()::text);""")
    
    cur.execute("CREATE INDEX idx_t1_x ON t1 (x);")
    cur.execute("CREATE INDEX idx_t1_y ON t1 (y);")
    cur.execute("CREATE INDEX idx_t2_z ON t1 (z);")
    cur.execute("CREATE INDEX idx_t1_info ON t1 (info);")
    cur.execute("CREATE INDEX idx_t2_info ON t1 (info);")

    cur.execute("ANALYZE t1;")
    cur.execute("ANALYZE t2;")

    cur.execute("""EXPLAIN ANALYZE
                SELECT t1.*, t2.*
                FROM t1, t2
                WHERE t2.z = t1.x and t2.z = t1.y;""")
    print("执行慢sql")
    cur.execute("""SELECT t1.*, t2.*
                FROM t1, t2
                WHERE t2.z = t1.x and t2.z = t1.y;""")
    print("执行完毕")
    cur.close()
    conn.commit()

if __name__ == "__main__":
    redundant_index_setup()
    redundant_index()