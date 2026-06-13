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

def slow_sql_setup():
    cmd1 = f"source {config['opengauss_bashrc_path']}; gs_guc reload -Z datanode -N all -I all -c \"log_min_duration_statement=1000\""
    print(cmd1)
    ssh_exec(cmd1, "primary_node", 'normal_users')
    time.sleep(10)

if __name__ == "__main__":
    slow_sql_setup()